"""One decision for "is this call money?", one definition of "spent" (brec_85de2541).

Two faults behind "Govern says $8.47, the ledger says $749":

* `included_in_subscription` flipped inside one metered run: the implement stage FAILED (an
  exception, "Credit balance is too low"), the failure row carried no `billing` stamp, and the
  engine-name rule called a claude_code call a subscription call. Every other row of the run,
  on the same API key, was money. Now every recording site asks `included_spend.call_is_included`,
  and an adapter that knows its credential stamps it on failed rows too.
* Govern summed `cost_receipts`; get_costs summed `llm_calls` with subscription estimates in it.
  On Community the code-task pipeline's budget port is unmetered, so builds never settle a
  receipt. `included_spend.ledger_spend` is the one definition both read now.
"""
from __future__ import annotations

import ast
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from boss_engine.services import included_spend, llm_telemetry, model_routing

SERVICES = Path(included_spend.__file__).parent


# ── one decision ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("engine", ["claude_code", "codex", "ollama", "openai", "guest:codex",
                                    "claude_agent_sdk", ""])
@pytest.mark.parametrize("usage", [None, {}, {"billing": "metered"}, {"billing": "subscription"},
                                   {"billing": "local"}, {"model": "m"}])
def test_every_recording_site_asks_the_same_question(engine, usage):
    one = included_spend.call_is_included(engine, usage)
    assert llm_telemetry.engine_call_is_included(engine, usage) is one
    assert model_routing.turn_is_included(model_routing.ModelChoice(engine, ""), usage) is one


def test_the_stamp_is_the_credential_that_ran():
    assert included_spend.call_is_included("claude_code", {"billing": "metered"}) is False
    assert included_spend.call_is_included("claude_code", {"billing": "subscription"}) is True
    assert included_spend.call_is_included("openai", {"billing": "local"}) is True


#: Adapters that stamp the credential on their successful rows: each of their failed rows must
#: carry the same stamp, or a failure reads as the other credential (the run's flip).
STAMPING = {"code_agent.py": {"ClaudeCodeAgent", "PacedCodeAgent"},
            "codex_code_agent.py": None}


def test_an_adapter_that_knows_its_credential_stamps_its_failed_rows():
    missing = []
    for fname, classes in STAMPING.items():
        tree = ast.parse((SERVICES / fname).read_text())
        scopes = [n for n in tree.body if isinstance(n, ast.ClassDef)
                  and (classes is None or n.name in classes)]
        for scope in scopes:
            for node in ast.walk(scope):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") ==
                        "record_llm_call"):
                    continue
                kws = {k.arg: k.value for k in node.keywords}
                failed = isinstance(kws.get("ok"), ast.Constant) and kws["ok"].value is False
                if failed and "billing" not in kws:
                    missing.append(f"{fname}:{node.lineno} ({scope.name})")
    assert missing == []


def test_a_never_dispatched_claude_call_carries_the_agents_credential(monkeypatch):
    from boss_engine.services.code_agent import PacedCodeAgent
    from boss_engine.services.model_rate_limiter import RateLimitAcquireTimeout

    recorded = []
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call",
                        lambda **kw: recorded.append(kw))

    class _Inner:
        engine = "claude_code"
        _billing = "metered"
        settings = SimpleNamespace(claude_cli_model="claude-sonnet-5")

        def run_role(self, *a, **k):  # pragma: no cover — never reached: no slot
            return {}

    class _NoSlot:
        from contextlib import contextmanager

        @contextmanager
        def slot(self, provider):
            raise RateLimitAcquireTimeout("could not acquire a anthropic call slot within 600s")
            yield  # pragma: no cover

        def lane_state(self, provider):
            return {"in_flight": 1, "limit": 1, "backoff_remaining_seconds": 0,
                    "pacing_wait_seconds": 0}

    with pytest.raises(RateLimitAcquireTimeout):
        PacedCodeAgent(_Inner(), _NoSlot()).run_role("p", "/tmp")
    assert recorded[0]["billing"] == "metered"
    assert included_spend.call_is_included(
        "claude_code", {**recorded[0]["usage"], "billing": recorded[0]["billing"]}) is False


# ── one ledger (Postgres) ────────────────────────────────────────────────────────────────────

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
PROJECT = "p_ledger"


@pytest.fixture()
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from sqlalchemy import create_engine, text

    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_led', 'led') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_led', 'org_led', 'led') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_led', 'led', 'p-ledger') ON CONFLICT DO NOTHING"), {"p": PROJECT})
        for table in ("llm_calls", "cost_receipts"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": PROJECT})
    yield eng
    with eng.begin() as c:
        for table in ("llm_calls", "cost_receipts"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": PROJECT})


def _call(c, *, run=None, cost, included, at):
    from sqlalchemy import text

    c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, "
                   "cost_usd_micros, included_in_subscription, created_at) VALUES "
                   "(:i, :p, :r, 'implement', 'claude_code', :c, :inc, :at)"),
              {"i": f"llmc_{uuid4().hex[:12]}", "p": PROJECT, "r": run, "c": cost,
               "inc": included, "at": at})


def _receipt(c, *, sid, money=0, included=False, equivalent=None, at):
    from sqlalchemy import text

    c.execute(text("INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
                   "estimated_usd_micros, actual_usd_micros, equivalent_usd_micros, status, "
                   "created_at, included_in_subscription) VALUES (:i, :p, 'model', :s, :a, :a, "
                   ":e, 'actual', :at, :inc)"),
              {"i": f"cost_{uuid4().hex[:12]}", "p": PROJECT, "s": sid, "a": money,
               "e": equivalent, "at": at, "inc": included})


def test_a_failed_call_on_an_api_key_is_money_like_its_run(pg):
    llm_telemetry.set_llm_context(db_engine=pg, project_id=PROJECT, run_id="crun_led1",
                                  stage="implement")
    try:
        llm_telemetry.record_llm_call(engine_name="claude_code",
                                      usage={"model": "claude-sonnet-5", "billing": "metered"},
                                      cost_usd_micros=648_944)
        llm_telemetry.record_llm_call(engine_name="claude_code", usage={"model": "claude-sonnet-5"},
                                      ok=False, error="Credit balance is too low",
                                      billing="metered")
    finally:
        llm_telemetry.set_llm_context(run_id=None, stage=None, project_id=None)
    from sqlalchemy import text

    with pg.begin() as c:
        flags = {r[0] for r in c.execute(text(
            "SELECT included_in_subscription FROM llm_calls WHERE code_task_run_id = 'crun_led1'"))}
    assert flags == {False}


def test_govern_and_get_costs_say_one_spent_figure(pg):
    from boss_engine.services import govern_view
    from boss_engine.tools import build_default_registry

    now = datetime.now(timezone.utc)
    at = now - timedelta(hours=2)
    with pg.begin() as c:
        # a build on an API key that no receipt settled (Community: unmetered port)
        _call(c, run="crun_a", cost=600_000, included=False, at=at)
        _call(c, run="crun_a", cost=400_000, included=False, at=at)
        # a build call on a subscription: an estimate, never money
        _call(c, run="crun_a", cost=900_000, included=True, at=at)
        # a build the paid port settled in full: in the receipts, not counted twice
        _call(c, run="crun_b", cost=250_000, included=False, at=at)
        _receipt(c, sid="code_task:crun_b", money=250_000, at=at)
        # a chat turn receipted by the governor (no llm_calls row), and a receipted plan call
        _receipt(c, sid="model.agent_decision", money=70_000, at=at)
        _receipt(c, sid="anthropic", included=True, equivalent=30_000, at=at)

    with pg.begin() as c:
        ledger = included_spend.ledger_spend(c, PROJECT, now - timedelta(days=1))
        meter = govern_view.ledger_for(c, PROJECT, {"period_start": now - timedelta(days=1),
                                                    "resets_at": now + timedelta(hours=1)})
    assert ledger == meter
    assert ledger["under_cap_usd_micros"] == 320_000            # what the caps count
    assert ledger["not_under_cap_usd_micros"] == 1_000_000      # crun_a's money, no receipt
    assert ledger["money_usd_micros"] == 1_320_000
    assert ledger["included_usd_micros"] == 930_000             # estimate, in no money total

    ctx = SimpleNamespace(settings=SimpleNamespace(), begin=pg.begin)
    costs = build_default_registry().get("get_costs").handler(
        ctx, project_id=PROJECT, actor="ceo", args={"days": 1})
    assert costs["total_cost_usd"] == pytest.approx(1.32)
    assert costs["included_estimate_usd"] == pytest.approx(0.93)
    assert costs["not_under_a_cap_usd"] == pytest.approx(1.0)
    run_a = next(r for r in costs["runs"] if r["run_id"] == "crun_a")
    assert run_a["cost_usd"] == pytest.approx(1.0) and run_a["included_estimate_usd"] == pytest.approx(0.9)
