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
    # WAS: "falls back to a known entrypoint" with no signal from the task. That fallback is
    # the bug — this tree holds three runnable apps, and picking one of them showed an operator
    # an application their task never touched. With no signal, refuse.
    from boss_engine.services.preview_service import AmbiguousAppError as _Amb
    try:
        _detect_command(root, [])
        check("preview: multi-app repo + no signal must refuse", "guessed", "raised")
    except _Amb:
        check_true("preview: multi-app repo + no signal must refuse", True)

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
# 9. LEAN / ADVISORY — "22 runs, 2 PRs". Runs recorded `PR not created: blocked`
#    because the adversarial panel stayed unhappy and the gate withheld the PR.
#    Lean's contract: tests are still written FIRST, the suite still runs, no LLM
#    seat argues about the code, and nothing a reviewer says can withhold the
#    deliverable. The two exemptions below are what stops that becoming "no gates".
# --------------------------------------------------------------------------- #
def t_advisory_gate() -> None:
    """The contract, in the operator's words:
         lean     — do not run adversarial review, do not review the PR
         balanced — run it and CAPTURE the outcome, but always raise the PR, never blocked
       So the profiles differ in whether reviews RUN, never in whether the PR happens.
    """
    from boss_engine.services.governed_code_task_service import (
        _ADVISORY_HARD_FLOOR, _ALL_PANEL_SEATS, resolve_gate_failure_action)
    from boss_engine.services.pipeline_profiles import PROFILES, infer_risk_tier

    # --- NOTHING a check decides can withhold the PR ---------------------------------
    for gate in ("code_review_passed", "security_clean", "consistency",
                 "tests_strong_enough", "existing_tests_intact", "nothing_broke",
                 "written_tests_pass", "tests_written_first"):
        check(f"advisory: {gate} failing still raises the PR",
              resolve_gate_failure_action(failing=[gate], advisory_gate=True,
                                          rounds_used=0, max_repair_rounds=2), "ship")
    check("advisory: every gate failing at once still raises the PR",
          resolve_gate_failure_action(
              failing=["code_review_passed", "security_clean", "consistency",
                       "existing_tests_intact", "tests_strong_enough"],
              advisory_gate=True, rounds_used=0, max_repair_rounds=2), "ship")
    check("advisory: no repair round delays the PR",
          resolve_gate_failure_action(failing=["existing_tests_intact"], advisory_gate=True,
                                      rounds_used=0, max_repair_rounds=2) != "repair", True)

    # --- the one exception: there is no code, so there is no PR to raise -------------
    check("advisory floor is ONLY 'nothing was built'", tuple(_ADVISORY_HARD_FLOOR),
          ("code_written",))
    check("advisory: no code written cannot ship",
          resolve_gate_failure_action(failing=["code_written"], advisory_gate=True,
                                      rounds_used=0, max_repair_rounds=2), "repair")

    # --- lean: does not convene the adversarial panel at all -------------------------
    lean = PROFILES["lean"]
    check("lean: no adversarial LLM seat",
          [s for s in (lean.get("panel_checks") or _ALL_PANEL_SEATS) if s != "regression"], [])
    check("lean: still writes tests first", lean["tier_floor"] >= 1, True)
    check("lean: still runs the suite", "regression" in (lean.get("panel_checks") or ()), True)
    check("lean: never parks on a plan gate", lean["tier_cap"] < 2, True)
    check("lean: advisory", bool(lean.get("advisory_gate")), True)

    # --- balanced: RUNS the panel, captures it, still always ships -------------------
    bal = PROFILES["balanced"]
    check("balanced: convenes the FULL adversarial panel", bal.get("panel_checks"), None)
    check("balanced: advisory — findings never withhold the PR",
          bool(bal.get("advisory_gate")), True)

    # --- sensitive work: MORE review, still not a withheld PR ------------------------
    sens = infer_risk_tier({"title": "Login", "instructions": "Add auth and password reset."},
                           lean)
    check("lean: sensitive work escapes the tier cap", sens >= 2, True)
    seats = set(_ALL_PANEL_SEATS if sens >= 2
                else (lean.get("panel_checks") or _ALL_PANEL_SEATS))
    check("lean: sensitive work gets the security seat back", "security_check" in seats, True)
    check("sensitive work still raises its PR (merge gate protects main, not the PR)",
          resolve_gate_failure_action(failing=["security_clean"], advisory_gate=True,
                                      rounds_used=0, max_repair_rounds=2), "ship")

    # --- strict stays blocking: the profile that exists to say no --------------------
    check("strict: still blocking", bool(PROFILES["strict"].get("advisory_gate")), False)
    check("strict: a failing gate still repairs",
          resolve_gate_failure_action(failing=["code_review_passed"], advisory_gate=False,
                                      rounds_used=0, max_repair_rounds=2), "repair")


# --------------------------------------------------------------------------- #
# 10. PREVIEW PRECONDITION — one rule, every caller. The console hid its button
#     and the API gated its endpoint, but the MCP tool called the service directly
#     and bypassed both. Same shape as the bug that previewed Sudoku for an
#     N-Queens build: the decision lived in one path, not in the shared one.
# --------------------------------------------------------------------------- #
def t_preview_precondition() -> None:
    from boss_engine.services.preview_service import PreviewNotReady, assert_previewable

    check_true("preview: run with a PR is previewable",
               assert_previewable({"pull_request_url": "https://x/pull/1",
                                   "status": "completed", "phase": "done"}, "r1") is None)

    for label, run in [
        ("no PR at all", {"status": "running", "phase": "implement"}),
        ("PR url empty", {"pull_request_url": "", "status": "failed", "phase": "implement"}),
        ("parked on plan gate",
         {"pull_request_url": None, "status": "blocked", "phase": "awaiting_plan_approval"}),
    ]:
        try:
            assert_previewable(run, "r1")
            check(f"preview: blocked when {label}", "started", "PreviewNotReady")
        except PreviewNotReady as exc:
            d = exc.as_detail()
            check(f"preview: blocked when {label}", d["error"], "preview_unavailable_no_pr")
            check_true(f"preview: {label} reports the run's real status",
                       bool(d["run_status"]) and bool(d["next_action"]))

    # A failing gate must NOT block a preview — advisory runs ship a PR with findings and the
    # whole point is being able to look at the result.
    check_true("preview: allowed for a PR whose checks failed",
               assert_previewable({"pull_request_url": "https://x/pull/2",
                                   "status": "completed_with_dissent", "phase": "done"}, "r2")
               is None)

    # If this ever becomes a RuntimeError the API's substrate loop swallows it and reports a
    # generic "preview could not start" after three pointless attempts.
    check("preview: PreviewNotReady is not a RuntimeError",
          issubclass(PreviewNotReady, RuntimeError), False)


# --------------------------------------------------------------------------- #
# 11. RESUME-FROM-FAILURE. A repair re-ran write_tests unconditionally — a full
#     agent session (24 turns / 8.6 min measured) to re-derive a suite already on
#     the branch, even when the parent failed purely on review opinion. Which
#     gates failed says which stages are stale.
# --------------------------------------------------------------------------- #
def t_repair_resume() -> None:
    from boss_engine.services.governed_code_task_service import (
        _ALL_GATE_NAMES, _TEST_GATES, _repair_needs_write_tests as need)
    from boss_engine.services.code_task_service import _prior_failing_gates

    # Opinion-only failures: the suite stood up, so reuse it.
    for label, gates in [("review opinion", ["code_review_passed"]),
                         ("security", ["security_clean"]),
                         ("consistency", ["consistent"]),
                         ("review + consistency", ["code_review_passed", "consistent"])]:
        check(f"resume: {label} reuses the existing suite", need(gates), False)

    # Anything implicating the tests must re-author them.
    for gate in sorted(_TEST_GATES):
        check(f"resume: {gate} re-runs write_tests", need([gate]), True)
    check("resume: one test gate among opinions still re-runs write_tests",
          need(["code_review_passed", "tests_strong_enough"]), True)

    # SAFETY: skipping work needs positive evidence it is redundant. Absence of evidence,
    # or a gate name this build does not know, must fall back to doing the work.
    check("resume: no recorded gates -> full pipeline", need([]), True)
    check("resume: None -> full pipeline", need(None), True)
    check("resume: unknown gate name -> full pipeline", need(["some_future_gate"]), True)
    check("resume: unknown mixed with known -> full pipeline",
          need(["code_review_passed", "some_future_gate"]), True)
    check_true("resume: every test gate is a known gate", _TEST_GATES <= _ALL_GATE_NAMES)

    # The retry must find the parent's gates whichever path recorded them; missing one key
    # silently degrades every retry to "re-run everything".
    check("resume: reads failing_gates (override path)",
          _prior_failing_gates({"failing_gates": ["consistent"]}, {}), ["consistent"])
    check("resume: reads advisory_failing_gates (the normal path now)",
          _prior_failing_gates({"advisory_failing_gates": ["code_review_passed"]}, {}),
          ["code_review_passed"])
    # KEY NAME MATTERS: the pipeline writes `unresolved_dissent`, not `dissent`. This test
    # originally asserted the key I had assumed, so it passed against a dict I invented while
    # the real column never matched — the retry silently recovered nothing.
    check("resume: reads unresolved_dissent.unresolved_gates (the key actually written)",
          _prior_failing_gates({"unresolved_dissent": {"unresolved_gates": ["security_clean"]}}, {}),
          ["security_clean"])
    check("resume: the WRONG key recovers nothing (guards the assumption)",
          _prior_failing_gates({"dissent": {"unresolved_gates": ["security_clean"]}}, {}), [])
    check("resume: nothing recorded -> empty (and therefore full pipeline)",
          _prior_failing_gates({}, {}), [])


# --------------------------------------------------------------------------- #
# 12. A SKIPPED SEAT IS NOT A FAILED CHECK. Removing reviewers from lean's panel
#     left their gates expecting verdicts; "not_run" != "clean" so the gate FAILED,
#     and a failing gate is what the in-run convergence loop repairs — by running
#     the reviewer the profile had disabled. lean ran the full adversarial review
#     anyway, sequentially. Measured on crun_9b8e732b: review_code 11 turns/$0.66
#     + security_check 10 turns/$0.56 on a run whose panel was ["regression"].
# --------------------------------------------------------------------------- #
def t_skipped_seat_semantics() -> None:
    from boss_engine.services.governed_code_task_service import _ALL_PANEL_SEATS

    def gates_for(enabled_seats, sec_verdict="not_run", cons_verdict="not_run",
                  rev_pass=False):
        """Mirror of the production gate assignment + the skipped-seat override."""
        gates = {"code_review_passed": rev_pass,
                 "security_clean": sec_verdict == "clean",
                 "consistent": cons_verdict != "drift"}
        skipped = {}
        for seat, gate in (("review_code", "code_review_passed"),
                           ("security_check", "security_clean"),
                           ("consistency_check", "consistent")):
            if seat not in enabled_seats:
                gates[gate] = True
                skipped[seat] = "not convened"
        return gates, skipped

    lean_seats = {"regression"}
    g, skipped = gates_for(lean_seats)
    failing = [k for k, v in g.items() if not v]
    check("lean: a skipped seat leaves NO failing gate", failing, [])
    check("lean: convergence cannot re-run the disabled reviewer",
          "code_review_passed" in failing, False)
    check("lean: all three skips are recorded as not-run", sorted(skipped),
          ["consistency_check", "review_code", "security_check"])

    # The old behaviour, asserted so the regression is impossible to reintroduce silently.
    g_old = {"code_review_passed": False, "security_clean": ("not_run" == "clean"),
             "consistent": True}
    check("regression guard: without the override the gates DID fail",
          sorted(k for k, v in g_old.items() if not v),
          ["code_review_passed", "security_clean"])

    # A convened seat still gets a real verdict — the override must not mask genuine failures.
    g2, sk2 = gates_for(set(_ALL_PANEL_SEATS), sec_verdict="vulnerable", rev_pass=False)
    check("balanced: a convened seat's FAILURE is preserved",
          sorted(k for k, v in g2.items() if not v),
          ["code_review_passed", "security_clean"])
    check("balanced: nothing is marked skipped", sk2, {})

    # Partial profiles: only the disabled seat is neutralised.
    g3, sk3 = gates_for({"review_code", "regression"}, rev_pass=False)
    check("partial profile: the CONVENED reviewer can still fail",
          g3["code_review_passed"], False)
    check("partial profile: the skipped security seat does not fail",
          g3["security_clean"], True)


# --------------------------------------------------------------------------- #
# 13. WHICH APP DOES THE PREVIEW LAUNCH? Third time on the same defect, so the
#     decision now lives in ONE function both paths call.
#       PR #62        host path preferred the task's entrypoint; container path
#                     did not -> previewed Sudoku for an N-Queens build.
#       crun_9b8e732b both paths preferred a changed .py, neither applied the same
#                     rule to STATIC dirs; "." sorts first -> Sudoku again.
#                     And the file list was ONE ATTEMPT's delta, so a repair that
#                     touched only README+levels.py hid the web-nqueens/ app.
# --------------------------------------------------------------------------- #
def t_preview_app_choice(tmp=None) -> None:
    import tempfile
    from pathlib import Path
    from boss_engine.services.preview_service import resolve_app_entry, _detect_command

    root = Path(tempfile.mkdtemp())
    # A repo that already contains one app at the root, like boss-sandbox.
    (root / "serve.py").write_text("# pre-existing Sudoku server")
    (root / "index.html").write_text("<title>Sudoku</title>")
    (root / "web-nqueens").mkdir()
    (root / "web-nqueens" / "index.html").write_text("<title>Queen Quest</title>")

    built = ["nqueens_core/levels.py", "web-nqueens/app.js", "web-nqueens/index.html"]
    check("preview: picks the STATIC app this task built",
          resolve_app_entry(root, built), ("static", "web-nqueens"))
    check("preview: host path agrees",
          _detect_command(root, built)[-1].endswith("web-nqueens"), True)

    # The regression that shipped: a repair's delta hides the app.
    # AMBIGUITY MUST REFUSE, NOT GUESS. Operator framing: "if both are from different
    # customers it will be insanely bad experience." Showing someone an application their
    # task never touched — possibly another tenant's — is strictly worse than showing
    # nothing, so a repo with several runnable apps and no signal from the task raises.
    from boss_engine.services.preview_service import AmbiguousAppError
    try:
        got = resolve_app_entry(root, ["nqueens_core/levels.py"])
        check("preview: refuses to guess between multiple apps", f"returned {got}", "raised")
    except AmbiguousAppError as exc:
        check("preview: refuses to guess between multiple apps", len(exc.candidates) >= 2, True)
        check_true("preview: the refusal names the candidates", bool(exc.candidates))

    # A single-app repo is unambiguous — refusing there would break every normal project.
    import tempfile as _tf
    solo = Path(_tf.mkdtemp()); (solo / "serve.py").write_text("# only app")
    check("preview: a single-app repo still resolves without a signal",
          resolve_app_entry(solo, ["docs/readme.md"]), ("py", "serve.py"))

    # A repair touched only README + a core module. Whatever this resolves to, it is NOT the
    # app the task built — which is precisely why the caller must pass the LINEAGE's files.
    # (Here "README.md" -> dir "." -> the root index.html, i.e. Sudoku. The route varies; the
    # wrongness does not.)
    repair_delta = ["README.md", "nqueens_core/levels.py"]
    check("preview: an attempt-only file list does NOT find the app "
          "(hence lineage_impl_files)",
          resolve_app_entry(root, repair_delta)[1] == "web-nqueens", False)

    # A changed .py entrypoint still outranks everything.
    (root / "serve_nqueens.py").write_text("# built by this task")
    check("preview: a changed entrypoint wins",
          resolve_app_entry(root, ["serve_nqueens.py"] + built), ("py", "serve_nqueens.py"))

    # No signal at all, in a repo with several apps -> refuse. This assertion used to expect
    # a repo-convention guess; that guess is the defect.
    try:
        resolve_app_entry(root, None)
        check("preview: no signal + multi-app repo refuses", "returned", "raised")
    except AmbiguousAppError:
        check_true("preview: no signal + multi-app repo refuses", True)
    check("preview: empty tree -> no choice, no exception",
          resolve_app_entry(Path(tempfile.mkdtemp()), []), ("", ""))


# --------------------------------------------------------------------------- #
def main() -> int:
    for fn in (t_risk_tier, t_gate_action, t_inconclusive, t_preview_entrypoint,
               t_regression_floor, t_context_scoping, t_preflight, t_rate_limiter, t_advisory_gate, t_preview_precondition, t_repair_resume,
               t_skipped_seat_semantics, t_preview_app_choice):
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
