"""The plumbing gate (scripts/e2e-gate.sh plumbing) stays wired, without Docker.

The gate itself needs Docker and the worker image; these checks are the parts that can drift
silently in a normal change and only show up when someone runs the gate before a release.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "tests" / "e2e" / "gate"
FIXTURE = ROOT / "tests" / "e2e" / "fixtures" / "helpdesk-mini"


def test_the_gate_is_a_p0_release_step():
    tiers = yaml.safe_load((ROOT / "tests" / "tiers.yaml").read_text())
    assert "scripts/e2e-gate.sh plumbing" in (tiers["P0"].get("release") or [])
    assert (ROOT / "scripts" / "e2e-gate.sh").stat().st_mode & 0o111


def test_the_fixtures_connector_is_the_templates_byte_for_byte():
    """The fixture applies login-oauth as a build does: its files unchanged."""
    ref = ROOT / "templates" / "apps" / "login-oauth" / "stacks" / "node"
    for sub in ("auth", "tests/template_login_oauth"):
        for path in sorted((ref / sub).iterdir()):
            copy = FIXTURE / "helpdesk" / sub / path.name
            assert copy.read_bytes() == path.read_bytes(), f"copy {path.relative_to(ROOT)} again"


def test_the_gate_calls_the_app_start_proof_as_the_pipeline_does():
    """When the pipeline passes the serve proof something new (a secret, an environment), the
    gate must pass it too, or the gate proves a different call than the one builds make."""
    sys.path.insert(0, str(GATE))
    try:
        import plumbing
    finally:
        sys.path.remove(str(GATE))
    extra = plumbing._call_site_kwargs() - plumbing._GATE_SERVE_KWARGS
    assert not extra, f"teach tests/e2e/gate/plumbing.py check_serve about: {sorted(extra)}"


def test_the_fixture_declares_how_it_runs_and_deploys():
    assert (FIXTURE / "helpdesk" / "boss.preview.command").read_text().strip()
    assert (FIXTURE / "helpdesk" / "package-lock.json").is_file()
    from boss_engine.services.app_deploy_manifest import load_manifest

    manifest = load_manifest(FIXTURE / "boss.deploy.yaml")
    assert {e.key for e in manifest.env} >= {"SESSION_SECRET", "DATABASE_URL"}
