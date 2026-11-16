"""A session's spend ceiling is sized to a session, and a session it stops is a partial attempt.

THE CASE (Northgate, northgate-gateway, 2026-11-16): every Claude CLI session of a build ran
under `--max-budget-usd 2.00`. The per-task MONEY budget (`code_task_budget_usd_micros`, $2) had
become a per-SESSION cap at LIST price, and on a subscription the run's money is $0, so the
ceiling never shrank and never grew: the implement session was killed (crun_24f39976), then the
acceptance check AND its escalation re-ask (crun_9a9a535c). `error_max_budget_usd` raised, so the
killed sessions' spend never reached `llm_calls`, every acceptance item read UNJUDGED with no
reason, and the escalation only raised the turns — the same $2 stopped it the same way.

What these tests hold, with no model (a fake `claude` binary and the scripted engine):
  A. the ceiling is the session budget (default $5, scaled by risk tier and repo size), bounded by
     what is left of the build's LIST-PRICE allowance — included spend counts;
  B. a session stopped at its ceiling returns its partial answer, its cost lands in llm_calls,
     and it reads as a partial attempt (resumable), never a crash;
  C. the acceptance record says which pass stopped and why;
  D. the escalation lifts the binding limit (a raised ceiling and smaller batches), and a re-ask
     under the same limit is refused.
"""
from __future__ import annotations

import inspect
import shutil
import textwrap
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.config import Settings, get_settings
from boss_engine.services import acceptance_gate as ag
from boss_engine.services import build_budget as bb
from boss_engine.services import engine_contract as ec
from boss_engine.services import governed_code_task_service as g
from boss_engine.services.llm_telemetry import set_llm_context

from .test_every_engine_keeps_the_same_contract import _bin, _rows, ledger  # noqa: F401

RUN = "crun_budget_ceiling"


# ── A. the ceiling ───────────────────────────────────────────────────────────────────────────

def test_the_defaults_size_a_session_not_a_task():
    s = Settings(_env_file=None)
    assert s.code_agent_session_budget_usd == 5.0
    assert s.code_agent_run_list_budget_usd == 60.0
    assert s.code_task_budget_usd_micros == 2_000_000   # the task's money budget is unchanged
    assert s.code_task_build_money_budget_usd == 15.0
    assert bb.build_money_budget_micros(s) == 15_000_000
    assert bb.session_budget_usd(s) == 5.0


@pytest.mark.parametrize("tier,files,want", [
    (1, 500, 5.0), (2, 500, 7.5), (3, 500, 10.0), (1, 2_500, 7.5), (1, 12_000, 10.0),
    (3, 12_000, 20.0), (None, None, 5.0)])
def test_the_session_budget_scales_with_risk_tier_and_repo_size(tier, files, want):
    s = Settings(_env_file=None)
    assert bb.session_budget_usd(s, risk_tier=tier, repo_files=files) == want


def test_a_subscription_runs_ceiling_counts_its_list_price_spend(ledger):  # noqa: F811
    """On a subscription the run's MONEY is $0; its list price is what the CLI counts."""
    with ledger.begin() as c:
        for i in range(3):
            c.execute(text("INSERT INTO llm_calls (id, code_task_run_id, cost_usd_micros, "
                           "included_in_subscription) VALUES (:i, :r, 19000000, true)"),
                      {"i": f"l{i}", "r": RUN})
    s = Settings(_env_file=None)
    with ledger.begin() as c:
        spent = bb.list_price_spend(c, RUN)
    assert spent == 57_000_000
    # $60 allowance - $57 list spent = $3 left: the ceiling is $3, not the session's $5 ...
    assert bb.session_ceiling_usd(s, list_spent_micros=spent) == 3.0
    # ... and a fresh run gets the full session budget.
    assert bb.session_ceiling_usd(s, list_spent_micros=0) == 5.0
    off = s.model_copy(update={"code_agent_run_list_budget_usd": 0.0})
    assert bb.session_ceiling_usd(off, list_spent_micros=spent) == 5.0
    none = off.model_copy(update={"code_agent_session_budget_usd": 0.0})
    assert bb.session_ceiling_usd(none, list_spent_micros=spent) is None


# ── B + C. a budget-stopped Claude session is a partial attempt, its spend kept ────────────────

FAKE_BUDGET_CLAUDE = textwrap.dedent('''\
    #!{python}
    import json, sys
    argv = sys.argv[1:]
    with open({log!r}, "a") as log:
        log.write(json.dumps({{"argv": argv}}) + "\\n")
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "sess-budget"}}),
          flush=True)
    print(json.dumps({{"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                      "result": "ITEM 1: MET — a.go", "session_id": "sess-budget",
                      "num_turns": 7, "total_cost_usd": 2.03,
                      "usage": {{"input_tokens": 900, "output_tokens": 120}}}}), flush=True)
    sys.exit(1)
''')


@pytest.fixture
def budget_claude(tmp_path, monkeypatch, ledger):  # noqa: F811
    from boss_engine.services.code_agent import ClaudeCodeAgent

    monkeypatch.setenv("BOSS_CODE_AGENT_IDLE_TIMEOUT_SECONDS", "0")
    fake = _bin(tmp_path, "claude", FAKE_BUDGET_CLAUDE)
    settings = get_settings().model_copy(update={"claude_cli_path": str(fake),
                                                 "claude_code_oauth_token": "test-only"})
    agent = ec.contracted(ClaudeCodeAgent(settings))
    agent.budget_ceiling = lambda: 2.0            # what Northgate's sessions ran under
    set_llm_context(run_id=RUN, stage=ag.SEAT)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.go").write_text("package a\n")
    return agent, tree, ledger, tmp_path


def test_a_session_stopped_at_its_ceiling_returns_its_partial_answer_and_its_cost(budget_claude):
    agent, tree, db, tmp_path = budget_claude
    r = agent.run_role("Judge the items.", tree, read_only=True)
    assert r["stop_reason"] == "budget_cap"
    assert r["budget_ceiling_usd"] == 2.0
    assert r["cost_usd_micros"] == 2_030_000
    assert "ITEM 1: MET" in r["summary"]
    assert g.check_is_inconclusive(r["summary"]), "a partial attempt, resumable — not a verdict"
    assert r["session_id"] == "sess-budget"
    assert ec.stop_words(r) == "stopped at the $2.00 session ceiling"
    rows = _rows(db, "SELECT cost_usd_micros, ok FROM llm_calls WHERE code_task_run_id = :r",
                 r=RUN)
    assert [row["cost_usd_micros"] for row in rows] == [2_030_000], "the spend is in the ledger"
    import json
    argv = json.loads((tmp_path / "fake.log").read_text().splitlines()[-1])["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "2.00"


def test_the_acceptance_record_names_the_ceiling_and_keeps_the_partial_answers(budget_claude):
    agent, tree, _db, _ = budget_claude
    items = ag.acceptance_items({"acceptance_criteria": ["first", "second"]})
    rec, cost = ag.run_check(items, ask=lambda p: agent.run_role(p, tree, read_only=True),
                             title="t", stack={}, root=tree)
    assert cost == 2_030_000
    assert rec["verdict"] == "not_run" and not ag.passed(rec)
    assert rec["met"] == [1] and rec["unjudged"] == [2], "the answered item is kept"
    assert "pass 1: stopped at the $2.00 session ceiling" in rec["why"]
    assert rec["stops"][0]["stop"] == "budget_cap" and rec["stops"][0]["ceiling_usd"] == 2.0
    assert "stopped at the $2.00 session ceiling" in ag.findings_summary(rec)


def test_a_pass_that_raised_says_its_failure_class():
    def boom(_prompt):
        raise RuntimeError("postgres://secret@host/db refused")

    items = ag.acceptance_items({"acceptance_criteria": ["first"]})
    rec, _ = ag.run_check(items, ask=boom, title="t", stack={}, root=None)
    assert rec["verdict"] == "not_run"
    assert "pass 1: failed (RuntimeError)" in rec["why"] and "secret" not in rec["why"]


# ── D. the escalation lifts the binding limit (scripted conformance engine) ──────────────────

def _scripted(tmp_path, ledger):  # noqa: F811
    from boss_engine.services import scripted_engine as se

    se.reset()
    root = tmp_path / "tree"
    shutil.copytree(se._DEFAULT_FIXTURE, root)  # noqa: SLF001
    raw = se.ScriptedEngine(get_settings(), faults={"acceptance_budget_cap"},
                            caps={"budget_limit"}, app_dir="")
    agent = ec.contracted(raw)
    agent.budget_ceiling = lambda: 2.0
    set_llm_context(run_id=RUN, stage=ag.SEAT)
    return se, agent, root


def _check(agent, root, items, **plan):
    ceiling = ({"max_budget_usd": plan["max_budget_usd"]}
               if plan.get("max_budget_usd") is not None else {})
    return ag.run_check(items, title="t", stack={}, root=root, batch_size=plan.get("batch_size"),
                        ask=lambda p: agent.run_role(p, root, read_only=True,
                                                     max_turns=plan.get("max_turns"), **ceiling))


def test_a_budget_stopped_acceptance_pass_is_re_asked_with_the_limit_lifted(tmp_path, ledger):  # noqa: F811
    se, agent, root = _scripted(tmp_path, ledger)
    items = ag.acceptance_items({"acceptance_criteria": [f"criterion {i}" for i in range(6)]})
    first, cost = _check(agent, root, items)
    assert first["verdict"] == "not_run" and first["unjudged"]
    assert "pass 1: stopped at the $2.00 session ceiling" in first["why"]
    assert cost == 2_000_000
    assert sum(r["cost_usd_micros"] for r in _rows(
        ledger, "SELECT cost_usd_micros FROM llm_calls WHERE code_task_run_id = :r",
        r=RUN)) == 2_000_000, "the stopped pass's spend is in the ledger"

    # Re-asking under the SAME ceiling (what the old escalation did: turns only) fails the same.
    same, _ = _check(agent, root, items, max_turns=64)
    assert same["verdict"] == "not_run" and "$2.00 session ceiling" in same["why"]

    plan = ag.escalation_plan(first, max_turns=64, default_ceiling_usd=2.0)
    assert plan == {"max_turns": 64, "max_budget_usd": 4.0, "batch_size": ag.MAX_ITEMS // 2}
    lifted, _ = _check(agent, root, items, **plan)
    assert lifted["verdict"] == "met" and ag.passed(lifted), lifted["why"]
    seen = [c["max_budget_usd"] for c in se.run_state(RUN).calls]
    assert seen[-1] == 4.0


def test_a_turn_capped_pass_keeps_the_turn_escalation():
    rec = {"stops": [{"pass": 1, "stop": "turn_cap", "words": "ran out of turns"}],
           "batch_size": ag.MAX_ITEMS}
    assert ag.escalation_plan(rec, max_turns=32) == {
        "max_turns": 32, "max_budget_usd": None, "batch_size": ag.MAX_ITEMS}


def test_a_re_ask_under_the_same_limit_is_refused():
    """A budget stop with no known ceiling and a batch of one: nothing can change — refused."""
    rec = {"stops": [{"pass": 1, "stop": "budget_cap", "ceiling_usd": None}], "batch_size": 1}
    assert ag.escalation_plan(rec, max_turns=32, default_ceiling_usd=None) is None
    # With a ceiling known it is raised, never repeated.
    rec["stops"][0]["ceiling_usd"] = 3.0
    assert ag.escalation_plan(rec, max_turns=32)["max_budget_usd"] == 6.0


def test_the_review_re_ask_lifts_a_ceiling_that_bound_it():
    assert g._lifted_ceiling({"stop_reason": "budget_cap", "budget_ceiling_usd": 2.0}, 5.0) == {
        "max_budget_usd": 4.0}
    assert g._lifted_ceiling({"stop_reason": "budget_cap"}, 5.0) == {"max_budget_usd": 10.0}
    assert g._lifted_ceiling({"stop_reason": "done", "budget_ceiling_usd": 2.0}, 5.0) == {}
    assert g._lifted_ceiling(None, 5.0) == {}


def test_the_pipeline_escalation_lifts_the_limit_and_refuses_a_same_limit_re_ask():
    src = inspect.getsource(g)
    block = src[src.index('evidence["review_escalated_turns"] = escalated') - 3000:]
    block = block[:block.index("gate_faults = classify_gate_faults")]
    assert "_lifted_ceiling(\n                            r3, _session_budget_ceiling(review_agent)," in block
    assert "clamp=lambda usd: _lift_ceiling(usd, review_agent)" in block
    assert "clamp=lambda usd: _lift_ceiling(usd, consistency_agent)" in block
    assert "max_turns=escalated, **_rev_ceiling" in block
    assert "_acceptance_gate.escalation_plan(" in block and "if _plan is None:" in block
    assert "acceptance_escalation_refused" in block


def test_every_claude_engine_treats_a_budget_stop_as_partial():
    from boss_engine.services import agent_sdk_engine, code_agent

    assert ec.partial_stop_reason("error_max_budget_usd") == "budget_cap"
    assert ec.partial_stop_reason("error_max_turns") == "turn_cap"
    assert "budget_cap" in ec.STOP_REASONS and "budget_cap" in ec.PARTIAL_STOPS
    assert "if data.get(\"subtype\") in PARTIAL_SUBTYPES:" in inspect.getsource(
        code_agent.ClaudeCodeAgent._invoke)
    assert "ec.PARTIAL_SUBTYPES" in inspect.getsource(agent_sdk_engine)
    r = ec.normalise_result({"summary": "half done", "stop_reason": "budget_cap"},
                            engine="any")
    assert ec.BUDGET_PARTIAL_MARKER in r["summary"] and g.check_is_inconclusive(r["summary"])


def test_the_standing_ceiling_outside_a_build_is_the_session_budget():
    from boss_engine.services.code_agent import _standing_session_ceiling

    assert _standing_session_ceiling(SimpleNamespace(code_agent_session_budget_usd=5.0))() == 5.0
    assert _standing_session_ceiling(SimpleNamespace(code_agent_session_budget_usd=0))() is None


# ── REVIEW #145: money-billed lanes, the spent allowance, compounding escalations ─────────────

class _Nested:
    def begin_nested(self):
        import contextlib
        return contextlib.nullcontext()


def _caps(monkeypatch, *, cap=10_000_000, actual=3_000_000, inflight=4_000_000, override=0):
    from boss_engine.services import agent_requests, spend_limits

    monkeypatch.setattr(bb, "inflight_money", lambda c, p: inflight)
    monkeypatch.setattr(spend_limits, "limit_period", lambda c, p, s: None)
    monkeypatch.setattr(spend_limits, "at_limit", lambda c, s, p: "pause")
    monkeypatch.setattr(agent_requests, "override_headroom", lambda c, p, r: override)
    return SimpleNamespace(settings=None, status_for_project=lambda c, p: {"periods": {
        "day": {"cap_usd_micros": cap, "actual_usd_micros": actual}}})


def test_the_money_headroom_is_cap_less_settled_less_in_flight(monkeypatch):
    budget = _caps(monkeypatch)
    assert bb.money_headroom(budget, _Nested(), "p") == 3_000_000
    budget = _caps(monkeypatch, override=2_000_000)
    assert bb.money_headroom(budget, _Nested(), "p", run_id="r") == 5_000_000
    budget = _caps(monkeypatch, actual=9_000_000)
    assert bb.money_headroom(budget, _Nested(), "p") == 0
    # cap_reached keeps its arithmetic on the same helper
    assert bb.cap_reached(budget, _Nested(), "p")["message"].startswith("Budget cap reached")
    budget = _caps(monkeypatch, cap=0)
    assert bb.money_headroom(budget, _Nested(), "p") is None


@pytest.mark.parametrize("headroom,money_spent,tier,files,want", [
    (3_000_000, 0, 3, 12_000, 3.0),            # a $20 session on a $10 cap: held to the $3 left
    (1_500_000, 0, 1, 500, 1.5),
    (None, 13_000_000, 1, 500, 2.0),           # the build's $15 money budget: $2 left
    (None, 0, 3, 12_000, 15.0),                # never more than the build's money budget
    (50_000_000, 0, 1, 500, 5.0),              # roomy cap: the session budget
])
def test_an_api_key_builds_session_never_exceeds_cap_headroom_or_its_money_budget(
        headroom, money_spent, tier, files, want):
    s = Settings(_env_file=None)
    got = bb.session_ceiling_usd(s, list_spent_micros=money_spent, money=True,
                                 build_money_spent_micros=money_spent,
                                 period_headroom_micros=headroom, risk_tier=tier,
                                 repo_files=files)
    assert got == want
    if headroom is not None:
        assert got <= headroom / 1e6
    assert got <= (bb.build_money_budget_micros(s) - money_spent) / 1e6
    # The same numbers on a subscription: only the list-price limits apply.
    sub = bb.session_ceiling_usd(s, list_spent_micros=money_spent, money=False,
                                 build_money_spent_micros=money_spent,
                                 period_headroom_micros=headroom, risk_tier=tier,
                                 repo_files=files)
    assert sub == bb.session_budget_usd(s, risk_tier=tier, repo_files=files)


def test_a_build_reserves_its_real_cost_within_the_caps_room(monkeypatch):
    from boss_engine.services import included_spend

    s = Settings(_env_file=None)
    monkeypatch.setattr(included_spend, "routed_reservation_estimate",
                        lambda c, st, p, amount: amount)            # an API-key project
    room = {"v": 4_000_000}
    monkeypatch.setattr(bb, "money_headroom", lambda *_a, **_k: room["v"])
    assert bb.reservation_estimate(_Nested(), s, "p") == 4_000_000   # what the cap allows
    room["v"] = None
    assert bb.reservation_estimate(_Nested(), s, "p") == 15_000_000  # the real build cost
    room["v"] = 0
    assert bb.reservation_estimate(_Nested(), s, "p") == 15_000_000  # no room: refused upstream
    assert bb.reservation_estimate(_Nested(), s, "p", 200_000) == 200_000
    monkeypatch.setattr(included_spend, "routed_reservation_estimate",
                        lambda c, st, p, amount: 0)                 # a subscription
    assert bb.reservation_estimate(_Nested(), s, "p") == 0


def test_a_spent_allowance_stops_the_build_in_its_own_words():
    s = Settings(_env_file=None)
    assert bb.allowance_reached(s, list_spent_micros=59_000_000) is None
    said = bb.allowance_reached(s, list_spent_micros=60_000_000)
    assert said and "used its $60.00 allowance" in said
    assert bb.allowance_reached(s, list_spent_micros=60_000_000,
                                override_micros=10_000_000) is None, "a person's override"
    assert bb.allowance_reached(s, list_spent_micros=1_000_000, money=False,
                                build_money_spent_micros=15_000_000) is None
    said = bb.allowance_reached(s, list_spent_micros=15_000_000, money=True,
                                build_money_spent_micros=15_000_000)
    assert said and "used its $15.00 money budget" in said
    src = inspect.getsource(g)
    block = src[src.index("def _stop_if_cap_reached"):src.index("def _stop_on_harness_failure")]
    assert "_build_budget.allowance_reached(" in block and "raise BuildBudgetStopped(" in block
    assert block.index("cap_reached(") < block.index("allowance_reached(")


def test_an_escalated_ceiling_is_clamped_and_never_compounds():
    s = Settings(_env_file=None)
    # x4 the session budget at most, and within every run-level limit
    assert bb.lifted_ceiling_usd(s, 80.0, list_spent_micros=0) == 20.0
    assert bb.lifted_ceiling_usd(s, 8.0, list_spent_micros=0) == 8.0
    assert bb.lifted_ceiling_usd(s, 8.0, list_spent_micros=55_000_000) == 5.0
    assert bb.lifted_ceiling_usd(s, 8.0, list_spent_micros=0, money=True,
                                 period_headroom_micros=3_000_000) == 3.0
    clamp = lambda usd: bb.lifted_ceiling_usd(s, usd, list_spent_micros=0)  # noqa: E731
    # Round after round: $5 -> $10 -> $20 -> held at $20 (only the batch can still shrink).
    seen, ceiling, batch = [], 5.0, ag.MAX_ITEMS
    for _ in range(6):
        rec = {"stops": [{"pass": 1, "stop": "budget_cap", "ceiling_usd": ceiling}],
               "batch_size": batch}
        plan = ag.escalation_plan(rec, max_turns=32, clamp=clamp)
        if plan is None:
            break
        ceiling, batch = plan["max_budget_usd"], plan["batch_size"]
        seen.append(ceiling)
    assert seen[:3] == [10.0, 20.0, 20.0] and max(seen) == 20.0
    assert plan is None, "once neither the ceiling nor the batch can change, refused"
    # The review re-ask: lifted within the clamp, nothing when no lift is left.
    assert g._lifted_ceiling({"stop_reason": "budget_cap", "budget_ceiling_usd": 20.0}, 5.0,
                             clamp=clamp) == {}
    assert g._lifted_ceiling({"stop_reason": "budget_cap", "budget_ceiling_usd": 6.0}, 5.0,
                             clamp=clamp) == {"max_budget_usd": 12.0}


def test_the_pipeline_bounds_money_lanes_by_the_caps():
    src = inspect.getsource(g)
    block = src[src.index("def _money_billed"):src.index("def _agent_for_locked")]
    assert "money_headroom(" in block and "unsettled_money(" in block
    assert "list_price_spend(" in block and "lifted_ceiling_usd(" in block
    assert "a.budget_ceiling = (lambda _a=a: _session_budget_ceiling(_a))" in src
