#!/usr/bin/env python3
"""THE FAULT KIT: each fault case of the functional test plan is one command (`boss qa fault`).

WHY. The plan's failure cases (ENT-REC-01..14, and the FEAT/CONC cases built on them) each began
with "Way 1": clone the project, make a branch, break the app by hand the way the case describes,
push, merge. A tester did that thirteen times, by hand, differently each time — and a fault that is
not the same twice cannot tell a regression from a different fault. Here each case is a
deterministic generator: the same app gets the same fault, on a branch named for the case, and
`--revert` puts it back.

    boss qa fault --list
    boss qa fault REC-01 --project Northgate [--app recipe-box]
    boss qa fault --revert REC-01 --project Northgate

WHAT IT DOES (`inject`): clones the project's LOCAL repository through BOSS's git remote (the
person's token reaches git through the credential helper, never an argument or a URL), finds the
app (`--app`, or the only one), detects its stack (Node, Python or Go), applies the case's fault,
commits it on `test/fault-<case>` with a `BOSS-Fault: <case>` trailer and pushes it — BOSS opens a
change for it — then prints what to do next: merge it on purpose, start the work the case names,
what should happen, and how to put it back. A stack the case has no generator for is refused with
the reason; nothing is pushed.

WHAT IT NEVER DOES. It never touches a project that ships to production: before anything is cloned,
BOSS's own production resolver (GET /v1/autonomy/resolve, the verdict every gate acts on) is asked
about this install, the project's default branch and every deployment target it has, and any
production verdict — or none at all — refuses. It never force-pushes, never pushes to the default
branch, never merges (the person does, on purpose), and only works on a local project.

THE GENERATORS are pure functions over an app directory (`apply(case, app_dir)`), so the test suite
applies the same faults the tester gets (engine/tests/test_qa_fault_kit.py) and the pipeline tests
build on them (engine/tests/test_recovery_faults_*.py). Stdlib only: this runs on the host.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

STACKS = ("node", "python", "go")
#: Markers of a stack the kit does not break (refused by name, never guessed at).
OTHER_STACKS = {"pom.xml": "Java (Maven)", "build.gradle": "Java (Gradle)",
                "build.gradle.kts": "Java (Gradle)", "Cargo.toml": "Rust", "Gemfile": "Ruby",
                "composer.json": "PHP", "mix.exs": "Elixir", "pubspec.yaml": "Dart"}
_MARKERS = {"node": ("package.json",),
            "python": ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
            "go": ("go.mod",)}
_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "coverage", "venv", ".venv",
              "__pycache__", "vendor", ".boss", "target", ".pytest_cache", ".mypy_cache"}
BRANCH_PREFIX = "test/fault-"
TRAILER = "BOSS-Fault"
#: A dependency the missing-dependency and lockfile cases add: the first the app does not have.
NODE_PACKAGES = (("dayjs", "^1.11.13"), ("ms", "^2.1.3"), ("slugify", "^1.6.6"))
PY_PACKAGES = (("pendulum", "pendulum", ">=3.0"), ("arrow", "arrow", ">=1.3"),
               ("humanize", "humanize", ">=4.9"))
GO_MODULES = (("github.com/google/uuid", "v1.6.0"), ("github.com/dustin/go-humanize", "v1.0.1"))
WRONG_PORT = "5999"


class Unsupported(Exception):
    """This case cannot be applied to this app: the message says why, in words a tester reads."""


@dataclass
class Applied:
    case: str
    stack: str
    changed: list[str]
    what: str                       # one sentence: the fault, where
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    stacks: tuple[str, ...]
    then: str                       # what the tester starts once the fault is merged
    expect: str                     # the plan's "Pass if", short
    apply: Callable[["App"], Applied]


# ── the app ──────────────────────────────────────────────────────────────────────────────────

class App:
    """One app directory, its stack, and the files the generators read and write."""

    def __init__(self, root: Path, stack: Optional[str] = None):
        self.root = Path(root)
        self.stack = stack or detect_stack(self.root)
        self.changed: list[str] = []

    # -- files --------------------------------------------------------------------------------
    def files(self, *suffixes: str) -> list[str]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                rel = (Path(dirpath) / name).relative_to(self.root).as_posix()
                if not suffixes or rel.endswith(suffixes):
                    out.append(rel)
        # Shallow first: the app's own top-level files before deep helpers.
        return sorted(out, key=lambda r: (r.count("/"), r))

    def has(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8", errors="replace")

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if rel not in self.changed:
            self.changed.append(rel)

    def rename(self, rel: str, to: str) -> None:
        (self.root / to).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).rename(self.root / to)
        for r in (rel, to):
            if r not in self.changed:
                self.changed.append(r)

    def done(self, case: str, what: str, *notes: str) -> Applied:
        return Applied(case, self.stack, list(self.changed), what, [n for n in notes if n])

    # -- node ---------------------------------------------------------------------------------
    def package(self) -> dict:
        try:
            return json.loads(self.read("package.json"))
        except (OSError, ValueError) as exc:
            raise Unsupported(f"package.json cannot be read as JSON ({exc})") from exc

    def write_package(self, data: dict) -> None:
        self.write("package.json", json.dumps(data, indent=2) + "\n")

    def node_deps(self) -> set[str]:
        pkg = self.package()
        return {n for k in ("dependencies", "devDependencies", "peerDependencies",
                            "optionalDependencies") for n in (pkg.get(k) or {})}

    def is_esm(self, rel: str) -> bool:
        if rel.endswith((".mjs", ".ts", ".tsx", ".mts")):
            return True
        if rel.endswith(".cjs"):
            return False
        text = self.read(rel)
        if re.search(r"(?m)^\s*(import\s.+\sfrom\s|import\s+['\"]|export\s)", text):
            return True
        if re.search(r"\brequire\(", text):
            return False
        return self.package().get("type") == "module"

    # -- every stack --------------------------------------------------------------------------
    def tests(self) -> list[str]:
        if self.stack == "node":
            return [r for r in self.files(".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
                    if _is_test(r)]
        if self.stack == "python":
            return [r for r in self.files(".py")
                    if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", r)]
        return self.files("_test.go")

    def sources(self) -> list[str]:
        if self.stack == "node":
            return [r for r in self.files(".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
                    if not _is_test(r) and not r.endswith(".d.ts")
                    and not re.search(r"(^|/)[^/]*\.config\.[a-z]+$|(^|/)\.", r)]
        if self.stack == "python":
            tests = set(self.tests())
            return [r for r in self.files(".py") if r not in tests
                    and not r.startswith(("tests/", "test/")) and not r.endswith("conftest.py")
                    and Path(r).name not in ("setup.py", "__init__.py")]
        return [r for r in self.files(".go") if not r.endswith("_test.go")]

    def declared_command(self) -> str:
        """The run command the app declares (boss.preview.command), else ""."""
        if self.has("boss.preview.command"):
            return self.read("boss.preview.command").strip()
        if self.stack == "node":
            scripts = self.package().get("scripts") or {}
            return str(scripts.get("start") or scripts.get("dev") or "")
        return ""

    def entry(self) -> str:
        """The file the app starts from: what its run command names, else the usual names."""
        command = self.declared_command()
        for token in re.split(r"[\s;&|]+", command):
            token = token.strip("'\"")
            if token and self.has(token) and token.endswith((".js", ".mjs", ".cjs", ".ts",
                                                            ".py")):
                return token
        if self.stack == "node":
            main = str(self.package().get("main") or "")
            if main and self.has(main):
                return main
            names = [f"{d}{n}.{e}" for d in ("", "src/", "server/") for n in
                     ("server", "index", "app", "main") for e in ("js", "mjs", "ts", "cjs")]
        elif self.stack == "python":
            m = re.search(r"-m\s+([\w.]+)", command)
            if m:
                for rel in (m.group(1).replace(".", "/") + ".py",
                            m.group(1).replace(".", "/") + "/__main__.py"):
                    if self.has(rel):
                        return rel
            for rel in self.sources():
                if re.search(r"serve_forever\(|app\.run\(|uvicorn\.run\(|Flask\(__name__\)|"
                             r"FastAPI\(", self.read(rel)):
                    return rel
            names = ["app.py", "main.py", "server.py", "wsgi.py"]
        else:
            for rel in self.sources():
                text = self.read(rel)
                if re.search(r"(?m)^package main\b", text) and re.search(r"(?m)^func main\(\)",
                                                                         text):
                    return rel
            names = []
        for rel in names:
            if self.has(rel):
                return rel
        raise Unsupported("cannot tell which file the app starts from: declare its run command "
                          "in boss.preview.command (or package.json scripts.start)")

    def go_package_dir(self) -> str:
        """The directory of the main package (where a new .go file joins it)."""
        return str(Path(self.entry()).parent).replace(".", "") or ""

    def go_file(self, name: str, body: str) -> str:
        rel = (Path(self.go_package_dir()) / name).as_posix().lstrip("/")
        self.write(rel, body)
        return rel


def _is_test(rel: str) -> bool:
    low = rel.lower()
    return (".test." in low or ".spec." in low or "/__tests__/" in f"/{low}"
            or low.startswith(("test/", "tests/")) or "/test/" in low or "/tests/" in low)


def detect_stack(root: Path) -> str:
    """node | python | go, from the app's own markers. Anything else is refused by name."""
    root = Path(root)
    found = [s for s in STACKS if any((root / m).is_file() for m in _MARKERS[s])]
    if found:
        return found[0]
    other = sorted({name for marker, name in OTHER_STACKS.items() if (root / marker).is_file()}
                   | ({".NET"} if any(root.glob("*.csproj")) else set()))
    if other:
        raise Unsupported(f"{', '.join(other)} is not a stack the fault kit breaks: it supports "
                          "Node (package.json), Python (pyproject.toml / requirements.txt) and Go "
                          "(go.mod). Cause this fault by hand (the plan's Way 1 or Way 2)")
    raise Unsupported(f"no Node, Python or Go app in {root.name or root}: looked for "
                      "package.json, pyproject.toml, requirements.txt, setup.py and go.mod")


def _after_imports(text: str, line: str, *, python: bool = False) -> str:
    """`line` inserted after the module's leading docstring, imports and `from __future__`."""
    lines = text.splitlines(keepends=True)
    at = 0
    in_doc = False
    for i, raw in enumerate(lines):
        s = raw.strip()
        if python and i == at and not in_doc and s.startswith(('"""', "'''")):
            quote = s[:3]
            if s.count(quote) >= 2 and len(s) > 3:
                at = i + 1
                continue
            in_doc = True
            continue
        if in_doc:
            if s.endswith(('"""', "'''")):
                in_doc = False
                at = i + 1
            continue
        if (s.startswith(("import ", "from ")) or (not python and s.startswith(("'use strict'",
                                                                               '"use strict"')))
                or not s or s.startswith(("#", "//")) or (i == 0 and s.startswith("#!"))):
            if s.startswith(("import ", "from ", "'use strict'", '"use strict"')):
                at = i + 1
            continue
        break
    if not python:
        # A multi-line `import {\n a,\n b\n} from "x";` ends where its `from` is.
        while at < len(lines) and at > 0 and not re.search(r"from\s+['\"][^'\"]+['\"];?\s*$|"
                                                           r"^import\s+['\"]|;\s*$",
                                                           lines[at - 1].strip()):
            at += 1
    return "".join(lines[:at]) + line.rstrip("\n") + "\n" + "".join(lines[at:])


def _python_main_block(text: str) -> Optional[tuple[int, str]]:
    """(index of the first line inside `if __name__ == "__main__":`, its indent) or None."""
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        if re.match(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", raw):
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    return j, re.match(r"^(\s*)", lines[j]).group(1)
    return None


def _python_at_start(app: App, rel: str, statements: list[str]) -> None:
    """Statements that run when the app STARTS: first in its __main__ block, else at the top."""
    text = app.read(rel)
    block = _python_main_block(text)
    if block:
        at, indent = block
        lines = text.splitlines(keepends=True)
        lines[at:at] = [indent + s + "\n" for s in statements]
        app.write(rel, "".join(lines))
    else:
        app.write(rel, _after_imports(text, "\n".join(statements), python=True))


_GO_NOT_IN_TESTS = ('if strings.HasSuffix(os.Args[0], ".test") {\n\t\treturn // go test runs '
                    'the package\'s init too\n\t}\n')


def _go_start_file(app: App, name: str, why: str, body: str) -> str:
    """A file of the main package whose init runs `body` when the app starts (not under go test)."""
    pkg = re.search(r"(?m)^package\s+(\w+)", app.read(app.entry())).group(1)
    return app.go_file(name, (
        f"package {pkg}\n\n// {why}\n\nimport (\n\t\"os\"\n\t\"strings\"\n)\n\n"
        f"func init() {{\n\t{_GO_NOT_IN_TESTS}\t{body}\n}}\n"))


# ── the generators ───────────────────────────────────────────────────────────────────────────

_NODE_INVERT = ((r"\bassert\.strictEqual\(", "assert.notStrictEqual("),
                (r"\bassert\.deepStrictEqual\(", "assert.notDeepStrictEqual("),
                (r"\bassert\.deepEqual\(", "assert.notDeepEqual("),
                (r"\bassert\.equal\(", "assert.notEqual("),
                (r"(?<!\.not)\.toStrictEqual\(", ".not.toStrictEqual("),
                (r"(?<!\.not)\.toEqual\(", ".not.toEqual("),
                (r"(?<!\.not)\.toBe\(", ".not.toBe("),
                (r"\.to\.equal\(", ".to.not.equal("),
                (r"\.to\.deep\.equal\(", ".to.not.deep.equal("))
_PY_INVERT = ((r"\bself\.assertEqual\(", "self.assertNotEqual("),
              (r"\bself\.assertTrue\(", "self.assertFalse("),
              (r"(?m)^(\s*assert\s+[^\n#]*?)\s==\s", r"\1 != "))


def broken_test(app: App) -> Applied:
    """ENT-REC-01: one assertion of an existing test is inverted, so that test fails."""
    tests = app.tests()
    if not tests:
        raise Unsupported("the app has no test file to break (REC-01 changes an existing test)")
    rules = _NODE_INVERT if app.stack == "node" else _PY_INVERT if app.stack == "python" else ()
    for rel in tests:
        text = app.read(rel)
        if app.stack == "go":
            lines = text.splitlines(keepends=True)
            # An `if` whose body fails the test; a comparison without nil first.
            hits = [i for i, ln in enumerate(lines) if re.match(r"\s*if .*(!=|==)", ln)
                    and any(re.search(r"\bt\.(Errorf|Fatalf|Error|Fatal)\(", x)
                            for x in lines[i + 1:i + 3])]
            hits.sort(key=lambda i: "nil" in lines[i])
            if hits:
                i = hits[0]
                op = "!=" if "!=" in lines[i] else "=="
                lines[i] = lines[i].replace(f" {op} ", " == " if op == "!=" else " != ", 1)
                app.write(rel, "".join(lines))
                return app.done("REC-01", f"inverted the check on line {i + 1} of {rel}, so "
                                          "that test now fails")
            continue
        for pattern, repl in rules:
            m = re.search(pattern, text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                app.write(rel, text[:m.start()] + re.sub(pattern, repl, text[m.start():], count=1))
                return app.done("REC-01", f"inverted the assertion on line {line} of {rel} "
                                          "(its expected value no longer holds), so that test "
                                          "now fails")
    raise Unsupported("found no assertion to invert in the app's tests "
                      f"({', '.join(tests[:3])})")


def type_error(app: App) -> Applied:
    """ENT-REC-02: a number assigned to a string field, where the app's type check sees it."""
    if app.stack == "node":
        ts = [r for r in app.sources() if r.endswith((".ts", ".tsx")) and not r.endswith(".d.ts")]
        if not (app.has("tsconfig.json") and ts):
            raise Unsupported("REC-02 needs a TypeScript app (tsconfig.json and .ts files); this "
                              "is plain JavaScript, which has no type check to fail")
        rel = ts[0]
        app.write(rel, app.read(rel).rstrip("\n") + "\n\n// BOSS fault REC-02: a number in a "
                  "string field (the typecheck must fail)\nconst bossFaultLabel: { name: string } "
                  "= { name: 42 };\nvoid bossFaultLabel;\n")
        return app.done("REC-02", f"{rel} assigns the number 42 to a string field")
    if app.stack == "python":
        configured = (app.has("mypy.ini") or app.has("pyrightconfig.json")
                      or (app.has("pyproject.toml") and re.search(r"\[tool\.(mypy|pyright)\]",
                                                                  app.read("pyproject.toml")))
                      or (app.has("setup.cfg") and "[mypy" in app.read("setup.cfg")))
        if not configured:
            raise Unsupported("REC-02 needs a type checker the app runs (mypy or pyright "
                              "configured); this Python app has none, so a type error would "
                              "pass unnoticed")
        rel = app.sources()[0]
        app.write(rel, app.read(rel).rstrip("\n") + "\n\n# BOSS fault REC-02: a number in a "
                  "string field (the type check must fail)\nboss_fault_label: str = 42\n")
        return app.done("REC-02", f"{rel} assigns the number 42 to a str variable")
    pkg = re.search(r"(?m)^package\s+(\w+)", app.read(app.entry())).group(1)
    rel = app.go_file("boss_fault_rec02.go", f"package {pkg}\n\n// BOSS fault REC-02: a number "
                      "in a string field (the build must fail).\nvar bossFaultLabel string = 42\n")
    return app.done("REC-02", f"{rel} assigns the number 42 to a string variable")


def lint_error(app: App) -> Applied:
    """ENT-REC-03: an unused variable (import) the app's own lint config rejects."""
    if app.stack == "node":
        pkg = app.package()
        configured = (any(app.has(n) for n in ("eslint.config.js", "eslint.config.mjs",
                                               "eslint.config.cjs", "eslint.config.ts",
                                               ".eslintrc", ".eslintrc.js", ".eslintrc.cjs",
                                               ".eslintrc.json", ".eslintrc.yml"))
                      or "eslintConfig" in pkg)
        if not configured:
            raise Unsupported("REC-03 needs a lint config the app runs (ESLint); this app has "
                              "none, so a lint error would pass unnoticed")
        rel = app.entry() if app.entry() in app.sources() else app.sources()[0]
        app.write(rel, app.read(rel).rstrip("\n") + "\n\n// BOSS fault REC-03: an unused "
                  "variable (no-unused-vars)\nconst bossFaultUnused = 1;\n")
        return app.done("REC-03", f"{rel} declares bossFaultUnused and never uses it "
                                  "(no-unused-vars)")
    if app.stack == "python":
        cfg = "\n".join(app.read(n) for n in ("pyproject.toml", "setup.cfg", "tox.ini")
                        if app.has(n))
        configured = (app.has("ruff.toml") or app.has(".ruff.toml") or app.has(".flake8")
                      or re.search(r"\[tool\.(ruff|flake8|pylint)|\[flake8\]", cfg))
        if not configured:
            raise Unsupported("REC-03 needs a lint config the app runs (ruff, flake8 or "
                              "pylint); this Python app has none")
        rel = app.sources()[0]
        app.write(rel, _after_imports(app.read(rel), "import os as _boss_fault_unused  # BOSS "
                                      "fault REC-03: an unused import (F401)", python=True))
        return app.done("REC-03", f"{rel} imports os as _boss_fault_unused and never uses it "
                                  "(F401)")
    pkg = re.search(r"(?m)^package\s+(\w+)", app.read(app.entry())).group(1)
    rel = app.go_file("boss_fault_rec03.go", (
        f"package {pkg}\n\nimport \"fmt\"\n\n// BOSS fault REC-03: go vet rejects a format verb "
        "that does not match its argument.\nfunc bossFaultLint() string { return "
        "fmt.Sprintf(\"%d\", \"text\") }\n"))
    return app.done("REC-03", f"{rel} formats a string with %d (go vet: printf)")


def _imported_by_tests(app: App) -> Optional[str]:
    """The first source module a test imports (so a broken import fails the tests too)."""
    sources = set(app.sources())
    for t in app.tests():
        text = app.read(t)
        if app.stack == "node":
            for spec in re.findall(r"(?:from\s+|require\(\s*|import\(\s*)['\"](\.[^'\"]+)['\"]",
                                   text):
                base = (Path(t).parent / spec).as_posix()
                base = os.path.normpath(base)
                for cand in (base, *(f"{base}{e}" for e in (".js", ".ts", ".mjs", ".tsx")),
                             f"{base}/index.js", f"{base}/index.ts"):
                    if cand in sources:
                        return cand
        elif app.stack == "python":
            for mod in re.findall(r"(?m)^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
                                  text):
                name = (mod[0] or mod[1]).replace(".", "/")
                for cand in (f"{name}.py", f"{name}/__init__.py", f"src/{name}.py"):
                    if cand in sources:
                        return cand
    return None


def missing_dependency(app: App) -> Applied:
    """ENT-REC-04: a package imported that the manifest does not list."""
    if app.stack == "node":
        have = app.node_deps()
        name = next((n for n, _ in NODE_PACKAGES if n not in have), None)
        if name is None:
            raise Unsupported("the app already depends on every package the kit would import")
        rel = _imported_by_tests(app) or app.entry()
        line = (f'import bossFaultDep from "{name}"; // BOSS fault REC-04: not in package.json'
                if app.is_esm(rel) else
                f'const bossFaultDep = require("{name}"); // BOSS fault REC-04: not in '
                'package.json')
        text = _after_imports(app.read(rel), line)
        app.write(rel, text.rstrip("\n") + "\nvoid bossFaultDep;\n")
        return app.done("REC-04", f"{rel} imports {name}, which package.json does not list")
    if app.stack == "python":
        listed = "\n".join(app.read(n) for n in ("requirements.txt", "pyproject.toml",
                                                 "setup.py", "Pipfile") if app.has(n)).lower()
        pick = next(((mod, dist) for mod, dist, _ in PY_PACKAGES if dist.lower() not in listed),
                    None)
        if pick is None:
            raise Unsupported("the app already depends on every package the kit would import")
        rel = _imported_by_tests(app) or app.entry()
        app.write(rel, _after_imports(app.read(rel), f"import {pick[0]}  # noqa: F401  BOSS fault "
                                      f"REC-04: {pick[1]} is not in the app's requirements",
                                      python=True))
        return app.done("REC-04", f"{rel} imports {pick[0]} ({pick[1]}), which the app's "
                                  "requirements do not list")
    mods = app.read("go.mod") if app.has("go.mod") else ""
    mod = next((m for m, _ in GO_MODULES if m not in mods), None)
    if mod is None:
        raise Unsupported("go.mod already requires every module the kit would import")
    pkg = re.search(r"(?m)^package\s+(\w+)", app.read(app.entry())).group(1)
    rel = app.go_file("boss_fault_rec04.go", f"package {pkg}\n\n// BOSS fault REC-04: a module "
                      f"go.mod does not require.\nimport _ \"{mod}\"\n")
    return app.done("REC-04", f"{rel} imports {mod}, which go.mod does not require")


def lockfile_drift(app: App) -> Applied:
    """ENT-REC-05: the manifest names a dependency its lockfile does not have."""
    if app.stack == "node":
        if not app.has("package-lock.json"):
            others = [n for n in ("pnpm-lock.yaml", "yarn.lock") if app.has(n)]
            raise Unsupported("REC-05 needs a committed package-lock.json"
                              + (f" (this app has {others[0]}, which BOSS's npm install does "
                                 "not read)" if others else " (this app has no lockfile)"))
        pkg = app.package()
        have = app.node_deps()
        pick = next(((n, v) for n, v in NODE_PACKAGES if n not in have), None)
        if pick is None:
            raise Unsupported("the app already depends on every package the kit would add")
        pkg.setdefault("dependencies", {})[pick[0]] = pick[1]
        app.write_package(pkg)
        return app.done("REC-05", f"package.json now asks for {pick[0]}@{pick[1]}; "
                                  "package-lock.json was not updated (npm ci: not in sync)")
    if app.stack == "python":
        listed = (app.read("pyproject.toml") if app.has("pyproject.toml") else "").lower()
        pick = next(((d, v) for _, d, v in PY_PACKAGES if d.lower() not in listed), None)
        if app.has("poetry.lock") and pick:
            text = app.read("pyproject.toml")
            m = re.search(r"(?m)^\[tool\.poetry\.dependencies\]\s*$", text)
            if m:
                app.write("pyproject.toml", text[:m.end()] + f"\n{pick[0]} = \"{pick[1]}\"" +
                          text[m.end():])
                return app.done("REC-05", f"pyproject.toml now asks for {pick[0]}; poetry.lock "
                                          "was not updated")
        if app.has("uv.lock") and pick:
            text = app.read("pyproject.toml")
            m = re.search(r"(?m)^dependencies\s*=\s*\[", text)
            if m:
                app.write("pyproject.toml", text[:m.end()] + f"\n    \"{pick[0]}{pick[1]}\"," +
                          text[m.end():])
                return app.done("REC-05", f"pyproject.toml now asks for {pick[0]}; uv.lock was "
                                          "not updated")
        if app.has("requirements.in") and app.has("requirements.txt") and pick:
            app.write("requirements.in", app.read("requirements.in").rstrip("\n")
                      + f"\n{pick[0]}{pick[1]}\n")
            return app.done("REC-05", f"requirements.in now asks for {pick[0]}; the compiled "
                                      "requirements.txt was not updated")
        raise Unsupported("REC-05 needs a Python lockfile (poetry.lock, uv.lock, or "
                          "requirements.in compiled to requirements.txt); this app has none")
    text = app.read("go.mod")
    pick = next(((m, v) for m, v in GO_MODULES if m not in text), None)
    if pick is None:
        raise Unsupported("go.mod already requires every module the kit would add")
    app.write("go.mod", text.rstrip("\n") + f"\n\nrequire {pick[0]} {pick[1]} // BOSS fault "
                                            "REC-05: no go.sum entry\n")
    # Go loads only what the code imports: the module is used, so its missing sum is a failure.
    pkg = re.search(r"(?m)^package\s+(\w+)", app.read(app.entry())).group(1)
    app.go_file("boss_fault_rec05.go", f"package {pkg}\n\n// BOSS fault REC-05: go.mod requires "
                f"this module; go.sum has no entry for it.\nimport _ \"{pick[0]}\"\n")
    return app.done("REC-05", f"go.mod now requires {pick[0]} {pick[1]} and the app imports it; "
                              "go.sum was not updated")


def wrong_node(app: App) -> Applied:
    """ENT-REC-06: package.json declares a Node no box has, and npm is told to enforce it."""
    if app.stack != "node":
        raise Unsupported(f"REC-06 is the Node case; this is a {app.stack} app "
                          "(REC-07 is the Python one)")
    pkg = app.package()
    pkg.setdefault("engines", {})["node"] = ">=99"
    app.write_package(pkg)
    npmrc = app.read(".npmrc") if app.has(".npmrc") else ""
    if not re.search(r"(?m)^engine-strict\s*=\s*true", npmrc):
        app.write(".npmrc", (npmrc.rstrip("\n") + "\n" if npmrc else "") + "engine-strict=true\n")
    return app.done("REC-06", 'package.json declares "engines": {"node": ">=99"} and .npmrc '
                              "sets engine-strict=true")


def wrong_python(app: App) -> Applied:
    """ENT-REC-07: pyproject.toml requires a Python no box has."""
    if app.stack != "python":
        raise Unsupported(f"REC-07 is the Python case; this is a {app.stack} app"
                          + (" (REC-06 is the Node one)" if app.stack == "node" else ""))
    if not app.has("pyproject.toml"):
        raise Unsupported("REC-07 needs a pyproject.toml (it sets requires-python there); this "
                          "app has only requirements.txt")
    text = app.read("pyproject.toml")
    if re.search(r"(?m)^requires-python\s*=", text):
        text = re.sub(r"(?m)^requires-python\s*=.*$", 'requires-python = ">=3.99"', text, count=1)
    elif re.search(r"(?m)^\[project\]\s*$", text):
        text = re.sub(r"(?m)^\[project\]\s*$", '[project]\nrequires-python = ">=3.99"', text,
                      count=1)
    elif re.search(r"(?m)^python\s*=", text):
        text = re.sub(r"(?m)^python\s*=.*$", 'python = ">=3.99"', text, count=1)
    else:
        text = text.rstrip("\n") + '\n\n[project]\nrequires-python = ">=3.99"\n'
    app.write("pyproject.toml", text)
    return app.done("REC-07", 'pyproject.toml requires Python ">=3.99"')


def flaky_test(app: App) -> Applied:
    """ENT-REC-08: a new test, beside the app's own, that fails about half the time."""
    tests = app.tests()
    if not tests:
        raise Unsupported("the app has no tests to add a flaky one beside (REC-08)")
    first = tests[0]
    folder = Path(first).parent
    if app.stack == "node":
        text = app.read(first)
        ext = Path(first).suffix
        name = (folder / f"boss-fault-flaky.test{ext}").as_posix()
        esm = app.is_esm(first)
        if "vitest" in text:
            body = ('import { test, expect } from "vitest";\n\n'
                    'test("BOSS fault REC-08: flaky", () => {\n'
                    "  expect(Math.random()).toBeLessThan(0.5);\n});\n")
        elif "node:test" in text:
            body = (('import { test } from "node:test";\nimport assert from "node:assert";\n'
                     if esm else 'const { test } = require("node:test");\n'
                     'const assert = require("node:assert");\n')
                    + '\ntest("BOSS fault REC-08: flaky", () => {\n'
                      "  assert.ok(Math.random() < 0.5);\n});\n")
        elif re.search(r"\bexpect\(", text):
            body = ('test("BOSS fault REC-08: flaky", () => {\n'
                    "  expect(Math.random()).toBeLessThan(0.5);\n});\n")
        else:
            body = (('import assert from "node:assert";\n' if esm else
                     'const assert = require("node:assert");\n')
                    + '\nit("BOSS fault REC-08: flaky", () => {\n'
                      "  assert.ok(Math.random() < 0.5);\n});\n")
        app.write(name, body)
    elif app.stack == "python":
        name = (folder / "test_boss_fault_flaky.py").as_posix()
        app.write(name, "import random\nimport unittest\n\n\nclass BossFaultFlakyTest("
                        "unittest.TestCase):\n    def test_flaky(self):\n        \"\"\"BOSS "
                        "fault REC-08: fails about half the time.\"\"\"\n        self.assertLess("
                        "random.random(), 0.5)\n")
    else:
        pkg = re.search(r"(?m)^package\s+(\w+)", app.read(first)).group(1)
        name = (folder / "boss_fault_flaky_test.go").as_posix().lstrip("./")
        app.write(name, f"package {pkg}\n\nimport (\n\t\"math/rand\"\n\t\"testing\"\n)\n\n"
                        "// BOSS fault REC-08: fails about half the time.\n"
                        "func TestBossFaultFlaky(t *testing.T) {\n\tif rand.Float64() >= 0.5 "
                        "{\n\t\tt.Fatal(\"flaky\")\n\t}\n}\n")
    return app.done("REC-08", f"{name} fails about half the time (a random draw)")


def crash_on_start(app: App) -> Applied:
    """ENT-REC-09: the app throws as it starts."""
    words = "boom: BOSS fault REC-09, the app crashes on start"
    if app.stack == "node":
        command = app.declared_command()
        if re.search(r"\b(vite|next)\b", command):
            cfg = next((c for c in app.files(".js", ".mjs", ".ts", ".cjs")
                        if re.match(r"(vite|next)\.config\.", Path(c).name)), None)
            if cfg is None:
                raise Unsupported("the app starts with vite/next and has no config file to "
                                  "make it crash on start")
            rel = cfg
        else:
            rel = app.entry()
        app.write(rel, _after_imports(app.read(rel), f'throw new Error("{words}");'))
        return app.done("REC-09", f"{rel} throws on start ({words})")
    if app.stack == "python":
        rel = app.entry()
        _python_at_start(app, rel, [f'raise RuntimeError("{words}")'])
        return app.done("REC-09", f"{rel} raises on start ({words})")
    rel = _go_start_file(app, "boss_fault_rec09.go", "BOSS fault REC-09: the app crashes on "
                         "start.", f'panic("{words}")')
    return app.done("REC-09", f"{rel} panics on start ({words})")


def wrong_port(app: App) -> Applied:
    """ENT-REC-10: the server listens on a fixed port, not the $PORT it is given."""
    patterns = {"node": (r"process\.env\.PORT\b", r"process\.env\[['\"]PORT['\"]\]"),
                "python": (r"os\.environ\.get\(\s*['\"]PORT['\"][^)]*\)",
                           r"os\.getenv\(\s*['\"]PORT['\"][^)]*\)",
                           r"os\.environ\[['\"]PORT['\"]\]"),
                "go": (r"os\.Getenv\(\"PORT\"\)",)}[app.stack]
    hit = []
    for rel in app.sources():
        text = app.read(rel)
        new = text
        for p in patterns:
            new = re.sub(p, f'"{WRONG_PORT}"', new)
        if new != text:
            if app.stack == "go" and "os." not in new.replace('"os"', ""):
                new = new.rstrip("\n") + "\n\nvar _ = os.Getenv // BOSS fault REC-10 keeps the " \
                                         "os import\n"
            app.write(rel, new)
            hit.append(rel)
    command = app.declared_command()
    if re.search(r"\$\{?PORT\}?", command):
        new = re.sub(r"\$\{?PORT\}?", WRONG_PORT, command)
        if app.has("boss.preview.command"):
            app.write("boss.preview.command", new + "\n")
        else:
            pkg = app.package()
            for key in ("start", "dev"):
                if (pkg.get("scripts") or {}).get(key) == command:
                    pkg["scripts"][key] = new
            app.write_package(pkg)
        hit.append("the run command")
    if not hit:
        raise Unsupported("found no place the app reads PORT (process.env.PORT, os.environ "
                          "PORT, os.Getenv(\"PORT\") or $PORT in its run command)")
    return app.done("REC-10", f"the app listens on {WRONG_PORT} whatever $PORT says "
                              f"({', '.join(hit)})")


def no_home_route(app: App) -> Applied:
    """ENT-REC-11: the home route is gone, so GET / answers 404."""
    moved = "/__boss_fault_home"
    route = {"node": r"(\.(?:get|all|route)\(\s*)(['\"`])/\2",
             "python": r"(@\w+\.(?:route|get)\(\s*)(['\"])/\2",
             "go": r"((?:HandleFunc|Handle|GET|Get)\(\s*)\"((?:GET )?)/(?:\{\$\})?\""}[app.stack]
    for rel in app.sources():
        text = app.read(rel)
        m = re.search(route, text)
        if m:
            if app.stack == "go":
                new = text[:m.start()] + f'{m.group(1)}"{m.group(2)}{moved}"' + text[m.end():]
            else:
                new = text[:m.start()] + f"{m.group(1)}{m.group(2)}{moved}{m.group(2)}" + \
                    text[m.end():]
            app.write(rel, new)
            line = text.count("\n", 0, m.start()) + 1
            return app.done("REC-11", f"the home route on line {line} of {rel} moved to {moved}, "
                                      "so GET / answers 404")
    if app.stack == "node":
        for rel in app.sources():
            text = app.read(rel)
            m = re.search(r"createServer\(\s*(?:async\s+)?(?:function\s*)?\(\s*(\w+)\s*,\s*(\w+)"
                          r"[^)]*\)\s*(?:=>\s*)?\{", text)
            if m:
                req, res = m.group(1), m.group(2)
                guard = (f'\n  if (({req}.url || "/").split("?")[0] === "/") {{ // BOSS fault '
                         f'REC-11\n    {res}.statusCode = 404;\n    return {res}.end("Not '
                         'found");\n  }')
                app.write(rel, text[:m.end()] + guard + text[m.end():])
                return app.done("REC-11", f"the request handler in {rel} answers 404 at /")
        for page in ("app/page.tsx", "app/page.jsx", "app/page.js", "pages/index.tsx",
                     "pages/index.jsx", "pages/index.js", "src/app/page.tsx", "src/pages/index.tsx"):
            if app.has(page):
                to = page.replace("app/page", "app/boss-fault-home/page").replace(
                    "pages/index", "pages/boss-fault-home")
                app.rename(page, to)
                return app.done("REC-11", f"the home page {page} moved to {to}, so / is a 404")
    if app.stack == "python":
        for rel in app.sources():
            text = app.read(rel)
            m = re.search(r"(?m)^(\s*)def do_GET\(self\):\s*$", text)
            if m:
                ind = m.group(1) + "    "
                guard = (f"\n{ind}if self.path.split(\"?\")[0] == \"/\":  # BOSS fault REC-11\n"
                         f"{ind}    self.send_error(404)\n{ind}    return")
                app.write(rel, text[:m.end()] + guard + text[m.end():])
                return app.done("REC-11", f"do_GET in {rel} answers 404 at /")
            m = re.search(r"\bpath\(\s*(['\"])\1\s*,", text)
            if m and Path(rel).name == "urls.py":
                app.write(rel, text[:m.start()] + 'path("boss-fault-home/",' + text[m.end():])
                return app.done("REC-11", f"the empty route in {rel} moved to boss-fault-home/")
    raise Unsupported("found no home route to remove (an Express/Fastify/Flask/FastAPI/Go "
                      "route for /, a Node or Python request handler, or a Next.js home page)")


def _needs_variable(app: App, name: str, case: str, why: str) -> list[str]:
    """Code that refuses to start without `name`, placed where the app starts."""
    words = f"{name} is not set: {why} (BOSS fault {case})"
    if app.stack == "node":
        rel = app.entry()
        app.write(rel, _after_imports(app.read(rel), f'if (!process.env.{name}) {{ throw new '
                                                     f'Error("{words}"); }}'))
        return [rel]
    if app.stack == "python":
        rel = app.entry()
        text = app.read(rel)
        if not re.search(r"(?m)^import os\b", text):
            app.write(rel, _after_imports(text, "import os", python=True))
        _python_at_start(app, rel, [f'if not os.environ.get("{name}"):',
                                    f'    raise SystemExit("{words}")'])
        return [rel]
    rel = _go_start_file(app, f"boss_fault_{case.lower().replace('-', '')}.go",
                         f"BOSS fault {case}: the app needs {name} at start.",
                         f'if os.Getenv("{name}") == "" {{\n\t\tpanic("{words}")\n\t}}')
    return [rel]


def missing_env(app: App) -> Applied:
    """ENT-REC-12: the app needs PAGE_SIZE at start, with no default (BOSS can supply one)."""
    where = _needs_variable(app, "PAGE_SIZE", "REC-12", "the app needs PAGE_SIZE at start")
    return app.done("REC-12", f"{where[0]} refuses to start without PAGE_SIZE (no default)")


def missing_secret(app: App) -> Applied:
    """ENT-REC-13: the app refuses to start without PAYMENTS_API_KEY, declared as a secret."""
    where = _needs_variable(app, "PAYMENTS_API_KEY", "REC-13",
                            "the payments provider's key, which only a person has")
    example = app.read(".env.example") if app.has(".env.example") else ""
    app.write(".env.example", (example.rstrip("\n") + "\n" if example else "")
              + "# Required secret: the payments provider's API key. A person sets it in the "
                "app's Secrets.\nPAYMENTS_API_KEY=\n")
    notes = []
    if app.has("boss.deploy.yaml"):
        text = app.read("boss.deploy.yaml")
        m = re.search(r"(?m)^env:\s*$", text)
        if m:
            app.write("boss.deploy.yaml", text[:m.end()] + "\n  PAYMENTS_API_KEY: {secret: "
                      "payments-api-key}" + text[m.end():])
        else:
            app.write("boss.deploy.yaml", text.rstrip("\n") + "\nenv:\n  PAYMENTS_API_KEY: "
                                                              "{secret: payments-api-key}\n")
    notes.append("An app's own files can declare a secret only as .env.example (read as "
                 "optional) or boss.deploy.yaml {secret} (read as the deploy's): a secret a "
                 "preview must wait for is today declared only by an applied template. Record "
                 "whether BOSS asks a person or sends it to a repair round.")
    return app.done("REC-13", f"{where[0]} refuses to start without PAYMENTS_API_KEY; "
                              ".env.example declares it as a required secret", *notes)


def needs_migration(app: App) -> Applied:
    """ENT-REC-14: the code reads a table at start that no migration creates."""
    table = "boss_fault_widgets"
    if app.stack == "node":
        for rel in app.sources():
            text = app.read(rel)
            m = re.search(r"(?m)^(\s*)(?:const|let|var)\s+(\w+)\s*=\s*new\s+(?:\w+\.)?Database\("
                          r"[^\n]*$", text)
            if m:
                line = (f"{m.group(1)}{m.group(2)}.prepare(\"SELECT count(*) AS n FROM {table}\")"
                        ".get(); // BOSS fault REC-14: no migration creates this table")
                app.write(rel, text[:m.end()] + "\n" + line + text[m.end():])
                return app.done("REC-14", f"{rel} reads the table {table} at start; no "
                                          "migration creates it")
    if app.stack == "python":
        for rel in app.sources():
            text = app.read(rel)
            m = re.search(r"(?m)^(\s*)(\w+)\s*=\s*sqlite3\.connect\([^\n]*$", text)
            if m:
                line = (f"{m.group(1)}{m.group(2)}.execute(\"SELECT count(*) FROM {table}\")  "
                        "# BOSS fault REC-14: no migration creates this table")
                app.write(rel, text[:m.end()] + "\n" + line + text[m.end():])
                return app.done("REC-14", f"{rel} reads the table {table} where it opens its "
                                          "database; no migration creates it")
    if app.stack == "go":
        raise Unsupported("ENT-REC-14 covers Node and Python apps that open SQLite; this is a "
                          "Go app")
    raise Unsupported("REC-14 needs an app that opens a SQLite database in its code "
                      "(better-sqlite3 / node:sqlite `new Database(...)`, or Python "
                      "`sqlite3.connect(...)`); other databases and ORMs need the fault by hand")


START_NEW_WORK = "Merge it on purpose, then start New work on that app: \"Add a recipe counter\""
START_PREVIEW = "Merge it on purpose, then press Start preview on that app"
CASES: dict[str, Case] = {c.id: c for c in (
    Case("REC-01", "A broken existing test", STACKS, START_NEW_WORK,
         "BOSS fixes the test or the code and checks pass within 30 min, or opens a draft change "
         "marked checks failing, naming the test, with a follow-up — never a loop", broken_test),
    Case("REC-02", "A type error", STACKS, START_NEW_WORK.replace("\"Add a recipe counter\"",
                                                                  "a small change"),
         "the typecheck fails, BOSS fixes it and checks pass within 30 min; else the message "
         "names the file and the error", type_error),
    Case("REC-03", "A lint error", STACKS, START_NEW_WORK,
         "fixed within 30 min by changing the code, never by turning the rule off; else the "
         "message names the rule and the file", lint_error),
    Case("REC-04", "A missing dependency", STACKS, START_NEW_WORK,
         "BOSS adds the package through your registry, install and tests pass within 30 min; "
         "else the message names the package and the registry", missing_dependency),
    Case("REC-05", "Lockfile drift", STACKS, START_NEW_WORK,
         "BOSS updates the lockfile through your registry within 30 min, never deletes it or "
         "switches registry", lockfile_drift),
    Case("REC-06", "Wrong Node version declared", ("node",), START_NEW_WORK,
         "BOSS corrects the pin in a repair round, or stops at once with a card naming the "
         "missing version — never a test failure", wrong_node),
    Case("REC-07", "Wrong Python version declared", ("python",), START_NEW_WORK,
         "as REC-06", wrong_python),
    Case("REC-08", "A flaky test", STACKS, START_NEW_WORK,
         "BOSS says the test is flaky and fixes or stabilises it within 30 min; never passes by "
         "luck", flaky_test),
    Case("REC-09", "Crash on start", STACKS, START_PREVIEW,
         "BOSS sees \"the app exited (code N) before serving HTTP\" and starts a fix build; the "
         "fixed preview loads within 20 min", crash_on_start),
    Case("REC-10", "Wrong port", STACKS, START_PREVIEW,
         "BOSS reports the app \"did not answer HTTP\" and repairs the port within 20 min",
         wrong_port),
    Case("REC-11", "404 at /", STACKS, START_PREVIEW,
         "the preview is failed, not live; BOSS builds a fix and / loads within 20 min",
         no_home_route),
    Case("REC-12", "A missing environment variable BOSS can supply", STACKS, START_PREVIEW,
         "BOSS adds a safe default or declares the variable; the preview loads within 20 min",
         missing_env),
    Case("REC-13", "A missing secret only a person has", STACKS, START_PREVIEW,
         "BOSS invents no value and asks a person at once through a secret card with Store; no "
         "repair loop", missing_secret),
    Case("REC-14", "The app needs a database migration", ("node", "python"), START_PREVIEW,
         "BOSS adds or runs the migration and the preview loads within 30 min; else the message "
         "says a migration is missing", needs_migration),
)}


def normalise_case(raw: str) -> str:
    """REC-01, rec-1, ENT-REC-01 and 1 all name ENT-REC-01."""
    m = re.fullmatch(r"(?:ENT-)?(?:REC-?)?0*(\d{1,2})", str(raw or "").strip().upper())
    if not m:
        raise SystemExit(f"boss: {raw!r} is not a fault case. See: boss qa fault --list")
    case = f"REC-{int(m.group(1)):02d}"
    if case not in CASES:
        raise SystemExit(f"boss: ENT-{case} has no fault generator (the kit covers "
                         f"{', '.join(CASES)}). Machine-level faults (disk, containers, the VM) "
                         "use the commands in the plan; see also boss qa disk-pressure")
    return case


def apply(case: str, app_dir: Path, stack: Optional[str] = None) -> Applied:
    """Apply one case's fault to the app at `app_dir` (files only: no git). Raises Unsupported."""
    case = normalise_case(case)
    app = App(Path(app_dir), stack)
    # Each generator refuses a stack it does not cover itself, with the case that does.
    return CASES[case].apply(app)


# ── the app inside a repository ──────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def find_apps(repo: Path) -> list[str]:
    """Every app directory in the repository (relative, "." for the root): a Node, Python or Go
    marker at the root, one level down, or two (apps/x, services/x)."""
    out = []
    for depth_glob in ("", "*", "*/*"):
        for d in sorted(repo.glob(depth_glob)) if depth_glob else [repo]:
            if not d.is_dir() or any(p in _SKIP_DIRS or p.startswith(".")
                                     for p in d.relative_to(repo).parts):
                continue
            if any((d / m).is_file() for ms in _MARKERS.values() for m in ms):
                rel = d.relative_to(repo).as_posix() or "."
                # A root marker of a monorepo's tooling is not an app when apps sit below it.
                out.append(rel)
    if "." in out and len(out) > 1:
        out.remove(".")
    return out


def pick_app(repo: Path, wanted: Optional[str]) -> str:
    apps = find_apps(repo)
    if not apps:
        try:
            detect_stack(repo)
        except Unsupported as exc:
            raise Unsupported(str(exc)) from None
        return "."
    if wanted:
        w = _slug(wanted)
        hits = [a for a in apps if _slug(a) == w or _slug(Path(a).name) == w]
        if len(hits) == 1:
            return hits[0]
        raise Unsupported(f"no app {wanted!r} in this project; its apps are: "
                          + ", ".join(apps) + " (pass one with --app)")
    if len(apps) == 1:
        return apps[0]
    raise Unsupported("this project has several apps (" + ", ".join(apps) + "): name one with "
                      "--app")


# ── BOSS: the API, the git remote ────────────────────────────────────────────────────────────

class Boss:
    """The person's view of BOSS: the token read once from stdin, never printed or passed on."""

    def __init__(self, api: str, token: str):
        self.api = api.rstrip("/")
        self._token = token

    def get(self, path: str, **query: str) -> tuple[int, object]:
        url = self.api + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except ValueError:
                return exc.code, None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SystemExit(f"boss: could not reach BOSS at {self.api} ({exc}). Is BOSS "
                             "running?") from None


def _said(body: object) -> str:
    if isinstance(body, dict):
        d = body.get("detail")
        if isinstance(d, dict):
            return str(d.get("headline") or d.get("message") or d)
        return str(d or body.get("message") or body.get("error") or "")
    return ""


def resolve_project(boss: Boss, name: str) -> dict:
    code, body = boss.get("/v1/projects")
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"boss: BOSS refused the project list (HTTP {code}): {_said(body)}")
    items = body.get("items") or []
    want = str(name or "").strip()
    for key in ("id", "slug"):
        hit = [p for p in items if str(p.get(key) or "") == want]
        if hit:
            return hit[0]
    hit = [p for p in items if str(p.get("name") or "").strip().lower() == want.lower()]
    if len(hit) == 1:
        return hit[0]
    if len(hit) > 1:
        raise SystemExit(f"boss: several projects are named {name!r}: "
                         + ", ".join(p["id"] for p in hit) + ". Pass the id.")
    raise SystemExit(f"boss: no project {name!r} you can see. Your projects: "
                     + (", ".join(str(p.get("name")) for p in items[:12]) or "none"))


def production_refusal(boss: Boss, project: dict, local: dict) -> Optional[str]:
    """Why this project must not be touched, or None. Asked of BOSS's own resolver; no answer
    is a refusal (fail closed)."""
    pid = project["id"]

    def verdict(**query: str) -> tuple[bool, str]:
        code, body = boss.get("/v1/autonomy/resolve", project_id=pid, **query)
        if code != 200 or not isinstance(body, dict) or "verdict" not in body:
            raise SystemExit(f"boss: BOSS did not say whether {project.get('name')} is "
                             f"production (HTTP {code}: {_said(body) or 'no verdict'}), so the "
                             "fault kit will not touch it")
        return bool(body["verdict"].get("prod")), str(body.get("words") or "")

    prod, words = verdict(install="true")
    if prod:
        return f"this BOSS install is production ({words})"
    prod, words = verdict(repo=str(local.get("full_name") or ""),
                          base_branch=str(local.get("default_branch") or "main"))
    if prod:
        return f"a merge into {local.get('default_branch')} ships to production ({words})"
    code, body = boss.get("/v1/deployment-targets", project_id=pid, limit="200")
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"boss: BOSS did not list {project.get('name')}'s deployment targets "
                         f"(HTTP {code}: {_said(body)}), so the fault kit will not touch it")
    for target in body.get("items") or []:
        if str(target.get("status") or "") in ("deleted", "retired"):
            continue
        prod, words = verdict(target_id=str(target["id"]))
        if prod:
            return (f"its target {target.get('app_name') or target['id']} "
                    f"({target.get('environment')}) is production ({words})")
    return None


class Git:
    """git with the person's credential helper and no other configuration of theirs."""

    def __init__(self, helper: str, cwd: Optional[Path] = None):
        self.helper = helper
        self.cwd = cwd

    def __call__(self, *args: str, check: bool = True, quiet: bool = False
                 ) -> subprocess.CompletedProcess:
        cmd = ["git", "-c", "credential.helper=", "-c", f"credential.helper={self.helper}",
               "-c", "user.name=" + _git_identity("name"),
               "-c", "user.email=" + _git_identity("email"), *args]
        done = subprocess.run(cmd, cwd=self.cwd, text=True, capture_output=True,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if check and done.returncode != 0:
            raise SystemExit(f"boss: git {args[0]} failed: {(done.stderr or done.stdout).strip()}")
        if not quiet:
            for line in (done.stderr or "").splitlines():
                if line.startswith("remote:"):
                    print(line)
        return done


def _git_identity(key: str) -> str:
    got = subprocess.run(["git", "config", "--get", f"user.{key}"], text=True,
                         capture_output=True).stdout.strip()
    return got or ("BOSS QA" if key == "name" else "qa@boss.invalid")


def _clone(boss: Boss, args, project: dict, local: dict, work: Path) -> tuple[Git, Path]:
    url = f"{args.remote.rstrip('/')}/git/{project['id']}.git"
    dest = work / "repo"
    Git(args.helper)("clone", "-q", url, str(dest), quiet=True)
    return Git(args.helper, dest), dest


def _local(boss: Boss, project: dict) -> dict:
    code, body = boss.get(f"/v1/projects/{project['id']}/local-repo")
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"boss: BOSS did not describe {project.get('name')}'s repository "
                         f"(HTTP {code}: {_said(body)})")
    if not body.get("local") or not body.get("exists", True):
        raise SystemExit(f"boss: {project.get('name')} is not a local project (its code is not "
                         "kept on this machine by BOSS). The fault kit only breaks local "
                         "projects; cause the fault by hand on a hosted repository")
    return body


def _branch(case: str) -> str:
    return BRANCH_PREFIX + case.lower()


def inject(args, token: str) -> int:
    case = normalise_case(args.case)
    spec = CASES[case]
    boss = Boss(args.api, token)
    project = resolve_project(boss, args.project)
    local = _local(boss, project)
    why = production_refusal(boss, project, local)
    if why:
        raise SystemExit(f"boss: refused: {project.get('name')} is a production project — {why}. "
                         "The fault kit never breaks production; use a test project")
    with tempfile.TemporaryDirectory(prefix="boss-qa-fault-") as tmp:
        git, repo = _clone(boss, args, project, local, Path(tmp))
        branch = _branch(case)
        if git("ls-remote", "--heads", "origin", branch, quiet=True).stdout.strip():
            raise SystemExit(f"boss: {branch} already exists in {project.get('name')}: this "
                             f"fault is already out. Put it back first: boss qa fault --revert "
                             f"{case} --project {args.project}")
        try:
            rel = pick_app(repo, args.app)
            applied = apply(case, repo / rel)
        except Unsupported as exc:
            raise SystemExit(f"boss: ENT-{case} cannot be applied: {exc}. Nothing was pushed")
        git("switch", "-q", "-c", branch)
        git("add", "-A")
        app_words = "the repository root" if rel == "." else rel
        message = (f"test fault ENT-{case}: {spec.title.lower()}\n\n{applied.what} "
                   f"(in {app_words}).\nMade by `boss qa fault {case}` on purpose; put it back "
                   f"with `boss qa fault --revert {case}`.\n\n{TRAILER}: {case}\n"
                   f"{TRAILER}-App: {rel}\n")
        git("commit", "-q", "-m", message)
        print(f"ENT-{case} · {spec.title} · {project.get('name')} · {app_words} ({applied.stack})")
        print(f"  The fault: {applied.what}.")
        git("push", "-u", "origin", branch)
    print(f"\nPushed {branch}. Next:")
    print(f"  1. Open the change BOSS opened for {branch}. If BOSS's review catches the fault, "
          "note it (a good result).")
    print(f"  2. {spec.then} — press Merge anyway… with the reason \"test fault {case}\".")
    print(f"  3. Watch, hands off. Pass if: {spec.expect}.")
    for note in applied.notes:
        print(f"  Note: {note}")
    print(f"  Put it back: boss qa fault --revert {case} --project {args.project}")
    return 0


def revert(args, token: str) -> int:
    case = normalise_case(args.case)
    boss = Boss(args.api, token)
    project = resolve_project(boss, args.project)
    local = _local(boss, project)
    why = production_refusal(boss, project, local)
    if why:
        raise SystemExit(f"boss: refused: {project.get('name')} is a production project — {why}")
    default = str(local.get("default_branch") or "main")
    with tempfile.TemporaryDirectory(prefix="boss-qa-fault-") as tmp:
        git, repo = _clone(boss, args, project, local, Path(tmp))
        branch = _branch(case)
        merged = git("log", f"origin/{default}", "--format=%H", "--extended-regexp",
                     f"--grep=^{TRAILER}: {case}$", quiet=True).stdout.split()
        if merged:
            back = f"{branch}-revert"
            if git("ls-remote", "--heads", "origin", back, quiet=True).stdout.strip():
                raise SystemExit(f"boss: {back} is already pushed: merge its change to put "
                                 f"ENT-{case} back")
            git("switch", "-q", "-c", back, f"origin/{default}")
            done = git("revert", "--no-edit", *merged, check=False, quiet=True)
            if done.returncode != 0:
                git("revert", "--abort", check=False, quiet=True)
                raise SystemExit(f"boss: the fault's lines have changed since it was merged (BOSS "
                                 f"may already have fixed it), so ENT-{case} cannot be reverted "
                                 f"automatically. Compare {default} with commit {merged[0][:12]}")
            git("commit", "--amend", "-q", "-m", f"put back test fault ENT-{case}\n\nReverts "
                + ", ".join(m[:12] for m in merged) + f".\n\n{TRAILER}-Revert: {case}\n")
            git("push", "-u", "origin", back)
            print(f"\nPushed {back}: merge its change to put ENT-{case} back on {default}.")
            if git("ls-remote", "--heads", "origin", branch, quiet=True).stdout.strip():
                print(f"  ({branch} is still there; delete it when you like: git push origin "
                      f"--delete {branch})")
            return 0
        if git("ls-remote", "--heads", "origin", branch, quiet=True).stdout.strip():
            git("push", "origin", "--delete", branch)
            print(f"\nENT-{case} was never merged: deleted {branch}, which closes its change.")
            return 0
    print(f"Nothing to put back: ENT-{case} is neither on {default} nor on {branch}.")
    return 0


def list_cases() -> int:
    width = max(len(c.title) for c in CASES.values())
    print(f"{'case':<11} {'fault':<{width}}  stacks")
    for c in CASES.values():
        print(f"ENT-{c.id:<7} {c.title:<{width}}  {', '.join(c.stacks)}")
    print("\nboss qa fault CASE --project NAME [--app APP]    push the fault on test/fault-<case>")
    print("boss qa fault --revert CASE --project NAME       put it back")
    print("boss qa disk-pressure warn|critical|floor|off    the low-disk cases (ENT-REC-29..31)")
    return 0


# ── disk pressure (ENT-REC-29..31) ───────────────────────────────────────────────────────────

def _put(boss: Boss, path: str, body: dict) -> tuple[int, object]:
    req = urllib.request.Request(boss.api + path, method="PUT", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {boss._token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except ValueError:
            return exc.code, None


#: The thresholds each level moves, as `free % + margin`: just above what the disk has now, so the
#: watch reads that level on its next two readings without a byte written.
PRESSURE = {"warn": ("warn_free_pct",), "critical": ("warn_free_pct", "critical_free_pct"),
            "floor": ("warn_free_pct", "critical_free_pct", "floor_free_pct")}


def disk_pressure(args, token: str) -> int:
    """Move the storage thresholds just above today's free space (warn, critical or floor), and
    back (off). The saved thresholds live in the install's .boss/qa-disk-pressure.json."""
    boss = Boss(args.api, token)
    saved = Path(args.state)
    code, body = boss.get("/v1/infra/storage/summary")
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"boss: BOSS did not give its storage reading (HTTP {code}: "
                         f"{_said(body)})")
    if args.level == "off":
        if not saved.is_file():
            print("Nothing to put back: the thresholds were not moved by boss qa disk-pressure.")
            return 0
        before = json.loads(saved.read_text())
        code, out = _put(boss, "/v1/infra/storage/rules", {"rules": [],
                                                           "thresholds": before["thresholds"]})
        if code != 200:
            raise SystemExit(f"boss: BOSS refused the thresholds (HTTP {code}: {_said(out)})")
        saved.unlink()
        print("The storage thresholds are back as they were. Held work starts again once the "
              "watch reads the disk as fine (two readings, about a minute).")
        return 0
    free_pct = _free_pct(body)
    if body.get("stale"):
        raise SystemExit("boss: BOSS's storage reading is stale (the host helper is not "
                         "answering: boss steward status). Nothing was changed")
    if free_pct is None:
        raise SystemExit("boss: BOSS has no current storage reading (is the host helper running? "
                         "boss steward status). Nothing was changed")
    code, rules = boss.get("/v1/infra/storage/rules")
    if code != 200 or not isinstance(rules, dict):
        raise SystemExit(f"boss: BOSS did not give its storage thresholds (HTTP {code}: "
                         f"{_said(rules)})")
    current = dict(rules.get("thresholds") or {})
    if not saved.is_file():
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps({"thresholds": current}, indent=1))
    moved = {**json.loads(saved.read_text())["thresholds"]}
    margin = {"warn_free_pct": 3.0, "critical_free_pct": 2.0, "floor_free_pct": 1.0}
    for key in PRESSURE[args.level]:
        moved[key] = round(min(99.0, free_pct + margin[key]), 1)
    # The exit bands must stay above the entry bands (hysteresis), or the watch cannot leave.
    moved["warn_exit_free_pct"] = max(float(moved.get("warn_exit_free_pct") or 0),
                                      moved["warn_free_pct"] + 4)
    moved["target_free_pct"] = max(float(moved.get("target_free_pct") or 0),
                                   moved.get("critical_free_pct", 0) + 10)
    code, out = _put(boss, "/v1/infra/storage/rules", {"rules": [], "thresholds": moved})
    if code != 200:
        raise SystemExit(f"boss: BOSS refused the thresholds (HTTP {code}: {_said(out)})")
    print(f"The disk is {free_pct:.1f}% free. Storage thresholds moved just above it for the "
          f"{args.level} case ({', '.join(f'{k}={moved[k]}' for k in PRESSURE[args.level])}); "
          "nothing was written to disk.")
    print("Expect the watch to read the new level within about a minute (two readings).")
    print("Put it back: boss qa disk-pressure off")
    return 0


def _free_pct(summary: dict) -> Optional[float]:
    for src in (summary.get("disk") or {}, summary):
        if not isinstance(src, dict):
            continue
        try:
            if src.get("free_pct") is not None:
                return float(src["free_pct"])
            if src.get("used_pct") is not None:
                return 100.0 - float(src["used_pct"])
            if src.get("free_gb") is not None and src.get("total_gb"):
                return 100.0 * float(src["free_gb"]) / float(src["total_gb"])
        except (TypeError, ValueError):
            continue
    return None


# ── the ledger (boss qa report) and a run's diagnostics ──────────────────────────────────────

LEDGER_COLUMNS = ("Project", "Case", "Run(s)", "Workflow", "Failure seen", "Green / draft",
                  "Time to fix", "Human actions", "Unattended", "Fixed by", "Final state",
                  "Diagnostics")


def _duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


def ledger_markdown(rows: list[dict]) -> str:
    out = ["| " + " | ".join(LEDGER_COLUMNS) + " |", "|" + "---|" * len(LEDGER_COLUMNS)]
    for r in rows:
        end = r.get("green_at") or (f"draft {r['draft_at']}" if r.get("draft_at") else "—")
        cells = (r.get("project") or "", r["case"], ", ".join(r["runs"]),
                 ", ".join(r.get("workflow_runs") or []) or "—", r.get("failure_at") or "—", end,
                 _duration(r.get("fix_seconds")), r["human_actions"],
                 "Y" if r["unattended"] else "N", ", ".join(r.get("acted") or []) or "—",
                 r["final_state"], f"`{r['diagnostics']}`")
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |")
    return "\n".join(out)


def report(args, token: str) -> int:
    boss = Boss(args.api, token)
    if args.project:
        projects = [resolve_project(boss, args.project)]
    else:
        code, body = boss.get("/v1/projects")
        if code != 200 or not isinstance(body, dict):
            raise SystemExit(f"boss: BOSS refused the project list (HTTP {code}): {_said(body)}")
        projects = list(body.get("items") or [])
    query = {k: v for k, v in (("since", args.since), ("case", args.case)) if v}
    if args.case:
        query["case"] = normalise_case(args.case)
    rows: list[dict] = []
    masked: dict[str, int] = {}
    for p in projects:
        code, body = boss.get("/v1/qa/ledger", project_id=p["id"], **query)
        if code != 200 or not isinstance(body, dict):
            raise SystemExit(f"boss: BOSS refused the ledger of {p.get('name')} (HTTP {code}): "
                             f"{_said(body)}")
        for r in body.get("rows") or []:
            rows.append({"project": p.get("name") or p["id"], **r})
        for k, v in (body.get("masked") or {}).items():
            masked[k] = masked.get(k, 0) + int(v)
    rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    if args.json:
        print(json.dumps({"rows": rows, "masked": masked}, indent=1))
        return 0
    if not rows:
        print("No runs of a fault case" + (f" since {args.since}" if args.since else "")
              + ". A run counts when it names a test/fault-<case> branch, or pass --case "
                "to tag the window.")
        return 0
    print(ledger_markdown(rows))
    if masked:
        print(f"\n{sum(masked.values())} masked ("
              + ", ".join(f"{k} {v}" for k, v in sorted(masked.items())) + ")")
    print("\nMessage actionable? is yours to judge: read each run's last message.")
    return 0


def diagnostics(args, token: str) -> int:
    """The run's diagnostics report, as the console's Download diagnostics makes it: masked by
    BOSS before it is written here, and still yours to read before you send it."""
    boss = Boss(args.api, token)
    code, run = boss.get(f"/v1/code-tasks/{urllib.parse.quote(args.run, safe='')}")
    if code != 200 or not isinstance(run, dict):
        raise SystemExit(f"boss: no run {args.run} you can read (HTTP {code}): {_said(run)}")
    code, body = boss.get("/v1/telemetry/share-bundle", project_id=str(run.get("project_id")),
                          unit_kind="build", unit_id=args.run)
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"boss: BOSS did not make the report (HTTP {code}): {_said(body)}")
    out = Path(args.out or f"boss-diagnostics-{args.run}.txt")
    out.write_text(str(body.get("bundle") or ""), encoding="utf-8")
    masked = body.get("masked") or {}
    print(f"Wrote {out} ({sum(int(v) for v in masked.values())} masked). Read it and remove "
          "anything private before you send it.")
    return 0


# ── the command ──────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="boss qa", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fault")
    f.add_argument("case", nargs="?")
    f.add_argument("--list", action="store_true")
    f.add_argument("--revert", metavar="CASE")
    f.add_argument("--project")
    f.add_argument("--app")
    f.add_argument("--api", default=os.environ.get("BOSS_QA_API", ""))
    f.add_argument("--remote", default=os.environ.get("BOSS_QA_REMOTE", ""))
    f.add_argument("--helper", default=os.environ.get("BOSS_QA_HELPER", ""))
    a = sub.add_parser("apply", help="apply a fault to a folder (no git, no BOSS)")
    a.add_argument("case")
    a.add_argument("dir")
    d = sub.add_parser("disk-pressure")
    d.add_argument("level", choices=["warn", "critical", "floor", "off"])
    d.add_argument("--api", default=os.environ.get("BOSS_QA_API", ""))
    d.add_argument("--state", default=os.environ.get("BOSS_QA_STATE", ".boss/qa-disk-pressure.json"))
    r = sub.add_parser("report")
    r.add_argument("--since")
    r.add_argument("--project")
    r.add_argument("--case")
    r.add_argument("--json", action="store_true")
    r.add_argument("--api", default=os.environ.get("BOSS_QA_API", ""))
    g = sub.add_parser("diagnostics")
    g.add_argument("run")
    g.add_argument("--out")
    g.add_argument("--api", default=os.environ.get("BOSS_QA_API", ""))
    args = ap.parse_args(argv)
    if args.cmd == "apply":
        try:
            done = apply(args.case, Path(args.dir))
        except Unsupported as exc:
            print(f"boss: cannot apply {args.case}: {exc}", file=sys.stderr)
            return 3
        print(json.dumps({"case": done.case, "stack": done.stack, "changed": done.changed,
                          "what": done.what, "notes": done.notes}))
        return 0
    if args.cmd == "fault" and args.list:
        return list_cases()
    if args.cmd == "fault":
        if args.revert:
            args.case = args.revert
        if not args.case:
            raise SystemExit("boss: which case? boss qa fault CASE --project NAME (boss qa fault "
                             "--list)")
        normalise_case(args.case)
        if not args.project:
            raise SystemExit("boss: which project? Pass --project NAME (a local test project)")
    token = sys.stdin.readline().strip()
    if not token:
        raise SystemExit("boss: no personal BOSS token reached the fault kit")
    if args.cmd == "disk-pressure":
        return disk_pressure(args, token)
    if args.cmd == "report":
        return report(args, token)
    if args.cmd == "diagnostics":
        return diagnostics(args, token)
    return revert(args, token) if args.revert else inject(args, token)


if __name__ == "__main__":
    sys.exit(main())
