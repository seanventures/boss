"""GET /v1/govern tells the truth about every control.

Each `value_effective` is computed by the helper the engine reads; every control names what
enforces it; only the token guides are Advisory; evidence is counted from recorded refusals;
and the Spend tab's money excludes subscription calls. Plus the previews: Pace's preview is
exactly what set_intensity writes, and a cap preview replays real spend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import govern_view
from boss_engine.services import project_settings as ps
from boss_engine.services.budget import BudgetGovernor, intensity_envelope

PROJECT = "boss-dogfood"
KNOB_KEYS = ("code_agent_max_concurrency", "code_agent_max_turns",
             "captain_leads_projects", "code_review_inrun_max_rounds")


@pytest.fixture
def clean(engine):
    def wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM project_settings WHERE project_id = :p AND key = ANY(:k)"),
                      {"p": PROJECT, "k": list(KNOB_KEYS)})
            c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
            c.execute(text("DELETE FROM budget_reservations WHERE project_id = :p"),
                      {"p": PROJECT})
            c.execute(text("DELETE FROM events WHERE project_id = :p AND event_type IN "
                           "('budget.refused', 'budget.reservation_denied', "
                           "'code_task.implement_continued')"), {"p": PROJECT})
            c.execute(text("UPDATE budget_policies SET operational_intensity = NULL "
                           "WHERE scope_type = 'project' AND scope_id = :p"), {"p": PROJECT})
    wipe()
    yield
    wipe()


def _receipt(c, amount: int, at: datetime, included: bool = False) -> None:
    c.execute(text(
        "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
        "estimated_usd_micros, actual_usd_micros, equivalent_usd_micros, status, created_at, "
        "included_in_subscription) VALUES (:i, :p, 'model', 'claude-sonnet', :a, :a, :a, "
        "'actual', :at, :inc)"),
        {"i": f"cost_{uuid4().hex[:12]}", "p": PROJECT, "a": amount, "at": at, "inc": included})


def _controls(client) -> dict[str, dict]:
    body = client.get("/v1/govern", params={"project_id": PROJECT}).json()
    return {c["key"]: c for c in body["controls"]}


def test_every_value_is_the_one_the_engine_uses(client, engine, clean):
    for stored, effective in ((1, 6), (3, 6), (6, 6), (10, 10)):
        with engine.begin() as c:
            ps.set_one(c, PROJECT, "code_agent_max_concurrency", stored, "ceo")
        control = _controls(client)["code_agent_max_concurrency"]
        assert control["value_effective"] == effective == ps.effective_review_lanes(stored)
        assert control["value_stored"] == stored and control["source"] == "set"
        assert (control["note"] or {}).get("floor") == (6 if stored < 6 else None)
    with engine.begin() as c:
        ps.set_one(c, PROJECT, "code_review_inrun_max_rounds", 0, "ceo")
        ps.set_one(c, PROJECT, "captain_leads_projects", 1, "ceo")
    controls = _controls(client)
    rounds = controls["code_review_inrun_max_rounds"]
    assert rounds["value_effective"] == 2 and rounds["note"] == {
        "kind": "reads_as", "stored": 0, "reads_as": 2}
    assert controls["captain_leads_projects"]["value_effective"] == 1
    turns = controls["code_agent_max_turns"]
    assert turns["value_effective"] == 90 and turns["source"] == "default"
    assert turns["bounds"] == {"min": 10, "max": 200, "step": 1}


def test_pace_and_its_caps_say_where_they_came_from(client, clean):
    controls = _controls(client)
    assert controls["pace"]["value_effective"] is None, "a fresh install's pace is Not set"
    assert controls["daily_cap"]["editable_by"] == "pace"
    assert controls["monthly_cap"]["write"] == {"method": "PUT", "path": "/v1/setup/budget"}
    client.post("/v1/budget/set_intensity", json={"scope_id": PROJECT, "intensity": 50,
                                                  "actor": "ceo"})
    controls = _controls(client)
    assert controls["pace"]["value_effective"] == 50
    assert controls["daily_cap"]["value_effective"] == 252_000_000
    assert controls["daily_cap"]["source"] == "pace"
    assert controls["max_workers_total"]["value_effective"] == 15
    assert controls["max_workers_total"]["source"] == "pace"


def test_every_control_names_its_enforcer_and_only_token_guides_are_advisory(client, clean):
    body = client.get("/v1/govern", params={"project_id": PROJECT}).json()
    for control in body["controls"]:
        assert control["key"] in govern_view.ENFORCED
        assert control["enforced"]["word"] == govern_view.ENFORCED[control["key"]][0]
        assert control["advisory"] is False
    assert body["token_guides"]["advisory"] is True
    assert body["token_guides"]["enforced"]["word"] == "Advisory"
    # Every int knob in the registry and every budget_policies limit has an entry.
    for key, spec in ps.KNOBS.items():
        if spec["kind"] == "int":
            assert key in govern_view.ENFORCED, key
    for limit in ("daily_cap", "monthly_cap", "burst", "max_concurrent_jobs",
                  "max_workers_total"):
        assert limit in govern_view.ENFORCED
    text_blob = str(body)
    assert "percent_complete" not in text_blob and "'eta'" not in text_blob
    assert "allowed_model_tiers" not in str(body["controls"]), "Govern claims no model tiers"


def test_evidence_is_counted_from_recorded_refusals(client, engine, clean):
    governor = BudgetGovernor(api_main.settings)
    with engine.begin() as c:
        _receipt(c, 9_900_000, datetime.now(timezone.utc))
        for _ in range(2):
            refused = governor.reserve(c, PROJECT, "model", 500_000, "evidence test", 60)
            assert refused["refusal"] == "cap_exceeded"
        c.execute(text("INSERT INTO events (id, project_id, event_type, actor, entity_type, "
                       "entity_id, data) VALUES (:i, :p, 'code_task.implement_continued', "
                       "'boss', 'code_task_run', 'crun_x', '{}'::jsonb)"),
                  {"i": f"evt_{uuid4().hex[:10]}", "p": PROJECT})
    controls = _controls(client)
    assert controls["daily_cap"]["evidence"]["count_7d"] == 2
    assert controls["daily_cap"]["evidence"]["word"] == "Stopped 2× · 7d"
    assert controls["monthly_cap"]["evidence"]["word"] == "Never stopped"
    assert controls["code_agent_max_turns"]["evidence"]["word"] == "Hit 1× · 7d"
    assert controls["code_agent_max_concurrency"]["evidence"] is None
    with engine.begin() as c:
        refusals = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p "
                                  "AND event_type = 'budget.refused' AND data->>'kind' = 'cap'"),
                             {"p": PROJECT}).scalar()
    assert refusals == 2


def test_spend_money_excludes_subscription_and_the_window_is_one(client, engine, clean):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        _receipt(c, 3_000_000, now - timedelta(hours=1))
        _receipt(c, 7_000_000, now - timedelta(hours=2), included=True)
        _receipt(c, 1_000_000, now - timedelta(days=3))
    day = client.get("/v1/govern/spend", params={"project_id": PROJECT, "window": "24h"}).json()
    assert day["money_usd_micros"] == 3_000_000
    assert day["included_usd_micros"] == 7_000_000
    week = client.get("/v1/govern/spend", params={"project_id": PROJECT, "window": "7d"}).json()
    assert week["money_usd_micros"] == 4_000_000
    assert len(week["daily"]) == 30 and week["daily"][-1]["money_usd_micros"] == 3_000_000
    assert week["daily"][-1]["included_usd_micros"] == 7_000_000
    assert "by_agent" not in week and "by_model" not in week, \
        "model spend by agent/model is Observability › Models' job"
    assert week["spent_vs_cap"]["state_label"] in govern_view.STATE_LABELS.values()


def test_pace_preview_is_exactly_what_set_intensity_writes(client, clean):
    for value in (0, 33, 34, 50, 66, 67, 100):
        preview = client.post("/v1/budget/intensity/preview",
                              json={"intensity": value}).json()["envelope"]
        written = client.post("/v1/budget/set_intensity",
                              json={"scope_id": PROJECT, "intensity": value, "actor": "ceo"}
                              ).json()
        for key, expected in preview.items():
            assert written[key] == expected, (value, key)
        assert preview == intensity_envelope(value)


def test_a_cap_preview_replays_real_spend(client, engine, clean):
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    spends = {1: 12, 2: 4, 3: 15, 5: 9, 8: 30}
    with engine.begin() as c:
        for days_ago, dollars in spends.items():
            _receipt(c, dollars * 1_000_000, today - timedelta(days=days_ago))
        _receipt(c, 50_000_000, today - timedelta(days=4), included=True)  # not money
    body = client.post("/v1/budget/cap/preview",
                       json={"project_id": PROJECT, "period": "day", "cap_usd": 10}).json()
    assert body["days_stopped"] == 3, body["stopped_days"]  # 12, 15, 30 are over $10
    assert body["not_enough_history"] is False and body["days_considered"] == 30
    none = client.post("/v1/budget/cap/preview",
                       json={"project_id": PROJECT, "period": "day", "cap_usd": 100}).json()
    assert none["days_stopped"] == 0
    with engine.begin() as c:
        c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": PROJECT})
        _receipt(c, 5_000_000, today - timedelta(days=1))
    thin = client.post("/v1/budget/cap/preview",
                       json={"project_id": PROJECT, "period": "month", "cap_usd": 1}).json()
    assert thin["not_enough_history"] is True


def test_a_month_cap_preview_counts_from_the_first(engine, clean):
    """D6: the replay's first month starts with what was spent before the window opened, as
    the governor counts it — not from zero at the window's first day."""
    now = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)  # window: 2026-09-21 .. today
    with engine.begin() as c:
        _receipt(c, 100_000_000, datetime(2026, 9, 5, 12, tzinfo=timezone.utc))   # before it
        _receipt(c, 10_000_000, datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
        _receipt(c, 10_000_000, datetime(2026, 10, 3, 12, tzinfo=timezone.utc))
        preview = govern_view.cap_preview(c, PROJECT, period="month", cap_usd_micros=50_000_000,
                                          now=now)
    # September was over $50 from the 5th: every September day in the window is stopped
    # (21st-30th = 10 days); October restarts at the 1st and stays under.
    assert preview["stopped_days"] == [f"2026-09-{d}" for d in range(21, 31)]
    assert preview["days_considered"] == 30
