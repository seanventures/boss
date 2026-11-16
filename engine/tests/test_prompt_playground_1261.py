"""BOSSAPP-1261: a project prompt playground spends once through the tenant model gateway.

A run reserves budget before the gateway is called, writes one receipt, and a repeat of
the same prompt and model does not call again. Tests pass a fake gateway. The default
path calls the project's chat adapter (model_routing.project_chat_adapter), never a
provider SDK.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import prompt_playground as playground
from boss_engine.services.prompt_playground import (
    PlaygroundError,
    PlaygroundFailed,
    PlaygroundRefused,
)


def _settings():
    from boss_engine.config import Settings

    return Settings(_env_file=None)


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url.startswith("postgres") or "test" not in name:
        pytest.skip("needs DATABASE_URL naming a test database")
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def _project(engine, *, daily=100_000_000):
    pid = "p1261_" + uuid.uuid4().hex[:10]
    org = "org_" + pid
    ws = "ws_" + pid
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'Play')"), {"id": org})
        connection.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'Play')"),
            {"id": ws, "org": org},
        )
        connection.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, :ws, 'Play', :slug)"
            ),
            {"id": pid, "ws": ws, "slug": pid},
        )
        connection.execute(
            text(
                "INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
                "VALUES (:id, 'project', :p, :daily, :monthly, :burst)"
            ),
            {
                "id": "bp_" + pid,
                "p": pid,
                "daily": daily,
                "monthly": max(daily, 100_000_000),
                "burst": 100_000_000,
            },
        )
    return pid


def _rows(engine, pid):
    with engine.connect() as connection:
        receipts = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT status, result->>'output' AS output "
                    "FROM receipts WHERE project_id = :p ORDER BY created_at"
                ),
                {"p": pid},
            ).mappings()
        ]
        reservations = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT status FROM budget_reservations WHERE project_id = :p "
                    "ORDER BY created_at"
                ),
                {"p": pid},
            ).mappings()
        ]
        costs = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT actual_usd_micros FROM cost_receipts WHERE project_id = :p"
                ),
                {"p": pid},
            ).mappings()
        ]
    return receipts, reservations, costs


def test_a_blank_prompt_never_calls_the_gateway():
    def gateway(**_kwargs):
        raise AssertionError("gateway called")

    with pytest.raises(PlaygroundError, match="prompt"):
        playground.run(None, None, "p", prompt="  ", model="harbor-7b", gateway=gateway)


def test_a_run_reserves_then_calls_the_fake_gateway_and_writes_one_receipt(engine):
    pid = _project(engine)
    seen = {}

    def gateway(**kwargs):
        with engine.connect() as connection:
            receipt = dict(
                connection.execute(
                    text("SELECT status FROM receipts WHERE project_id = :p"),
                    {"p": pid},
                ).mappings().one()
            )
            reservation = dict(
                connection.execute(
                    text(
                        "SELECT status FROM budget_reservations WHERE project_id = :p"
                    ),
                    {"p": pid},
                ).mappings().one()
            )
        seen["during"] = (receipt["status"], reservation["status"], kwargs["prompt"], kwargs["model"])
        return {
            "output": "The harbor is quiet.",
            "usage": {
                "model": kwargs["model"],
                "cost_usd_micros": 40,
                "prompt_tokens": 5,
                "completion_tokens": 4,
            },
        }

    out = playground.run(
        engine,
        _settings(),
        pid,
        prompt="Describe the harbor.",
        model="harbor-7b",
        gateway=gateway,
    )
    assert seen["during"] == ("in_progress", "active", "Describe the harbor.", "harbor-7b")
    assert out["prompt"] == "Describe the harbor."
    assert out["model"] == "harbor-7b"
    assert out["output"] == "The harbor is quiet."
    assert out["receipt_id"].startswith("rcpt_")
    receipts, reservations, costs = _rows(engine, pid)
    assert receipts == [{"status": "succeeded", "output": "The harbor is quiet."}]
    assert reservations == [{"status": "consumed"}]
    assert costs == [{"actual_usd_micros": 40}]


def test_the_same_prompt_does_not_spend_twice(engine):
    pid = _project(engine)
    calls = {"n": 0}

    def gateway(**kwargs):
        calls["n"] += 1
        return {"output": "once", "usage": {"cost_usd_micros": 10, "model": kwargs["model"]}}

    first = playground.run(
        engine, _settings(), pid, prompt="Say once.", model="harbor-7b", gateway=gateway
    )
    second = playground.run(
        engine, _settings(), pid, prompt="Say once.", model="harbor-7b", gateway=gateway
    )
    assert first == second
    assert calls["n"] == 1
    receipts, reservations, costs = _rows(engine, pid)
    assert len(receipts) == 1 and receipts[0]["status"] == "succeeded"
    assert len(reservations) == 1 and reservations[0]["status"] == "consumed"
    assert len(costs) == 1


def test_a_gateway_failure_is_visible_and_a_repeat_does_not_call_again(engine):
    pid = _project(engine)
    calls = {"n": 0}

    def gateway(**_kwargs):
        calls["n"] += 1
        raise RuntimeError("gateway refused the model")

    with pytest.raises(PlaygroundFailed, match="gateway refused the model"):
        playground.run(
            engine, _settings(), pid, prompt="Break.", model="harbor-7b", gateway=gateway
        )
    with pytest.raises(PlaygroundFailed, match="gateway refused the model"):
        playground.run(
            engine, _settings(), pid, prompt="Break.", model="harbor-7b", gateway=gateway
        )
    assert calls["n"] == 1
    receipts, reservations, costs = _rows(engine, pid)
    assert receipts == [{"status": "failed", "output": None}]
    assert reservations == [{"status": "released"}]
    assert costs == []


def test_a_budget_refusal_does_not_call_the_gateway(engine):
    pid = _project(engine, daily=1)
    calls = {"n": 0}

    def gateway(**_kwargs):
        calls["n"] += 1
        return {"output": "no", "usage": {"cost_usd_micros": 1}}

    with pytest.raises(PlaygroundRefused):
        playground.run(
            engine, _settings(), pid, prompt="Too dear.", model="harbor-7b", gateway=gateway
        )
    assert calls["n"] == 0
    receipts, reservations, costs = _rows(engine, pid)
    assert receipts == [] and reservations == [] and costs == []


def test_the_default_path_calls_the_tenant_model_gateway(engine, monkeypatch):
    pid = _project(engine)
    calls = []

    class Adapter:
        def create_text_response(self, prompt, system_prompt=None, model=None):
            calls.append((prompt, model))
            return "prov_1", "from the gateway", {"model": model, "cost_usd_micros": 7}

    monkeypatch.setattr(
        "boss_engine.services.model_routing.project_chat_adapter",
        lambda *_args, **_kwargs: Adapter(),
    )
    out = playground.run(engine, _settings(), pid, prompt="Hello", model="harbor-7b")
    assert calls == [("Hello", "harbor-7b")]
    assert out["output"] == "from the gateway"
    assert out["receipt_id"].startswith("rcpt_")
    receipts, reservations, _costs = _rows(engine, pid)
    assert len(receipts) == 1 and receipts[0]["status"] == "succeeded"
    assert len(reservations) == 1 and reservations[0]["status"] == "consumed"


def test_the_module_does_not_call_a_provider_sdk():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "boss_engine" / "services" / "prompt_playground.py"
    ).read_text()
    assert "project_chat_adapter" in source
    for banned in ("OpenAI(", "Anthropic(", "chat.completions", "messages.create"):
        assert banned not in source
