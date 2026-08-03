"""Decision-table tests for every judgement BOSS makes autonomously.

WHY THIS FILE EXISTS
Every production failure in the coding pipeline traced to one shape: a decision function
returning the wrong value for its inputs. Risk tier, gate action, merge gate, preview
entrypoint, regression floor, context scoping, model routing -- eight decision points,
eight defects, and *zero* dedicated tests across the five files that hold them. They were
being discovered one per run, at roughly $6 and 50 minutes each.

Each is a pure function of its inputs, so each is a two-line assertion. This file locks in
the fixes and, just as importantly, locks in the SAFETY side of each one: it is easy to
"fix" a false positive by disabling the check, so every relaxation here is paired with a
case proving the real signal still fires.

RUN IT DIRECTLY -- `python tests/test_decision_layer.py`.
Do NOT run under pytest: collection imports app modules that reset the local BOSS database.
No model calls, no network, no money; the whole suite is milliseconds.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))

from boss_engine.services.pipeline_profiles import (  # noqa: E402
    PROFILES, infer_risk_tier, sensitive_markers_in,
)
from boss_engine.services.governed_code_task_service import (  # noqa: E402
    _TEST_DEF_RE, check_is_inconclusive, classify_gate_faults,
    resolve_gate_failure_action,
)
from boss_engine.services.code_agent import (  # noqa: E402
    _scope_to_task_app, build_repo_context,
)
from boss_engine.services.preview_service import (  # noqa: E402
    _detect_command, _generate_dockerfile,
)
from boss_engine.services.code_task_service import preflight_test_commands  # noqa: E402
from boss_engine.services.model_rate_limiter import (  # noqa: E402
    ModelRateLimiter, RateLimitAcquireTimeout,
)

BALANCED = PROFILES["balanced"]
_RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, got, want, note: str = "") -> None:
    _RESULTS.append((got == want, name, f"got={got!r} want={want!r} {note}".strip()))


def check_true(name: str, got, note: str = "") -> None:
    check(name, bool(got), True, note)


# --------------------------------------------------------------------------- #
# 1. RISK TIER — decides which stages run. Four production defects lived here.
# --------------------------------------------------------------------------- #
DEPLOY_NOTE = ("\n\nDEPLOYABILITY (required for any network service): bind the host from "
               "the HOST env var, and the port from PORT.")
REVIEW_PROSE = ("\n\n=== ITERATION FEEDBACK (attempt 2) ===\n## Code Review\nThe auth "
                "token handling in the harness looks fine; the defect is an off-by-one.")


def t_risk_tier() -> None:
    inc = {"title": "Tweak the board",
           "instructions": "Update the existing N-Queens board; small and surgical."}

    # BOSS's own boilerplate must not classify the task (production defect).
    check("tier: deployability boilerplate is not 'sensitive'",
          sensitive_markers_in({**inc, "instructions": inc["instructions"] + DEPLOY_NOTE}), [])
    check("tier: deployability boilerplate stays tier 1",
          infer_risk_tier({**inc, "instructions": inc["instructions"] + DEPLOY_NOTE}, BALANCED), 1)

    # Reviewer prose folded into a retry must not classify it either (production defect).
    check("tier: reviewer prose saying 'auth/token' is not 'sensitive'",
          sensitive_markers_in({**inc, "instructions": inc["instructions"] + REVIEW_PROSE}), [])
    check("tier: reviewer prose stays tier 1",
          infer_risk_tier({**inc, "instructions": inc["instructions"] + REVIEW_PROSE}, BALANCED), 1)

    # SAFETY: genuinely sensitive work must still escalate.
    for label, text in [("auth", "Add auth and password reset to the existing app."),
                        ("payments", "Update the existing billing and payment flow."),
                        ("migration", "Run a schema change and data migration."),
                        ("deploy", "Change the deploy pipeline for the existing service.")]:
        check(f"tier: real {label} work is tier 3",
              infer_risk_tier({"title": label, "instructions": text}, BALANCED), 3)

    # Word-boundary matching: substrings must not fire.
    for label, text in [("authentic", "Reword the existing copy to sound more authentic."),
                        ("tokenizer", "Adjust the existing tokenizer helper.")]:
        check(f"tier: '{label}' is NOT a sensitive marker",
              sensitive_markers_in({"title": label, "instructions": text}), [])

    # Size-aware inference: greenfield vs incremental vs trivial.
    check("tier: greenfield build is tier 2",
          infer_risk_tier({"title": "New app",
                           "instructions": "Build a new Sudoku platform from scratch."}, BALANCED), 2)
    check("tier: incremental behaviour change is tier 1", infer_risk_tier(inc, BALANCED), 1)
    check("tier: presentation-only change is tier 0",
          infer_risk_tier({"title": "Restyle",
                           "instructions": "Update the existing board CSS colors; small tweak."},
                          BALANCED), 0)
    check("tier: behaviour words veto the trivial lane",
          infer_risk_tier({"title": "Scoring",
                           "instructions": "Update the existing scoring algorithm; small tweak."},
                          BALANCED), 1)
    # An explicit caller tier is honoured (Copilot sends risk_tier=1).
    check("tier: explicit risk_tier is honoured",
          infer_risk_tier({**inc, "risk_tier": 1}, BALANCED), 1)
    # SAFETY: an explicit low tier must NOT override real sensitivity.
    check("tier: explicit low tier cannot downgrade sensitive work",
          infer_risk_tier({"title": "login", "instructions": "Add auth.", "risk_tier": 0},
                          BALANCED) >= 2, True)


# --------------------------------------------------------------------------- #
# 2. GATE ACTION — decides repair vs re-check vs ship. Two defects lived here.
# --------------------------------------------------------------------------- #
def _action(evidence: dict, failing: list[str], rounds: int = 0, never_block: bool = True) -> str:
    faults = classify_gate_faults(failing=failing, evidence=evidence)
    return resolve_gate_failure_action(
        failing=failing, advisory_gate=False, rounds_used=rounds,
        max_repair_rounds=2, open_pr=True, never_block=never_block, faults=faults)


def t_gate_action() -> None:
    turns_out = {"review_summary": "(ran out of turns before finishing — partial attempt; "
                                   "resume to continue)", "adversarial_verdict": "not_run"}
    real = {"review_summary": "FINDING: MAJOR app.js:42 — off-by-one in the level index.",
            "adversarial_verdict": "refuted"}

    # A check that never finished must re-run the CHECK, never repair the code.
    check("gate: reviewer out of turns -> inconclusive", _action(turns_out, ["code_review_passed"]),
          "inconclusive")
    check("gate: empty summary -> inconclusive",
          _action({"review_summary": "", "adversarial_verdict": "not_run"},
                  ["code_review_passed"]), "inconclusive")
    check("gate: panel deadline exceeded -> inconclusive",
          _action({"review_summary": "2 review check(s) exceeded 900s — degraded",
                   "adversarial_verdict": "not_run"}, ["code_review_passed"]), "inconclusive")
    check("gate: rate-limit acquire timeout -> inconclusive",
          _action({"review_summary": "could not acquire a anthropic call slot within 600s",
                   "adversarial_verdict": "not_run"}, ["code_review_passed"]), "inconclusive")

    # SAFETY: a real finding must still repair — including when its PROSE contains the
    # words that used to be treated as harness markers (production defect: a MAJOR
    # finding mentioning "timeout" was classified harness and never repaired).
    check("gate: real finding -> repair", _action(real, ["code_review_passed"]), "repair")
    for word in ["the fetch has no timeout, it hangs",
                 "retry loop exceeded safe bounds; performance degraded",
                 "the check was not_run in CI"]:
        check(f"gate: real finding mentioning '{word[:18]}...' -> repair",
              _action({"review_summary": f"FINDING: MAJOR x.js:1 — {word}",
                       "adversarial_verdict": "refuted"}, ["code_review_passed"]), "repair")

    # Test gates are authoritative evidence about the code, never harness faults.
    check("gate: red tests -> repair (never inconclusive)",
          _action({}, ["written_tests_pass"]), "repair")
    check("gate: mixed harness + red tests -> repair",
          _action(turns_out, ["code_review_passed", "written_tests_pass"]), "repair")

    # Exhausting the rounds must SHIP with dissent, never block.
    check("gate: rounds exhausted -> ship_with_dissent",
          _action(real, ["code_review_passed"], rounds=2), "ship_with_dissent")
    check("gate: never_block=false can still block",
          _action(real, ["code_review_passed"], rounds=2, never_block=False), "block")
    check("gate: nothing failing -> ship", _action({}, []), "ship")


# --------------------------------------------------------------------------- #
# 3. INCONCLUSIVE DETECTION — structured evidence must win over prose.
# --------------------------------------------------------------------------- #
def t_inconclusive() -> None:
    check_true("inconclusive: engine 'ran out of turns' phrase",
               check_is_inconclusive("(ran out of turns before finishing — partial attempt)",
                                     "not_run"))
    check("inconclusive: a clean SURVIVED review is conclusive",
          check_is_inconclusive("No defects found. VERDICT: SURVIVED", "survived"), False)
    check("inconclusive: structured findings beat prose",
          check_is_inconclusive("FINDING: MINOR a.js:1 — request has no timeout", "refuted"),
          False)
    check_true("inconclusive: empty summary", check_is_inconclusive("", None))


# --------------------------------------------------------------------------- #
# 4. PREVIEW ENTRYPOINT — host and container paths must agree. Two defects.
# --------------------------------------------------------------------------- #
def t_preview_entrypoint() -> None:
    root = pathlib.Path(tempfile.mkdtemp()) / "repo"
    (root / "web").mkdir(parents=True)
    (root / "serve.py").write_text("# pre-existing sudoku server\n")
    (root / "serve_nqueens.py").write_text("# the app THIS task built\n")
    (root / "web" / "index.html").write_text("<html>sudoku</html>")

    changed = ["serve_nqueens.py", "nqueens_core/board.py"]
    check("preview(host): picks the entrypoint the task built",
          _detect_command(root, changed), ["python3", "serve_nqueens.py"])
    _generate_dockerfile(root, changed)
    cmd = [l for l in (root / "Dockerfile").read_text().splitlines() if l.startswith("CMD")][0]
    check_true("preview(container): agrees with the host path", "serve_nqueens.py" in cmd)

    # SAFETY: a task that genuinely changed serve.py must still get serve.py.
    check("preview: no regression when serve.py IS the change",
          _detect_command(root, ["serve.py"]), ["python3", "serve.py"])
    # And with no knowledge of the change, the fixed list still resolves something runnable.
    check_true("preview: falls back to a known entrypoint", bool(_detect_command(root, [])))

    # A static-only app is a runnable app, not a library.
    st = pathlib.Path(tempfile.mkdtemp()) / "static"
    (st / "web").mkdir(parents=True)
    (st / "web" / "index.html").write_text("<html>x</html>")
    check_true("preview: static site is served, not treated as a library",
               "http.server" in " ".join(_detect_command(st, []) or []))


# --------------------------------------------------------------------------- #
# 5. REGRESSION FLOOR — must measure coverage lost, not bytes changed.
# --------------------------------------------------------------------------- #
BEFORE_T = "class T:\n    def test_a(self): pass\n    def test_b(self): pass\n"
RENAMED_T = ("class T:\n    def test_a_renamed_accurately(self): pass\n"
             "    def test_new_case(self): pass\n    def test_b(self): pass\n")
REMOVED_T = "class T:\n    def test_b(self): pass\n"


def t_regression_floor() -> None:
    n = lambda s: len(_TEST_DEF_RE.findall(s))  # noqa: E731
    check("floor: rename + add is NOT a regression", n(RENAMED_T) >= n(BEFORE_T), True,
          f"{n(BEFORE_T)} -> {n(RENAMED_T)}")
    check("floor: removing a test IS a regression", n(REMOVED_T) < n(BEFORE_T), True,
          f"{n(BEFORE_T)} -> {n(REMOVED_T)}")
    # Multi-language, so the floor is not python-only.
    check("floor: detects JS tests", n("it('x', () => {})\ntest('y', () => {})"), 2)
    check("floor: detects Go tests", n("func TestFoo(t *testing.T) {}"), 1)
    check("floor: detects Java tests", n("@Test\npublic void thing() {}"), 1)
    check("floor: detects async python tests", n("async def test_x(): pass"), 1)


# --------------------------------------------------------------------------- #
# 6. CONTEXT SCOPING — the app the task names, not its siblings.
# --------------------------------------------------------------------------- #
def t_context_scoping() -> None:
    files = ["nqueens_core/board.py", "nqueens_core/levels.py", "web-nqueens/app.js",
             "sudoku_core/board.py", "sudoku_core/scoring.py", "web/app.js",
             "tests/test_nqueens_board.py", "tests/test_sudoku_rules.py",
             "tests/test_streak.py", "README.md"]
    entries = [{"rel": f, "content": "x", "size": 1} for f in files]

    _, kept = _scope_to_task_app(entries, files, "Expand the N-Queens app to support 1-10 queens")
    check_true("scoping: keeps the named app", "nqueens_core/board.py" in kept)
    check("scoping: drops the sibling app", "sudoku_core/board.py" in kept, False)
    check_true("scoping: keeps the named app's tests", "tests/test_nqueens_board.py" in kept)
    check("scoping: drops a sibling app's CLEARLY-NAMED tests",
          "tests/test_sudoku_rules.py" in kept, False)
    # An unnamed shared test (test_streak.py) is DELIBERATELY kept: it belongs to the suite
    # this change must not break, and dropping it would hide a regression risk. Ambiguity
    # resolves toward showing the agent more of the suite, not less.
    check_true("scoping: keeps ambiguous shared tests (regression safety)",
               "tests/test_streak.py" in kept)
    check_true("scoping: keeps shared root files", "README.md" in kept)

    _, kept2 = _scope_to_task_app(entries, files, "Add a hint button to the Sudoku app")
    check_true("scoping: reverse direction keeps sudoku", "sudoku_core/board.py" in kept2)
    check("scoping: reverse direction drops nqueens", "nqueens_core/board.py" in kept2, False)

    # SAFETY: a task naming no app must not be narrowed.
    _, kept3 = _scope_to_task_app(entries, files, "Update the README")
    check("scoping: inert when no app is named", len(kept3), len(files))


# --------------------------------------------------------------------------- #
# 7. SANDBOX PREFLIGHT — reject commands the sandbox will refuse, before spending.
# --------------------------------------------------------------------------- #
class _S:
    sandbox_validation_allowed_commands = "npm,npx,node,python,python3,pytest,make,go,cargo,gradlew"


def t_preflight() -> None:
    s = _S()
    check("preflight: shell builtin '[' rejected",
          preflight_test_commands(s, ["[ -f x ] && python3 -m unittest"]), ["["])
    check("preflight: 'test' builtin rejected", preflight_test_commands(s, ["test -f x"]), ["test"])
    check("preflight: absolute path rejected",
          preflight_test_commands(s, ["/usr/bin/python3 -m pytest"]), ["/usr/bin/python3"])
    check("preflight: valid command accepted",
          preflight_test_commands(s, ["python3 -m unittest discover -s tests"]), [])
    check("preflight: repo-local wrapper accepted",
          preflight_test_commands(s, ["./gradlew test"]), [])
    check("preflight: no commands is not an error", preflight_test_commands(s, []), [])


# --------------------------------------------------------------------------- #
# 8. RATE LIMITER — a provider Retry-After must never sleep out a quota window.
# --------------------------------------------------------------------------- #
def t_rate_limiter() -> None:
    lim = ModelRateLimiter(max_concurrency=1, max_backoff_s=300.0)
    lim.report_rate_limited("anthropic", retry_after_s=7200)   # "retry in 2 hours"
    remaining = lim._lane("anthropic").backoff_until - lim._time()
    check_true("ratelimit: a 7200s Retry-After is capped", remaining <= 301, f"{remaining:.0f}s")

    lim2 = ModelRateLimiter(max_concurrency=1, max_backoff_s=300.0, acquire_timeout_s=1.0)
    lim2.report_rate_limited("anthropic", retry_after_s=7200)
    try:
        lim2._acquire("anthropic")
        check("ratelimit: acquire fails fast under backoff", "acquired", "RateLimitAcquireTimeout")
    except RateLimitAcquireTimeout:
        check_true("ratelimit: acquire fails fast under backoff", True)

    lim3 = ModelRateLimiter(max_concurrency=2)
    with lim3.slot("anthropic"):
        pass
    check_true("ratelimit: uncontended slot still works", True)


# --------------------------------------------------------------------------- #
def main() -> int:
    for fn in (t_risk_tier, t_gate_action, t_inconclusive, t_preview_entrypoint,
               t_regression_floor, t_context_scoping, t_preflight, t_rate_limiter):
        try:
            fn()
        except Exception as exc:  # a crash in a decision fn is itself a failure
            _RESULTS.append((False, f"{fn.__name__} raised", f"{type(exc).__name__}: {exc}"))

    failed = [r for r in _RESULTS if not r[0]]
    for ok, name, detail in _RESULTS:
        if not ok:
            print(f"  FAIL  {name}\n        {detail}")
    print(f"\n  {len(_RESULTS) - len(failed)}/{len(_RESULTS)} decision assertions passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
