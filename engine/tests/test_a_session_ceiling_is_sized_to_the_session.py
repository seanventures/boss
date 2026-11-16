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
    assert "_lifted_ceiling(r3, _session_budget_ceiling())" in block
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
