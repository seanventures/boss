"""An acceptance item about the project's DEPENDENCIES, settled by reading its manifests
(BOSSAPP-1333, PR #172 review P1).

A manifest is evidence only of what it declares, so BOSS reads it rather than trusting a
citation of it: on FXQuote's tree (go.mod requires gopkg.in/yaml.v3) "Standard library only, no
external modules" came out MET because the answer cited go.mod. Now:

  * an item is a dependency item only when it is ABOUT the dependencies — every content word is
    the vocabulary of dependencies, stacks and manifests (or a dependency the manifest names).
    "Rate YAML parsing uses only the standard library" and "Quote IDs use no third-party
    library and are unguessable" mix behaviour in: behaviour items, judged on code;
  * its claim is checked mechanically against the direct runtime requirements the manifests
    declare (go.mod `require`, package.json `dependencies`, requirements*.txt, pyproject.toml,
    pom.xml): "standard library only" / "no third-party" means none; "single dependency X"
    means exactly X; "X pinned to v3" checks X's version; "one Go module" counts go.mod files;
  * what a manifest cannot settle (licences, anything else said about the dependencies) needs a
    person — never MET.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

#: (verdict, why): "met" (the manifests say so), "false" (they say otherwise), "human".
Claim = tuple[str, str]

_SKIP = {"node_modules", "vendor", ".git", "testdata", "dist", "build", "target", ".venv",
         "venv", "__pycache__"}

#: Every word a dependency item may be made of (beyond names its manifests declare).
_VOCAB = frozenset("""
a an the one single sole only no zero other any all every each of and or with without via in on
to at as is are be being must should shall will exactly just than beyond besides except apart
from its their this that project service app application codebase code it has have uses use
used using declares declare declared confirmed present listed exist exists depends depend
stack go golang python node nodejs javascript typescript java kotlin rust ruby language written
dependency dependencies dep deps module modules package packages library libraries import
imports requirement requirements require requires version versions pinned pin pins locked lock
lockfile lockfiles manifest manifests standard stdlib third party thirdparty external runtime
direct transitive vendored upstream allowed approved exact minimum supported open source
licence licences license licenses licensed mit apache bsd gpl mpl isc permissive upgrade
upgraded bump bumped update updated mod sum json txt toml xml gradle cargo pom pyproject
""".split())

_LICENCE = re.compile(r"(?i)\blicen[cs]e[ds]?\b|\b(?:MIT|Apache|BSD|GPL|MPL|ISC)\b")
_NONE = re.compile(
    r"(?i)\bstandard[\s\-]+library\s+only\b|\bonly\s+(?:the\s+)?standard[\s\-]+library\b|"
    r"\bstdlib\s+only\b|\bonly\s+(?:the\s+)?stdlib\b|"
    r"\bno\s+(?:third[\s\-]*party|external|runtime)?\s*(?:dependenc(?:y|ies)|deps|modules?|"
    r"packages?|librar(?:y|ies))\b|\bno\s+(?:third[\s\-]*party|external)\b|"
    r"\bzero\s+(?:third[\s\-]*party\s+|external\s+)?(?:dependenc(?:y|ies)|deps)\b")
_SINGLE = re.compile(r"(?i)\b(?:single|sole|one|only)\s+(?:pinned\s+|direct\s+|runtime\s+|"
                     r"third[\s\-]*party\s+|external\s+)*(?:dependency|dep|requirement)\b")
_NO_OTHER = re.compile(r"(?i)\bno\s+other\b")
_PIN = re.compile(r"(?i)\b(?:pinned|pin|locked|upgrade\w*|bump\w*|update\w*)\b[^.;]{0,40}?"
                  r"\b(?:to|at|=|@)\s*v?(\d+(?:\.\d+)*)")
_ONE_MODULE = re.compile(r"(?i)\b(?:one|single)\s+(?:go\s+)?module\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _ticked(text: str) -> list[str]:
    return [t.strip() for t in re.findall(r"`([^`]+)`", text)]


def is_dependency_item(item_text: Any, deps: Optional[dict[str, str]] = None) -> bool:
    """Whether every content word of the item is about dependencies (backticked names and
    version numbers aside, and a dependency the manifests declare named in plain words)."""
    text = str(item_text or "")
    bare = re.sub(r"`[^`]*`", " ", text)
    named = {p.lower() for name in (deps or {}) for p in re.split(r"[/.\-_@:]", name) if p}
    words = [w.lower() for w in _WORD.findall(bare) if not re.fullmatch(r"v?\d+", w.lower())]
    if not words:
        return False
    about = any(w in {"dependency", "dependencies", "dep", "deps", "module", "modules",
                      "package", "packages", "library", "libraries", "stdlib", "licence",
                      "license", "licensed", "licences", "licenses", "requirement",
                      "requirements", "pinned"} for w in words)
    return about and all(w in _VOCAB or w in named for w in words)


def manifests(root: Any) -> dict[str, Any]:
    """{"files", "deps": {name: version}, "go_modules": n, "unreadable": [why]}: every runtime
    requirement the tree's manifests declare — go.mod `require` (an `// indirect` one too: it
    is linked into the binary); package.json `dependencies`, `optionalDependencies` and
    `peerDependencies`; requirements*.txt with its `-r` includes followed; pyproject.toml's
    [project] dependencies and [tool.poetry.dependencies]; pom.xml outside test scope.
    `unreadable` names what the parser cannot fully read (`-e git+…`, a URL requirement,
    `dynamic = ["dependencies"]`, setup.py, setup.cfg, Pipfile): a claim over it needs a person
    (PR #172 re-review)."""
    import os

    out: dict[str, Any] = {"files": [], "deps": {}, "go_modules": 0, "unreadable": []}
    root = Path(root)
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        for name in filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            low = name.lower()
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            got: Optional[dict[str, str]] = None
            if name == "go.mod":
                out["go_modules"] += 1
                got = _go_mod(text)
            elif name == "package.json" and "node_modules" not in rel:
                try:
                    pkg = json.loads(text)
                    got = {}
                    for key in ("dependencies", "optionalDependencies", "peerDependencies"):
                        got.update({k: str(v) for k, v in (pkg.get(key) or {}).items()})
                except (ValueError, AttributeError):
                    got = {}
                    out["unreadable"].append(f"{rel} is not valid JSON")
            elif low.startswith("requirements") and low.endswith(".txt"):
                got = _requirements(text, p, out["unreadable"], root)
            elif name == "pyproject.toml":
                if re.search(r"(?m)^\s*dynamic\s*=\s*\[[^\]]*[\"']dependencies[\"']", text):
                    out["unreadable"].append(f"{rel} declares its dependencies dynamically")
                got = _pyproject(text)
            elif name in ("setup.py", "setup.cfg", "Pipfile"):
                out["unreadable"].append(f"{rel} (its requirements are not read)")
                got = {}
            elif name == "pom.xml":
                got = _pom(text)
            if got is not None:
                out["files"].append(rel)
                out["deps"].update(got)
    return out


def _go_mod(text: str) -> dict[str, str]:
    deps: dict[str, str] = {}
    lines = []
    for blk in re.findall(r"(?ms)^\s*require\s*\((.*?)\)", text):
        lines += blk.splitlines()
    lines += re.findall(r"(?m)^\s*require\s+([^\s(].*)$", text)
    for ln in lines:
        m = re.match(r"\s*(\S+)\s+(\S+)", ln.split("//")[0])
        if m:
            deps[m.group(1)] = m.group(2)
    return deps


def _requirements(text: str, path: Optional[Path] = None, unreadable: Optional[list] = None,
                  root: Optional[Path] = None, seen: Optional[set] = None) -> dict[str, str]:
    deps: dict[str, str] = {}
    seen = seen if seen is not None else set()
    if path is not None:
        seen.add(path.resolve())
    where = (path.relative_to(root).as_posix() if path is not None and root is not None
             else "requirements")
    for ln in text.splitlines():
        ln = ln.split(" #")[0].strip() if not ln.lstrip().startswith("#") else ""
        if not ln:
            continue
        inc = re.match(r"(?:-r|--requirement)\s*=?\s*(\S+)", ln)
        if inc:
            target = (path.parent / inc.group(1)) if path is not None else None
            if target is None or not target.is_file():
                if unreadable is not None:
                    unreadable.append(f"{where} includes {inc.group(1)}, not in the tree")
            elif target.resolve() not in seen:
                deps.update(_requirements(target.read_text(encoding="utf-8", errors="ignore"),
                                          target, unreadable, root, seen))
            continue
        if re.match(r"(?:-e|--editable)\b", ln) or re.search(r"(?i)\b(?:git|hg|svn|bzr)\+|://", ln):
            if unreadable is not None:
                unreadable.append(f"{where}: {ln[:60]}")
            continue
        if ln.startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(?:[=<>!~]=?\s*([\w.*]+))?", ln)
        if m:
            deps[m.group(1).lower()] = m.group(2) or ""
    return deps


def _pyproject(text: str) -> dict[str, str]:
    deps = {}
    m = re.search(r"(?ms)^\[project\].*?^dependencies\s*=\s*\[(.*?)\]", text)
    if m:
        for s in re.findall(r"[\"']([^\"']+)[\"']", m.group(1)):
            d = _requirements(s)
            deps.update(d)
    m = re.search(r"(?ms)^\[tool\.poetry\.dependencies\](.*?)(?:^\[|\Z)", text)
    if m:
        for k, v in re.findall(r"(?m)^\s*([A-Za-z0-9_.\-]+)\s*=\s*[\"']?([^\"'\n{]*)", m.group(1)):
            if k.lower() != "python":
                deps[k.lower()] = v.strip()
    return deps


def _pom(text: str) -> dict[str, str]:
    deps = {}
    for blk in re.findall(r"(?s)<dependency>(.*?)</dependency>", text):
        if re.search(r"<scope>\s*test\s*</scope>", blk):
            continue
        g = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", blk)
        a = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", blk)
        v = re.search(r"<version>\s*([^<]+?)\s*</version>", blk)
        if a:
            deps[f"{g.group(1)}:{a.group(1)}" if g else a.group(1)] = v.group(1) if v else ""
    return deps


def _belongs(imported: str, dep: str) -> bool:
    """Whether an imported path or name is provided by the declared dependency `dep`."""
    i, d = imported.lower(), dep.lower()
    return i == d or i.startswith(d + "/") or i == d.split(":")[-1]


_JS_BUILTINS = frozenset("""assert async_hooks buffer child_process cluster console constants crypto
dgram diagnostics_channel dns domain events fs http http2 https inspector module net os path
perf_hooks process punycode querystring readline repl stream string_decoder sys timers tls
trace_events tty url util v8 vm wasi worker_threads zlib test""".split())


def third_party_imports(root: Any, found: Optional[dict] = None) -> list[str]:
    """What the CODE imports from outside the standard library and the project (non-test files):
      Go      an import whose first path element has a dot, outside the tree's own module paths;
      Python  a top-level `import x` / `from x import` not in the standard library and not a
              module or package of the tree (relative imports skipped);
      JS/TS   a `require('x')` / `import … from 'x'` of a bare name that is not a Node builtin.
    """
    import os
    import sys

    root = Path(root)
    own_go: list[str] = []
    for p in root.rglob("go.mod"):
        if not any(part in _SKIP for part in p.relative_to(root).parts):
            m = re.search(r"(?m)^\s*module\s+(\S+)", p.read_text(encoding="utf-8", errors="ignore"))
            if m:
                own_go.append(m.group(1))
    local_py = {p.stem for p in root.glob("*.py")} | {p.name for p in root.iterdir() if p.is_dir()}
    local_py |= {p.name for p in (root / "src").iterdir() if p.is_dir()} if (root / "src").is_dir() else set()
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names)
    out: list[str] = []
    test = re.compile(r"(?i)(^|/)(tests?|__tests__|testdata|spec)/|_test\.go$|(^|/)test_[^/]*\.py$|"
                      r"_test\.py$|\.(test|spec)\.[jt]sx?$")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        for name in filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if test.search(rel):
                continue
            ext = p.suffix.lower()
            if ext not in (".go", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
                continue
            try:
                code = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if ext == ".go":
                specs = [ln for blk in re.findall(r"(?ms)^\s*import\s*\((.*?)\)", code)
                         for ln in blk.splitlines()]
                specs += re.findall(r"(?m)^\s*import\s+((?:[\w.]+\s+)?\"[^\"]+\")", code)
                for spec in specs:
                    m = re.search(r"\"([^\"]+)\"", spec)
                    if not m:
                        continue
                    path = m.group(1)
                    if "." in path.split("/")[0] and not any(
                            path == o or path.startswith(o + "/") for o in own_go):
                        out.append(path)
            elif ext == ".py":
                for m in re.finditer(r"(?m)^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|"
                                     r"import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*))", code):
                    names = [m.group(1)] if m.group(1) else [x.strip() for x in m.group(2).split(",")]
                    for n in names:
                        top = n.split(".")[0]
                        if top and top not in stdlib and top not in local_py and top != "__future__":
                            out.append(top)
            else:
                for m in re.finditer(r"(?:\brequire\s*\(\s*|\bfrom\s+|^\s*import\s+)['\"]([^'\"]+)['\"]",
                                     code, re.M):
                    spec = m.group(1)
                    if spec.startswith((".", "/", "node:")):
                        continue
                    pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
                    if pkg not in _JS_BUILTINS:
                        out.append(pkg)
    return list(dict.fromkeys(out))


def _named(deps: dict[str, str], name: str) -> Optional[str]:
    """The declared dependency `name` refers to (exact, or its last path part)."""
    n = name.strip().lower()
    for d in deps:
        dl = d.lower()
        if dl == n or dl.rsplit("/", 1)[-1] == n or dl.split(":")[-1] == n:
            return d
    return None


def claim(item_text: Any, root: Any, *, assume: bool = False) -> Optional[Claim]:
    """The item's dependency claim settled against the tree's manifests, or None when the item
    is not about dependencies (a behaviour item, judged on code). `assume`: the caller already
    knows the text is a dependency claim (a part of a stack item, `stack_claim`)."""
    if root is None:
        return None
    found = manifests(root)
    deps: dict[str, str] = found["deps"]
    text = str(item_text or "")
    if not assume and not is_dependency_item(text, deps):
        return None
    files = ", ".join(found["files"][:3]) or "no manifest"
    if not found["files"]:
        return ("human", "no manifest in the tree to settle a dependency claim")
    if found["unreadable"] and not _LICENCE.search(text):
        return ("human", "the manifests cannot be fully read: "
                         + "; ".join(found["unreadable"][:3]))
    used = third_party_imports(root, found)
    declared = ", ".join(f"{d} {v}".strip() for d, v in sorted(deps.items())) or "nothing"
    problems: list[str] = []
    settled = False
    if _ONE_MODULE.search(text):
        settled = True
        if found["go_modules"] != 1:
            problems.append(f"{found['go_modules']} go.mod files, not one module")
    names = [t for t in _ticked(text) if not re.search(r"(?i)\.(?:mod|sum|json|txt|toml|xml)$", t)]
    if _SINGLE.search(text) or (_NO_OTHER.search(text) and names):
        settled = True
        if names:
            want = {(_named(deps, n) or n) for n in names}
            if set(deps) != want:
                problems.append(f"{files} declares {declared}; the item says exactly "
                                f"{', '.join(sorted(names))}")
            stray = [u for u in used if not any(_belongs(u, w) for w in want)]
            if stray:
                problems.append(f"the code imports {', '.join(stray[:3])}; the item says "
                                f"exactly {', '.join(sorted(names))}")
        elif len(deps) != 1:
            problems.append(f"{files} declares {declared}; the item says a single dependency")
    elif _NONE.search(text):
        settled = True
        if deps:
            problems.append(f"{files} requires {declared}; the item says standard library "
                            "only / no third-party dependency")
        if used:
            problems.append(f"the code imports {', '.join(used[:3])}; the item says standard "
                            "library only / no third-party dependency")
    for m in _PIN.finditer(text):
        settled = True
        version = m.group(1)
        target = next((_named(deps, n) for n in names if _named(deps, n)), None) or next(
            (d for d in deps if any(p and p in re.sub(r"`[^`]*`", " ", m.group(0)).lower()
                                    for p in re.split(r"[/.\-_@:]", d.lower()))), None)
        if target is None:
            problems.append(f"{files} declares no dependency the item pins")
        elif not re.sub(r"^[v^~=<>]+", "", deps[target]).startswith(version):
            problems.append(f"{target} is {deps[target] or 'unpinned'} in {files}, not "
                            f"{version}")
    if "pinned" in text.lower() and not _PIN.search(text):
        settled = True
        loose = [d for d, v in deps.items() if not re.match(r"^v?\d", v or "")]
        if loose:
            problems.append(f"not pinned to an exact version in {files}: {', '.join(loose[:3])}")
    if _LICENCE.search(text):
        return ("human", "a manifest does not say what licence a dependency has; a person "
                         "checks it")
    if problems:
        return ("false", "; ".join(problems))
    if not settled:
        return ("human", "a manifest cannot settle this claim about the dependencies")
    return ("met", f"{files} declares {declared}")


# ── a STACK item, part by part (Northgate r8b, item 6) ──────────────────────────────────────

_STACK_LINE = re.compile(r"(?i)^\s*stack\s*:")
#: Each language a Stack line names, by what in the tree shows it is that language.
_LANGUAGES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "go": (("go.mod",), (".go",)), "golang": (("go.mod",), (".go",)),
    "python": (("pyproject.toml", "requirements.txt", "setup.py"), (".py",)),
    "node": (("package.json",), (".js", ".mjs", ".ts")),
    "nodejs": (("package.json",), (".js", ".mjs", ".ts")),
    "javascript": (("package.json",), (".js", ".mjs", ".jsx")),
    "typescript": (("tsconfig.json", "package.json"), (".ts", ".tsx")),
    "java": (("pom.xml", "build.gradle", "build.gradle.kts"), (".java",)),
    "kotlin": (("build.gradle.kts", "build.gradle", "pom.xml"), (".kt",)),
    "rust": (("Cargo.toml",), (".rs",)), "ruby": (("Gemfile",), (".rb",)),
}
_STATIC = re.compile(r"(?i)\bstatic(?:ally)?(?:[\s\-]+linked)?\s+(?:binar(?:y|ies)|executables?|"
                     r"builds?)\b|\bstatically[\s\-]+linked\b")
_STACK_WORDS = frozenset("""static statically linked linking binary binaries executable executables
build builds built cgo cross compiled single one module modules both two three""".split())
_STATIC_OUTPUT = re.compile(r"(?i)not a dynamic executable|statically linked|static-pie linked")

def _walk_dirs(root: Path):
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        yield Path(dirpath), dirnames, filenames


def _get(cmd: Any, key: str, default: Any = None) -> Any:
    return cmd.get(key, default) if isinstance(cmd, dict) else getattr(cmd, key, default)


def _go_build_target(seg: str) -> tuple[Optional[str], Optional[str]]:
    """(the package a `go build` segment builds, its -o output) or (None, None)."""
    m = re.search(r"\bgo\s+build\b(.*)", seg)
    if not m:
        return None, None
    args = m.group(1).split()
    out, pkg, skip = None, None, False
    for k, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "-o" and k + 1 < len(args):
            out, skip = args[k + 1], True
        elif a.startswith("-o="):
            out = a[3:]
        elif a.startswith("-"):
            skip = a in ("-tags", "-ldflags", "-gcflags", "-mod", "-p", "-buildmode") and "=" not in a
        else:
            pkg = a
    return pkg, out


def _names_package(pkg: Optional[str], dirs: list[str]) -> Optional[str]:
    """The named package directory a build target (`./cmd/gwlint`) is, or None."""
    if not pkg:
        return None
    p = re.sub(r"^\./", "", pkg).rstrip("/")
    return next((d for d in dirs if p == d), None)


def _static_by_command(commands: Any, dirs: list[str]) -> set[str]:
    """The named packages a RECORDED command proves static: `ldd` / `file` of a binary named
    after one (saying static), or a successful `go build` with CGO_ENABLED=0 of one."""
    proven: set[str] = set()
    bins = {Path(d).name: d for d in dirs}
    for cmd in commands or ():
        argv = list(_get(cmd, "argv", None) or [])
        line = " ".join(argv) or str(_get(cmd, "cmd", "") or "")
        words = line.split()
        out = f"{_get(cmd, 'stdout', '') or ''}\n{_get(cmd, 'stderr', '') or ''}"
        tool = Path(words[0]).name if words else ""
        if tool in ("ldd", "file") and len(words) > 1:
            target = Path(words[-1]).name
            ok = (re.search(r"(?i)not a dynamic executable", out) if tool == "ldd"
                  else re.search(r"(?i)statically linked|static-pie linked", out))
            if target in bins and ok:
                proven.add(bins[target])
            continue
        for seg in re.split(r"&&|;|\|\|", line):
            pkg, o = _go_build_target(seg)
            hit = _names_package(pkg, dirs) or (bins.get(Path(o).name) if o else None)
            if hit and "CGO_ENABLED=0" in line and str(_get(cmd, "exit", "")) == "0":
                proven.add(hit)
    return proven


def _stages(text: str) -> list[dict[str, Any]]:
    """A Dockerfile's stages: {alias, base, lines} with continuation lines joined."""
    joined = re.sub(r"\\\s*\n", " ", text)
    stages: list[dict[str, Any]] = []
    for ln in joined.splitlines():
        m = re.match(r"(?i)\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", ln)
        if m:
            stages.append({"base": m.group(1), "alias": (m.group(2) or "").lower(),
                           "index": len(stages), "lines": []})
        elif stages:
            stages[-1]["lines"].append(ln)
    return stages


def _static_by_dockerfile(root: Path, dirs: list[str]) -> set[str]:
    """The named packages a Dockerfile builds static, by the build itself: its `go build
    ./cmd/<named>` runs with an explicit CGO_ENABLED=0 (on that command, or in that stage's
    ENV) in the stage the final stage copies the binary from; or the final stage is FROM
    scratch / distroless static and copies exactly that binary."""
    proven: set[str] = set()
    for here, _, files in _walk_dirs(root):
        for fname in files:
            low = fname.lower()
            if not (low.startswith("dockerfile") or low.endswith(".dockerfile")):
                continue
            stages = _stages((here / fname).read_text(encoding="utf-8", errors="ignore"))
            if len(stages) < 1:
                continue
            final = stages[-1]
            bare = re.match(r"(?i)(?:scratch|\S*distroless/static\S*)$", final["base"])
            copied: list[tuple[str, str]] = []          # (from stage, source path)
            for ln in final["lines"]:
                m = re.match(r"(?i)\s*COPY\s+(.*)$", ln)
                if not m:
                    continue
                flags = re.findall(r"--from=(\S+)", m.group(1))
                if flags:
                    parts = [w for w in m.group(1).split() if not w.startswith("--")]
                    for src in parts[:-1]:
                        copied.append((flags[0].lower(), src))
            for st in stages[:-1] if len(stages) > 1 else stages:
                env_off = any(re.match(r"(?i)\s*ENV\s+.*\bCGO_ENABLED[=\s]+0\b", ln)
                              for ln in st["lines"])
                for ln in st["lines"]:
                    m = re.match(r"(?i)\s*RUN\s+(.*)$", ln)
                    if not m:
                        continue
                    exported = False
                    for seg in re.split(r"&&|;|\|\|", m.group(1)):
                        if re.search(r"\bexport\s+CGO_ENABLED=0\b", seg):
                            exported = True
                        pkg, out = _go_build_target(seg)
                        named = _names_package(pkg, dirs)
                        if not named:
                            continue
                        name = (st["alias"] or str(st["index"]))
                        takes = [src for frm, src in copied if frm == name]
                        if out and out not in takes:
                            continue        # the final stage does not ship this build
                        if not out and not takes:
                            continue
                        if "CGO_ENABLED=0" in seg or exported or env_off:
                            proven.add(named)
                        elif bare and out and takes == [out]:
                            proven.add(named)
    return proven


def _packages(root: Path, mod: Path, named: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(named packages found exactly, names only near a package, names not found). A name
    matches a directory under the module whose path ends with it at a `/` (`gwlint` →
    cmd/gwlint) and that holds .go files; `gateway` beside cmd/northgate-gateway is only near."""
    dirs = []
    for here, _, files in _walk_dirs(mod):
        if here != mod and any(f.endswith(".go") for f in files):
            dirs.append(here.relative_to(root).as_posix())
    exact, near, absent = [], [], []
    for n in named:
        hit = next((d for d in dirs if d == n or d.endswith("/" + n)), None)
        if hit:
            exact.append(hit)
        elif any(n in re.split(r"[-_./]", Path(d).name) for d in dirs):
            near.append(n)
        else:
            absent.append(n)
    return exact, near, absent


def stack_claim(item_text: Any, root: Any, commands: Any = ()) -> Optional[Claim]:
    """A `Stack:` item settled part by part against the tree (Northgate r8b item 6: "Stack: Go.
    (gateway and gwlint: one Go module, static binaries)"):

      language        the tree has that language's manifest or code;
      one Go module   exactly one go.mod (a go.work using several is not one), and each package
                      the item names before the colon is a directory under it, matched exactly
                      at a `/` and holding .go files;
      static binaries a BUILD property of the NAMED binaries, proven only by a recorded,
                      verified `ldd` / `file` of one of them or `go build` with CGO_ENABLED=0 of
                      one of them, or by the Dockerfile that builds them: an explicit
                      CGO_ENABLED=0 in the stage the final stage copies the binary from, or a
                      final stage FROM scratch / distroless static copying exactly that binary;
      dependencies    "standard library only", "single dependency X", "pinned" (`claim`).
    Met only when every part is proven. A part disproved is PARTIAL ("false"); a part BOSS
    cannot prove (static binaries without such a check, a name that only resembles a package)
    needs a person ("human") — never met, never a major gap. None for a non-Stack item."""
    text = str(item_text or "")
    if root is None or not _STACK_LINE.match(text):
        return None
    root = Path(root)
    proven: list[str] = []
    wrong: list[str] = []
    unproven: list[str] = []
    m = re.match(r"(?i)^\s*stack\s*:\s*([^.(;]+)", text)
    langs = [w.lower() for w in re.split(r"[\s,/+]+|\band\b", m.group(1) if m else "") if w.strip()]
    all_files = [(here, files) for here, _, files in _walk_dirs(root)]
    names_in_tree = {f for _, files in all_files for f in files}
    exts = {Path(f).suffix.lower() for f in names_in_tree}
    for lang in langs:
        if lang not in _LANGUAGES:
            unproven.append(f"the stack {lang!r} cannot be checked")
            continue
        manifests_, suffixes = _LANGUAGES[lang]
        if any(mf in names_in_tree for mf in manifests_) or exts & set(suffixes):
            proven.append(f"{lang} ({next((mf for mf in manifests_ if mf in names_in_tree), 'code')})")
        else:
            wrong.append(f"no {lang} in the tree")
    label = re.search(r"\(\s*([^():]+?)\s*:", text)
    named = [n.strip().lower() for n in re.split(r",|\band\b|&", label.group(1))
             if n.strip()] if label else []
    dirs: list[str] = []
    if _ONE_MODULE.search(text):
        mods = [here for here, files in all_files if "go.mod" in files]
        work = [here / "go.work" for here, files in all_files if "go.work" in files]
        uses = sum(len(re.findall(r"(?m)^\s*(?:use\s+)?\.?/?[\w./\-]+\s*$",
                                  re.sub(r"(?s)^.*?use\s*\((.*?)\).*$", r"\1", w.read_text(
                                      encoding="utf-8", errors="ignore"))))
                   if re.search(r"use\s*\(", w.read_text(encoding="utf-8", errors="ignore"))
                   else len(re.findall(r"(?m)^\s*use\s+\S+", w.read_text(encoding="utf-8",
                                                                          errors="ignore")))
                   for w in work)
        if len(mods) != 1:
            wrong.append(f"{len(mods)} go.mod files, not one module")
        elif uses > 1:
            unproven.append(f"go.work uses {uses} modules, so one module is not shown")
        else:
            exact, near, absent = _packages(root, mods[0], named)
            dirs = exact
            if absent:
                wrong.append(f"no package for {', '.join(absent)} in the module")
            if near:
                unproven.append(f"no package named exactly {', '.join(near)} (only a similar "
                                "name); a person should confirm which it is")
            if not absent and not near:
                proven.append("one Go module" + (f" ({', '.join(exact)})" if exact else ""))
    if _STATIC.search(text):
        if not dirs and named:
            mods = [here for here, files in all_files if "go.mod" in files]
            dirs = _packages(root, mods[0], named)[0] if len(mods) == 1 else []
        targets = dirs or []
        shown = (_static_by_command(commands, targets) | _static_by_dockerfile(root, targets)
                 if targets else set())
        if targets and set(targets) <= shown:
            proven.append(f"static: {', '.join(targets)} built with cgo off")
        else:
            unproven.append("static binaries: not proven by a recorded check or an explicit "
                            "CGO_ENABLED=0 build; a person should confirm")
    if _NONE.search(text) or _SINGLE.search(text) or _PIN.search(text) or \
            re.search(r"(?i)\bpinned\b", text) or _LICENCE.search(text):
        dep = claim(text, root, assume=True)
        if dep and dep[0] == "false":
            wrong.append(dep[1])
        elif dep and dep[0] == "human":
            unproven.append(dep[1])
        elif dep:
            proven.append(dep[1])
    # Anything else the line says is a part BOSS cannot check: never passed over in silence.
    rest = re.sub(r"(?i)^\s*stack\s*:\s*[^.(;]+", " ", text)
    for rx in (_ONE_MODULE, _STATIC, _NONE, _SINGLE, _PIN, _NO_OTHER):
        rest = rx.sub(" ", rest)
    rest = re.sub(r"`[^`]*`", " ", rest)
    named_parts = {p for n in named for p in re.split(r"[-_./\s]+", n) if p}
    left = [w for w in (x.lower() for x in _WORD.findall(rest))
            if w not in _VOCAB and w not in _STACK_WORDS and w not in named_parts
            and w not in langs and not re.fullmatch(r"v?\d+", w)]
    if left:
        unproven.append(f"cannot check: {' '.join(dict.fromkeys(left))}")
    if wrong:
        return ("false", "; ".join(wrong + [f"not proven: {u}" for u in unproven]))
    if unproven:
        return ("human", "; ".join(unproven))
    return ("met", "; ".join(proven))


__all__ = ["claim", "is_dependency_item", "manifests", "stack_claim", "third_party_imports"]
