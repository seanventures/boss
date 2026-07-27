"""BOSS-280 — cost-governance P0s against the DB: freeze_tier2 enforced at reserve(),
tier2_frozen surfaced in status, and settle-then-release reconciling real LLM spend."""
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.db import begin
from boss_engine.services.budget import BudgetGovernor
from boss_engine.services.code_task_service import _release
from boss_engine.services.core import make_id

PID = "boss-dogfood"


def _governor():
    return BudgetGovernor(get_settings())


def _fill_to(connection, gov, fraction):
    """Reserve+consume enough to push utilization to ~fraction of the daily cap."""
    status = gov.status_for_project(connection, PID)
    cap = status["cap_usd_micros"]
    amount = int(cap * fraction) - status["committed_usd_micros"]
    assert amount > 0, "cap already exceeded fixture assumption"
    res = gov.reserve(connection, PID, category="test_fill", amount_usd_micros=amount,
                      reason="fill", expires_in_minutes=5)
    assert res["status"] == "reserved", res
    return res["reservation_id"]


# --- freeze_tier2 enforcement -------------------------------------------------
def test_tier2_frozen_denies_high_risk_but_not_low(engine):
    with begin(engine) as c:
        gov = _governor()
        _fill_to(c, gov, 0.96)                       # past freeze_tier2 (0.95)
        status = gov.status_for_project(c, PID)
        assert status["tier2_frozen"] is True
        assert status["freeze_tier2_pct"] == 0.95

        # tier-2 work refused with a blocking decision
        denied = gov.reserve(c, PID, category="code_task", amount_usd_micros=1000,
                             reason="risky", expires_in_minutes=5, risk_tier=2)
        assert denied["status"] == "denied"
        assert denied["decision_id"]
        assert "Tier-2" in denied["message"]

        # low-risk work still fits in the remaining headroom
        ok = gov.reserve(c, PID, category="code_task", amount_usd_micros=1000,
                         reason="safe", expires_in_minutes=5, risk_tier=1)
        assert ok["status"] == "reserved"
        # and untiered work is unaffected
        ok2 = gov.reserve(c, PID, category="code_task", amount_usd_micros=1000,
                          reason="untiered", expires_in_minutes=5)
        assert ok2["status"] == "reserved"


def test_below_threshold_tier2_flows(engine):
    with begin(engine) as c:
        gov = _governor()
        status = gov.status_for_project(c, PID)
        assert status["tier2_frozen"] is False
        ok = gov.reserve(c, PID, category="code_task", amount_usd_micros=1000,
                         reason="risky but plenty of budget", expires_in_minutes=5, risk_tier=3)
        assert ok["status"] == "reserved"


# --- settle-then-release ledger reconciliation --------------------------------
def _insert_llm_call(connection, run_id, cost):
    connection.execute(
        text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
             "prompt_tokens, completion_tokens, total_tokens, cost_usd_micros, ok) "
             "VALUES (:id, :p, :r, 'implement', 'claude_code', 'm', 10, 10, 20, :c, true)"),
        {"id": make_id("llm"), "p": PID, "r": run_id, "c": cost})


def test_release_settles_real_spend_from_llm_calls(engine):
    with begin(engine) as c:
        gov = _governor()
        res = gov.reserve(c, PID, category="code_task", amount_usd_micros=500_000,
                          reason="run", expires_in_minutes=5)
        rid = res["reservation_id"]
        run_id = make_id("crun")
        _insert_llm_call(c, run_id, 120_000)
        _insert_llm_call(c, run_id, 80_000)

        _release(gov, c, PID, rid, "tests did not pass", run_id=run_id)

        # the reservation settled as CONSUMED with the true llm sum, not released
        row = c.execute(text("SELECT status FROM budget_reservations WHERE id = :id"),
                        {"id": rid}).scalar_one()
        assert row == "consumed"
        receipt = c.execute(
            text("SELECT actual_usd_micros, skill_or_model_id FROM cost_receipts "
                 "WHERE reservation_id = :id"), {"id": rid}).mappings().one()
        assert receipt["actual_usd_micros"] == 200_000
        assert receipt["skill_or_model_id"] == f"code_task:{run_id}"

        # and the settled spend now counts in the governor's ledger
        status = gov.status_for_project(c, PID)
        assert status["actual_usd_micros"] >= 200_000


def test_release_with_zero_spend_releases_cleanly(engine):
    with begin(engine) as c:
        gov = _governor()
        res = gov.reserve(c, PID, category="code_task", amount_usd_micros=100_000,
                          reason="run", expires_in_minutes=5)
        rid = res["reservation_id"]
        _release(gov, c, PID, rid, "denied before any model call", run_id=make_id("crun"))
        row = c.execute(text("SELECT status FROM budget_reservations WHERE id = :id"),
                        {"id": rid}).scalar_one()
        assert row == "released"


# --- BOSS-281: task board derived status --------------------------------------
def test_tasks_endpoint_derives_lifecycle_from_jobs(engine, client):
    from boss_engine.services.core import make_id as mk
    with begin(engine) as c:
        tid = mk("task")
        c.execute(text(
            "INSERT INTO tasks (id, project_id, title, status, priority, risk_tier, dedupe_key) "
            "VALUES (:id, :p, 'board task', 'open', 'medium', 1, :dk)"),
            {"id": tid, "p": PID, "dk": mk("dk")})
        c.execute(text(
            "INSERT INTO jobs (id, project_id, task_id, kind, status, payload) "
            "VALUES (:id, :p, :t, 'code.task', 'running', '{}'::jsonb)"),
            {"id": mk("job"), "p": PID, "t": tid})
    r = client.get("/v1/tasks", params={"project_id": PID, "limit": 50})
    assert r.status_code == 200
    mine = next(t for t in r.json()["items"] if t["id"] == tid)
    assert mine["status"] == "open"
    assert mine["derived_status"] == "in_progress"     # latest job drives the board column
