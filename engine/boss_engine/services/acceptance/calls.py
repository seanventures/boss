"""PLAIN CALLS (E1, PR #154 review): the only commands whose output can be evidence.

A block-list of code-writing commands is never complete (`python3 -m timeit "open('.env',...)"`,
`npm exec -c`, `make --eval`, `mvn exec:exec`, -m pdb/code/cProfile ...). So it is an ALLOW-LIST:
a command is a PLAIN CALL INTO THE DELIVERED WORK only when, per toolchain,

  python   a tracked script; `-m <module>` for a module in the tree, or pytest / unittest
           (interpreter flags limited to harmless ones; -W/-X take a value)
  pytest   always (it runs the tree's tests)
  node     a tracked script, or `node --test`; `bun test` / `deno test`
  sh/bash  a tracked script, no options
  make     targets defined in the tracked Makefile; no variable assignments, --eval/-E, -f/-C
  npm/yarn/pnpm  a script defined in the tracked package.json (`npm test`, `npm run <s>`,
           `yarn <s>`, `pnpm run <s>`), or `ci`/`install` with no package arguments; never
           exec / dlx / x / -c / --call
  go       test / vet / build / run on tree packages, with flags from a short list
  mvn      lifecycle phases with a tracked pom.xml; no plugin goals (`x:y`), -D, -P, -f
  gradle   standard tasks with a tracked build script; no -D/-P/-I/--init-script/--args
  cargo    test / build / check / run with a tracked Cargo.toml; no --config / -Z

Anything else — every other interpreter mode, any wrapper, an unknown tool — is not a plain call:
its output is never evidence, and if it changes a file outside the tracked tree (ignored files,
HOME, temp) the session is tainted (session.AcceptanceSandbox).
"""
from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Iterable, Optional

_PY = re.compile(r"^(python[\d.]*|pypy[\d.]*)$")
_SHELL = re.compile(r"^(sh|bash|dash|zsh|ksh|ash)$")
_PY_FLAGS = frozenset({"-u", "-B", "-O", "-OO", "-q", "-s", "-E", "-I", "-b", "-bb"})
_PY_VALUE = frozenset({"-W", "-X"})
_PY_MODULES = frozenset({"pytest", "unittest"})
_MAKE_FLAGS = frozenset({"-s", "--silent", "--quiet", "-k", "--keep-going",
                         "--no-print-directory", "-B", "--always-make", "-n", "--dry-run",
                         "--just-print"})
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.%/\-][A-Za-z0-9_.%/\- ]*?)\s*::?(?![=])")
_NPM_FLAGS = frozenset({"--silent", "-s", "--if-present", "--quiet", "-q"})
_GO_SUBS = frozenset({"test", "vet", "build", "run"})
_GO_FLAGS = frozenset({"-v", "-race", "-short", "-cover", "-failfast", "-json", "-x"})
_GO_VALUE = frozenset({"-count", "-run", "-timeout", "-o", "-p", "-tags", "-bench",
                       "-coverprofile", "-skip"})
_MVN_PHASES = frozenset({"validate", "compile", "test-compile", "test", "package", "verify",
                         "install", "clean"})
_MVN_FLAGS = frozenset({"-q", "--quiet", "-B", "--batch-mode", "-e", "--errors", "-o",
                        "--offline", "-ntp", "--no-transfer-progress"})
_GRADLE_TASKS = frozenset({"test", "check", "build", "assemble", "clean", "jar", "classes",
                           "testClasses"})
_GRADLE_FLAGS = frozenset({"-q", "--quiet", "--offline", "--no-daemon", "--console=plain",
                           "-i", "--info", "--stacktrace", "--warning-mode=all"})
_CARGO_SUBS = frozenset({"test", "build", "check", "run"})
_CARGO_FLAGS = frozenset({"--release", "-q", "--quiet", "--all-targets", "--workspace",
                          "--offline", "--locked", "--frozen", "--all-features"})


class Tree:
    """The delivered tree as the classifier sees it: the TRACKED files (git ls-files at the PR
    head) and the directory a command runs in, both relative to the clone's root."""

    def __init__(self, root: Path, tracked: Iterable[str], cwd: str = "."):
        self.root = Path(root)
        self.tracked = frozenset(tracked)
        self.cwd = posixpath.normpath(cwd or ".")

    def rel(self, path: str) -> Optional[str]:
        """`path` (as a command names it) relative to the root, or None outside it."""
        joined = posixpath.normpath(posixpath.join(self.cwd, str(path)))
        if joined.startswith("../") or joined == ".." or posixpath.isabs(str(path)):
            return None
        return joined

    def has(self, path: str) -> bool:
        rel = self.rel(path)
        return bool(rel) and rel in self.tracked

    def has_dir(self, path: str) -> bool:
        rel = self.rel(path)
        if rel is None:
            return False
        prefix = "" if rel == "." else rel.rstrip("/") + "/"
        return any(t.startswith(prefix) for t in self.tracked)

    def read(self, name: str) -> Optional[str]:
        if not self.has(name):
            return None
        try:
            return (self.root / self.rel(name)).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None


def plain_call(argv: list, tree: Tree) -> Optional[str]:
    """None when `argv` is a plain call into the delivered work; otherwise why it is not."""
    if not argv:
        return "an empty command"
    prog = Path(str(argv[0])).name
    args = [str(a) for a in argv[1:]]
    if _PY.match(prog):
        return _python(args, tree)
    if prog in ("pytest", "py.test"):
        return None
    if prog in ("node", "nodejs"):
        return _node(args, tree)
    if prog in ("bun", "deno"):
        return None if args[:1] == ["test"] else f"{prog} runs only its test command here"
    if _SHELL.match(prog):
        if args and not args[0].startswith(("-", "+")) and tree.has(args[0]):
            return None
        return f"{prog} runs only a tracked script, with no options"
    if prog == "make":
        return _make(args, tree)
    if prog in ("npm", "yarn", "pnpm"):
        return _npm(prog, args, tree)
    if prog == "go":
        return _go(args, tree)
    if prog in ("mvn", "mvnw"):
        return _mvn(args, tree)
    if prog in ("gradle", "gradlew"):
        return _gradle(args, tree)
    if prog == "cargo":
        return _cargo(args, tree)
    return f"{prog} is not a plain call into the delivered work"


def _python(args: list[str], tree: Tree) -> Optional[str]:
    why = "python runs only a tracked script, a tree module, pytest or unittest"
    i = 0
    while i < len(args):
        a = args[i]
        if a in _PY_FLAGS:
            i += 1
        elif a in _PY_VALUE:
            i += 2
        elif a[:2] in _PY_VALUE and len(a) > 2:
            i += 1
        elif a == "-m":
            mod = args[i + 1] if i + 1 < len(args) else ""
            if mod in _PY_MODULES or _tree_module(mod, tree):
                return None
            return why
        elif a.startswith("-"):
            return why
        else:
            return None if a.endswith(".py") and tree.has(a) else why
    return why


def _tree_module(mod: str, tree: Tree) -> bool:
    if not re.fullmatch(r"[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*", mod or ""):
        return False
    base = mod.replace(".", "/")
    return any(tree.has(p) for p in (f"{base}.py", f"{base}/__init__.py", f"{base}/__main__.py"))


def _node(args: list[str], tree: Tree) -> Optional[str]:
    rest = [a for a in args if a not in ("--enable-source-maps", "--no-warnings")]
    if rest[:1] == ["--test"]:
        return None if all(not a.startswith("-") and tree.has(a) for a in rest[1:]) else \
            "node --test runs only tracked test files"
    if rest and not rest[0].startswith("-") and tree.has(rest[0]):
        return None
    return "node runs only a tracked script"


def _make_targets(text: str) -> set[str]:
    out: set[str] = set()
    for line in text.splitlines():
        if line.startswith(("\t", "#", " ")) or "=" in line.split(":", 1)[0]:
            continue
        m = _MAKE_TARGET.match(line)
        if m:
            out.update(t for t in m.group(1).split() if t)
    return out


def _make(args: list[str], tree: Tree) -> Optional[str]:
    text = next((t for t in (tree.read(n) for n in ("GNUmakefile", "makefile", "Makefile"))
                 if t is not None), None)
    if text is None:
        return "make needs a tracked Makefile"
    targets = _make_targets(text)
    i = 0
    while i < len(args):
        a = args[i]
        if a in _MAKE_FLAGS or re.fullmatch(r"-j\d*|--jobs(=\d+)?", a):
            i += 1
            continue
        if a.startswith("-") or "=" in a:
            return f"make {a} is not allowed (only targets of the tracked Makefile)"
        if a not in targets:
            return f"make target {a} is not defined in the tracked Makefile"
        i += 1
    return None


def _npm(prog: str, args: list[str], tree: Tree) -> Optional[str]:
    text = tree.read("package.json")
    if text is None:
        return f"{prog} needs a tracked package.json"
    try:
        scripts = dict(json.loads(text).get("scripts") or {})
    except (ValueError, AttributeError):
        return "package.json could not be read"
    words = [a for a in args if a not in _NPM_FLAGS]
    if "--" in words:
        words = words[:words.index("--")]
    if not words:
        return f"{prog} needs a script name" if prog != "yarn" else None   # yarn = install
    sub, rest = words[0], words[1:]
    if sub in ("ci", "install", "i") and not rest:
        return None
    if sub in ("run", "run-script"):
        if len(rest) >= 1 and rest[0] in scripts and not any(r.startswith("-") for r in rest):
            return None
        return f"{prog} run needs a script defined in the tracked package.json"
    if sub in ("test", "t", "start") and (sub if sub != "t" else "test") in scripts and not rest:
        return None
    if prog in ("yarn", "pnpm") and sub in scripts and not any(r.startswith("-") for r in rest):
        return None
    return f"{prog} {sub} is not a script of the tracked package.json"


def _go(args: list[str], tree: Tree) -> Optional[str]:
    if not args or args[0] not in _GO_SUBS:
        return "go runs only test, vet, build or run on the tree"
    i = 1
    while i < len(args):
        a = args[i]
        name = a.split("=", 1)[0]
        if a in _GO_FLAGS:
            i += 1
        elif name in _GO_VALUE:
            i += 1 if "=" in a else 2
        elif a.startswith("-"):
            return f"go {a} is not allowed"
        else:
            p = a[:-4] if a.endswith("/...") else a
            if not (tree.has_dir(p) or tree.has(a)):
                return f"go package {a} is not in the tree"
            if args[0] == "run":
                return None                       # the rest are the program's arguments
            i += 1
    return None


def _mvn(args: list[str], tree: Tree) -> Optional[str]:
    if not tree.has("pom.xml"):
        return "mvn needs a tracked pom.xml"
    for a in args:
        if a in _MVN_FLAGS or a in _MVN_PHASES:
            continue
        return f"mvn {a} is not a lifecycle phase (no plugin goals, -D, -P or -f)"
    return None


def _gradle(args: list[str], tree: Tree) -> Optional[str]:
    if not any(tree.has(f) for f in ("build.gradle", "build.gradle.kts")):
        return "gradle needs a tracked build script"
    for a in args:
        if a in _GRADLE_FLAGS or a in _GRADLE_TASKS:
            continue
        return f"gradle {a} is not a standard task (no -D, -P, -I, --init-script, --args)"
    return None


def _cargo(args: list[str], tree: Tree) -> Optional[str]:
    if not tree.has("Cargo.toml"):
        return "cargo needs a tracked Cargo.toml"
    if not args or args[0] not in _CARGO_SUBS:
        return "cargo runs only test, build, check or run"
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--":
            return None
        if a in _CARGO_FLAGS:
            i += 1
        elif a in ("-p", "--package", "--bin", "--test"):
            i += 2
        else:
            return f"cargo {a} is not allowed"
    return None


__all__ = ["Tree", "plain_call"]
