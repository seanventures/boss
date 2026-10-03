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
    timeout_seconds: 900

and the gate runs only the suites a change touches, and in each only the tests that reference a
changed file (``by: imports``) or the whole glob (``by: all``). A changed test file is always
selected. When a suite is touched but nothing in it references the change, ``fallback`` decides:
``skip`` (the default) runs nothing for that suite, ``all`` runs its whole glob.

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


class VerifyManifestError(ValueError):
    """A ``boss.verify.yaml`` the gate cannot honour. The message names the field and the fix."""


@dataclass(frozen=True)
class VerifySuite:
    name: str
    paths: tuple[str, ...]
    run: tuple[str, ...]
    tests_glob: str = ""
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


@dataclass
class VerifyPlan:
    """What the gate runs for one set of changed files: ``steps`` in order, each an argv string
    and the repo-relative directory it runs in."""

    steps: list[tuple[str, str]] = field(default_factory=list)
    suites: list[dict[str, Any]] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @property
    def commands(self) -> list[str]:
        return [command for command, _ in self.steps]

    def describe(self) -> str:
        """One line for the console and the agent prompt: which suites, how many tests each."""
        if not self.suites:
            return f"{MANIFEST_NAME}: no suite covers the changed files"
        parts = [f"{s['name']} ({s['selected']} test file(s), {s['reason']})" for s in self.suites]
        return f"{MANIFEST_NAME}: " + "; ".join(parts)


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
    unknown = set(data) - {"version", "setup", "suites", "timeout_seconds"}
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
        tests_glob = str(select.get("tests_glob") or "").strip()
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
    return VerifyManifest(
        suites=tuple(suites),
        setup=_commands(data.get("setup"), f"{MANIFEST_NAME}: setup"),
        timeout_seconds=timeout,
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


def _references_changed(refs: Iterable[str], changed_stems: set[str]) -> bool:
    """A reference names a changed file when, extension aside, one ends with the other on a
    path boundary. ``boss_engine/services/x`` names ``engine/boss_engine/services/x.py``;
    ``app/deploy-ui.tsx`` (relative to apps/web) names ``apps/web/app/deploy-ui.tsx``.
    A bare package specifier (``react``) never names a repo file."""
    for ref in refs:
        cleaned = ref.strip()
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        for prefix in ("@/", "~/"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        if "/" not in cleaned and "." not in cleaned:
            # `import yaml` must not select a test because some `x/yaml.py` changed; a bare
            # Python module is only matched when it IS a top-level file.
            if cleaned in changed_stems:
                return True
            continue
        stem = _stem(cleaned)
        for changed in changed_stems:
            if changed == stem or changed.endswith("/" + stem):
                return True
    return False


def select_tests(root: Path | str, suite: VerifySuite, changed: list[str]) -> tuple[list[str], str]:
    """Test files of ``suite`` to run for ``changed``, and why. Repo-relative, sorted."""
    base = Path(root)
    pattern = suite.tests_glob
    all_tests = sorted(
        p.relative_to(base).as_posix()
        for p in base.glob(pattern)
        if p.is_file()
    ) if pattern else []
    if suite.by == "all":
        return all_tests, "all tests"
    changed_set = {c for c in changed}
    picked = {t for t in all_tests if t in changed_set}
    stems = {_stem(c) for c in changed if not glob_match(c, pattern)}
    if stems:
        for rel in all_tests:
            if rel in picked:
                continue
            try:
                text = (base / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            refs = (_python_references(text, rel) if rel.endswith(".py")
                    else _js_references(text, rel))
            if _references_changed(refs, stems):
                picked.add(rel)
    if picked:
        return sorted(picked), "tests that reference the change"
    if suite.fallback == "all":
        return all_tests, "no test references the change; whole suite"
    return [], "no test references the change"


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
    for suite in manifest.suites:
        if changed:
            if not any(glob_match(c, p) for c in changed for p in suite.paths):
                continue
            tests, reason = select_tests(root, suite, changed)
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
        plan.suites.append({"name": suite.name, "selected": len(tests), "reason": reason,
                            "ran": True, "tests": tests[:200]})
        suite_steps.extend((c, suite.cwd) for c in suite.setup)
        suite_steps.extend((_expand(c, tests, suite.cwd), suite.cwd) for c in suite.run)
    if suite_steps:
        plan.steps = [*((c, ".") for c in manifest.setup), *suite_steps]
    return plan
