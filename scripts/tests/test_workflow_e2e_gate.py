"""Unit tests for scripts/qa/workflow_e2e_gate.py (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "workflow_e2e_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("workflow_e2e_gate", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def test_join_url_and_is_2xx():
    assert gate.join_url("https://api.example/", "/v1/tasks") == "https://api.example/v1/tasks"
    assert gate.is_2xx(200) is True
    assert gate.is_2xx(201) is True
    assert gate.is_2xx(299) is True
    assert gate.is_2xx(199) is False
    assert gate.is_2xx(404) is False
    assert gate.is_2xx(None) is False


def test_extract_task_id_and_item_count():
    assert gate.extract_task_id({"task_id": "t1"}) == "t1"
    assert gate.extract_task_id({"id": "t2"}) == "t2"
    assert gate.extract_task_id({"task": {"id": "t3"}}) == "t3"
    assert gate.extract_task_id("nope") is None
    assert gate.item_count({"items": [1, 2]}) == 2
    assert gate.item_count({"tasks": []}) == 0
    assert gate.item_count([1]) == 1


def test_resolve_env_precedence(monkeypatch):
    monkeypatch.setenv("BOSS_API_BASE", "https://base.example")
    monkeypatch.setenv("BOSS_API_URL", "https://url.example")
    assert gate.resolve_api_base(None) == "https://base.example"
    assert gate.resolve_api_base("https://cli.example") == "https://cli.example"

    monkeypatch.delenv("BOSS_API_BASE", raising=False)
    assert gate.resolve_api_base(None) == "https://url.example"

    monkeypatch.setenv("PROJECT_ID", "from-project-id")
    monkeypatch.setenv("BOSS_PROJECT_ID", "from-boss-project")
    assert gate.resolve_project_id(None) == "from-project-id"
    assert gate.resolve_project_id("cli-proj") == "cli-proj"


def test_command_and_budget_bodies():
    body = gate.command_propose_body("boss-dogfood", "abc123")
    assert body["project_id"] == "boss-dogfood"
    assert gate.COMMAND_PREFIX in body["command"]
    assert body["constraints"]["no_spend"] is True
    assert body["constraints"]["run_id"] == "abc123"

    q = gate.budget_quote_body("boss-dogfood")
    assert q["project_id"] == "boss-dogfood"
    assert q["amount_usd_micros"] == 50_000
    assert "category" in q


def test_dry_run_structure_passes():
    checks = gate.validate_dry_run_structure(
        api_base="https://boss-api.example",
        project_id="boss-dogfood",
    )
    assert checks
    assert all(c.ok for c in checks if c.required)
    names = {c.name for c in checks}
    assert "structure.step_order" in names
    assert "structure.command_body" in names
    assert "structure.budget_quote_body" in names


def test_dry_run_main_exit_0(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    out = tmp_path / "report.json"
    code = gate.main(["--dry-run", "--output", str(out), "--quiet"])
    assert code == 0
    assert out.is_file()
    report = json.loads(out.read_text())
    assert report["gate"] == gate.GATE_NAME
    assert report["mode"] == "dry-run"
    assert report["summary"]["passed"] is True
    assert report["critical_steps"] == list(gate.CRITICAL_STEP_NAMES)


def test_live_main_missing_key_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    code = gate.main(["--api-base", "http://example.invalid"])
    assert code == 2
    err = capsys.readouterr().err
    assert "BOSS_API_KEY" in err


def test_gate_exit_code_and_report():
    checks = [
        gate.CheckResult(name="health", ok=True, required=True),
        gate.CheckResult(name="command.propose", ok=False, required=True, detail="boom"),
    ]
    report = gate.build_report(
        api_url="https://example",
        project_id="boss-dogfood",
        checks=checks,
        started_at="t0",
        finished_at="t1",
        duration_ms=5,
        mode="live",
    )
    assert report["summary"]["passed"] is False
    assert gate.gate_exit_code(report) == 1

    ok_checks = [gate.CheckResult(name="a", ok=True, required=True)]
    report2 = gate.build_report(
        api_url="https://example",
        project_id="p",
        checks=ok_checks,
        started_at="t0",
        finished_at="t1",
        duration_ms=1,
        mode="dry-run",
    )
    assert gate.gate_exit_code(report2) == 0


def test_redact_secrets():
    secret = "sekrit-key-value"
    assert secret not in gate.redact_secrets(f"fail {secret}", [secret])
