"""Unit tests for scripts/qa/system_gate.py (no live network, no spend)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "system_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("system_gate", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def test_child_scripts_exist():
    for name, path in gate.CHILD_SCRIPTS.items():
        assert path.is_file(), f"missing {name} at {path}"


def test_compose_dry_run_order_without_key():
    steps = gate.compose_steps(
        has_api_key=False,
        dry_run=True,
        fixture_supported=None,
        preview_fixture=False,
    )
    assert [s["id"] for s in steps] == list(gate.DRY_RUN_STEP_IDS)
    assert all(s["kind"] == "subprocess" for s in steps)
    assert all("--dry-run" in s["argv"] for s in steps)


def test_compose_live_order_with_key():
    steps = gate.compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=False,
        preview_fixture=False,
    )
    assert [s["id"] for s in steps] == list(gate.LIVE_STEP_IDS)
    harness_catalog = next(s for s in steps if s["id"] == "harness.catalog")
    assert harness_catalog["argv"] == ["--canary-mode", "catalog"]
    assert any(s["id"] == "live_dogfood" for s in steps)
    assert any(s["id"] == "workflow.live" for s in steps)
    assert any(s["id"] == "factory.dry_run" for s in steps)


def test_factory_create_fixture_only_when_supported():
    with_fix = gate.compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=True,
        preview_fixture=False,
    )
    without = gate.compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=False,
        preview_fixture=False,
    )
    unknown = gate.compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=None,
        preview_fixture=False,
    )
    with_live = next(s for s in with_fix if s["id"] == "factory.live")
    without_live = next(s for s in without if s["id"] == "factory.live")
    unknown_live = next(s for s in unknown if s["id"] == "factory.live")
    assert "--create-fixture" in with_live["argv"]
    assert with_live["create_fixture"] is True
    assert "--create-fixture" not in without_live["argv"]
    assert without_live["create_fixture"] is False
    assert "--create-fixture" not in unknown_live["argv"]
    assert unknown_live["create_fixture"] is False


def test_preview_only_when_flag():
    off = gate.compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=False,
        preview_fixture=False,
    )
    on = gate.compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=False,
        preview_fixture=True,
    )
    dry = gate.compose_steps(
        has_api_key=True,
        dry_run=True,
        fixture_supported=None,
        preview_fixture=True,
    )
    assert "preview.fixture" not in [s["id"] for s in off]
    preview = next(s for s in on if s["id"] == "preview.fixture")
    assert preview["required"] is False
    assert preview["kind"] == "http"
    assert preview["skipped"] is False
    dry_preview = next(s for s in dry if s["id"] == "preview.fixture")
    assert dry_preview["skipped"] is True


def test_no_spend_flags_in_any_plan():
    plans = [
        gate.compose_steps(has_api_key=False, dry_run=True, fixture_supported=None, preview_fixture=True),
        gate.compose_steps(has_api_key=True, dry_run=False, fixture_supported=True, preview_fixture=True),
        gate.compose_steps(has_api_key=True, dry_run=False, fixture_supported=False, preview_fixture=False),
    ]
    for steps in plans:
        for step in steps:
            assert gate.step_is_no_spend(step), step
            argv = " ".join(step.get("argv") or [])
            assert "--create-code-task" not in argv
            assert "launch" not in argv


def test_validate_dry_run_structure_passes():
    checks = gate.validate_dry_run_structure(
        api_base="https://boss-api.example",
        project_id="boss-dogfood",
    )
    required = [c for c in checks if c.required]
    assert required
    assert all(c.ok for c in required)
    names = {c.name for c in checks}
    assert "structure.scripts_exist" in names
    assert "structure.dry_run_order" in names
    assert "structure.live_order" in names
    assert "structure.factory_fixture_gated" in names
    assert "structure.no_spend_flags" in names
    assert "structure.preview_optional" in names
    docs = next(c for c in checks if c.name == "structure.live_env_docs")
    assert docs.ok
    assert "BOSS_API_KEY" in docs.detail or "BOSS_API_KEY" in json.dumps(gate.ENV_FOR_FULL_LIVE)


def test_redact_secrets_and_obj():
    secret = "sekrit-system-key-value"
    assert secret not in gate.redact_secrets(f"fail {secret}", [secret])
    blob = {"msg": f"key={secret}", "nested": ["X-Boss-Api-Key: x", secret]}
    redacted = gate.redact_obj(blob, [secret])
    dumped = json.dumps(redacted)
    assert secret not in dumped
    assert "***" in dumped
    assert "X-Boss-Api-Key(redacted)" in dumped


def test_child_argv_never_includes_api_key(tmp_path):
    step = {
        "id": "harness.catalog",
        "script": "harness.py",
        "argv": ["--canary-mode", "catalog"],
    }
    argv = gate.build_child_argv(
        step,
        api_url="https://boss-api.example",
        project_id="boss-dogfood",
        output=tmp_path / "child.json",
    )
    assert "--api-key" not in argv
    assert "catalog" in argv
    assert "--canary-mode" in argv
    factory = {
        "id": "factory.live",
        "script": "factory_e2e_gate.py",
        "argv": ["--create-fixture"],
    }
    fargv = gate.build_child_argv(
        factory,
        api_url="https://boss-api.example",
        project_id="boss-dogfood",
        output=tmp_path / "factory.json",
    )
    assert "--create-fixture" in fargv
    assert "--quiet" in fargv
    assert "--create-code-task" not in fargv


def test_summarize_and_exit_code():
    fail = [
        gate.StepResult(id="harness.dry_run", ok=True, required=True),
        gate.StepResult(id="workflow.dry_run", ok=False, required=True, detail="boom"),
        gate.StepResult(id="preview.fixture", ok=False, required=False),
    ]
    summary = gate.summarize_steps(fail)
    assert summary["passed"] is False
    assert summary["failed_required_names"] == ["workflow.dry_run"]
    report = {"summary": summary}
    assert gate.gate_exit_code(report) == 1

    ok = [gate.StepResult(id="harness.dry_run", ok=True, required=True)]
    ok_report = {"summary": gate.summarize_steps(ok)}
    assert gate.gate_exit_code(ok_report) == 0


def test_dry_run_main_exit_0_and_json_structure(monkeypatch, tmp_path):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    monkeypatch.delenv("QA_PREVIEW_FIXTURE", raising=False)
    out = tmp_path / "system-report.json"
    code = gate.main(["--dry-run", "--output", str(out), "--quiet"])
    assert code == 0
    assert out.is_file()
    report = json.loads(out.read_text())
    assert report["gate"] == gate.GATE_NAME
    assert report["mode"] == "dry-run"
    assert report["has_api_key"] is False
    assert report["summary"]["passed"] is True
    assert report["summary"]["ok"] is True
    ids = [s["id"] for s in report["steps"]]
    assert ids == list(gate.DRY_RUN_STEP_IDS)
    planned_ids = [s["id"] for s in report["planned_steps"]]
    assert planned_ids == list(gate.DRY_RUN_STEP_IDS)
    for step in report["steps"]:
        assert step["ok"] is True
        assert step["exit_code"] == 0
        assert step["output"]
        child = Path(step["output"])
        assert child.is_file()
        child_report = json.loads(child.read_text())
        assert "gate" in child_report
    assert any("BOSS_API_KEY" in x for x in report["env_for_full_live"]["required"])
    structure_names = {c["name"] for c in report["structure"]}
    assert "structure.dry_run_order" in structure_names
    assert all(c["ok"] for c in report["structure"] if c.get("required", True))


def test_output_report_redacts_api_key(monkeypatch, tmp_path):
    secret = "super-secret-system-live-key"
    monkeypatch.setenv("BOSS_API_KEY", secret)
    monkeypatch.delenv("QA_PREVIEW_FIXTURE", raising=False)
    out = tmp_path / "redacted.json"
    code = gate.main(["--dry-run", "--output", str(out), "--quiet"])
    assert code == 0
    text = out.read_text()
    assert secret not in text
    for child in tmp_path.glob("redacted.*.json"):
        assert secret not in child.read_text()


def test_preview_flag_skipped_in_dry_run(monkeypatch, tmp_path):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.setenv("QA_PREVIEW_FIXTURE", "1")
    out = tmp_path / "preview-dry.json"
    code = gate.main(["--dry-run", "--output", str(out), "--quiet"])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["preview_fixture_requested"] is True
    preview = next(s for s in report["steps"] if s["id"] == "preview.fixture")
    assert preview["skipped"] is True
    assert preview["required"] is False
    assert preview["ok"] is True
