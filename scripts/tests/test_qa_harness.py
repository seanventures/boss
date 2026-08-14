"""Unit tests for scripts/qa/harness.py (no network, no spend)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "harness.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    spec = importlib.util.spec_from_file_location("qa_harness", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


harness = _load_module()


def _try_sibling_modules():
    """Prefer real sibling modules; skip with reason if they have not landed."""
    try:
        from scripts.qa import canary_apps, feature_sweep
    except ImportError as exc:
        pytest.skip(f"feature_sweep/canary_apps not landed yet: {exc}")
    return feature_sweep, canary_apps


class _StubSweep:
    @staticmethod
    def dry_run_catalog():
        return [{"name": "settings.edition"}]


class _StubCanaries:
    @staticmethod
    def dry_run_catalog():
        return [{"id": "bugbox"}, {"id": "hookwatch"}, {"id": "approveit"}]


def test_parse_args_defaults(monkeypatch):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("BOSS_API_URL", raising=False)
    monkeypatch.delenv("BOSS_API_BASE", raising=False)
    monkeypatch.delenv("BOSS_PROJECT_ID", raising=False)
    monkeypatch.delenv("BOSS_ACTOR", raising=False)
    args = harness.parse_args([])
    assert args.api_url == harness.DEFAULT_API_URL
    assert args.project_id == harness.DEFAULT_PROJECT_ID
    assert args.api_key is None
    assert args.actor == "executive_console"
    assert args.phases == "preflight,features,control,canaries"
    assert args.canary_mode == "catalog"
    assert args.dry_run is False
    assert args.timeout == 30.0
    assert args.output is None


def test_parse_args_env_aliases(monkeypatch):
    monkeypatch.setenv("BOSS_API_BASE", "https://base.example")
    monkeypatch.setenv("BOSS_API_URL", "https://url.example")
    monkeypatch.setenv("BOSS_PROJECT_ID", "from-env")
    monkeypatch.setenv("BOSS_ACTOR", "qa-actor")
    args = harness.parse_args([])
    assert args.api_url == "https://base.example"
    assert args.project_id == "from-env"
    assert args.actor == "qa-actor"

    monkeypatch.delenv("BOSS_API_BASE", raising=False)
    args = harness.parse_args([])
    assert args.api_url == "https://url.example"


def test_launch_mode_without_allow_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("BOSS_QA_ALLOW_CANARIES", raising=False)
    code = harness.main(["--canary-mode", "launch", "--dry-run"])
    assert code == 2
    err = capsys.readouterr().err
    assert "BOSS_QA_ALLOW_CANARIES" in err


def test_summarize_required_fail_logic():
    checks = [
        harness.CheckResult(name="health", ok=True, required=True),
        harness.CheckResult(name="features.edition", ok=False, required=True, detail="boom"),
        harness.CheckResult(name="optional.preview", ok=False, required=False),
    ]
    summary = harness.summarize(checks)
    assert summary["ok"] is False
    assert summary["failed"] == ["features.edition"]
    assert summary["required"] == 2
    assert summary["required_passed"] == 1
    assert summary["optional_passed"] == 0
    assert summary["total"] == 3

    optional_only_fail = harness.summarize(
        [
            harness.CheckResult(name="health", ok=True, required=True),
            harness.CheckResult(name="optional.preview", ok=False, required=False),
        ]
    )
    assert optional_only_fail["ok"] is True
    assert optional_only_fail["failed"] == []


def test_redact_secrets_strips_api_key():
    secret = "boss-live-api-key-value"
    text = f"X-Boss-Api-Key: {secret} failed"
    redacted = harness.redact_secrets(text, [secret])
    assert secret not in redacted
    assert "***" in redacted
    assert "X-Boss-Api-Key(redacted)" in redacted


def test_dry_run_with_stubbed_siblings(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_import_feature_sweep", lambda: _StubSweep)
    monkeypatch.setattr(harness, "_import_canaries", lambda: _StubCanaries)
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    out = tmp_path / "harness-stub.json"
    code = harness.main(["--dry-run", "--output", str(out)])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["gate"] == harness.GATE_NAME
    assert report["dry_run"] is True
    assert report["summary"]["ok"] is True
    assert report["catalog"]["features"] == [{"name": "settings.edition"}]
    assert [c["id"] for c in report["catalog"]["canaries"]] == [
        "bugbox",
        "hookwatch",
        "approveit",
    ]


def test_dry_run_skips_missing_sibling_modules(monkeypatch, tmp_path):
    def _missing_sweep():
        raise ImportError("No module named 'scripts.qa.feature_sweep'")

    def _missing_canaries():
        raise ImportError("No module named 'scripts.qa.canary_apps'")

    monkeypatch.setattr(harness, "_import_feature_sweep", _missing_sweep)
    monkeypatch.setattr(harness, "_import_canaries", _missing_canaries)
    out = tmp_path / "harness-skip.json"
    code = harness.main(["--dry-run", "--output", str(out)])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["summary"]["ok"] is True
    assert report["catalog"]["features"]["skipped"] is True
    assert "feature_sweep" in report["catalog"]["features"]["reason"]
    assert report["catalog"]["canaries"]["skipped"] is True
    assert "canary_apps" in report["catalog"]["canaries"]["reason"]


def test_dry_run_prefers_real_sibling_modules(monkeypatch, tmp_path):
    _try_sibling_modules()
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    out = tmp_path / "harness-real.json"
    code = harness.main(["--dry-run", "--output", str(out)])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["gate"] == harness.GATE_NAME
    assert report["dry_run"] is True
    assert report["summary"]["ok"] is True
    assert "features" in report["catalog"]
    assert "canaries" in report["catalog"]
    assert not (
        isinstance(report["catalog"]["features"], dict)
        and report["catalog"]["features"].get("skipped")
    )
    assert not (
        isinstance(report["catalog"]["canaries"], dict)
        and report["catalog"]["canaries"].get("skipped")
    )


def test_output_report_redacts_api_key(monkeypatch, tmp_path):
    secret = "super-secret-live-key"
    monkeypatch.setenv("BOSS_API_KEY", secret)
    monkeypatch.setattr(harness, "_import_feature_sweep", lambda: _StubSweep)
    monkeypatch.setattr(harness, "_import_canaries", lambda: _StubCanaries)
    out = tmp_path / "redacted.json"
    code = harness.main(["--dry-run", "--output", str(out)])
    assert code == 0
    text = out.read_text()
    assert secret not in text
