"""Unit tests for pure helpers in scripts/qa/live_dogfood_gate.py (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "live_dogfood_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("live_dogfood_gate", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def test_redact_secrets_strips_api_key():
    secret = "super-secret-key-value"
    text = f"Authorization failed for {secret} in header"
    assert secret not in gate.redact_secrets(text, [secret])
    assert "***" in gate.redact_secrets(text, [secret])


def test_join_url_handles_slashes():
    assert gate.join_url("https://api.example/", "/v1/health") == "https://api.example/v1/health"
    assert gate.join_url("https://api.example", "v1/health") == "https://api.example/v1/health"


def test_item_count_from_items_and_lists():
    assert gate.item_count({"items": [1, 2, 3]}) == 3
    assert gate.item_count([1, 2]) == 2
    assert gate.item_count({"decisions": []}) == 0
    assert gate.item_count({"nope": 1}) is None


def test_extract_task_id_variants():
    assert gate.extract_task_id({"task_id": "t1"}) == "t1"
    assert gate.extract_task_id({"id": "t2"}) == "t2"
    assert gate.extract_task_id({"task": {"id": "t3"}}) == "t3"
    assert gate.extract_task_id("nope") is None


def test_evaluate_health_ok_and_db_down():
    ok, detail = gate.evaluate_health(
        {"status": "ok", "database_connected": True}, 200
    )
    assert ok is True
    assert "ok" in detail

    ok, detail = gate.evaluate_health(
        {"status": "ok", "database_connected": False}, 200
    )
    assert ok is False
    assert "database" in detail.lower()

    ok, _ = gate.evaluate_health({"status": "ok"}, 500)
    assert ok is False


def test_evaluate_unauth():
    assert gate.evaluate_unauth(401)[0] is True
    assert gate.evaluate_unauth(403)[0] is True
    assert gate.evaluate_unauth(200)[0] is False


def test_evaluate_auth_projects_requires_dogfood():
    payload = [{"id": "boss-dogfood", "name": "Dogfood"}]
    ok, detail = gate.evaluate_auth_projects(payload, 200, "boss-dogfood")
    assert ok is True
    assert "boss-dogfood" in detail

    ok, _ = gate.evaluate_auth_projects([{"id": "other"}], 200, "boss-dogfood")
    assert ok is False

    ok, _ = gate.evaluate_auth_projects({"items": [{"id": "boss-dogfood"}]}, 200, "boss-dogfood")
    assert ok is True


def test_evaluate_list_ok_404_and_empty():
    ok, detail, evidence = gate.evaluate_list_ok({"items": []}, 200, label="providers")
    assert ok is True
    assert evidence["count"] == 0

    ok, detail, _ = gate.evaluate_list_ok({"items": []}, 404, label="providers")
    assert ok is False
    assert "404" in detail


def test_evaluate_budget_quote():
    ok, detail, evidence = gate.evaluate_budget_quote(
        {
            "allowed": True,
            "budget_state": "green",
            "requested_usd_micros": 50_000,
            "available_usd_micros": 1_000_000,
        },
        200,
    )
    assert ok is True
    assert evidence["allowed"] is True
    assert "green" in detail

    # Denied quote is still a live budget path.
    ok, _, evidence = gate.evaluate_budget_quote(
        {"allowed": False, "budget_state": "red", "requested_usd_micros": 1},
        200,
    )
    assert ok is True
    assert evidence["allowed"] is False

    ok, _, _ = gate.evaluate_budget_quote({"nope": 1}, 200)
    assert ok is False


def test_evaluate_command_propose_and_task():
    ok, detail, evidence = gate.evaluate_command_propose(
        {"status": "accepted", "task_id": "task_abc", "job_id": "job_1"},
        201,
    )
    assert ok is True
    assert evidence["task_id"] == "task_abc"

    ok, _, _ = gate.evaluate_command_propose({"status": "accepted"}, 201)
    assert ok is False

    ok, detail, evidence = gate.evaluate_task_exists(
        {"id": "task_abc", "title": "x"}, 200, "task_abc"
    )
    assert ok is True
    assert evidence["task_id"] == "task_abc"

    ok, _, _ = gate.evaluate_task_exists(
        {"items": [{"id": "task_abc"}]}, 200, "task_abc"
    )
    assert ok is True

    ok, _, _ = gate.evaluate_task_exists({"id": "other"}, 200, "task_abc")
    assert ok is False


def test_evaluate_metatron():
    ok, detail, evidence = gate.evaluate_metatron(
        {"answer": "Budget is green.", "references": []}, 200
    )
    assert ok is True
    assert evidence["has_answer"] is True
    assert "Budget" in detail

    ok, _, _ = gate.evaluate_metatron({"project_id": "boss-dogfood"}, 200)
    assert ok is True

    ok, _, _ = gate.evaluate_metatron({}, 500)
    assert ok is False


def test_summarize_and_exit_code():
    checks = [
        gate.CheckResult(name="a", ok=True, required=True),
        gate.CheckResult(name="b", ok=False, required=True, detail="boom"),
        gate.CheckResult(name="c", ok=False, required=False, detail="soft"),
    ]
    summary = gate.summarize_report(checks)
    assert summary["passed"] is False
    assert summary["required_failed"] == 1
    assert summary["failed_required_names"] == ["b"]
    assert summary["optional_failed"] == 1

    report = gate.build_report(
        api_url="https://example",
        project_id="boss-dogfood",
        checks=checks,
        started_at="t0",
        finished_at="t1",
        duration_ms=12,
    )
    assert report["gate"] == gate.GATE_NAME
    assert report["plane"] == "BOSS-351"
    assert gate.gate_exit_code(report) == 1

    all_ok = [
        gate.CheckResult(name="a", ok=True, required=True),
        gate.CheckResult(name="c", ok=False, required=False),
    ]
    report2 = gate.build_report(
        api_url="https://example",
        project_id="boss-dogfood",
        checks=all_ok,
        started_at="t0",
        finished_at="t1",
        duration_ms=1,
    )
    assert gate.gate_exit_code(report2) == 0


def test_build_gate_command_is_self_identifying():
    cmd = gate.build_gate_command("boss-dogfood", "abc123")
    assert gate.COMMAND_PREFIX in cmd
    assert "boss-dogfood" in cmd
    assert "abc123" in cmd


def test_parse_args_defaults(monkeypatch):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("BOSS_API_URL", raising=False)
    monkeypatch.delenv("BOSS_PROJECT_ID", raising=False)
    args = gate.parse_args(["--api-key", "k"])
    assert args.api_url == gate.DEFAULT_API_URL
    assert args.project_id == gate.DEFAULT_PROJECT_ID
    assert args.api_key == "k"
    assert args.skip_metatron is False


def test_main_missing_api_key_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    code = gate.main(["--api-url", "http://example.invalid"])
    assert code == 2
    err = capsys.readouterr().err
    assert "BOSS_API_KEY" in err
