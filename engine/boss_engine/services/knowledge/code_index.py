"""The code graph: index a repository into the KnowledgeStore (KNOWLEDGE_GRAPH_SPEC.md §3.3, §4; Phase 1).

`code_parse` turns each file into facts; this module resolves names across the repository and
writes the graph. Nodes are `file`, `module`, `symbol`, `endpoint`, `table`, `queue` and
`config_key`. Edges are `defines`, `imports`, `calls`, `references`, `exposes`, `reads`,
`writes`, `publishes` and `subscribes`.

Tables, queues, webhooks and config keys are keyed project-wide, not per repository. Two
repositories that use the same table, or that publish and consume the same webhook, meet at the
same node: those are the cross-repository edges.

* **Full** (first index, nightly reconcile): every file is hashed, and only files whose content
  changed are parsed. The cache is `kg_code_files`. The whole graph is resolved again, and
  anything the repository no longer contains has its validity closed.
* **Incremental** (every commit BOSS pushes): `git diff` names the changed files. They are
  re-parsed. The other files whose references they can change are re-resolved: those that
  mention a name the change defined or removed, or that import a changed module. Only that
  slice of the graph is rewritten.

Nothing is deleted: a symbol that disappears gets `valid_to`, as every fact in the store does.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import now_utc
from .code_parse import Def, FileFacts, Import, Ref, language_for, parse_file
from .store import PostgresKnowledgeStore, Scope

SOURCE = "code_index"
SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".git/", "__pycache__/", "third_party/")
GO_LIKE = ("go", "java", "csharp")       # a module (package/namespace) spans several files
JS_FAMILY = ("javascript", "typescript", "tsx")
PARSE_CHUNK = 64


# --- files and parsing ----------------------------------------------------------------------------
def indexable(path: str) -> bool:
    if language_for(path) is None or path.endswith((".min.js", ".d.ts")):
        return False
    return not any(f"/{d}" in f"/{path}" for d in SKIP_DIRS)


def list_files(root: Path) -> list[str]:
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p and indexable(p))


def _parse_batch(root: str, paths: Sequence[str]) -> list[dict]:
    out = []
    for path in paths:
        try:
            source = (Path(root) / path).read_bytes()
        except OSError:
            continue
        out.append(asdict(parse_file(path, source)))
    return out


def parse_files(root: Path, paths: Sequence[str], workers: int | None = None) -> list[FileFacts]:
    """Parse in parallel across processes (tree-sitter work is CPU-bound)."""
    batches = [paths[i:i + PARSE_CHUNK] for i in range(0, len(paths), PARSE_CHUNK)]
    workers = workers if workers is not None else max(1, min(8, (os.cpu_count() or 2)))
    if workers <= 1 or len(batches) <= 1:
        dicts = [d for b in batches for d in _parse_batch(str(root), b)]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            dicts = [d for part in pool.map(_parse_batch, [str(root)] * len(batches), batches) for d in part]
    return [facts_from_dict(d) for d in dicts]


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
JSONC_NOISE = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.S)


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
        proc = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
        paths = [p for p in proc.stdout.decode("utf-8", "replace").split("\0")
                 if p and not any(f"/{d}" in f"/{p}" for d in SKIP_DIRS)]
        packages: dict[str, tuple[str, dict]] = {}
        aliases: list[tuple[str, str, list[str]]] = []
        for path in paths:
            name = path.rsplit("/", 1)[-1]
            directory = path.rsplit("/", 1)[0] if "/" in path else ""
            if name == "package.json":
                try:
                    meta = json.loads((root / path).read_text(errors="replace"))
                except (OSError, ValueError):
                    continue
                if isinstance(meta, dict) and isinstance(meta.get("name"), str):
                    packages.setdefault(meta["name"], (directory, meta))
            elif name in ("jsconfig.json",) or (name.startswith("tsconfig") and name.endswith(".json")):
                try:
                    options = _jsonc((root / path).read_text(errors="replace")).get("compilerOptions") or {}
                except OSError:
                    continue
                base = _norm(f"{directory}/{options.get('baseUrl') or '.'}")
                for pattern, targets in (options.get("paths") or {}).items():
                    if isinstance(targets, list):
                        aliases.append((directory, pattern, [_norm(f"{base}/{t}") for t in targets if isinstance(t, str)]))
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

    def __init__(self, repo: str, files: Iterable[FileFacts], go_prefix: str | None = None,
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


def symbol_key(repo: str, path: str, qualname: str) -> str:
    return f"{repo}:{path}::{qualname}"


def file_key(repo: str, path: str) -> str:
    return f"{repo}:{path}"


def module_key(repo: str, module: str) -> str:
    return f"{repo}:module:{module}"


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
        cands = [k for k in self.ix.by_name.get(ref.chain[-1], ()) if _family(self.ix.lang_of.get(_path_of(k))) == family]
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


def _path_of(key: str) -> str:
    return key.split(":", 1)[1].split("::", 1)[0]


def graph_for(index: RepoIndex, files: Iterable[FileFacts]) -> tuple[list[dict], list[dict]]:
    """Nodes and edges contributed by these files (edges keyed by node keys, not ids yet)."""
    repo = index.repo
    nodes: dict[tuple[str, str], dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}

    def node(kind, key, name, attrs=None, path=None, repo_scoped=True):
        nodes.setdefault((kind, key), {"kind": kind, "key": key, "name": name, "attrs": attrs or {},
                                       "repo": repo if repo_scoped else None, "path": path})
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
                qual = target.split("::", 1)[1]
                tmodule = index.module_of.get(_path_of(target), "")
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
            ek = node("endpoint", f"{repo}:endpoint:{method} {route}", f"{method} {route}",
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


# --- the indexer ----------------------------------------------------------------------------------
class CodeIndexer:
    def __init__(self, store: PostgresKnowledgeStore | None = None, *, workers: int | None = None):
        self.store = store or PostgresKnowledgeStore()
        self.workers = workers

    # state ---------------------------------------------------------------------------------------
    def state(self, c: Connection, scope: Scope, repo: str) -> dict | None:
        row = c.execute(text("SELECT last_commit, last_full_at, last_run FROM kg_code_index_state "
                             "WHERE project_id = :p AND repo = :r"), {"p": scope.project_id, "r": repo}).mappings().first()
        return dict(row) if row else None

    def _save_state(self, c, scope, repo, commit, full: bool, run: dict) -> None:
        c.execute(text(
            """INSERT INTO kg_code_index_state (project_id, repo, last_commit, last_full_at, last_run, updated_at)
               VALUES (:p, :r, :c, CASE WHEN :full THEN now() END, CAST(:run AS JSONB), now())
               ON CONFLICT (project_id, repo) DO UPDATE SET last_commit = EXCLUDED.last_commit,
                 last_full_at = COALESCE(EXCLUDED.last_full_at, kg_code_index_state.last_full_at),
                 last_run = EXCLUDED.last_run, updated_at = now()"""),
            {"p": scope.project_id, "r": repo, "c": commit, "full": full, "run": _json(run)})

    # cache ---------------------------------------------------------------------------------------
    def _cached(self, c, scope, repo, paths: Sequence[str] | None = None, *, full_facts: bool = True) -> dict[str, FileFacts]:
        cols = "facts" if full_facts else "jsonb_build_object('defs', facts->'defs', 'imports', facts->'imports', " \
                                          "'lines', facts->'lines')"
        where = "" if paths is None else " AND path = ANY(:paths)"
        params: dict[str, Any] = {"p": scope.project_id, "r": repo}
        if paths is not None:
            params["paths"] = list(paths)
        rows = c.execute(text(f"SELECT path, lang, module, content_hash, {cols} AS facts FROM kg_code_files "
                              f"WHERE project_id = :p AND repo = :r{where}"), params).mappings().all()
        return {r["path"]: row_to_facts(r["path"], r["lang"], r["module"], r["content_hash"], r["facts"]) for r in rows}

    def _store_files(self, c, scope, repo, files: Sequence[FileFacts], commit: str | None) -> None:
        for start in range(0, len(files), 1000):
            part = files[start:start + 1000]
            c.execute(text(
                """INSERT INTO kg_code_files (project_id, repo, path, lang, module, content_hash, facts, names,
                                              indexed_commit, updated_at)
                   SELECT :p, :r, u.path, u.lang, u.module, u.hash, CAST(u.facts AS JSONB),
                          CAST(u.names AS TEXT[]), :commit, now()
                   FROM unnest(CAST(:paths AS TEXT[]), CAST(:langs AS TEXT[]), CAST(:modules AS TEXT[]),
                               CAST(:hashes AS TEXT[]), CAST(:facts AS TEXT[]), CAST(:names AS TEXT[]))
                        AS u(path, lang, module, hash, facts, names)
                   ON CONFLICT (project_id, repo, path) DO UPDATE SET lang = EXCLUDED.lang, module = EXCLUDED.module,
                     content_hash = EXCLUDED.content_hash, facts = EXCLUDED.facts, names = EXCLUDED.names,
                     indexed_commit = EXCLUDED.indexed_commit, updated_at = now()"""),
                {"p": scope.project_id, "r": repo, "commit": commit, "paths": [f.path for f in part],
                 "langs": [f.lang for f in part], "modules": [f.module for f in part],
                 "hashes": [f.content_hash for f in part], "facts": [_json(facts_to_row(f)) for f in part],
                 "names": ["{" + ",".join(_pg_array_item(n) for n in names_of(f)) + "}" for f in part]})

    # write ---------------------------------------------------------------------------------------
    def _write(self, c, scope, index: RepoIndex, files: Sequence[FileFacts], seen_at) -> dict[str, int]:
        nodes, edges = graph_for(index, files)
        ids = self.store.upsert_nodes_bulk(c, scope, nodes, seen_at=seen_at, source=SOURCE)
        # Edge ends outside this slice (a symbol in an unchanged file) already exist: look them up.
        by_key = {key: i for (_, key), i in ids.items()}
        missing = sorted({e[end] for e in edges for end in ("src", "dst")} - set(by_key))
        for start in range(0, len(missing), 5000):
            rows = c.execute(text("SELECT node_key, id FROM kg_nodes WHERE project_id = :p AND node_key = ANY(:k) "
                                  "AND valid_to IS NULL"), {"p": scope.project_id, "k": missing[start:start + 5000]})
            by_key.update({r.node_key: r.id for r in rows})
        rows = [{**e, "src": by_key[e["src"]], "dst": by_key[e["dst"]]} for e in edges
                if e["src"] in by_key and e["dst"] in by_key]
        written = self.store.upsert_edges_bulk(c, scope, rows, seen_at=seen_at, source=SOURCE)
        return {"nodes": len(nodes), "edges": written}

    # entry points --------------------------------------------------------------------------------
    def index_full(self, c: Connection, scope: Scope, repo: str, root: Path, commit: str | None = None) -> dict:
        t0 = time.monotonic()
        seen_at = now_utc()
        commit = commit or _head(root)
        paths = list_files(root)
        cached = self._cached(c, scope, repo)
        hashes = {p: _hash(root / p) for p in paths}
        to_parse = [p for p in paths if p not in cached or cached[p].content_hash != hashes[p]]
        parsed = parse_files(root, to_parse, self.workers)
        t_parse = time.monotonic() - t0
        self._store_files(c, scope, repo, parsed, commit)
        gone = sorted(set(cached) - set(paths))
        if gone:
            c.execute(text("DELETE FROM kg_code_files WHERE project_id = :p AND repo = :r AND path = ANY(:g)"),
                      {"p": scope.project_id, "r": repo, "g": gone})
        files = {**{p: f for p, f in cached.items() if p in hashes}, **{f.path: f for f in parsed}}
        index = RepoIndex(repo, files.values(), _go_prefix(root), JsWorkspace.load(root))
        wrote = self._write(c, scope, index, list(files.values()), seen_at)
        closed = self.store.close_unseen(c, scope, repo=repo, source=SOURCE, before=seen_at)
        run = {"mode": "full", "commit": commit, "files": len(files), "parsed": len(parsed),
               "symbols": sum(len(f.defs) for f in files.values()), **wrote, "closed": closed,
               "parse_seconds": round(t_parse, 1), "seconds": round(time.monotonic() - t0, 1)}
        self._save_state(c, scope, repo, commit, True, run)
        return run

    def index_incremental(self, c: Connection, scope: Scope, repo: str, root: Path, to_commit: str | None = None,
                          from_commit: str | None = None) -> dict:
        t0 = time.monotonic()
        state = self.state(c, scope, repo)
        from_commit = from_commit or (state or {}).get("last_commit")
        to_commit = to_commit or _head(root)
        if not from_commit:
            return self.index_full(c, scope, repo, root, to_commit)
        try:
            changed, deleted = git_changes(root, from_commit, to_commit)
        except subprocess.CalledProcessError:
            return self.index_full(c, scope, repo, root, to_commit)   # history we cannot diff: reconcile
        seen_at = now_utc()
        old = self._cached(c, scope, repo, changed + deleted)
        parsed = parse_files(root, changed, self.workers)
        # Other files' edges change only when a definition appears or disappears (a changed body
        # keeps every key), or when a module itself appears or disappears.
        new_by_path = {f.path: f for f in parsed}
        touched_names: set[str] = set()
        for path in set(old) | set(new_by_path):
            before = {d.qualname for d in old[path].defs} if path in old else set()
            after = {d.qualname for d in new_by_path[path].defs} if path in new_by_path else set()
            for qual in before ^ after:
                touched_names.update(qual.split("."))
            # A module that appears, disappears, moves, or changes what it imports (and so what it
            # re-exports) changes the edges of the files that import it.
            if (path in old) != (path in new_by_path) or (path in old and path in new_by_path and (
                    old[path].module != new_by_path[path].module or old[path].imports != new_by_path[path].imports)):
                for f in (old.get(path), new_by_path.get(path)):
                    if f is not None:
                        touched_names.update({f.module, f.module.rsplit(".", 1)[-1].rsplit("/", 1)[-1]})
        self._store_files(c, scope, repo, parsed, to_commit)
        if deleted:
            c.execute(text("DELETE FROM kg_code_files WHERE project_id = :p AND repo = :r AND path = ANY(:g)"),
                      {"p": scope.project_id, "r": repo, "g": deleted})
        affected = [r.path for r in c.execute(text(
            "SELECT path FROM kg_code_files WHERE project_id = :p AND repo = :r AND names && CAST(:n AS TEXT[])"),
            {"p": scope.project_id, "r": repo, "n": sorted(touched_names)})]
        slice_paths = sorted(set(affected) | set(changed))
        everything = self._cached(c, scope, repo, full_facts=False)
        index = RepoIndex(repo, everything.values(), _go_prefix(root), JsWorkspace.load(root))
        slice_files = {**self._cached(c, scope, repo, slice_paths), **{f.path: f for f in parsed}}
        wrote = self._write(c, scope, index, list(slice_files.values()), seen_at)
        closed = self.store.close_unseen(c, scope, repo=repo, source=SOURCE, before=seen_at,
                                         paths=sorted(set(slice_paths) | set(deleted)))
        run = {"mode": "incremental", "from": from_commit, "commit": to_commit, "changed": len(changed),
               "deleted": len(deleted), "reresolved": len(slice_paths), **wrote, "closed": closed,
               "seconds": round(time.monotonic() - t0, 2)}
        self._save_state(c, scope, repo, to_commit, False, run)
        return run


def git_changes(root: Path, a: str, b: str) -> tuple[list[str], list[str]]:
    """Indexable files changed (added, modified, renamed-to) and deleted (or renamed-from) in a..b."""
    out = subprocess.run(["git", "diff", "--name-status", "-M", "-z", a, b], cwd=root, capture_output=True,
                         check=True).stdout.decode("utf-8", "replace").split("\0")
    changed, deleted, i = [], [], 0
    while i < len(out) and out[i]:
        status = out[i]
        if status.startswith(("R", "C")):
            old, new = out[i + 1], out[i + 2]
            if status.startswith("R") and indexable(old):
                deleted.append(old)
            if indexable(new):
                changed.append(new)
            i += 3
            continue
        path = out[i + 1]
        if indexable(path):
            (deleted if status.startswith("D") else changed).append(path)
        i += 2
    return sorted(set(changed)), sorted(set(deleted))


def _head(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                          check=True).stdout.strip()


def _hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _go_prefix(root: Path) -> str | None:
    gomod = root / "go.mod"
    if gomod.exists():
        for line in gomod.read_text(errors="replace").splitlines():
            if line.startswith("module "):
                return line.split()[1]
    return None


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


def _pg_array_item(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
