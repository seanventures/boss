"""A SELF-FIX NEVER WEAKENS A TEST, STAYS IN ITS DIAGNOSIS, AND PROVES ITSELF RED THEN GREEN.

BOSSAPP-571 (docs/product/BOSS_FIXES_ITSELF.md §5.2 Stage 4–6, §8.6, F8, F9, R2, R3, R16).
The mechanical gates of a self-fix that run BEFORE any reviewer (F16) and again on the final
diff. Each is fail-closed and NONE is waivable: a self-fix is not a code task, and no override
route reaches it.

  tests_not_weakened     (R3, F9) an existing test file is APPEND-ONLY: no deleted or edited
                         line, so no deleted test, no edited body, no loosened assert, no
                         `skip`/`xfail` added to it; conftest, fixtures, snapshots and test
                         config never change; every NEW test is checked for skip/xfail/only/todo
                         markers, an early return, `assert True`, `assert x or True`, a body
                         wrapped whole in `pytest.raises(Exception)`, no assertion at all, and
                         must reference the diagnosed module. A weakening attempt is never
                         retried: it is a signal (refused: tests_weakened).
  scope_matches_diagnosis (R16) every changed non-test file is the diagnosed file, one import hop
                         from it (either direction), or named in the diagnosis with a reason.
  no_new_dependencies    manifests, lockfiles, images and compose files (also `never` paths;
                         this gate gives the clearer words).
  size                   ≤ max_files files and ≤ max_diff_lines changed lines outside the new tests.
  no_committed_secrets   no secret-shaped value on an added line (the one redaction layer).
  red_then_green         (R2) the new test FAILS on the base for the diagnosed reason (an
                         assertion, never an import or collection error, naming the location or
                         the symptom) and PASSES on the head.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Optional

from .self_fix_policy import (FileChange, _line_changes, changed_lines, diff_size, is_test_path,
                              matches, norm)

#: Test files that only ever ADD in a self-fix (v1): any change to an existing one refuses.
FROZEN_TEST_GLOBS = ("**/conftest.py", "**/fixtures/**", "**/__snapshots__/**", "**/golden/**",
                     "**/*.snap", "**/pytest.ini", "**/pyproject.toml", "**/setup.cfg",
                     "**/tox.ini", "**/vitest.config.*", "tests/tiers.yaml", "boss.verify.yaml")
DEPENDENCY_GLOBS = ("**/requirements*.txt", "**/package.json", "**/pnpm-lock.yaml",
                    "**/package-lock.json", "**/yarn.lock", "**/poetry.lock", "**/uv.lock",
                    "**/Pipfile*", "**/Dockerfile*", "**/*compose*.yml", "**/*compose*.yaml",
                    "**/go.mod", "**/go.sum", "**/Cargo.toml", "**/Cargo.lock")

_SKIP_DECORATORS = re.compile(r"\b(?:skip|skipif|xfail|skipUnless|skipIf|expectedFailure)\b")
_SKIP_CALLS = {"skip", "xfail", "importorskip", "fail"}
_JS_WEAK = re.compile(r"\b(?:it|test|describe)\.(?:skip|only|todo)\b|\b(?:xit|xdescribe|xtest|"
                      r"fit|fdescribe)\s*\(|\.todo\s*\(")


@dataclass
class GateResult:
    name: str
    ok: bool
    why: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: A refusal that must not be retried (a weakening attempt is a signal, not a mistake).
    final: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "why": self.why, "detail": self.detail,
                **({"final": True} if self.final else {})}


# ── tests_not_weakened ───────────────────────────────────────────────────────────────────────

def _test_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    out: list[Any] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            out.append(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            out.extend(n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and n.name.startswith("test"))
    return out


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _decorator_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""


def _shape(fn: Any) -> str:
    """A test function's decorators, signature and body, without positions."""
    return ast.dump(ast.Module(body=[fn], type_ignores=[]), include_attributes=False)


def _weak_python_test(fn: Any) -> Optional[str]:
    """Why a NEW Python test is too weak to count, or None."""
    for deco in fn.decorator_list:
        if _SKIP_DECORATORS.search(_decorator_text(deco)):
            return f"{fn.name} is marked {_decorator_text(deco)}"
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(
        getattr(n, "value", None), ast.Constant) and isinstance(n.value.value, str))]
    if not body:
        return f"{fn.name} has no body"
    first = body[0]
    if isinstance(first, ast.Return):
        return f"{fn.name} returns before it tests anything"
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _call_name(node) in _SKIP_CALLS and isinstance(
                node.func, ast.Attribute) and _decorator_text(node.func.value) == "pytest":
            return f"{fn.name} calls pytest.{_call_name(node)}"
    asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
    for a in asserts:
        test = a.test
        if isinstance(test, ast.Constant) and bool(test.value):
            return f"{fn.name} asserts a constant"
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) and any(
                isinstance(v, ast.Constant) and bool(v.value) for v in test.values):
            return f"{fn.name} asserts something `or True`"
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.With) and any(
        _call_name(i.context_expr) == "raises" for i in n.items)]
    for w in raises:
        for item in w.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and call.args and _decorator_text(call.args[0]) in (
                    "Exception", "BaseException"):
                if len(body) == 1 and body[0] is w:
                    return f"{fn.name} wraps its whole body in pytest.raises(Exception)"
    if not asserts and not raises:
        # A helper that asserts counts (`_check(...)`), as long as SOMETHING in the module does.
        return f"{fn.name} asserts nothing"
    return None


def _references(source: str, location: Mapping[str, Any]) -> bool:
    """The new test names the diagnosed module (an import or a name), so it can fail for it."""
    file = str(location.get("file") or "")
    function = str(location.get("function") or "").split(".")[-1]
    stem = PurePosixPath(file).stem if file else ""
    return bool((stem and re.search(rf"\b{re.escape(stem)}\b", source))
                or (function and re.search(rf"\b{re.escape(function)}\b", source)))


def tests_not_weakened(changes: Iterable[FileChange], *,
                       location: Optional[Mapping[str, Any]] = None) -> GateResult:
    """THE `tests_not_weakened` GATE (§8.6). `location` is the diagnosis's {file, function}."""
    location = location or {}
    problems: list[str] = []
    new_tests: list[str] = []
    for change in changes:
        paths = change.paths
        if not any(is_test_path(p) for p in paths) and not any(
                matches(g, p) for g in FROZEN_TEST_GLOBS for p in paths):
            continue
        if any(matches(g, p) for g in FROZEN_TEST_GLOBS for p in paths):
            problems.append(f"{change.path}: test configuration, fixtures, snapshots and "
                            "conftest are never changed by a self-fix")
            continue
        if change.status == "D" or change.after is None:
            problems.append(f"{change.path}: deletes a test file")
            continue
        if change.status == "R":
            problems.append(f"{change.path}: renames a test file ({change.old_path})")
            continue
        if change.before is not None:
            removed, _, inserted_after = _line_changes(change.before, change.after)
            if removed:
                problems.append(f"{change.path}: edits or removes {len(removed)} existing "
                                "line(s); an existing test file only grows")
                continue
            base_len = len(change.before.splitlines())
            if not change.path.endswith(".py") and any(n < base_len for n in inserted_after):
                problems.append(f"{change.path}: inserts lines inside the existing tests; an "
                                "existing test file only grows at its end")
                continue
        if change.path.endswith(".py"):
            try:
                tree = ast.parse(change.after)
            except SyntaxError as error:
                problems.append(f"{change.path}: does not parse ({error.msg})")
                continue
            before_names = set()
            if change.before:
                try:
                    before_tests = _test_functions(ast.parse(change.before))
                except SyntaxError:
                    before_tests = []
                before_names = {f.name for f in before_tests}
                head = {f.name: f for f in _test_functions(tree)}
                for old in before_tests:
                    new = head.get(old.name)
                    if new is None or _shape(new) != _shape(old):
                        problems.append(f"{change.path}: changes the existing test {old.name} "
                                        "(its decorators or its body)")
            if re.search(r"^\s*pytestmark\s*=", change.after, re.M) and not re.search(
                    r"^\s*pytestmark\s*=", change.before or "", re.M):
                problems.append(f"{change.path}: adds a module-wide pytestmark")
            fresh = [f for f in _test_functions(tree) if f.name not in before_names]
            for fn in fresh:
                weak = _weak_python_test(fn)
                if weak:
                    problems.append(f"{change.path}: {weak}")
            new_tests.extend(f"{change.path}::{f.name}" for f in fresh)
            if fresh and location and not _references(change.after, location):
                problems.append(f"{change.path}: never names the diagnosed code "
                                f"({location.get('file')} {location.get('function') or ''})"
                                .rstrip())
        else:
            _, added = changed_lines(change.before, change.after)
            lines = change.after.splitlines()
            for n in sorted(added):
                if 0 < n <= len(lines) and _JS_WEAK.search(lines[n - 1]):
                    problems.append(f"{change.path}:{n}: adds a skipped, focused or todo test")
            new_tests.append(change.path)
    if problems:
        return GateResult("tests_not_weakened", False, "; ".join(problems[:6]),
                          {"problems": problems, "new_tests": new_tests}, final=True)
    return GateResult("tests_not_weakened", True, "", {"new_tests": new_tests})


def non_test_paths(changes: Iterable[FileChange]) -> list[str]:
    """Non-test paths a "write the failing test" turn touched (it may only add tests)."""
    return [c.path for c in changes if not is_test_path(c.path)]


def tests_edited(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Test files whose content the implementer changed (`coder_did_not_edit_tests`)."""
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


# ── scope, dependencies, size, secrets ───────────────────────────────────────────────────────

def _module_of(path: str) -> str:
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] == "engine":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports_of(path: str, source: Optional[str]) -> set[str]:
    """Repository paths a Python file imports (absolute boss_engine.* and relative imports)."""
    if not source or not path.endswith(".py"):
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    package = PurePosixPath(path).parent
    out: set[str] = set()
    for node in ast.walk(tree):
        mods: list[tuple[str, list[str]]] = []
        if isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names]
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.parent
                rest = (node.module or "").replace(".", "/")
                mods.append((str(base / rest) if rest else str(base), names))
            elif node.module and node.module.startswith("boss_engine"):
                mods.append(("engine/" + node.module.replace(".", "/"), names))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("boss_engine"):
                    mods.append(("engine/" + alias.name.replace(".", "/"), []))
        for base, names in mods:
            out.add(f"{base}.py")
            for name in names:
                out.add(f"{base}/{name}.py")
    return out


def scope_matches_diagnosis(changes: Iterable[FileChange], *, location: Mapping[str, Any],
                            named: Iterable[Mapping[str, Any]] = (),
                            sources: Optional[Mapping[str, str]] = None) -> GateResult:
    """THE `scope_matches_diagnosis` GATE (R16). `named`: the diagnosis's other files, each with
    a reason; `sources`: {path: head content} to resolve one import hop."""
    diagnosed = norm(location.get("file"))
    if not diagnosed:
        return GateResult("scope_matches_diagnosis", False,
                          "the diagnosis names no file, so no change can be in its scope",
                          final=True)
    allowed = {diagnosed}
    allowed |= {norm(n.get("path")) for n in named
                if n.get("path") and str(n.get("reason") or "").strip()}
    sources = dict(sources or {})
    diagnosed_src = sources.get(diagnosed)
    allowed |= imports_of(diagnosed, diagnosed_src)
    for path, src in sources.items():
        if diagnosed in imports_of(path, src):
            allowed.add(path)
    outside = [c.path for c in changes if not is_test_path(c.path) and c.path not in allowed]
    if outside:
        return GateResult("scope_matches_diagnosis", False,
                          f"{', '.join(outside)} {'is' if len(outside) == 1 else 'are'} outside "
                          f"the diagnosed area ({diagnosed} and what it imports or is imported "
                          "by); a self-fix changes only what its diagnosis is about",
                          {"outside": outside, "allowed": sorted(allowed)}, final=True)
    return GateResult("scope_matches_diagnosis", True, "", {"allowed": sorted(allowed)})


def no_new_dependencies(changes: Iterable[FileChange]) -> GateResult:
    hits = [p for c in changes for p in c.paths
            if any(matches(g, p) for g in DEPENDENCY_GLOBS)]
    if hits:
        return GateResult("no_new_dependencies", False,
                          f"{', '.join(sorted(set(hits)))}: a self-fix never adds or changes a "
                          "dependency, an image or a compose file", {"paths": hits}, final=True)
    return GateResult("no_new_dependencies", True)


def within_size(changes: Iterable[FileChange], *, max_files: int, max_lines: int) -> GateResult:
    code = [c for c in changes if not is_test_path(c.path)]
    lines = diff_size(code)
    if len(code) > max_files or lines > max_lines:
        return GateResult("size", False,
                          f"too big for a self-fix: {len(code)} file(s), {lines} changed line(s) "
                          f"(the most is {max_files} files and {max_lines} lines)",
                          {"files": len(code), "lines": lines}, final=True)
    if not code:
        return GateResult("code_changed", False, "the fix changed no code, only tests",
                          {"files": 0, "lines": 0})
    return GateResult("size", True, "", {"files": len(code), "lines": lines})


def no_committed_secrets(changes: Iterable[FileChange]) -> GateResult:
    from .redaction import redact

    hits = []
    for change in changes:
        if change.after is None:
            continue
        _, added = changed_lines(change.before, change.after)
        lines = change.after.splitlines()
        for n in sorted(added):
            line = lines[n - 1] if 0 < n <= len(lines) else ""
            masked, found = redact(line)
            if found and masked != line and not set(found) <= {"IP"}:
                hits.append(f"{change.path}:{n} ({', '.join(sorted(found))})")
    if hits:
        return GateResult("no_committed_secrets", False,
                          f"secret-shaped values on added lines: {', '.join(hits[:5])}",
                          {"hits": hits}, final=True)
    return GateResult("no_committed_secrets", True)


# ── red, then green (R2) ─────────────────────────────────────────────────────────────────────

@dataclass
class SuiteRun:
    """What one run of the new tests said."""
    exit_code: int
    output: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    collection_error: bool = False

    @classmethod
    def from_pytest(cls, exit_code: int, output: str) -> "SuiteRun":
        tail = output[-4000:]
        def count(word: str) -> int:
            m = re.findall(rf"(\d+) {word}", tail)
            return int(m[-1]) if m else 0
        collection = bool(re.search(r"ERROR collecting|ImportError|ModuleNotFoundError|"
                                    r"SyntaxError|errors? during collection|"
                                    r"no tests ran|NameError: name", output))
        return cls(exit_code=int(exit_code), output=output[-12000:], passed=count("passed"),
                   failed=count("failed"), errors=count("errors?"),
                   collection_error=collection or int(exit_code) in (2, 3, 4, 5))

    def as_dict(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "passed": self.passed, "failed": self.failed,
                "errors": self.errors, "collection_error": self.collection_error,
                "output_tail": self.output[-3000:]}


def _symptom_words(symptom: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", symptom or "")
    common = {"failed", "error", "there", "which", "would", "could", "should", "their", "about",
              "already", "another", "because", "release", "container"}
    return [w for w in dict.fromkeys(words) if w.lower() not in common][:12]


def is_red(run: SuiteRun, *, location: Mapping[str, Any], symptom: str = "") -> tuple[bool, str]:
    """(the new test fails on the base for the diagnosed reason, why not)."""
    if run.exit_code == 0 or (run.failed == 0 and run.errors == 0):
        return False, "the new test passes on BOSS as it is, so it does not reproduce the bug"
    if run.collection_error or (run.failed == 0 and run.errors):
        return False, ("the new test errors before it can fail (an import, a name or collection "
                       "error), so it proves nothing about the bug")
    text = run.output
    file = str(location.get("file") or "")
    names = [PurePosixPath(file).stem] if file else []
    names += [str(location.get("function") or "").split(".")[-1]]
    if any(n and n in text for n in names):
        return True, ""
    if any(w in text for w in _symptom_words(symptom)):
        return True, ""
    return False, ("the new test fails, but not for the diagnosed reason (its failure names "
                   "neither the diagnosed code nor the symptom)")


def is_green(run: SuiteRun) -> tuple[bool, str]:
    if run.exit_code == 0 and run.failed == 0 and run.errors == 0 and run.passed > 0:
        return True, ""
    if run.passed == 0 and run.exit_code == 0:
        return False, "no test ran"
    return False, f"the new test still fails ({run.failed} failed, {run.errors} errors)"


def red_then_green(red: Optional[SuiteRun], green: Optional[SuiteRun], *,
                   location: Mapping[str, Any], symptom: str = "") -> GateResult:
    if red is None or green is None:
        return GateResult("red_then_green", False, "the test was not run on both the base and "
                                                   "the fix")
    red_ok, red_why = is_red(red, location=location, symptom=symptom)
    green_ok, green_why = is_green(green)
    detail = {"base": red.as_dict(), "head": green.as_dict()}
    if not red_ok:
        return GateResult("red_then_green", False, red_why, detail)
    if not green_ok:
        return GateResult("red_then_green", False, green_why, detail)
    return GateResult("red_then_green", True, "", detail)


__all__ = ["DEPENDENCY_GLOBS", "FROZEN_TEST_GLOBS", "GateResult", "SuiteRun", "imports_of",
           "is_green", "is_red", "no_committed_secrets", "no_new_dependencies", "red_then_green",
           "scope_matches_diagnosis", "non_test_paths", "tests_edited", "tests_not_weakened",
           "within_size"]
