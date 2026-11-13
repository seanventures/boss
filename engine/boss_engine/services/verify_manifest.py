"""``boss.verify.yaml`` — how a big repository tells the code-task gate what to test.

The gate's default target is one command for the whole repo: ``unittest discover`` for Python,
``npm install && build && typecheck && lint && e2e`` for a web app. That is right for a scaffolded
app and wrong for a monorepo. BOSS's own tree has ~420 engine test files and ~250 console test
files; running all of them for a one-line engine fix takes longer than the fix, and ``npm run
build`` at the root of a pnpm workspace is not even a command. The repo knows its suites; the
gate does not. So the repo declares them, at its root:

    version: 1
    setup: [<command>]                      # optional, once before any suite
    suites:
      - name: engine
        paths: ["engine/**"]                # globs over the changed files
        run: ["python3 -m pytest -q {tests}"]
        select: {tests_glob: "engine/tests/test_*.py", by: imports, fallback: skip}
      - name: web
        cwd: apps/web                       # optional; commands run here, {tests} relative to it
        setup: ["pnpm install --frozen-lockfile"]   # optional, only when this suite runs
        paths: ["apps/web/**"]
        run: ["npx vitest run {tests}"]
        select: {tests_glob: "apps/web/tests/**/*.test.*"}
      - name: data                          # tests_glob may also be a list of globs
        paths: ["packages/specpack/**"]
        run: ["python3 -m pytest -q {tests}"]
        select: {by: all, tests_glob: ["engine/tests/test_a.py", "engine/tests/test_b_*.py"]}
    timeout_seconds: 900
    services: [postgres]                    # optional: a throwaway Postgres for this run
                                            # (build_services: DATABASE_URL + PG* injected)

and the gate runs only the suites a change touches, and in each only the tests that reference a
changed file (``by: imports``) or the whole glob (``by: all``). A changed test file is always
selected. When a suite is touched but nothing in it references the change, ``fallback`` decides:
``skip`` (the default) runs nothing for that suite, ``all`` runs its whole glob.

A CHANGED CODE FILE NO TEST COVERS IS NOT A PASS. A plan that ran zero tests used to read as
green, so a change outside every suite (BOSS's ``apps/api``) or in a module no test imports
passed the gate on no evidence at all — and so did a planted bug in such a module. Each changed
file is now either covered (a ran test references it, it is a selected test, or its suite ran
whole) or listed in ``VerifyPlan.uncovered``, and the gate fails on that list with the files'
names. Only documentation and other non-code files (``NON_CODE_SUFFIXES``, ``docs/``) and
deleted files may go uncovered. A workspace package import (``@boss/api-client``) names every
file of that package: the specifier is mapped to the directory whose ``package.json`` declares it.

COMMANDS ARE ARGV, NOT SHELL. They go to ``EphemeralSandbox.run``, which refuses shell syntax
and checks argv[0] against the allowlist — a manifest cannot widen what the sandbox runs.
``{tests}`` expands to one argument per selected file, relative to the suite's ``cwd`` (the
repo root by default) — a test runner filters by the path it sees, and ``vitest`` run inside
``apps/web`` does not recognise ``apps/web/tests/x.test.ts``. A suite whose ``{tests}`` would
expand to nothing is not run.

Explicit ``test_commands`` on a task still win over the manifest; that decision is made in
``verification_target.resolve_gate_plan``, not here.
"""
from __future__ import annotations

import ast
import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

MANIFEST_NAME = "boss.verify.yaml"
DEFAULT_TIMEOUT_SECONDS = 900
TESTS_PLACEHOLDER = "{tests}"
_SELECT_BY = ("imports", "all")
_FALLBACK = ("skip", "all")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
#: Changes that carry no behaviour a test could check. Everything else is code and must be
#: covered by a test the plan runs.
NON_CODE_SUFFIXES = frozenset({
    ".md", ".mdx", ".rst", ".txt", ".adoc",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf",
})
NON_CODE_NAMES = frozenset({"LICENSE", "NOTICE", "AUTHORS", "CODEOWNERS"})
_PACKAGE_SKIP_DIRS = frozenset({"node_modules", ".git", ".next", "dist", "build", ".venv",
                                "venv", "__pycache__", ".turbo", "coverage"})


class VerifyManifestError(ValueError):
    """A ``boss.verify.yaml`` the gate cannot honour. The message names the field and the fix."""


@dataclass(frozen=True)
class VerifySuite:
    name: str
    paths: tuple[str, ...]
    run: tuple[str, ...]
    #: One glob, or a tuple of them (``tests_glob: [a, b]``): the suite's tests are their union.
    tests_glob: str | tuple[str, ...] = ""
    by: str = "imports"
    fallback: str = "skip"
    setup: tuple[str, ...] = ()
    cwd: str = "."


@dataclass(frozen=True)
class VerifyManifest:
    suites: tuple[VerifySuite, ...]
    setup: tuple[str, ...] = ()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    version: int = 1
    services: tuple[str, ...] = ()
    #: BOSSAPP-856: the app's runtime services during a build (runtime_services.RuntimeSpec).
    runtime: Any = None


@dataclass
class VerifyPlan:
    """What the gate runs for one set of changed files: ``steps`` in order, each an argv string
    and the repo-relative directory it runs in."""

    steps: list[tuple[str, str]] = field(default_factory=list)
    suites: list[dict[str, Any]] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    #: Changed code files no test in this plan covers. Non-empty means the gate cannot pass.
    uncovered: list[str] = field(default_factory=list)

    @property
    def commands(self) -> list[str]:
        return [command for command, _ in self.steps]

    def describe(self) -> str:
        """One line for the console and the agent prompt: which suites, how many tests each."""
        if not self.suites:
            text = f"{MANIFEST_NAME}: no suite covers the changed files"
        else:
            parts = [f"{s['name']} ({s['selected']} test file(s), {s['reason']})"
                     for s in self.suites]
            text = f"{MANIFEST_NAME}: " + "; ".join(parts)
        if self.uncovered:
            text += "; " + uncovered_message(self.uncovered)
        return text


def uncovered_message(files: list[str]) -> str:
    """The gate's failure for changed code no test covers — what is missing and the way out."""
    shown = ", ".join(files[:10]) + (f" and {len(files) - 10} more" if len(files) > 10 else "")
    return (f"no test covers {shown}. Add or extend a test that imports or names each file "
            f"(or a suite in {MANIFEST_NAME} that covers it); an unverified change needs a "
            "human waiver, it does not pass on its own")


def is_code_path(path: str) -> bool:
    """False for documentation and other files no test could check."""
    p = Path(path)
    if p.parts and p.parts[0] == "docs":
        return False
    return p.suffix.lower() not in NON_CODE_SUFFIXES and p.name not in NON_CODE_NAMES


# ---------------------------------------------------------------------------------- loading
def _string_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise VerifyManifestError(f"{where} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _commands(value: Any, where: str) -> tuple[str, ...]:
    from .sandbox import looks_like_shell

    commands = _string_list(value, where)
    for command in commands:
        probe = command.replace(TESTS_PLACEHOLDER, "tests")
        if looks_like_shell(probe):
            raise VerifyManifestError(
                f"{where}: {command!r} uses shell syntax. Verify commands run without a shell; "
                "give one plain command per entry (a sequence is several entries).")
        try:
            shlex.split(probe)
        except ValueError as exc:
            raise VerifyManifestError(f"{where}: {command!r} does not parse: {exc}") from exc
    return commands


def parse_verify_manifest(text: str) -> VerifyManifest:
    """Parse and validate manifest text. Raises VerifyManifestError with an operator message."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise VerifyManifestError(f"{MANIFEST_NAME} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise VerifyManifestError(f"{MANIFEST_NAME} must be a mapping with version and suites")
    if data.get("version") != 1:
        raise VerifyManifestError(f"{MANIFEST_NAME}: version must be 1")
    unknown = set(data) - {"version", "setup", "suites", "timeout_seconds", "services",
                           "runtime"}
    if unknown:
        raise VerifyManifestError(f"{MANIFEST_NAME}: unknown key(s) {sorted(unknown)}")
    timeout = data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise VerifyManifestError(f"{MANIFEST_NAME}: timeout_seconds must be a positive integer")
    raw_suites = data.get("suites")
    if not isinstance(raw_suites, list) or not raw_suites:
        raise VerifyManifestError(f"{MANIFEST_NAME}: suites must be a non-empty list")
    suites: list[VerifySuite] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_suites):
        where = f"{MANIFEST_NAME}: suites[{index}]"
        if not isinstance(raw, dict):
            raise VerifyManifestError(f"{where} must be a mapping")
        extra = set(raw) - {"name", "paths", "run", "select", "setup", "cwd"}
        if extra:
            raise VerifyManifestError(f"{where}: unknown key(s) {sorted(extra)}")
        name = str(raw.get("name") or "").strip()
        if not _NAME.match(name):
            raise VerifyManifestError(f"{where}: name must be letters, digits, '.', '_' or '-'")
        if name in seen:
            raise VerifyManifestError(f"{where}: suite name {name!r} is used twice")
        seen.add(name)
        paths = _string_list(raw.get("paths"), f"{where}.paths")
        if not paths:
            raise VerifyManifestError(f"{where}.paths must list at least one glob")
        run = _commands(raw.get("run"), f"{where}.run")
        if not run:
            raise VerifyManifestError(f"{where}.run must list at least one command")
        select = raw.get("select") or {}
        if not isinstance(select, dict):
            raise VerifyManifestError(f"{where}.select must be a mapping")
        extra = set(select) - {"tests_glob", "by", "fallback"}
        if extra:
            raise VerifyManifestError(f"{where}.select: unknown key(s) {sorted(extra)}")
        by = str(select.get("by") or "imports")
        if by not in _SELECT_BY:
            raise VerifyManifestError(f"{where}.select.by must be one of {list(_SELECT_BY)}")
        fallback = str(select.get("fallback") or "skip")
        if fallback not in _FALLBACK:
            raise VerifyManifestError(f"{where}.select.fallback must be one of {list(_FALLBACK)}")
        raw_glob = select.get("tests_glob")
        if isinstance(raw_glob, list):
            # A suite whose tests are not one directory (the engine tests that check the
            # shipped specpack) names them as a list of globs.
            globs = _string_list(raw_glob, f"{where}.select.tests_glob")
            tests_glob: str | tuple[str, ...] = globs[0] if len(globs) == 1 else globs
        else:
            tests_glob = str(raw_glob or "").strip()
        cwd = str(raw.get("cwd") or ".").strip().strip("/") or "."
        if Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            raise VerifyManifestError(f"{where}.cwd must be a directory inside the repository")
        if any(TESTS_PLACEHOLDER in c for c in run) and not tests_glob:
            raise VerifyManifestError(
                f"{where}: run uses {TESTS_PLACEHOLDER} but select.tests_glob is missing")
        suites.append(VerifySuite(
            name=name, paths=paths, run=run, tests_glob=tests_glob, by=by,
            fallback=fallback, setup=_commands(raw.get("setup"), f"{where}.setup"), cwd=cwd,
        ))
    from .build_services import parse_services

    try:
        services = parse_services(data.get("services"), f"{MANIFEST_NAME}: services")
    except ValueError as exc:
        raise VerifyManifestError(str(exc)) from None
    from .runtime_services import parse_runtime

    return VerifyManifest(
        suites=tuple(suites),
        setup=_commands(data.get("setup"), f"{MANIFEST_NAME}: setup"),
        timeout_seconds=timeout,
        services=services,
        runtime=parse_runtime(data.get("runtime"), f"{MANIFEST_NAME}: runtime"),
    )


def load_verify_manifest(root: Path | str) -> VerifyManifest | None:
    """The manifest at the repo root, or None when the repo declares none."""
    path = Path(root) / MANIFEST_NAME
    if not path.is_file():
        return None
    return parse_verify_manifest(path.read_text(encoding="utf-8"))


# -------------------------------------------------------------------------------- selection
def glob_match(path: str, pattern: str) -> bool:
    """Repo-relative glob with ``**`` spanning directories (``engine/**`` matches any depth).

    fnmatch's ``*`` already crosses ``/``, so ``**/`` is made optional to let
    ``apps/web/tests/**/*.test.*`` also match a file directly in ``tests/``.
    """
    path = path.removeprefix("./")
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return "**/" in pattern and fnmatch.fnmatchcase(path, pattern.replace("**/", ""))


def _stem(path: str) -> str:
    """``engine/boss_engine/x.py`` -> ``engine/boss_engine/x``; ``a/index.ts`` -> ``a``."""
    p = Path(path)
    stem = str(p.with_suffix("")) if p.suffix else str(p)
    if p.stem in ("index", "__init__") and p.suffix:
        stem = str(p.parent)
    return stem.replace("\\", "/")


def workspace_packages(root: Path | str) -> dict[str, str]:
    """``package.json`` ``name`` -> its repo-relative directory, for every package in the tree.

    ``import { x } from "@boss/api-client"`` names ``packages/api-client``; without this map the
    specifier is compared as a path, never equals a file, and a change to the package selects
    none of the tests that import it.
    """
    import json

    base = Path(root)
    found: dict[str, str] = {}
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in _PACKAGE_SKIP_DIRS)
        if "package.json" not in files:
            continue
        rel = Path(current).relative_to(base).as_posix()
        if rel == ".":
            continue  # the workspace root is not something a test imports
        try:
            name = json.loads((Path(current) / "package.json").read_text(encoding="utf-8")).get(
                "name")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(name, str) and name.strip():
            found.setdefault(name.strip(), rel)
    return found


def _package_dir(spec: str, packages: dict[str, str]) -> str | None:
    for name, rel in packages.items():
        if spec == name or spec.startswith(name + "/"):
            return rel
    return None


_JS_SPEC = re.compile(
    r"""(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]"""
    r"""|import\s*\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|require\s*\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|import\s+['"]([^'"]+)['"]""",
)
_STRING_PATH = re.compile(r"""['"`]([A-Za-z0-9_@.\-][A-Za-z0-9_@./\-]*/[A-Za-z0-9_@.\-]+)['"`]""")


def _python_references(text: str, test_rel: str = "") -> set[str]:
    """Module paths a Python test imports (relative imports resolved against the test file),
    plus path-like string literals it reads."""
    refs: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                refs.update(alias.name.replace(".", "/") for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and (node.module or node.level):
                base = (node.module or "").replace(".", "/")
                if node.level:
                    # `from ._source import code_only` in engine/tests/x.py names
                    # engine/tests/_source.py — a shared helper whose change must select it.
                    anchor = Path(test_rel).parents[node.level - 1].as_posix()
                    base = "/".join(p for p in (anchor, base) if p and p != ".")
                if base:
                    refs.add(base)
                refs.update(f"{base}/{alias.name}" if base else alias.name
                            for alias in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "/" in node.value and "\n" not in node.value and len(node.value) < 300:
                    refs.add(node.value)
    return refs


def _js_references(text: str, test_rel: str) -> set[str]:
    """Import specifiers (relative ones resolved against the test) plus path-like strings —
    the console's source-reading tests name the file they check as a string literal."""
    refs: set[str] = set()
    here = Path(test_rel).parent
    for match in _JS_SPEC.finditer(text):
        spec = next(g for g in match.groups() if g)
        if spec.startswith("."):
            refs.add(_normalise(here / spec))
        else:
            refs.add(spec)
    for match in _STRING_PATH.finditer(text):
        refs.add(match.group(1))
    return refs


def _normalise(path: Path) -> str:
    parts: list[str] = []
    for part in path.as_posix().split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _referenced(refs: Iterable[str], changed_stems: set[str],
                packages: dict[str, str] | None = None) -> set[str]:
    """The changed stems these references name. A reference names a changed file when,
    extension aside, one ends with the other on a path boundary. ``boss_engine/services/x``
    names ``engine/boss_engine/services/x.py``; ``app/deploy-ui.tsx`` (relative to apps/web)
    names ``apps/web/app/deploy-ui.tsx``. A workspace package specifier names every file in
    that package. Any other bare package specifier (``react``) never names a repo file."""
    named: set[str] = set()
    for ref in refs:
        cleaned = ref.strip()
        package = _package_dir(cleaned, packages or {})
        if package is not None:
            named.update(c for c in changed_stems if c == package or c.startswith(package + "/"))
            continue
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        for prefix in ("@/", "~/"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        if "/" not in cleaned and "." not in cleaned:
            # `import yaml` must not select a test because some `x/yaml.py` changed; a bare
            # Python module is only matched when it IS a top-level file.
            if cleaned in changed_stems:
                named.add(cleaned)
            continue
        stem = _stem(cleaned)
        named.update(c for c in changed_stems if c == stem or c.endswith("/" + stem))
    return named


def _references_changed(refs: Iterable[str], changed_stems: set[str],
                        packages: dict[str, str] | None = None) -> bool:
    return bool(_referenced(refs, changed_stems, packages))


def select_tests(root: Path | str, suite: VerifySuite, changed: list[str],
                 packages: dict[str, str] | None = None) -> tuple[list[str], str]:
    """Test files of ``suite`` to run for ``changed``, and why. Repo-relative, sorted."""
    tests, reason, _covered = _select(root, suite, changed, packages)
    return tests, reason


def _globs(suite: VerifySuite) -> tuple[str, ...]:
    """The suite's test globs: ``tests_glob`` as one string or a list."""
    raw = suite.tests_glob
    return tuple(g for g in ((raw,) if isinstance(raw, str) else raw) if g)


def _select(root: Path | str, suite: VerifySuite, changed: list[str],
            packages: dict[str, str] | None) -> tuple[list[str], str, set[str]]:
    """``select_tests`` plus the changed files the selection covers."""
    base = Path(root)
    patterns = _globs(suite)
    all_tests = sorted({
        p.relative_to(base).as_posix()
        for pattern in patterns
        for p in base.glob(pattern)
        if p.is_file()
    })
    if suite.by == "all":
        return all_tests, "all tests", set(changed) if all_tests else set()
    changed_set = {c for c in changed}
    picked = {t for t in all_tests if t in changed_set}
    by_stem: dict[str, set[str]] = {}
    for c in changed:
        if not any(glob_match(c, pattern) for pattern in patterns):
            by_stem.setdefault(_stem(c), set()).add(c)
    stems = set(by_stem)
    covered = set(picked)
    if stems:
        # A test in the change counts too: a new module and its new test arrive together, and
        # skipping changed tests here left the module "uncovered" for good (crun_f1c382, 2026-10-28:
        # fix_lane_triage.py and test_fix_lane_triage.py, repair rounds chasing an unfixable gate).
        for rel in all_tests:
            try:
                text = (base / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            refs = (_python_references(text, rel) if rel.endswith(".py")
                    else _js_references(text, rel))
            named = _referenced(refs, stems, packages)
            if named:
                picked.add(rel)
                for stem in named:
                    covered.update(by_stem[stem])
    if picked:
        return sorted(picked), "tests that reference the change", covered
    if suite.fallback == "all":
        return (all_tests, "no test references the change; whole suite",
                set(changed) if all_tests else set())
    return [], "no test references the change", set()


def _expand(command: str, tests: list[str], cwd: str) -> str:
    if TESTS_PLACEHOLDER not in command:
        return command
    rel = [t if cwd == "." else os.path.relpath(t, cwd) for t in tests]
    return command.replace(TESTS_PLACEHOLDER, " ".join(shlex.quote(t) for t in rel))


def plan_verify(manifest: VerifyManifest, root: Path | str, changed: list[str] | None) -> VerifyPlan:
    """The commands the gate runs for ``changed``.

    No changed files known means the gate cannot scope, so every suite runs its whole glob —
    a complete check, never a silent empty one.
    """
    changed = [c.replace("\\", "/").lstrip("/") for c in (changed or []) if str(c).strip()]
    plan = VerifyPlan(timeout_seconds=manifest.timeout_seconds)
    suite_steps: list[tuple[str, str]] = []
    packages = workspace_packages(root) if changed else {}
    covered: set[str] = set()
    for suite in manifest.suites:
        if changed:
            mine = [c for c in changed if any(glob_match(c, p) for p in suite.paths)]
            if not mine:
                continue
            # Selection sees every changed file (a console test may read an engine file), but
            # a suite vouches only for the files its own paths claim.
            tests, reason, suite_covered = _select(root, suite, changed, packages)
        else:
            tests, reason = select_tests(root, VerifySuite(
                name=suite.name, paths=suite.paths, run=suite.run,
                tests_glob=suite.tests_glob, by="all", cwd=suite.cwd), [])
            reason = "changed files unknown; whole suite"
        needs_tests = any(TESTS_PLACEHOLDER in c for c in suite.run)
        if needs_tests and not tests:
            plan.suites.append({"name": suite.name, "selected": 0, "reason": reason,
                                "ran": False})
            continue
        if changed:
            covered.update(c for c in suite_covered if c in mine)
        plan.suites.append({"name": suite.name, "selected": len(tests), "reason": reason,
                            "ran": True, "tests": tests[:200]})
        suite_steps.extend((c, suite.cwd) for c in suite.setup)
        suite_steps.extend((_expand(c, tests, suite.cwd), suite.cwd) for c in suite.run)
    if suite_steps:
        plan.steps = [*((c, ".") for c in manifest.setup), *suite_steps]
    base = Path(root)
    plan.uncovered = sorted(
        c for c in changed
        if c not in covered and is_code_path(c) and c != MANIFEST_NAME
        # A deleted file has nothing left to test; a test that still imports it is selected
        # by the reference and fails on its own.
        and (base / c).exists()
    )
    return plan
