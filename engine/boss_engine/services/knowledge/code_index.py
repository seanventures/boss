"""Code graph indexer.

``code_parse`` turns each file into facts (Python through the stdlib AST; TypeScript,
JavaScript, Go, Java and C# through tree-sitter). This module resolves names ACROSS the files of
a repository and writes the graph through the knowledge store. Nodes are ``file``, ``module``,
``symbol``, ``endpoint``, ``table``, ``queue`` and ``config_key``; edges are ``defines``,
``imports``, ``calls``, ``references``, ``exposes``, ``reads``, ``writes``, ``publishes`` and
``subscribes``. Tables, queues, webhooks and config keys are keyed project-wide, so two
repositories that use the same table meet at the same node.

* ``index_repository`` indexes a whole tree: unchanged files (same content hash) are not
  re-parsed, the whole graph is resolved again, and code nodes the tree no longer contains get
  ``valid_to`` (nothing is deleted).
* ``index_files`` indexes the files BOSS just wrote. They are resolved against the facts cached
  for the rest of the repository (on each ``file`` node), and the files whose references the
  change can move (they mention a name the change defined or removed, or import a changed
  module) are re-resolved with them.

Without a tree-sitter grammar a file falls back to the declaration scan, which records
definitions and same-file calls only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Connection

from ..core import now_utc
from .code_parse import Def, FileFacts, Import, Ref, language_for, parse_file
from .store import PostgresKnowledgeStore

LANGUAGES = frozenset({"python", "typescript", "javascript", "go", "java", "csharp"})
SOURCE = "code_index"
SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".git/", "__pycache__/", "third_party/")
GO_LIKE = ("go", "java", "csharp")       # a module (package/namespace) spans several files
JS_FAMILY = ("javascript", "typescript", "tsx")

_DECL = {
    "javascript": (
        re.compile(r"(?:function|class)\s+([A-Za-z_]\w*)"),
        re.compile(r"import\s+(?:[^'\"]+from\s+)?['\"]([^'\"]+)['\"]"),
    ),
    "typescript": (
        re.compile(r"(?:function|class|interface)\s+([A-Za-z_]\w*)"),
        re.compile(r"import\s+(?:[^'\"]+from\s+)?['\"]([^'\"]+)['\"]"),
    ),
    "go": (
        re.compile(r"func\s+(?:\([^)]+\)\s+)?([A-Za-z_]\w*)\s*\("),
        re.compile(r"import\s+(?:\(\s*)?\"([^\"]+)\""),
    ),
    "java": (
        re.compile(r"\b(?:class|interface|enum)\s+([A-Za-z_]\w*)"),
        re.compile(r"import\s+([\w.]+)\s*;"),
    ),
    "csharp": (
        re.compile(r"\b(?:class|interface|record|struct)\s+([A-Za-z_]\w*)"),
        re.compile(r"using\s+([\w.]+)\s*;"),
    ),
}
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


@dataclass
class ParsedFile:
    """One file, as callers outside the indexer see it: short names and same-file calls."""

    path: str
    language: str
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    backend: str = "declaration_scan"
    qualnames: list[str] = field(default_factory=list)


def parser_backend(language: str) -> str:
    if language == "python":
        return "python_ast"
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    try:
        import importlib

        import tree_sitter  # noqa: F401

        importlib.import_module(f"tree_sitter_{language.replace('csharp', 'c_sharp')}")
    except Exception:
        return "declaration_scan"
    return "tree_sitter"


def _scan(path: str, language: str, source: str) -> ParsedFile:
    symbol_re, import_re = _DECL[language]
    parsed = ParsedFile(path=path, language=language, backend="declaration_scan")
    parsed.symbols = list(dict.fromkeys(symbol_re.findall(source)))
    parsed.qualnames = list(parsed.symbols)
    parsed.imports = list(dict.fromkeys(import_re.findall(source)))
    names = set(parsed.symbols)
    for match in _CALL.finditer(source):
        callee = match.group(1)
        if callee in names or callee in {"if", "for", "while", "switch", "catch", "return"}:
            continue
        if parsed.symbols:
            parsed.calls.append((parsed.symbols[0], callee))
    return parsed


def _parse_path(path: str, language: str) -> str:
    """The path code_parse reads the language from (a caller may name a .tsx file typescript)."""
    if language_for(path) is not None:
        return path
    suffix = {"python": ".py", "typescript": ".ts", "javascript": ".js", "go": ".go", "java": ".java",
              "csharp": ".cs"}[language]
    return path + suffix


def extract(path: str, language: str, source: str) -> FileFacts:
    """The file's facts; the declaration scan stands in when no tree-sitter grammar imports."""
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    facts = parse_file(_parse_path(path, language), source.encode("utf-8"))
    facts.path = path
    if facts.error == "no parser":
        scanned = _scan(path, language, source)
        facts.defs = [Def(name, "function", 0, 0) for name in scanned.symbols]
        facts.refs = [Ref(caller, (callee,), True, 0) for caller, callee in scanned.calls]
    return facts


def parse_source(path: str, language: str, source: str) -> ParsedFile:
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    if language == "python":
        backend = "python_ast"
        facts = parse_file(_parse_path(path, language), source.encode("utf-8"))
        if facts.error == "syntax":
            raise SyntaxError(f"{path} does not parse")
    elif parser_backend(language) == "tree_sitter":
        backend = "tree_sitter"
        facts = extract(path, language, source)
    else:
        return _scan(path, language, source)
    parsed = ParsedFile(path=path, language=language, backend=backend)
    parsed.qualnames = list(dict.fromkeys(d.qualname for d in facts.defs))
    parsed.symbols = list(dict.fromkeys(q.rsplit(".", 1)[-1] for q in parsed.qualnames))
    parsed.imports = list(dict.fromkeys(i.module for i in facts.imports if i.module))
    for ref in facts.refs:
        if ref.call and ref.src and ref.chain and ref.chain[-1] != "#publish":
            caller, callee = ref.src.rsplit(".", 1)[-1], ref.chain[-1]
            if caller != callee and (caller, callee) not in parsed.calls:
                parsed.calls.append((caller, callee))
    return parsed


# --- files ----------------------------------------------------------------------------------------
def indexable(path: str) -> bool:
    if language_for(path) is None or path.endswith((".min.js", ".d.ts")):
        return False
    return not any(f"/{d}" in f"/{path}" for d in SKIP_DIRS)


def is_workspace_config(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in ("package.json", "jsconfig.json") or (name.startswith("tsconfig") and name.endswith(".json"))


def repository_paths(root: Path) -> list[str]:
    """Tracked files when ``root`` is a git checkout, else every file below it; never vendored ones."""
    try:
        proc = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
        paths = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        paths = []
        for directory, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if f"{d}/" not in SKIP_DIRS]
            rel = os.path.relpath(directory, root)
            paths.extend(name if rel == "." else f"{rel}/{name}" for name in names)
    return sorted(p for p in paths if not any(f"/{d}" in f"/{p}" for d in SKIP_DIRS))


def _package_view(meta: dict) -> dict:
    """The package.json keys module resolution reads."""
    return {k: meta[k] for k in ("name", "exports", "source", "types", "typings", "module", "main") if k in meta}


# --- cached facts ----------------------------------------------------------------------------------
def facts_from_dict(d: dict) -> FileFacts:
    f = FileFacts(**{k: d[k] for k in ("path", "lang", "module", "content_hash", "lines")}, error=d.get("error"))
    f.defs = [Def(**x) if isinstance(x, dict) else Def(*x) for x in d.get("defs", [])]
    f.imports = [Import(**x) if isinstance(x, dict) else Import(*x) for x in d.get("imports", [])]
    f.refs = [Ref(x["src"], tuple(x["chain"]), x["call"], x["line"]) if isinstance(x, dict)
              else Ref(x[0], tuple(x[1].split(".")), bool(x[2]), x[3]) for x in d.get("refs", [])]
    for key in ("endpoints", "tables", "queues", "config"):
        setattr(f, key, [tuple(x) for x in d.get(key, [])])
    return f


def facts_to_row(f: FileFacts) -> dict:
    """Compact JSON for kg_code_files (refs dominate, so they are stored as tuples)."""
    return {"defs": [[d.qualname, d.kind, d.line, d.end_line] for d in f.defs],
            "imports": [[i.alias, i.module, i.name] for i in f.imports],
            "refs": [[r.src, ".".join(r.chain), int(r.call), r.line] for r in f.refs],
            "endpoints": f.endpoints, "tables": f.tables, "queues": f.queues, "config": f.config,
            "lines": f.lines, "error": f.error}


def row_to_facts(path: str, lang: str, module: str, content_hash: str, data: dict) -> FileFacts:
    return facts_from_dict({"path": path, "lang": lang, "module": module, "content_hash": content_hash,
                            "lines": data.get("lines", 0), "error": data.get("error"), **data})


def names_of(f: FileFacts) -> list[str]:
    """Every name whose meaning could change this file's edges: what it refers to and imports."""
    names: set[str] = set()
    for r in f.refs:
        names.update(part for part in r.chain if part and not part.startswith("#"))
    for i in f.imports:
        names.add(i.module)
        names.add(i.module.rsplit(".", 1)[-1].rsplit("/", 1)[-1])
        if i.name and i.name != "*":
            names.add(i.name)
    return sorted(n for n in names if n)


# --- how a JS/TS repository names its own code ----------------------------------------------------
JS_EXT = re.compile(r"(?:\.d)?\.(?:[cm]?[jt]sx?)$")
JSONC_NOISE = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.DOTALL)


def _jsonc(raw: str) -> dict:
    """tsconfig.json allows comments and trailing commas."""
    stripped = JSONC_NOISE.sub(lambda m: m.group(1) or "", raw)
    try:
        value = json.loads(re.sub(r",(\s*[}\]])", r"\1", stripped))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _norm(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/".join(parts)


def _strings(value: Any) -> list[str]:
    """Every path in a package.json `exports` entry (a string, or nested conditions)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


class JsWorkspace:
    """Workspace package names (each package.json's `name`) and tsconfig/jsconfig `paths` aliases,
    so `@acme/utils`, `@acme/framework/utils` and `@/lib/x` resolve to the repository's own modules.
    A package's published entry (`dist/...`) maps back to its source (`src/...`)."""

    def __init__(self, packages: dict[str, tuple[str, dict]] | None = None,
                 aliases: list[tuple[str, str, list[str]]] | None = None):
        self.packages = packages or {}                 # name -> (directory, package.json)
        self.aliases = sorted(aliases or [], key=lambda a: -len(a[0]))   # (config dir, pattern, targets)

    @classmethod
    def load(cls, root: Path) -> JsWorkspace:
        texts: dict[str, str] = {}
        for path in repository_paths(root):
            if is_workspace_config(path):
                try:
                    texts[path] = (root / path).read_text(errors="replace")
                except OSError:
                    continue
        return cls.from_texts(texts)

    @classmethod
    def from_texts(cls, texts: dict[str, str]) -> JsWorkspace:
        """From the repository's package.json, tsconfig*.json and jsconfig.json files, by path."""
        packages: dict[str, tuple[str, dict]] = {}
        aliases: list[tuple[str, str, list[str]]] = []
        for path in sorted(texts):
            name = path.rsplit("/", 1)[-1]
            directory = path.rsplit("/", 1)[0] if "/" in path else ""
            if name == "package.json":
                try:
                    meta = json.loads(texts[path])
                except ValueError:
                    continue
                if isinstance(meta, dict) and isinstance(meta.get("name"), str):
                    packages.setdefault(meta["name"], (directory, _package_view(meta)))
            elif is_workspace_config(path):
                options = _jsonc(texts[path]).get("compilerOptions") or {}
                base = _norm(f"{directory}/{options.get('baseUrl') or '.'}")
                for pattern, targets in (options.get("paths") or {}).items():
                    if isinstance(targets, list):
                        aliases.append((directory, pattern, [_norm(f"{base}/{t}") for t in targets if isinstance(t, str)]))
        return cls(packages, aliases)

    def to_dict(self) -> dict:
        return {"packages": {k: [d, m] for k, (d, m) in self.packages.items()},
                "aliases": [list(a) for a in self.aliases]}

    @classmethod
    def from_dict(cls, data: dict | None) -> JsWorkspace:
        data = data or {}
        packages = {k: (v[0], v[1]) for k, v in (data.get("packages") or {}).items()}
        aliases = [(a[0], a[1], list(a[2])) for a in (data.get("aliases") or [])]
        return cls(packages, aliases)

    def module_for(self, spec: str, importer: str, modules: dict[str, str]) -> str | None:
        """The repository module an import specifier names, or None (a third-party package)."""
        if not spec or spec in modules:
            return None
        for directory, pattern, targets in self.aliases:
            if directory and not importer.startswith(directory + "/"):
                continue
            head, star, tail = pattern.partition("*")
            if star:
                if not (spec.startswith(head) and spec.endswith(tail) and len(spec) >= len(head) + len(tail)):
                    continue
                captured = spec[len(head):len(spec) - len(tail)]
            elif spec != pattern:
                continue
            else:
                captured = ""
            for target in targets:
                hit = self._module(target.replace("*", captured), modules)
                if hit:
                    return hit
        name = max((n for n in self.packages if spec == n or spec.startswith(n + "/")), key=len, default=None)
        if name is None:
            return None
        directory, meta = self.packages[name]
        sub = spec[len(name):].lstrip("/")
        exports = meta.get("exports")
        entries: list[str] = []
        if isinstance(exports, dict):
            key = f"./{sub}" if sub else "."
            if key in exports:
                entries += _strings(exports[key])
            elif sub:
                for pat, value in exports.items():
                    head, star, tail = pat.partition("*")
                    if star and key.startswith(head) and key.endswith(tail):
                        entries += [v.replace("*", key[len(head):len(key) - len(tail)]) for v in _strings(value)]
        elif isinstance(exports, str) and not sub:
            entries.append(exports)
        if sub:
            entries += [f"src/{sub}", sub, f"lib/{sub}"]
        else:
            entries += [meta[k] for k in ("source", "types", "typings", "module", "main") if isinstance(meta.get(k), str)]
            entries += ["src/index", "index", "lib/index"]
        for entry in entries:
            hit = self._module(_norm(f"{directory}/{entry}"), modules)
            if hit:
                return hit
        return None

    @staticmethod
    def _module(path: str, modules: dict[str, str]) -> str | None:
        stem = JS_EXT.sub("", path).removesuffix("/index")
        for cand in (stem, *(re.sub(rf"(^|/){d}(/|$)", r"\1src\2", stem, count=1) for d in ("dist", "lib", "build"))):
            cand = cand.removesuffix("/index")
            if cand in modules:
                return cand
        return None


# --- the repository-wide index -------------------------------------------------------------------
class RepoIndex:
    """What every module defines, imports and exports, for resolving one file's names."""

    def __init__(self, repo: str | None, files: Iterable[FileFacts], go_prefix: str | None = None,
                 js: JsWorkspace | None = None):
        self.repo = repo
        self.go_prefix = go_prefix
        self.js = js or JsWorkspace()
        self.module_of: dict[str, str] = {}
        self.lang_of: dict[str, str] = {}
        self.defs: dict[str, dict[str, str]] = defaultdict(dict)          # module -> qualname -> key
        self.file_defs: dict[str, dict[str, str]] = defaultdict(dict)     # path -> qualname -> key
        self.def_kind: dict[str, str] = {}
        self.exports: dict[str, dict[str, Import]] = defaultdict(dict)    # module -> alias -> Import
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.module_path: dict[str, str] = {}
        self.star_exports: dict[str, list[str]] = defaultdict(list)       # module -> re-exported modules
        self._imports: dict[str, list[Import]] = {}
        self.path_of_key: dict[str, str] = {}
        self.qual_of_key: dict[str, str] = {}
        files = list(files)
        for f in files:
            self.add(f)
        for f in files:                       # exports need every module known, to map package names
            self.add_exports(f)

    def imports_of(self, f: FileFacts) -> list[Import]:
        """The file's imports, with JS/TS package names and path aliases mapped onto repository modules."""
        if f.lang not in JS_FAMILY:
            return f.imports
        got = self._imports.get(f.path)
        if got is None:
            got = self._imports[f.path] = [
                Import(i.alias, self.js.module_for(i.module, f.path, self.module_path) or i.module, i.name)
                for i in f.imports]
        return got

    def add_exports(self, f: FileFacts) -> None:
        if f.lang in ("python", *JS_FAMILY):
            for imp in self.imports_of(f):
                if imp.alias and imp.alias != "*":
                    self.exports[f.module][imp.alias] = imp
                elif f.lang in JS_FAMILY and imp.alias == "*" and imp.name == "*":
                    self.star_exports[f.module].append(imp.module)

    def add(self, f: FileFacts) -> None:
        self.module_of[f.path] = f.module
        self.lang_of[f.path] = f.lang
        if f.lang not in GO_LIKE:
            self.module_path[f.module] = f.path
        for d in f.defs:
            key = symbol_key(self.repo, f.path, d.qualname)
            self.defs[f.module][d.qualname] = key
            self.file_defs[f.path][d.qualname] = key
            self.def_kind[key] = d.kind
            self.path_of_key[key] = f.path
            self.qual_of_key[key] = d.qualname
            if d.kind in ("function", "method", "property"):
                self.by_name[d.qualname.rsplit(".", 1)[-1]].append(key)

    @property
    def modules(self) -> dict[str, dict[str, str]]:
        return self.defs

    def internal(self, module: str, lang: str) -> str | None:
        if lang == "go":
            if self.go_prefix and module.startswith(self.go_prefix + "/"):
                module = module[len(self.go_prefix) + 1:]
            elif self.go_prefix and module == self.go_prefix:
                module = "."
        if module in self.defs or module in self.module_path:
            return module
        return None

    @staticmethod
    def longest(defmap: dict[str, str], chain: Sequence[str]) -> str | None:
        for k in range(len(chain), 0, -1):
            hit = defmap.get(".".join(chain[:k]))
            if hit:
                return hit
        return None

    def via_import(self, imp: Import, rest: Sequence[str], lang: str, depth: int = 0) -> str | None:
        module = self.internal(imp.module, lang)
        if module is None or depth > 8:
            return None
        if lang in JS_FAMILY:
            if imp.name is None:
                return self.lookup(module, tuple(rest), lang, depth + 1) if rest else None
            if imp.name == "default":
                return self._default(module, imp, rest)
            return self.lookup(module, (imp.name, *rest), lang, depth + 1)
        if imp.name in (None, "*"):
            cur, i = module, 0
            while i <= len(rest):
                hit = self.longest(self.defs.get(cur, {}), rest[i:]) if i < len(rest) else None
                if hit:
                    return hit
                if i < len(rest) and lang == "python" and f"{cur}.{rest[i]}" in self.module_path:
                    cur, i = f"{cur}.{rest[i]}", i + 1
                    continue
                return None
            return None
        name = imp.name
        if name == "default":
            defs = self.defs.get(module, {})
            top = [q for q in defs if "." not in q]
            name = imp.alias if imp.alias in defs else (top[0] if len(top) == 1 else None)
            if name is None:
                return None
        hit = self.longest(self.defs.get(module, {}), (name, *rest))
        if hit:
            return hit
        again = self.exports.get(module, {}).get(name)
        if again is not None:
            return self.via_import(again, rest, lang, depth + 1)
        if lang == "python" and f"{module}.{name}" in self.module_path:
            return self.via_import(Import(name, f"{module}.{name}", None), rest, lang, depth + 1)
        return None

    def lookup(self, module: str, chain: tuple[str, ...], lang: str, depth: int = 0,
               seen: frozenset[str] = frozenset()) -> str | None:
        """A JS/TS module's member: its own definition, a named re-export, or one of its `export *`."""
        if depth > 8 or module in seen:
            return None
        hit = self.longest(self.defs.get(module, {}), chain)
        if hit:
            return hit
        again = self.exports.get(module, {}).get(chain[0])
        if again is not None:
            hit = self.via_import(again, chain[1:], lang, depth + 1)
            if hit:
                return hit
        for star in self.star_exports.get(module, ()):
            inner = self.internal(star, lang)
            if inner:
                hit = self.lookup(inner, chain, lang, depth + 1, seen | {module})
                if hit:
                    return hit
        return None

    def _default(self, module: str, imp: Import, rest: Sequence[str]) -> str | None:
        defs = self.defs.get(module, {})
        top = [q for q in defs if "." not in q]
        name = imp.alias if imp.alias in defs else (top[0] if len(top) == 1 else None)
        return self.longest(defs, (name, *rest)) if name else None


def _prefix(repo: str | None) -> str:
    return f"{repo}:" if repo else ""


def symbol_key(repo: str | None, path: str, qualname: str) -> str:
    return f"symbol:{_prefix(repo)}{path}:{qualname}"


def file_key(repo: str | None, path: str) -> str:
    return f"file:{_prefix(repo)}{path}"


def module_key(repo: str | None, module: str) -> str:
    return f"module:{_prefix(repo)}{module}"


class Resolver:
    """One file's references, resolved against the repository index."""

    def __init__(self, index: RepoIndex, f: FileFacts):
        self.ix = index
        self.f = f
        self.local = index.defs.get(f.module, {}) if f.lang in GO_LIKE else index.file_defs.get(f.path, {})
        self.aliases: dict[str, Import] = {}
        self.stars: list[Import] = []
        for imp in index.imports_of(f):
            if imp.alias == "*" or imp.name == "*":
                self.stars.append(imp)
            elif imp.alias:
                self.aliases[imp.alias] = imp

    def resolve(self, ref: Ref) -> tuple[str, float] | None:
        chain = ref.chain
        head = chain[0]
        lang = self.f.lang
        if head in ("self", "cls", "this") and len(chain) >= 2:
            cls = ref.src.split(".")[0] if ref.src else ""
            hit = self.ix.longest(self.ix.file_defs.get(self.f.path, {}), (cls, *chain[1:])) if cls else None
            if hit and hit != symbol_key(self.ix.repo, self.f.path, cls):
                return hit, 1.0
            return self._unique(ref)
        hit = self.ix.longest(self.local, chain)
        if hit:
            return hit, 1.0
        imp = self.aliases.get(head)
        if imp is not None:
            hit = self.ix.via_import(imp, chain[1:], lang)
            return (hit, 1.0) if hit else None
        for star in self.stars:
            module = self.ix.internal(star.module, lang)
            if module:
                hit = (self.ix.lookup(module, tuple(chain), lang) if lang in JS_FAMILY
                       else self.ix.longest(self.ix.defs.get(module, {}), chain))
                if hit:
                    return hit, 0.9
        return self._unique(ref)

    def _unique(self, ref: Ref) -> tuple[str, float] | None:
        """A member used on an object of unknown type: link it only when exactly one definition in the
        repository has that name (lower confidence). A call may be any function or method; a plain
        attribute read only a property, since that is how a property is used."""
        if len(ref.chain) < 2:
            return None
        family = _family(self.f.lang)
        cands = [k for k in self.ix.by_name.get(ref.chain[-1], ()) if _family(self.ix.lang_of.get(self.ix.path_of_key.get(k))) == family]
        if len(cands) != 1 or (not ref.call and self.ix.def_kind.get(cands[0]) != "property"):
            return None
        return cands[0], 0.5

    def src_key(self, src: str) -> str:
        defs = self.ix.file_defs.get(self.f.path, {})
        parts = src.split(".") if src else []
        while parts:
            key = defs.get(".".join(parts))
            if key:
                return key
            parts.pop()
        return file_key(self.ix.repo, self.f.path)


def _family(lang: str | None) -> str | None:
    return "js" if lang in JS_FAMILY else lang



def graph_for(index: RepoIndex, files: Iterable[FileFacts]) -> tuple[list[dict], list[dict]]:
    """Nodes and edges contributed by these files (edges keyed by node keys, not ids yet)."""
    repo = index.repo
    nodes: dict[tuple[str, str], dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}

    def node(kind, key, name, attrs=None, path=None, repo_scoped=True):
        nodes.setdefault((kind, key), {"kind": kind, "key": key, "name": name, "attrs": attrs or {},
                                       "repo": (repo or "") if repo_scoped else None, "path": path})
        return key

    def edge(src, dst, kind, line, conf=1.0):
        if src == dst:
            return
        e = edges.get((src, dst, kind))
        if e is None:
            edges[(src, dst, kind)] = {"src": src, "dst": dst, "kind": kind, "confidence": conf,
                                      "attrs": {"line": line, "count": 1}}
        else:
            e["attrs"]["count"] += 1
            e["confidence"] = max(e["confidence"], conf)

    kinds = {"file": "file", "module": "module", "symbol": "symbol"}
    for f in files:
        fk = node("file", file_key(repo, f.path), f.path,
                  {"lang": f.lang, "module": f.module, "lines": f.lines, "error": f.error}, path=f.path)
        node("module", module_key(repo, f.module), f.module, {"lang": f.lang},
             path=f.path if f.lang not in GO_LIKE else None)
        r = Resolver(index, f)
        for d in f.defs:
            sk = node("symbol", symbol_key(repo, f.path, d.qualname), d.qualname,
                      {"symbol_kind": d.kind, "line": d.line, "end_line": d.end_line, "lang": f.lang,
                       "module": f.module}, path=f.path)
            edge(fk, sk, "defines", d.line)
        for imp in index.imports_of(f):
            module = index.internal(imp.module, f.lang)
            if module is None:
                continue
            target = None
            if imp.name not in (None, "*"):
                target = index.via_import(imp, (), f.lang)
            if target is None:
                target = module_key(repo, module)
                node("module", target, module, {"lang": f.lang},
                     path=index.module_path.get(module) if f.lang not in GO_LIKE else None)
            edge(fk, target, "imports", 0)
        for ref in f.refs:
            publish = ref.chain[-1] == "#publish"
            chain = ref.chain[:-1] if publish else ref.chain
            hit = r.resolve(Ref(ref.src, chain, ref.call, ref.line))
            if not hit:
                continue
            target, conf = hit
            src = r.src_key(ref.src)
            if publish:
                qual = index.qual_of_key.get(target, "")
                tmodule = index.module_of.get(index.path_of_key.get(target, ""), "")
                qk = node("queue", f"queue:celery:{tmodule}.{qual}", f"celery:{tmodule}.{qual}", repo_scoped=False)
                edge(src, qk, "publishes", ref.line, conf)
            else:
                edge(src, target, "calls" if ref.call else "references", ref.line, conf)
        for src, method, route in f.endpoints:
            if src.startswith("#ref:"):
                hit = r.resolve(Ref("", tuple(src[5:].split(".")), False, 0))
                skey = hit[0] if hit else fk
            else:
                skey = r.src_key(src)
            ek = node("endpoint", f"endpoint:{_prefix(repo)}{method} {route}", f"{method} {route}",
                      {"method": method, "route": route}, path=f.path)
            edge(skey, ek, "exposes", 0)
        for src, table, how in f.tables:
            tk = node("table", f"table:{table}", table, repo_scoped=False)
            edge(r.src_key(src), tk, how, 0)
        for src, queue, how in f.queues:
            qk = node("queue", f"queue:{queue}", queue, repo_scoped=False)
            edge(r.src_key(src), qk, how, 0)
        for src, key in f.config:
            ck = node("config_key", f"config:{key}", key, repo_scoped=False)
            edge(r.src_key(src), ck, "reads", 0)
    del kinds
    return list(nodes.values()), list(edges.values())




# --- writing through the store -----------------------------------------------------------------------
_REPO_NODE_KEY = "code_repo:{repo}"


def _node_body(node: dict, facts_by_path: dict[str, FileFacts]) -> str:
    if node["kind"] == "file":
        f = facts_by_path.get(node.get("path") or "")
        return f.content_hash if f is not None else ""
    if node["kind"] == "symbol":
        attrs = node.get("attrs") or {}
        return f"{attrs.get('lang', '')} {node['name']}".strip()
    return node["name"]


def _write(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    index: RepoIndex,
    files: Sequence[FileFacts],
    seen_at,
    extra_nodes: Sequence[dict] = (),
) -> dict[str, int]:
    nodes, edges = graph_for(index, files)
    facts_by_path = {f.path: f for f in files}
    rows = []
    for node in [*nodes, *extra_nodes]:
        attrs = dict(node.get("attrs") or {})
        if node["kind"] == "file" and node.get("path") in facts_by_path:
            f = facts_by_path[node["path"]]
            # The per-file cache: an unchanged file is never parsed again, and a later commit
            # resolves against it without re-reading the tree.
            attrs.update({"content_hash": f.content_hash, "facts": facts_to_row(f), "names": names_of(f)})
        rows.append({**node, "attrs": attrs, "body": _node_body(node, facts_by_path)})
    ids = store.upsert_code_nodes(connection, project_id=project_id, rows=rows, seen_at=seen_at)
    by_key = {key: node_id for (_kind, key), node_id in ids.items()}
    missing = sorted({e[end] for e in edges for end in ("src", "dst")} - set(by_key))
    by_key.update(store.code_node_ids(connection, project_id=project_id, keys=missing))
    resolved = [{**e, "src": by_key[e["src"]], "dst": by_key[e["dst"]]} for e in edges
                if e["src"] in by_key and e["dst"] in by_key]
    written = store.upsert_code_edges(connection, project_id=project_id, rows=resolved, seen_at=seen_at)
    return {"nodes": len(rows), "edges": written}


def _cached(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    repo: str | None,
    paths: Sequence[str] | None = None,
) -> dict[str, FileFacts]:
    out: dict[str, FileFacts] = {}
    for row in store.code_file_facts(connection, project_id=project_id, repo=repo or "", paths=paths):
        attrs = row["attrs"] or {}
        if not isinstance(attrs.get("facts"), dict):
            continue
        out[row["path"]] = row_to_facts(row["path"], attrs.get("lang") or "", attrs.get("module") or "",
                                        attrs.get("content_hash") or "", attrs["facts"])
    return out


def _repo_node(repo: str | None, workspace: JsWorkspace, go_prefix: str | None, run: dict) -> dict:
    return {"kind": "repo", "key": _REPO_NODE_KEY.format(repo=repo or ""), "name": repo or "code",
            "attrs": {"workspace": workspace.to_dict(), "go_prefix": go_prefix, "last_run": run},
            "repo": repo or "", "path": None}


def _repo_state(connection, store, project_id: str, repo: str | None) -> tuple[JsWorkspace, str | None]:
    attrs = store.code_repo_attrs(connection, project_id=project_id,
                                  node_key=_REPO_NODE_KEY.format(repo=repo or ""))
    return JsWorkspace.from_dict(attrs.get("workspace")), attrs.get("go_prefix")


def _go_prefix(texts: dict[str, str]) -> str | None:
    for line in (texts.get("go.mod") or "").splitlines():
        if line.startswith("module "):
            return line.split()[1]
    return None


def _relink(connection: Connection, store: PostgresKnowledgeStore, project_id: str) -> None:
    """Records keep their changed-symbol links across a re-index (knowledge/capture.py)."""
    from .capture import sync_change_links

    sync_change_links(connection, store, project_id=project_id)


def index_repository(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    root: Path,
    repo: str | None = None,
) -> dict[str, Any]:
    """Index a whole tree. Re-running it on the same tree changes nothing."""
    import hashlib

    seen_at = now_utc()
    root = Path(root)
    paths = repository_paths(root)
    cached = _cached(connection, store, project_id=project_id, repo=repo)
    files: list[FileFacts] = []
    parsed = 0
    config: dict[str, str] = {}
    for path in paths:
        if is_workspace_config(path) or path == "go.mod":
            try:
                config[path] = (root / path).read_text(errors="replace")
            except OSError:
                pass
        if not indexable(path):
            continue
        try:
            raw = (root / path).read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        old = cached.get(path)
        if old is not None and old.content_hash == digest:
            files.append(old)
            continue
        files.append(parse_file(path, raw))
        parsed += 1
    workspace = JsWorkspace.from_texts({p: t for p, t in config.items() if p != "go.mod"})
    go_prefix = _go_prefix(config)
    index = RepoIndex(repo, files, go_prefix, workspace)
    run: dict[str, Any] = {"mode": "full", "files": len(files), "parsed": parsed,
                           "symbols": sum(len(f.defs) for f in files),
                           "unparsed": sorted(f.path for f in files if f.error)[:50]}
    wrote = _write(connection, store, project_id=project_id, index=index, files=files, seen_at=seen_at,
                   extra_nodes=[_repo_node(repo, workspace, go_prefix, run)])
    closed = store.close_unseen_code(connection, project_id=project_id, repo=repo or "", before=seen_at)
    _relink(connection, store, project_id)
    return {**run, **wrote, "closed": closed}


def index_files(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    files: list[dict],
    repo: str | None = None,
) -> dict:
    """Index the files BOSS just wrote. An unchanged file hash does not write again."""
    import hashlib

    cached = _cached(connection, store, project_id=project_id, repo=repo)
    changed: dict[str, FileFacts] = {}
    skipped = 0
    for item in files:
        path = str(item["path"])
        language = str(item["language"])
        source = str(item["source"])
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        old = cached.get(path)
        if old is not None and old.content_hash == digest:
            skipped += 1
            continue
        changed[path] = extract(path, language, source)
    if not changed:
        return {"indexed": 0, "skipped": skipped, "symbols": 0}
    # Other files' edges move only when a definition appears or disappears, or when a module
    # appears, disappears, or changes what it imports (and so what it re-exports).
    touched: set[str] = set()
    for path, new in changed.items():
        old = cached.get(path)
        before = {d.qualname for d in old.defs} if old is not None else set()
        for qual in before ^ {d.qualname for d in new.defs}:
            touched.update(qual.split("."))
        if old is None or old.module != new.module or old.imports != new.imports:
            for f in (old, new):
                if f is not None:
                    touched.update({f.module, f.module.rsplit(".", 1)[-1].rsplit("/", 1)[-1]})
    affected = {p for p, f in cached.items() if p not in changed and touched & set(names_of(f))}
    everything = {**cached, **changed}
    workspace, go_prefix = _repo_state(connection, store, project_id, repo)
    index = RepoIndex(repo, everything.values(), go_prefix, workspace)
    slice_files = [everything[p] for p in sorted(affected | set(changed))]
    seen_at = now_utc()
    wrote = _write(connection, store, project_id=project_id, index=index, files=slice_files, seen_at=seen_at)
    closed = store.close_unseen_code(connection, project_id=project_id, repo=repo or "", before=seen_at,
                                     paths=[f.path for f in slice_files])
    _relink(connection, store, project_id)
    return {"indexed": len(changed), "skipped": skipped,
            "symbols": sum(len(f.defs) for f in changed.values()),
            "reresolved": len(slice_files), **wrote, "closed": closed}


def symbol_keys_for(files: list[dict], repo: str | None = None) -> list[str]:
    """The node keys of what these files define, for linking a record to exactly those symbols."""
    keys: list[str] = []
    for item in files:
        path = str(item["path"])
        for qual in parse_source(path, str(item["language"]), str(item["source"])).qualnames:
            key = symbol_key(repo, path, qual)
            if key not in keys:
                keys.append(key)
    return keys
