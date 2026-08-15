"""Unit tests for scripts/qa/sealed_e2e_live.py (no live spend, no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "sealed_e2e_live.py"
BLOCKER_DOC = ROOT / "docs" / "operations" / "SEALED_E2E_BLOCKER.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("sealed_e2e_live", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def test_blocker_doc_names_exact_missing_flags():
    text = BLOCKER_DOC.read_text(encoding="utf-8")
    assert "BOSS_FACTORY_ALLOW_CREATE" in text
    assert "BOSS_QA_ALLOW_CANARIES" in text
    assert "BOSS-351" in text
    assert "b6d8ccc" in text
    assert "Not GA" in text or "not GA" in text
    assert "--env-template" in text
    assert "sealed-e2e-canary" in text
    assert "does not allow" in text or "was **not** started" in text


def test_canary_body_is_narrow_open_pr():
    body = gate.canary_create_body("boss-dogfood", owner="paramg", repo="boss-sandbox")
    assert body["title"] == "sealed-e2e-canary"
    assert body["open_pr"] is True
    assert body["simulated"] is False
    assert body["owner"] == "paramg"
    assert body["repo"] == "boss-sandbox"
    assert body["profile"] == "lean"
    assert "merge" in body["instructions"].lower()


def test_allow_create_from_env(monkeypatch):
    monkeypatch.delenv("BOSS_FACTORY_ALLOW_CREATE", raising=False)
    assert gate.allow_create_from_env() is False
    monkeypatch.setenv("BOSS_FACTORY_ALLOW_CREATE", "1")
    assert gate.allow_create_from_env() is True
    monkeypatch.setenv("BOSS_FACTORY_ALLOW_CREATE", "true")
    assert gate.allow_create_from_env() is True


def test_missing_live_flags_names_only(monkeypatch):
    for name in (
        "BOSS_FACTORY_ALLOW_CREATE",
        "BOSS_QA_ALLOW_CANARIES",
        "BOSS_API_KEY",
        "BOSS_FACTORY_OWNER",
        "GITHUB_OWNER",
        "BOSS_GITHUB_OWNER",
        "BOSS_TARGET_GITHUB_OWNER",
        "BOSS_FACTORY_REPO",
        "GITHUB_REPO",
        "BOSS_GITHUB_REPO",
        "BOSS_TARGET_GITHUB_REPO",
    ):
        monkeypatch.delenv(name, raising=False)
    missing = gate.missing_live_flags(require_api_key=True)
    assert "BOSS_FACTORY_ALLOW_CREATE" in missing
    assert "BOSS_API_KEY" in missing
    assert any("GITHUB_OWNER" in item for item in missing)
    assert any("GITHUB_REPO" in item for item in missing)
    assert gate.similar_absent_flags() == ["BOSS_QA_ALLOW_CANARIES"]


def test_github_target_aliases(monkeypatch):
    monkeypatch.delenv("BOSS_FACTORY_OWNER", raising=False)
    monkeypatch.delenv("BOSS_FACTORY_REPO", raising=False)
    monkeypatch.setenv("BOSS_TARGET_GITHUB_OWNER", "paramg")
    monkeypatch.setenv("BOSS_TARGET_GITHUB_REPO", "boss-sandbox")
    owner, repo, base = gate.resolve_github_target()
    assert (owner, repo, base) == ("paramg", "boss-sandbox", "main")


def test_decision_is_hold():
    assert gate.decision_is_hold(
        {"status": "pending", "default_action": "hold", "reason": "merge_decision_required"}
    )
    assert not gate.decision_is_hold({"status": "approved", "default_action": "hold"})
    assert not gate.decision_is_hold({"status": "pending", "default_action": "approve"})


def test_receipt_matches_canary():
    row = {
        "id": "rcpt_1",
        "effect_kind": "github.pull_request.create",
        "result": {"run_id": "crun_abc"},
    }
    assert gate.receipt_matches_canary(row, run_id="crun_abc")
    assert not gate.receipt_matches_canary(row, run_id="crun_other")
    assert not gate.receipt_matches_canary(
        {"effect_kind": "metatron.watch", "result": {"run_id": "crun_abc"}},
        run_id="crun_abc",
    )


def test_dry_run_main_exit_0(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("BOSS_FACTORY_ALLOW_CREATE", raising=False)
    out = tmp_path / "sealed.json"
    code = gate.main(["--dry-run", "--output", str(out), "--quiet"])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["gate"] == gate.GATE_NAME
    assert report["mode"] == "dry-run"
    assert report["ga"] is False
    assert report["merge_attempted"] is False
    assert report["summary"]["ok"] is True
    assert report["summary"]["proven_live"] is False
    assert report["plane"] == "BOSS-351"
    assert "BOSS_FACTORY_ALLOW_CREATE" in report["missing_live_flags"]
    err = capsys.readouterr()
    assert "BOSS_API_KEY=" not in err.out + err.err


def test_default_is_dry_run(monkeypatch, tmp_path):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    out = tmp_path / "default.json"
    code = gate.main(["--output", str(out), "--quiet"])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["mode"] == "dry-run"


def test_live_without_allow_create_does_not_post(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BOSS_API_KEY", "test-key-not-real")
    monkeypatch.delenv("BOSS_FACTORY_ALLOW_CREATE", raising=False)
    monkeypatch.setenv("BOSS_TARGET_GITHUB_OWNER", "paramg")
    monkeypatch.setenv("BOSS_TARGET_GITHUB_REPO", "boss-sandbox")
    posted: list[object] = []

    def boom(*_args, **_kwargs):
        posted.append(True)
        raise AssertionError("must not POST when allow-create is off")

    monkeypatch.setattr(gate.BossClient, "request", boom)
    out = tmp_path / "blocked.json"
    code = gate.main(["--live", "--output", str(out), "--quiet"])
    assert code == 2
    assert posted == []
    report = json.loads(out.read_text())
    assert report["mode"] == "live-blocked"
    assert report["summary"]["proven_live"] is False
    assert report["ga"] is False
    assert "BOSS_FACTORY_ALLOW_CREATE" in report["missing_live_flags"]
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "test-key-not-real" not in combined


def test_live_and_dry_run_mutually_exclusive(capsys):
    code = gate.main(["--live", "--dry-run"])
    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_redact_secrets():
    text = gate.redact_secrets("X-Boss-Api-Key: abc and abc", ["abc"])
    assert "abc" not in text
    assert "X-Boss-Api-Key(redacted)" in text


def test_script_has_no_env_template_and_only_code_task_post():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "--env-template" not in source
    assert 'request("POST", "/v1/code-tasks"' in source
    assert source.count('request("POST"') == 1
    assert "merge_attempted" in source
    assert "not ga" in source.lower()
