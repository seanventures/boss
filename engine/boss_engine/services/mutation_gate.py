"""VAG #106 — mutation-gate the frozen tests.
from .scheduling import low_priority

BOSS re-runs the frozen tests as its *authoritative* verifier (a passing suite is what lets a
cheap-model implementation through the gate). But a **weak** suite passes buggy code just as
happily as correct code. #105 measured the review verifier; this measures the **test** verifier:
plant deterministic mutations in the implementation and require the tests to **kill** them. A
mutant the tests don't catch is a whole bug-class they're blind to → the suite is too weak → the
gate fails closed, *before* we trust it to certify anything.

Deterministic **AST** operators (no model call — unlike the pipeline's single LLM-planted mutant),
so the mutation score is cheap, reproducible, and reviewable:

    mutation score = mutants killed / mutants run      gate: score ≥ min_score (and baseline green)

The engine (mutant generation) and the scoring/gate are pure and model-free (unit-tested); the
runner executes the frozen tests once per mutant and is the only part that needs a sandbox.
"""
from __future__ import annotations

import ast
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Each operator maps an AST op type to its single canonical mutation (one mutant per site keeps
# the site-count identical between the counting pass and the mutating pass).
_COMPARE = {
    ast.Lt: ast.GtE, ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.GtE: ast.Lt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
_BINOP = {
    ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
    ast.Mod: ast.Mult, ast.Pow: ast.Mult, ast.FloorDiv: ast.Mult,
}
_BOOLOP = {ast.And: ast.Or, ast.Or: ast.And}


class _Engine(ast.NodeTransformer):
    """Walks the tree in a fixed order, assigning each mutable site an index. With ``target=None``
    it only counts; with an integer it mutates exactly the target-th site (leaving all others)."""

    def __init__(self, target: Optional[int]) -> None:
        self.target = target
        self.count = 0
        self.applied: Optional[tuple[str, int]] = None

    def _site(self, node: ast.AST, op_name: str, mutate) -> ast.AST:
        idx = self.count
        self.count += 1
        if self.target is not None and idx == self.target:
            self.applied = (op_name, getattr(node, "lineno", 0))
            return mutate()
        return node

    # --- operators ---
    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        # only single-comparison expressions (a < b), not chained (a < b < c), to keep it clean
        if len(node.ops) == 1 and type(node.ops[0]) in _COMPARE:
            repl = _COMPARE[type(node.ops[0])]

            def _m():
                node.ops = [repl()]
                return node
            return self._site(node, f"compare:{type(node.ops[0]).__name__}", _m)
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if type(node.op) in _BINOP:
            repl = _BINOP[type(node.op)]

            def _m():
                node.op = repl()
                return node
            return self._site(node, f"binop:{type(node.op).__name__}", _m)
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if type(node.op) in _BOOLOP:
            repl = _BOOLOP[type(node.op)]

            def _m():
                node.op = repl()
                return node
            return self._site(node, f"boolop:{type(node.op).__name__}", _m)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        # bool must be checked before int (bool is a subclass of int)
        if isinstance(node.value, bool):
            return self._site(node, "const:bool", lambda: ast.copy_location(
                ast.Constant(value=not node.value), node))
        if isinstance(node.value, (int, float)) and not isinstance(node.value, complex):
            return self._site(node, "const:num", lambda: ast.copy_location(
                ast.Constant(value=node.value + 1), node))
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        # `return <something-not-None>` -> `return None`; a suite that can't tell the difference
        # isn't asserting on the return value at all.
        if node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            def _m():
                node.value = ast.copy_location(ast.Constant(value=None), node)
                return node
            return self._site(node, "return:None", _m)
        return node


def count_sites(source: str) -> int:
    """Number of mutable sites in the source (deterministic)."""
    eng = _Engine(target=None)
    eng.visit(ast.parse(source))
    return eng.count


def generate_mutants(source: str, max_mutants: Optional[int] = None) -> list[dict[str, Any]]:
    """Produce one mutant per site (up to max_mutants), each a full source string with exactly one
    canonical mutation applied. Deterministic: same source in, same mutants out."""
    total = count_sites(source)
    n = total if max_mutants is None else min(total, max_mutants)
    mutants: list[dict[str, Any]] = []
    for i in range(n):
        eng = _Engine(target=i)
        tree = eng.visit(ast.parse(source))
        if eng.applied is None:
            continue
        ast.fix_missing_locations(tree)
        try:
            mutated = ast.unparse(tree)
        except Exception:  # noqa: BLE001 — a mutation that won't round-trip is simply skipped
            continue
        op, lineno = eng.applied
        mutants.append({"id": i, "operator": op, "lineno": lineno, "source": mutated})
    return mutants


# --- scoring + gate (pure) ---------------------------------------------------
def mutation_score(results: list[dict[str, Any]]) -> Optional[float]:
    """killed / run, rounded. None when no mutants ran (nothing to conclude)."""
    run = [r for r in results if r.get("status") in ("killed", "survived")]
    if not run:
        return None
    killed = sum(1 for r in run if r["status"] == "killed")
    return round(killed / len(run), 3)


def has_property_tests(test_source: str) -> bool:
    """Heuristic: does the frozen test suite use property-based testing (Hypothesis)? Property tests
    constrain behavior across generated inputs, catching bug-classes example tests miss."""
    low = (test_source or "").lower()
    return "hypothesis" in low or "@given" in low or "from hypothesis" in low


def mutation_gate_decision(
    score: Optional[float], *, min_score: float, baseline_passed: bool,
    require_property_tests: bool = False, property_tests_present: bool = True,
) -> dict[str, Any]:
    """Fail-closed gate on TEST strength. Blocks when the baseline is red (tests broken → can't
    conclude anything), when the mutation score is below the floor (weak suite), or when property
    tests are required but absent."""
    reasons: list[str] = []
    if not baseline_passed:
        reasons.append("baseline tests do not pass on the unmutated code — cannot mutation-test")
    if score is None:
        reasons.append("no mutants ran — nothing to conclude (add code with mutable logic)")
    elif score < min_score:
        reasons.append(
            f"mutation score {score} < min {min_score} — surviving mutants are bugs the tests can't catch")
    if require_property_tests and not property_tests_present:
        reasons.append("property-based tests required but none found (no Hypothesis @given)")
    return {"passed": not reasons, "reasons": reasons, "score": score,
            "min_score": min_score, "baseline_passed": baseline_passed}


# --- runner (executes the frozen tests once per mutant) ----------------------
class MutationGateRefused(ValueError):
    """The request asks the gate to run something it never runs (an API 422)."""


#: The ONLY commands the gate runs. The caller's files are the code under test; the program that
#: runs them is never the caller's choice — a caller-named command was arbitrary command
#: execution on the API host (identity int-b). `python3` is this interpreter.
ALLOWED_TEST_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "unittest", "discover", "-v"),
    ("python3", "-m", "unittest", "discover"),
    ("python3", "-m", "pytest", "-q"),
    ("python3", "-m", "pytest"),
)
DEFAULT_TEST_COMMAND: tuple[str, ...] = ALLOWED_TEST_COMMANDS[0]


def allowed_test_command(test_command: Any) -> list[str]:
    """The argv to run for `test_command` (a list or a string), or MutationGateRefused when it is
    not one of ALLOWED_TEST_COMMANDS. None/empty means the default."""
    import shlex
    import sys

    if test_command in (None, "", []):
        argv: tuple[str, ...] = DEFAULT_TEST_COMMAND
    elif isinstance(test_command, str):
        argv = tuple(shlex.split(test_command))
    elif isinstance(test_command, (list, tuple)) and all(isinstance(a, str) for a in test_command):
        argv = tuple(test_command)
    else:
        raise MutationGateRefused("test_command must be a list of strings")
    if argv not in ALLOWED_TEST_COMMANDS:
        raise MutationGateRefused(
            "test_command is not one the gate runs; allowed: "
            + "; ".join(" ".join(c) for c in ALLOWED_TEST_COMMANDS))
    return [sys.executable, *argv[1:]]


def _safe_relative(rel: Any) -> str:
    """A file path INSIDE the gate's temporary tree: relative, no `..`, no drive or NUL. A path
    that escapes the tree would write the caller's content anywhere the API can write."""
    from pathlib import PurePosixPath

    text_rel = str(rel or "")
    if not text_rel or "\x00" in text_rel or "\\" in text_rel or ":" in text_rel:
        raise MutationGateRefused(f"file path {text_rel!r} is not a plain relative path")
    path = PurePosixPath(text_rel)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts) or text_rel.startswith("~"):
        raise MutationGateRefused(f"file path {text_rel!r} escapes the gate's working tree")
    return str(path)


def _write_tree(root, files: dict[str, str]) -> None:
    from pathlib import Path
    for rel, content in files.items():
        dest = Path(root) / _safe_relative(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)


def _scrubbed_env(workdir: str) -> dict[str, str]:
    """The tests run with NOTHING of the API's environment: no DATABASE_URL, no keys, no tokens."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": workdir, "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}


def _run_tests(workdir: str, test_command, timeout: int) -> bool:
    """True iff the test command exits 0 in ``workdir``. Any non-zero exit / timeout / crash counts
    as a failing suite (which, for a mutant, means the mutant was killed — the outcome we want)."""
    import subprocess

    from . import owned_process
    from .scheduling import low_priority

    cmd = allowed_test_command(test_command)
    try:
        # Mutation testing re-runs the project's whole suite once per mutant, in the API's
        # own container. See services/scheduling.py.
        proc = owned_process.run(low_priority(cmd), cwd=workdir, capture_output=True,
                              text=True, timeout=timeout, env=_scrubbed_env(workdir))
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:  # noqa: BLE001 — a mutant that won't even start is a killed mutant
        return False


def _test_source(files: dict[str, str]) -> str:
    return "\n".join(c for p, c in files.items() if "test" in p.lower())


def run_mutation_gate(
    files: dict[str, str], target_file: str, test_command, *,
    min_score: float, max_mutants: int = 30, timeout: int = 30,
    require_property_tests: bool = False,
) -> dict[str, Any]:
    """End-to-end: baseline the frozen tests, mutate ``target_file``, run the tests against each
    mutant, score, and gate. ``files`` maps path→content (implementation + tests). Executes the
    provided tests, so run it in a sandbox / trusted eval context. Model-free and deterministic.

    A **surviving** mutant is reported with its operator + line so QA can see exactly which behavior
    the suite fails to pin down.
    """
    import tempfile
    allowed_test_command(test_command)          # refuse before anything is written or run
    for rel in files:
        _safe_relative(rel)
    if target_file not in files:
        return {"error": f"target_file {target_file!r} not in files", "gate": {"passed": False}}

    with tempfile.TemporaryDirectory(prefix="mutgate_base_") as base:
        _write_tree(base, files)
        baseline_passed = _run_tests(base, test_command, timeout)

    results: list[dict[str, Any]] = []
    if baseline_passed:
        mutants = generate_mutants(files[target_file], max_mutants)
        for m in mutants:
            with tempfile.TemporaryDirectory(prefix="mutgate_") as root:
                _write_tree(root, {**files, target_file: m["source"]})
                killed = not _run_tests(root, test_command, timeout)
            results.append({"id": m["id"], "operator": m["operator"], "lineno": m["lineno"],
                            "status": "killed" if killed else "survived"})

    score = mutation_score(results)
    gate = mutation_gate_decision(
        score, min_score=min_score, baseline_passed=baseline_passed,
        require_property_tests=require_property_tests,
        property_tests_present=has_property_tests(_test_source(files)),
    )
    survivors = [r for r in results if r["status"] == "survived"]
    logger.info("mutation gate on %s: score=%s baseline=%s survivors=%d/%d",
                target_file, score, baseline_passed, len(survivors), len(results))
    return {"gate": gate, "score": score, "baseline_passed": baseline_passed,
            "mutants_run": len(results), "killed": len(results) - len(survivors),
            "survivors": survivors, "results": results}

