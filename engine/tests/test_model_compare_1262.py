"""BOSSAPP-1262: one prompt, one column per model, one receipt each.

Spend is reserved before the call and goes only through a tenant gateway the
test fakes. A failed column leaves the other receipts standing.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services.model_compare import (
    HOLD_USD_MICROS,
    UnconnectedTenantGateway,
    compare_prompt,
)

URL = os.environ["DATABASE_URL"]
ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("openai", "anthropic", "boto3", "litellm", "cohere", "vertexai")


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL)
    yield eng
    eng.dispose()


@pytest.fixture()
def project(engine):
    pid = "cmp_" + uuid.uuid4().hex[:12]
    with engine.begin() as db:
        db.execute(text("INSERT INTO orgs(id, name) VALUES(:p, :p)"), {"p": pid})
        db.execute(text("INSERT INTO workspaces(id, org_id, name) VALUES(:p, :p, :p)"), {"p": pid})
        db.execute(text(
            "INSERT INTO projects(id, workspace_id, name, slug) VALUES(:p, :p, :p, :p)"), {"p": pid})
        db.execute(text(
            "INSERT INTO budget_policies(id, scope_type, scope_id, daily_cap_usd_micros, "
            "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
            "VALUES(:p, 'project', :p, 100000000, 100000000, 0)"), {"p": pid})
    return pid


class ScriptedGateway:
    """A tenant gateway stand-in. It refuses to answer until the hold is active."""

    def __init__(self, engine, answers: dict, fail: dict | None = None):
        self.engine = engine
        self.answers = answers
        self.fail = fail or {}
        self.calls: list[dict] = []

    def complete(self, *, project_id: str, model: str, prompt: str, reservation_id: str) -> dict:
        row = self.engine.connect().execute(text(
            "SELECT status, amount_usd_micros FROM budget_reservations WHERE id = :id"),
            {"id": reservation_id}).mappings().one()
        assert row["status"] == "active"
        assert int(row["amount_usd_micros"]) == HOLD_USD_MICROS
        self.calls.append({"model": model, "prompt": prompt, "project_id": project_id,
                           "reservation_id": reservation_id})
        if model in self.fail:
            raise RuntimeError(self.fail[model])
        return dict(self.answers[model])


def _receipt(engine, receipt_id: str) -> dict:
    with engine.connect() as db:
        return dict(db.execute(text(
            "SELECT status, dedupe_key, error, result FROM receipts WHERE id = :id"),
            {"id": receipt_id}).mappings().one())


def _reservation(engine, receipt_id: str) -> str:
    with engine.connect() as db:
        return db.execute(text(
            "SELECT r.status FROM budget_reservations r "
            "JOIN receipts p ON p.effect_scope->>'budget_reservation_id' = r.id "
            "WHERE p.id = :id"), {"id": receipt_id}).scalar_one()


def test_each_column_reserves_then_spends_once_through_the_gateway(engine, project):
    gateway = ScriptedGateway(engine, {
        "filament": {"text": "the quiet part", "usd_micros": 1500},
        "brass": {"text": "the loud part", "usd_micros": 2400},
    })
    result = compare_prompt(engine, project_id=project, prompt="Name the quiet part.",
                            models=["filament", "brass"], gateway=gateway)
    assert [c["model"] for c in result["columns"]] == ["filament", "brass"]
    assert [c["status"] for c in result["columns"]] == ["succeeded", "succeeded"]
    assert result["columns"][0]["text"] == "the quiet part"
    assert result["columns"][0]["usd_micros"] == 1500
    assert result["columns"][1]["usd_micros"] == 2400
    assert gateway.calls[0]["prompt"] == "Name the quiet part."
    for column in result["columns"]:
        receipt = _receipt(engine, column["receipt_id"])
        assert receipt["status"] == "succeeded"
        assert receipt["dedupe_key"] == column["dedupe_key"]
        assert _reservation(engine, column["receipt_id"]) == "consumed"
    with engine.connect() as db:
        spent = db.execute(text(
            "SELECT actual_usd_micros FROM cost_receipts WHERE project_id = :p "
            "ORDER BY actual_usd_micros"), {"p": project}).scalars().all()
    assert spent == [1500, 2400]


def test_a_failed_column_leaves_the_other_receipt_standing(engine, project):
    gateway = ScriptedGateway(engine, {
        "filament": {"text": "still here", "usd_micros": 1500},
    }, fail={"oxide": "The oxide lane refused this prompt and did not spend."})
    result = compare_prompt(engine, project_id=project, prompt="Name the quiet part.",
                            models=["oxide", "filament"], gateway=gateway)
    failed, stood = result["columns"]
    assert failed["model"] == "oxide"
    assert failed["status"] == "failed"
    assert failed["error"] == "The oxide lane refused this prompt and did not spend."
    assert failed["text"] is None
    assert _receipt(engine, failed["receipt_id"])["status"] == "failed"
    assert _reservation(engine, failed["receipt_id"]) == "released"
    assert stood["status"] == "succeeded"
    assert stood["text"] == "still here"
    assert stood["receipt_id"]
    assert _receipt(engine, stood["receipt_id"])["status"] == "succeeded"
    assert _reservation(engine, stood["receipt_id"]) == "consumed"
    assert [call["model"] for call in gateway.calls] == ["oxide", "filament"]


def test_a_repeat_does_not_spend_again(engine, project):
    gateway = ScriptedGateway(engine, {
        "filament": {"text": "still here", "usd_micros": 1500},
    }, fail={"oxide": "The oxide lane refused this prompt and did not spend."})
    kwargs = dict(project_id=project, prompt="Name the quiet part.",
                  models=["oxide", "filament"], gateway=gateway)
    first = compare_prompt(engine, **kwargs)
    again = compare_prompt(engine, **kwargs)
    assert len(gateway.calls) == 2
    assert [c["receipt_id"] for c in again["columns"]] == [c["receipt_id"] for c in first["columns"]]
    assert [c["reused"] for c in again["columns"]] == [True, True]
    assert again["columns"][0]["status"] == "failed"
    assert again["columns"][0]["error"] == first["columns"][0]["error"]
    assert again["columns"][1]["status"] == "succeeded"
    with engine.connect() as db:
        costs = db.execute(text(
            "SELECT count(*) FROM cost_receipts WHERE project_id = :p"), {"p": project}).scalar_one()
        holds = db.execute(text(
            "SELECT count(*) FROM budget_reservations WHERE project_id = :p"), {"p": project}).scalar_one()
    assert costs == 1
    assert holds == 2


def test_an_unconnected_gateway_fails_every_column_without_a_vendor(engine, project):
    gateway = UnconnectedTenantGateway()
    result = compare_prompt(engine, project_id=project, prompt="Name the quiet part.",
                            models=["filament", "brass"], gateway=gateway)
    assert [c["status"] for c in result["columns"]] == ["failed", "failed"]
    assert all("not connected" in c["error"].lower() for c in result["columns"])
    assert all(c["receipt_id"] for c in result["columns"])
    with engine.connect() as db:
        costs = db.execute(text(
            "SELECT count(*) FROM cost_receipts WHERE project_id = :p"), {"p": project}).scalar_one()
    assert costs == 0


def test_one_model_is_refused_before_any_spend(engine, project):
    gateway = ScriptedGateway(engine, {})
    with pytest.raises(Exception, match="two"):
        compare_prompt(engine, project_id=project, prompt="Name the quiet part.",
                       models=["filament"], gateway=gateway)
    assert gateway.calls == []
    with engine.connect() as db:
        assert db.execute(text(
            "SELECT count(*) FROM receipts WHERE project_id = :p"), {"p": project}).scalar_one() == 0


def test_compare_does_not_import_a_provider_sdk():
    for relative in (
        "engine/boss_engine/services/model_compare.py",
        "apps/api/app/routers/gap_1262.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8").lower()
        for name in FORBIDDEN:
            assert f"import {name}" not in source
            assert f"from {name}" not in source


def test_the_route_spends_through_the_bound_gateway(engine, project, monkeypatch):
    from apps.api.app.routers import gap_1262

    monkeypatch.setenv("DATABASE_URL", URL)
    Settings()  # constructed so a later get_settings sees the test url if uncached
    gateway = ScriptedGateway(engine, {
        "filament": {"text": "lane a", "usd_micros": 100},
        "brass": {"text": "lane b", "usd_micros": 200},
    })
    gap_1262.bind_gateway(gateway)
    try:
        body = gap_1262.CompareBody(project_id=project, prompt="Name the quiet part.",
                                    models=["filament", "brass"])
        out = gap_1262.compare_models(body)
    finally:
        gap_1262.bind_gateway(None)
    assert [c["status"] for c in out["columns"]] == ["succeeded", "succeeded"]
    assert [call["model"] for call in gateway.calls] == ["filament", "brass"]
    assert gap_1262.router.routes
