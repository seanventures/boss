"""The enterprise lane (scripts/enterprise-lane.sh, BOSSAPP-1430) stays wired, without a VM.

The lane needs a Linux VM, sudo and an hour; these checks are the parts that drift silently in a
normal change: the release list, the steps the RCA asked for, and the e2e gate's option the lane
depends on to run the gates under the install's own security profiles.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
LANE = ROOT / "scripts" / "enterprise-lane.sh"
GATE = ROOT / "scripts" / "e2e-gate.sh"


def test_a_release_needs_the_lane():
    tiers = yaml.safe_load((ROOT / "tests" / "tiers.yaml").read_text())
    assert "scripts/enterprise-lane.sh" in (tiers["P0"].get("release") or [])
    doc = (ROOT / "docs" / "operations" / "TEST_TIERS.md").read_text()
    assert "A release needs a green enterprise lane" in doc
    for path in (LANE, ROOT / "scripts" / "enterprise-lane" / "lima.sh"):
        assert path.stat().st_mode & 0o111, f"{path.name} must be executable"
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_the_lane_runs_what_the_rca_asked_for():
    src = LANE.read_text()
    # A fresh install, in the dev tier, given the company network the documented way.
    assert "./install.sh --dir" in src and "--install-tier dev" in src
    for setting in ("BOSS_EXTRA_CA_FILE", "BOSS_UPSTREAM_PROXY", "BOSS_NPM_REGISTRY",
                    "BOSS_PIP_INDEX_URL", "BOSS_EGRESS_PRIVATE_ALLOW"):
        assert setting in src, setting
    # The inner sandbox on, through the documented loader and .env line.
    assert "load_apparmor.sh" in src and "BOSS_ENGINE_INNER_SANDBOX=$SANDBOX_MODE" in src
    # Both tier-A gates, under the install's profiles; the build inside the installed worker.
    assert 'gate plumbing' in src and 'gate engines' in src and "--inner-sandbox" in src
    assert "engines.py" in src and "--scenario clean" in src
    # The managed Codex policy and the check through the real launcher (sandbox_identity.bind).
    assert (ROOT / "scripts" / "enterprise-lane" / "codex-requirements.toml").exists()
    check = (ROOT / "scripts" / "enterprise-lane" / "launcher_check.py").read_text()
    assert "sandbox_identity.bind(" in check and "owned_process.run" in check
    assert "codex_sandbox_proof" in check


def test_the_gate_takes_the_installs_security_profiles():
    src = GATE.read_text()
    assert "--inner-sandbox)" in src
    assert 'seccomp=$ROOT/scripts/sandbox/engine-seccomp.json' in src
    assert "apparmor=boss-engine" in src
    # The default stays Docker's own profiles: nothing changes for a gate run without the option.
    assert "INNER=off" in src
    bad = subprocess.run(["bash", str(GATE), "plumbing", "--inner-sandbox", "nope"],
                         capture_output=True, text=True)
    assert bad.returncode == 2 and "--inner-sandbox takes" in bad.stderr
