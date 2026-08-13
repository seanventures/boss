"""Unit tests for scripts/qa/factory_e2e_gate.py (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "factory_e2e_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("factory_e2e_gate", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def test_join_url_is_2xx_extract_run_id():
    assert gate.join_url("https://x/", "v1/code-tasks") == "https://x/v1/code-tasks"
    assert gate.is_2xx(200)
    assert gate.is_2xx(201)
    assert not gate.is_2xx(422)
    assert gate.extract_run_id({"run_id": "r1", "status": "queued"}) == "r1"
    assert gate.extract_run_id({"id": "r2"}) == "r2"
    assert gate.extract_run_id({}) is None


def test_code_task_create_body_shape():
    body = gate.code_task_create_body(
        "boss-dogfood",
        owner="acme",
        repo="app",
        base_branch="develop",
        run_tag="t1",
    )
    assert body["project_id"] == "boss-dogfood"
    assert body["owner"] == "acme"
    assert body["repo"] == "app"
    assert body["base_branch"] == "develop"
    assert body["open_pr"] is False
    assert gate.COMMAND_PREFIX in body["title"]
    assert gate.COMMAND_PREFIX in body["instructions"]


def test_structural_endpoints_catalog():
    names = {e["name"] for e in gate.STRUCTURAL_ENDPOINTS}
    assert "health" in names
    assert "factory.wip" in names
    assert "code_tasks.list" in names
    assert "code_tasks.create" in names


def test_dry_run_structure_passes():
    checks = gate.validate_dry_run_structure(
        api_base="https://boss-api.example",
        project_id="boss-dogfood",
    )
    required = [c for c in checks if c.required]
    assert required
    assert all(c.ok for c in required)
    # env docs check present as optional
    docs = next(c for c in checks if c.name == "structure.live_env_docs")
    assert docs.ok
    assert "BOSS_API_KEY" in docs.detail


def test_dry_run_main_exit_0(monkeypatch, tmp_path):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    out = tmp_path / "factory-report.json"
    code = gate.main(["--dry-run", "--output", str(out), "--quiet"])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["gate"] == gate.GATE_NAME
    assert report["mode"] == "dry-run"
    assert report["summary"]["passed"] is True
    assert "env_for_full_live" in report
    assert any("BOSS_API_KEY" in x for x in report["env_for_full_live"]["required"])


def test_implicit_dry_run_without_key(monkeypatch, tmp_path, capsys):
    """Without API key, live invocation falls back to structural dry-run."""
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    out = tmp_path / "implicit.json"
    code = gate.main(["--output", str(out), "--quiet"])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["mode"] == "dry-run"
    err = capsys.readouterr().err
    assert "no BOSS_API_KEY" in err or report["summary"]["passed"]


def test_create_without_key_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    code = gate.main(["--create-code-task"])
    assert code == 2
    assert "BOSS_API_KEY" in capsys.readouterr().err


def test_allow_create_from_env(monkeypatch):
    monkeypatch.delenv("BOSS_FACTORY_ALLOW_CREATE", raising=False)
    assert gate.allow_create_from_env() is False
    monkeypatch.setenv("BOSS_FACTORY_ALLOW_CREATE", "1")
    assert gate.allow_create_from_env() is True


def test_resolve_factory_repo(monkeypatch):
    monkeypatch.setenv("BOSS_FACTORY_OWNER", "o1")
    monkeypatch.setenv("BOSS_FACTORY_REPO", "r1")
    monkeypatch.setenv("BOSS_FACTORY_BASE_BRANCH", "dev")
    owner, repo, base = gate.resolve_factory_repo()
    assert (owner, repo, base) == ("o1", "r1", "dev")


def test_gate_exit_code():
    fail = gate.build_report(
        api_url="u",
        project_id="p",
        checks=[gate.CheckResult(name="x", ok=False, required=True)],
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="live",
    )
    assert gate.gate_exit_code(fail) == 1
    ok = gate.build_report(
        api_url="u",
        project_id="p",
        checks=[gate.CheckResult(name="x", ok=True, required=True)],
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="dry-run",
    )
    assert gate.gate_exit_code(ok) == 0
