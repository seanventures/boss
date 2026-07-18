"""IaC verification lane (spec 05 §3.2 / T3): hermetic tests — no real terraform.

Every tool the lane shells out to (tofu / terraform / tflint / checkov / tfsec) is a
fake shell script in a temp bin dir prepended to PATH, with canned JSON output and
controllable exit codes. Covers: IaC-file detection, the all-green happy path, each
gate failing the lane, fail-closed tool_missing behaviour, credential-vs-graph plan
triage, cloud-credential env scrubbing, tofu>terraform auto-preference, and the
checkov→tfsec scanner fallback. Pipeline wiring is covered through the pure helpers
(`_run_iac_lane`, `_iac_gates`) — the full governed pipeline needs a live DB/GitHub/
sandbox stack, so the gate plumbing is asserted at the helper seam instead.
"""
import json
import os

import pytest

from apps.api.app.config import Settings
from apps.api.app.services.iac_verify import detect_iac_files, run_iac_verify_lane


# --------------------------------------------------------------------------- #
#  Fixtures: fake toolchain on PATH + a workdir with a .tf file               #
# --------------------------------------------------------------------------- #
@pytest.fixture
def bindir(tmp_path, monkeypatch):
    b = tmp_path / "fakebin"
    b.mkdir()
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ.get('PATH', '')}")
    return b


@pytest.fixture
def workdir(tmp_path):
    w = tmp_path / "repo"
    w.mkdir()
    (w / "main.tf").write_text('resource "null_resource" "x" {}\n', encoding="utf-8")
    return w


def _write_bin(bindir, name, body):
    p = bindir / name
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _fake_tf(bindir, name="tofu", *, fmt_exit=0, init_exit=0, validate_exit=0,
             plan_exit=0, plan_body=None):
    plan = plan_body or f'echo "Plan: 1 to add, 0 to change, 0 to destroy."; exit {plan_exit}'
    return _write_bin(bindir, name, (
        'case "$1" in\n'
        f'  fmt) echo "fmt checked"; exit {fmt_exit} ;;\n'
        f'  init) echo "Initializing the backend..."; exit {init_exit} ;;\n'
        '  validate) echo \'{"format_version":"1.0","valid":true,"error_count":0}\'; '
        f'exit {validate_exit} ;;\n'
        f'  plan) {plan} ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'))


def _fake_json_tool(bindir, name, payload='{"issues":[]}', exit_code=0):
    return _write_bin(bindir, name, f"echo '{payload}'; exit {exit_code}")


def _all_tools(bindir, tf_name="tofu", **tf_kwargs):
    _fake_tf(bindir, tf_name, **tf_kwargs)
    _fake_json_tool(bindir, "tflint")
    _fake_json_tool(bindir, "checkov", payload='{"summary":{"failed":0}}')


def _settings(**over):
    return Settings(**over)


def _step(result, name):
    return next(s for s in result.steps if s.step == name)


# --------------------------------------------------------------------------- #
#  Detection                                                                  #
# --------------------------------------------------------------------------- #
def test_detect_iac_files_matches_only_iac_suffixes():
    files = {
        "infra/main.tf": "", "infra/vars.tfvars": "", "modules/net.hcl": "",
        "export/plan.tf.json": "", "app.py": "", "data.json": "", "README.md": "",
        "src/testf": "", "notes.tfx": "",
    }
    assert detect_iac_files(files) == [
        "export/plan.tf.json", "infra/main.tf", "infra/vars.tfvars", "modules/net.hcl",
    ]


def test_detect_iac_files_empty_for_non_iac_diff():
    assert detect_iac_files({"app.py": "", "config.yaml": ""}) == []
    assert detect_iac_files([]) == []


# --------------------------------------------------------------------------- #
#  Happy path + per-gate failures                                             #
# --------------------------------------------------------------------------- #
def test_happy_path_all_green(bindir, workdir):
    _all_tools(bindir)
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert res.ok is True
    assert res.tf_binary == "tofu"
    assert res.scanner == "checkov"
    assert [s.step for s in res.steps] == [
        "fmt", "validate", "lint", "security", "plan_dry", "plan_live"]
    assert all(s.ok for s in res.steps)
    assert _step(res, "plan_live").skipped is True
    assert "approval Decision" in _step(res, "plan_live").reason
    # everything must survive JSON serialization (quality-gate evidence / review packet)
    round_trip = json.loads(json.dumps(res.to_dict()))
    assert round_trip["ok"] is True and len(round_trip["steps"]) == 6


def test_fmt_failure_fails_lane_but_later_steps_still_run(bindir, workdir):
    _all_tools(bindir, fmt_exit=3)
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert res.ok is False
    assert _step(res, "fmt").ok is False
    # evidence-gathering continues past the first failure
    assert _step(res, "validate").ok is True
    assert _step(res, "plan_dry").ok is True


def test_validate_failure_fails_lane(bindir, workdir):
    _all_tools(bindir, validate_exit=1)
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert res.ok is False
    assert _step(res, "validate").ok is False


def test_init_failure_fails_the_validate_step(bindir, workdir):
    _all_tools(bindir, init_exit=1)
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    step = _step(res, "validate")
    assert res.ok is False and step.ok is False
    assert step.reason == "init_failed"
    assert "init -backend=false" in step.output


def test_missing_tflint_fails_closed_not_silent_skip(bindir, workdir):
    _fake_tf(bindir, "tofu")
    _fake_json_tool(bindir, "checkov")  # tflint deliberately absent
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    step = _step(res, "lint")
    assert res.ok is False
    assert step.ok is False and step.skipped is False
    assert step.reason == "tool_missing"


def test_missing_tf_binary_fails_all_tf_steps_closed(bindir, workdir):
    _fake_json_tool(bindir, "tflint")
    _fake_json_tool(bindir, "checkov")  # no tofu/terraform anywhere on the fake PATH
    res = run_iac_verify_lane(
        workdir, _settings(BOSS_IAC_TF_BINARY="tofu"), files={"main.tf": ""})
    assert res.ok is False and res.tf_binary is None
    for name in ("fmt", "validate", "plan_dry"):
        assert _step(res, name).ok is False
        assert _step(res, name).reason == "tool_missing"


# --------------------------------------------------------------------------- #
#  plan-dry: credential-only failures pass; graph failures fail               #
# --------------------------------------------------------------------------- #
def test_plan_credential_only_error_passes_with_note(bindir, workdir):
    _all_tools(bindir, plan_body=(
        'echo "Error: No valid credential sources found for AWS Provider."; exit 1'))
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    step = _step(res, "plan_dry")
    assert step.ok is True
    assert "missing cloud credentials" in (step.note or "")
    assert res.ok is True


def test_plan_graph_error_fails(bindir, workdir):
    _all_tools(bindir, plan_body=(
        'echo "Error: Reference to undeclared resource aws_vpc.main"; exit 1'))
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert _step(res, "plan_dry").ok is False
    assert res.ok is False


def test_plan_graph_error_wins_even_with_credential_noise(bindir, workdir):
    _all_tools(bindir, plan_body=(
        'echo "Error: no valid credential sources"; '
        'echo "Error: Unsupported argument \\"subnet\\""; exit 1'))
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert _step(res, "plan_dry").ok is False


def test_plan_unclassified_error_fails_conservatively(bindir, workdir):
    _all_tools(bindir, plan_body='echo "Error: something exploded"; exit 1')
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert _step(res, "plan_dry").ok is False


# --------------------------------------------------------------------------- #
#  Env scrubbing: no cloud credentials reach the lane subprocesses            #
# --------------------------------------------------------------------------- #
def test_cloud_credentials_are_scrubbed_from_plan_env(bindir, workdir, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIALEAKME")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaky-secret")
    monkeypatch.setenv("BOSS_AWS_ACCESS_KEY_ID", "AKIABOSSLEAK")
    # the fake plan prints exactly the credential-shaped env vars it can see
    _all_tools(bindir, plan_body=(
        'echo "ENV-DUMP-BEGIN"; env | grep -E "^(AWS_|BOSS_AWS_)" ; '
        'echo "ENV-DUMP-END"; exit 0'))
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    out = _step(res, "plan_dry").output
    assert "ENV-DUMP-BEGIN" in out and "ENV-DUMP-END" in out  # the dump really ran
    assert "AWS_ACCESS_KEY_ID" not in out
    assert "AWS_SECRET_ACCESS_KEY" not in out
    assert "BOSS_AWS_" not in out
    assert _step(res, "plan_dry").ok is True


# --------------------------------------------------------------------------- #
#  Binary + scanner resolution                                                #
# --------------------------------------------------------------------------- #
def test_auto_binary_prefers_tofu_over_terraform(bindir, workdir):
    _all_tools(bindir, tf_name="tofu")
    _fake_tf(bindir, "terraform", fmt_exit=9)  # red herring: only runs if mis-picked
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert res.tf_binary == "tofu"
    assert res.ok is True  # proves the green tofu ran, not the failing terraform


def test_auto_binary_falls_back_to_terraform(bindir, workdir):
    _all_tools(bindir, tf_name="terraform")
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert res.tf_binary == "terraform"
    assert res.ok is True


def test_explicit_binary_setting_is_honored(bindir, workdir):
    _all_tools(bindir, tf_name="terraform")
    _fake_tf(bindir, "tofu", fmt_exit=9)  # auto would have picked (failing) tofu
    res = run_iac_verify_lane(
        workdir, _settings(BOSS_IAC_TF_BINARY="terraform"), files={"main.tf": ""})
    assert res.tf_binary == "terraform"
    assert res.ok is True


def test_scanner_falls_back_to_tfsec_when_checkov_absent(bindir, workdir):
    _fake_tf(bindir, "tofu")
    _fake_json_tool(bindir, "tflint")
    _fake_json_tool(bindir, "tfsec", payload='{"results":[]}')  # no checkov
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    step = _step(res, "security")
    assert res.scanner == "tfsec"
    assert step.ok is True
    assert "fell back to tfsec" in (step.note or "")
    assert res.ok is True


def test_no_scanner_at_all_fails_security_gate_closed(bindir, workdir):
    _fake_tf(bindir, "tofu")
    _fake_json_tool(bindir, "tflint")  # neither checkov nor tfsec
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    step = _step(res, "security")
    assert res.ok is False
    assert step.ok is False and step.reason == "tool_missing"


def test_security_scan_findings_fail_the_lane(bindir, workdir):
    _fake_tf(bindir, "tofu")
    _fake_json_tool(bindir, "tflint")
    _fake_json_tool(bindir, "checkov", payload='{"summary":{"failed":3}}', exit_code=1)
    res = run_iac_verify_lane(workdir, _settings(), files={"main.tf": ""})
    assert _step(res, "security").ok is False
    assert res.ok is False


def test_plan_live_flag_changes_skip_reason_but_stays_skipped(bindir, workdir):
    _all_tools(bindir)
    res = run_iac_verify_lane(
        workdir, _settings(BOSS_IAC_PLAN_LIVE_ENABLED="true"), files={"main.tf": ""})
    step = _step(res, "plan_live")
    assert step.skipped is True  # T3 scope: placeholder only, even when enabled
    assert "not implemented" in step.reason and "approval Decision" in step.reason
    assert res.ok is True  # skipped steps never count toward the verdict
