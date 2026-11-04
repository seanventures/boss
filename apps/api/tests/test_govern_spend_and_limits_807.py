"""Govern › Spend and Govern › Limits (BOSSAPP-807): the reads and writes behind the two screens.

* GET /v1/govern/spend says spend BY PROJECT across the projects the viewer may read, by model
  across them too, and the calls behind the money; `window=month` is this calendar month.
* GET /v1/govern carries the at-limit choice; its meter's periods carry a forecast.
* POST /v1/govern/limits saves the month/day/burst caps and the at-limit choice in one write,
  keeps the old fields of every response, and refuses a stale version (412).
* A project's monthly limit is a project budget envelope (POST /v1/govern/project-limit) that
  BudgetGovernor.reserve enforces like a cap; "No limit" closes it, deleting nothing.
* The at-limit choice is enforced: pause (a decision, as before), cheaper (work goes on past
  the cap), stop (refused, even $0 work, nobody asked).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import build_budget, spend_limits
from boss_engine.services.budget import BudgetGovernor

PROJECT = "boss-dogfood"
OTHER = "p807-other"


@pytest.fixture
def clean(engine):
    with engine.begin() as c:
        policy = c.execute(text(
            "SELECT daily_cap_usd_micros, monthly_cap_usd_micros, burst_cap_usd_micros_per_min "
            "FROM budget_policies WHERE scope_type = 'project' AND scope_id = :p"),
            {"p": PROJECT}).mappings().first()

    def wipe():
        with engine.begin() as c:
            for pid in (PROJECT, OTHER):
                c.execute(text("DELETE FROM cost_receipts WHERE project_id = :p"), {"p": pid})
                c.execute(text("DELETE FROM llm_calls WHERE project_id = :p"), {"p": pid})
                c.execute(text("DELETE FROM budget_reservations WHERE project_id = :p"), {"p": pid})
                c.execute(text("UPDATE budget_allocations SET status = 'closed' "
                               "WHERE project_id = :p AND scope_type = 'project'"), {"p": pid})
            c.execute(text("DELETE FROM project_settings WHERE project_id = :p AND key = :k"),
                      {"p": PROJECT, "k": spend_limits.AT_LIMIT_KNOB})
            c.execute(text(
                "UPDATE budget_policies SET daily_cap_usd_micros = :d, monthly_cap_usd_micros = :m, "
                "burst_cap_usd_micros_per_min = :b WHERE scope_type = 'project' AND scope_id = :p"),
                {"d": policy["daily_cap_usd_micros"], "m": policy["monthly_cap_usd_micros"],
                 "b": policy["burst_cap_usd_micros_per_min"], "p": PROJECT})
    wipe()
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) SELECT :o, workspace_id, "
            "'Other app', :o FROM projects WHERE id = :p ON CONFLICT (id) DO NOTHING"),
            {"o": OTHER, "p": PROJECT})
    yield
    wipe()


def _receipt(c, project: str, amount: int, *, included: bool = False) -> None:
    c.execute(text(
        "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
        "estimated_usd_micros, actual_usd_micros, equivalent_usd_micros, status, created_at, "
        "included_in_subscription) VALUES (:i, :p, 'model', 'claude-sonnet', :a, :a, :a, "
        "'actual', :at, :inc)"),
        {"i": f"cost_{uuid4().hex[:12]}", "p": project, "a": amount,
         "at": datetime.now(timezone.utc), "inc": included})


def _call(c, project: str, model: str, cost: int, *, included: bool = False) -> None:
    c.execute(text(
        "INSERT INTO llm_calls (id, project_id, stage, engine, model, cost_usd_micros, "
        "included_in_subscription) VALUES (:i, :p, 'implement', 'native', :m, :c, :inc)"),
        {"i": f"llm_{uuid4().hex[:12]}", "p": project, "m": model, "c": cost, "inc": included})


def test_spend_says_by_project_across_projects_and_keeps_every_old_field(client, engine, clean):
    with engine.begin() as c:
        _receipt(c, PROJECT, 3_000_000)
        _receipt(c, OTHER, 5_000_000)
        _receipt(c, OTHER, 9_000_000, included=True)
        _call(c, PROJECT, "claude-opus-4-1", 3_000_000)
        _call(c, OTHER, "gpt-4o", 5_000_000)
    body = client.get("/v1/govern/spend", params={"project_id": PROJECT, "window": "month"}).json()
    for old in ("window", "money_usd_micros", "included_usd_micros", "daily", "daily_cap_usd_micros",
                "scorecard", "savings", "top_drivers", "by_agent", "by_model", "spent_vs_cap"):
        assert old in body, old
    rows = {r["id"]: r for r in body["by_project"]}
    assert rows[OTHER]["money_usd_micros"] == 5_000_000
    assert rows[OTHER]["included_usd_micros"] == 9_000_000
    assert rows[OTHER]["name"] == "Other app"
    assert rows[PROJECT]["money_usd_micros"] == 3_000_000
    assert body["by_project"][0]["id"] == OTHER, "biggest first"
    assert {r["id"] for r in body["by_model_all"]} >= {"claude-opus-4-1", "gpt-4o"}
    assert [r["id"] for r in body["by_model"]] == ["claude-opus-4-1"], "by_model stays this project"
    assert body["window"] == "month" and body["money_usd_micros"] == 3_000_000
    assert client.get("/v1/govern/spend", params={"project_id": PROJECT, "window": "1y"}).status_code == 422


def test_the_limits_view_carries_the_choice_and_a_forecast(client, clean):
    body = client.get("/v1/govern", params={"project_id": PROJECT}).json()
    assert body["at_limit"]["value"] == "pause" and body["at_limit"]["source"] == "default"
    assert body["at_limit"]["choices"] == ["pause", "cheaper", "stop"]
    month = next(p for p in body["meter"]["periods"] if p["period"] == "month")
    assert month["forecast_usd_micros"] >= month["committed_usd_micros"]
    assert {c["key"] for c in body["controls"]} >= {"daily_cap", "monthly_cap", "burst", "pace"}


def test_one_save_writes_the_caps_and_the_choice(client, engine, clean):
    version = client.get("/v1/govern", params={"project_id": PROJECT}).json()["versions"]["budget"]
    saved = client.post("/v1/govern/limits", headers={"If-Match": f'"{version}"'}, json={
        "project_id": PROJECT, "actor": "ceo", "monthly_cap_usd": 1000, "daily_cap_usd": 100,
        "burst_usd_per_min": 2, "at_limit": "cheaper"})
    assert saved.status_code == 200, saved.text
    controls = {c["key"]: c for c in saved.json()["controls"]}
    assert controls["monthly_cap"]["value_effective"] == 1_000_000_000
    assert controls["daily_cap"]["value_effective"] == 100_000_000
    assert controls["burst"]["value_effective"] == 2_000_000
    assert saved.json()["at_limit"]["value"] == "cheaper"
    # The old version is stale now: a second save from the same screen is refused.
    stale = client.post("/v1/govern/limits", headers={"If-Match": f'"{version}"'},
                        json={"project_id": PROJECT, "actor": "ceo", "daily_cap_usd": 50})
    assert stale.status_code == 412
    bad = client.post("/v1/govern/limits", json={"project_id": PROJECT, "at_limit": "maybe"})
    assert bad.status_code == 422


def test_a_project_limit_is_an_envelope_and_refuses_like_a_cap(client, engine, clean):
    with engine.begin() as c:
        _receipt(c, PROJECT, 2_000_000)
    made = client.post("/v1/govern/project-limit",
                       json={"project_id": PROJECT, "actor": "ceo", "monthly_limit_usd": 3})
    assert made.status_code == 200, made.text
    assert made.json()["limit_usd_micros"] == 3_000_000
    assert made.json()["used_usd_micros"] == 2_000_000
    with engine.begin() as c:
        env = c.execute(text("SELECT scope_type, scope_id, status FROM budget_allocations "
                             "WHERE id = :i"), {"i": made.json()["envelope_id"]}).mappings().one()
    assert dict(env) == {"scope_type": "project", "scope_id": PROJECT, "status": "active"}
    # Changing it updates the same envelope; the list shows it beside the month's money.
    again = client.post("/v1/govern/project-limit",
                        json={"project_id": PROJECT, "actor": "ceo", "monthly_limit_usd": 2.5}).json()
    assert again["envelope_id"] == made.json()["envelope_id"]
    rows = {r["project_id"]: r for r in client.get(
        "/v1/govern/project-limits", params={"project_id": PROJECT}).json()["projects"]}
    assert rows[PROJECT]["limit_usd_micros"] == 2_500_000 and rows[PROJECT]["used_usd_micros"] == 2_000_000
    assert rows[OTHER]["limit_usd_micros"] is None
    governor = BudgetGovernor(api_main.settings)
    with engine.begin() as c:
        refused = governor.reserve(c, PROJECT, "model", 1_000_000, "project limit test", 60)
    assert refused["refusal"] == "cap_exceeded" and refused["decision_id"], refused
    assert "project's monthly" in refused["message"]
    # No limit: the envelope is closed (never deleted) and the same work fits.
    gone = client.post("/v1/govern/project-limit",
                       json={"project_id": PROJECT, "actor": "ceo", "monthly_limit_usd": None}).json()
    assert gone["limit_usd_micros"] is None
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM budget_allocations WHERE id = :i"),
                           {"i": made.json()["envelope_id"]}).scalar()
        fits = governor.reserve(c, PROJECT, "model", 1_000_000, "project limit test", 60)
    assert status == "closed" and fits["status"] == "reserved", fits


def _at_cap(engine) -> None:
    """Today's money reaches a $10 daily cap."""
    with engine.begin() as c:
        c.execute(text("UPDATE budget_policies SET daily_cap_usd_micros = 10000000, "
                       "monthly_cap_usd_micros = 1000000000, burst_cap_usd_micros_per_min = 0 "
                       "WHERE scope_type = 'project' AND scope_id = :p"), {"p": PROJECT})
        _receipt(c, PROJECT, 10_000_000)


def _choose(client, choice: str) -> None:
    r = client.post("/v1/govern/limits", json={"project_id": PROJECT, "actor": "ceo", "at_limit": choice})
    assert r.status_code == 200, r.text


def test_pause_asks_a_person_as_before(client, engine, clean):
    _at_cap(engine)
    with engine.begin() as c:
        refused = BudgetGovernor(api_main.settings).reserve(c, PROJECT, "model", 500_000, "t", 60)
        assert build_budget.cap_reached(BudgetGovernor(api_main.settings), c, PROJECT) is not None
    assert refused["refusal"] == "cap_exceeded" and refused["decision_id"]


def test_cheaper_goes_on_past_the_cap(client, engine, clean):
    _at_cap(engine)
    _choose(client, "cheaper")
    governor = BudgetGovernor(api_main.settings)
    with engine.begin() as c:
        went = governor.reserve(c, PROJECT, "model", 500_000, "t", 60)
        assert build_budget.cap_reached(governor, c, PROJECT) is None, "a running build goes on"
    assert went["status"] == "reserved", went
    assert went["budget_state"] == "red", "red: auto-lite routes build steps to low-cost models"


def test_stop_refuses_even_free_work_and_asks_nobody(client, engine, clean):
    _at_cap(engine)
    _choose(client, "stop")
    with engine.begin() as c:
        paid = BudgetGovernor(api_main.settings).reserve(c, PROJECT, "model", 500_000, "t", 60)
        free = BudgetGovernor(api_main.settings).reserve(c, PROJECT, "model", 0, "t", 60)
    for refused in (paid, free):
        assert refused["refusal"] == "cap_exceeded" and refused["decision_id"] is None, refused
        assert refused["at_limit"] == "stop" and refused["message"].startswith("Stopped at the limit")


def test_one_dataset_one_spent_figure_on_every_screen(client, engine, clean):
    """The walk found five figures for one project: the hero (settled receipts), By project (the
    ledger), Limits' "used" (receipts), By model (another project's calls) and the board (spent
    plus holds). One definition now: SPENT is the ledger; what a cap counts is spent + HELD."""
    with engine.begin() as c:
        for model, cost in (("claude-opus-4-8[1m]", 7_000_000), ("gpt-4o", 2_000_000)):
            _call(c, PROJECT, model, cost)
            _receipt(c, PROJECT, cost)
        c.execute(text("INSERT INTO llm_calls (id, project_id, stage, engine, model, cost_usd_micros, "
                       "included_in_subscription) VALUES (:i, :p, 'chat', 'claude_code', NULL, 0, true)"),
                  {"i": f"llm_{uuid4().hex[:12]}", "p": PROJECT})
        _receipt(c, PROJECT, 1_000_000)          # money no model call explains (e.g. a provider)
        _receipt(c, OTHER, 4_000_000)
        c.execute(text("INSERT INTO budget_reservations (id, project_id, category, amount_usd_micros, "
                       "status, reason, expires_at, created_at) VALUES (:i, :p, 'code_task', 5000000, "
                       "'active', 'held', now() + interval '1 hour', now())"),
                  {"i": f"res_{uuid4().hex[:10]}", "p": PROJECT})
    view = client.get("/v1/govern", params={"project_id": PROJECT}).json()
    periods = {p["period"]: p for p in view["meter"]["periods"]}
    month = client.get("/v1/govern/spend", params={"project_id": PROJECT, "window": "month"}).json()
    day = client.get("/v1/govern/spend", params={"project_id": PROJECT, "window": "day"}).json()
    week = client.get("/v1/govern/spend", params={"project_id": PROJECT, "window": "7d"}).json()
    limits = {r["project_id"]: r for r in client.get(
        "/v1/govern/project-limits", params={"project_id": PROJECT}).json()["projects"]}
    mine = lambda body: next(r for r in body["by_project"] if r["id"] == PROJECT)  # noqa: E731

    spent = 10_000_000
    assert periods["month"]["spent_usd_micros"] == month["money_usd_micros"] == spent
    assert mine(month)["money_usd_micros"] == limits[PROJECT]["used_usd_micros"] == spent
    assert sum(r["money_usd_micros"] for r in month["by_model_spent"]) == spent
    assert periods["day"]["spent_usd_micros"] == day["money_usd_micros"] == mine(day)["money_usd_micros"] == spent
    assert week["money_usd_micros"] == mine(week)["money_usd_micros"] == spent
    # What the cap counts (the board's meter, Setup's row) is spent + held, said apart.
    assert periods["month"]["held_usd_micros"] == 5_000_000
    assert periods["month"]["committed_usd_micros"] == spent + periods["month"]["held_usd_micros"]
    ids = [r["id"] for r in month["by_model_spent"]]
    assert "unknown" not in ids and "claude_code" in ids and "other" in ids
    assert next(r for r in month["by_model_spent"] if r["id"] == "other")["money_usd_micros"] == 1_000_000
    assert limits[OTHER]["used_usd_micros"] == next(
        r for r in month["by_project"] if r["id"] == OTHER)["money_usd_micros"] == 4_000_000
