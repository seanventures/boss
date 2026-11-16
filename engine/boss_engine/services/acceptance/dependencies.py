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


def claim(item_text: Any, root: Any) -> Optional[Claim]:
    """The item's dependency claim settled against the tree's manifests, or None when the item
    is not about dependencies (a behaviour item, judged on code)."""
    if root is None:
        return None
    found = manifests(root)
    deps: dict[str, str] = found["deps"]
    text = str(item_text or "")
    if not is_dependency_item(text, deps):
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


__all__ = ["claim", "is_dependency_item", "manifests", "third_party_imports"]
