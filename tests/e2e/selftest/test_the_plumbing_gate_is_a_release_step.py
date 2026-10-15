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
    assert "scripts/e2e-gate.sh engines" in (tiers["P0"].get("release") or [])
    assert (ROOT / "scripts" / "e2e-gate.sh").stat().st_mode & 0o111


def test_the_fixtures_connector_is_the_templates_byte_for_byte():
    """The fixture applies login-oauth as a build does: its files unchanged."""
    ref = ROOT / "templates" / "apps" / "login-oauth" / "stacks" / "node"
    for path in sorted((ref / "auth").iterdir()):
        copy = FIXTURE / "helpdesk" / "auth" / path.name
        assert copy.read_bytes() == path.read_bytes(), f"copy {path.relative_to(ROOT)} again"


def test_the_fixture_carries_none_of_the_connectors_own_suite():
    """A connector is pre-tested (templates.connector_policy): a build never copies its tests or
    its test scripts into the app, so the golden app does not either. The gate runs that suite in
    the connector's own tier, from the template (plumbing._connector_own_suite)."""
    import json

    ref = ROOT / "templates" / "apps" / "login-oauth" / "stacks" / "node"
    assert not (FIXTURE / "helpdesk" / "tests" / "template_login_oauth").exists()
    scripts = json.loads((FIXTURE / "helpdesk" / "package.json").read_text())["scripts"]
    ref_scripts = json.loads((ref / "package.json").read_text())["scripts"]
    assert not [k for k in ref_scripts if "test" in k and k in scripts]
    src = (GATE / "plumbing.py").read_text()
    assert "trial.run_stack(" in src and "as_connector=False" not in src


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

    # Beside the app, as HelpDesk AI keeps it (fix/listed-var-is-work).
    manifest = load_manifest(FIXTURE / "helpdesk" / "boss.deploy.yaml")
    assert not (FIXTURE / "boss.deploy.yaml").exists()
    assert {e.key for e in manifest.env} >= {"SESSION_SECRET"}
    # Its Postgres is DECLARED (a resource BOSS provisions), never a secret the gate wires by hand.
    assert "DATABASE_URL" not in {e.key for e in manifest.env}
    assert [(r.capability, r.engine) for r in manifest.resources] == [("database.relational", "postgres")]
    src = (GATE / "plumbing.py").read_text()
    assert 'item.key == "DATABASE_URL"' not in src and "_database_bindings(manifest)" in src


PLAIN = ROOT / "tests" / "e2e" / "fixtures" / "plain-node"


def test_the_plain_node_fixture_defines_only_its_tests_and_boss_gates_on_them():
    """The second golden fixture stays what it proves: a plain Node ESM app with no build,
    typecheck or lint script, gated on exactly the install and its own `npm test`."""
    import json

    from boss_engine.services import verification_target as vt

    scripts = json.loads((PLAIN / "plain" / "package.json").read_text())["scripts"]
    assert not {"build", "typecheck", "lint"} & set(scripts) and scripts["test"] == "node --test"
    plan = vt.resolve_gate_plan({"project_kind": "web"}, "Plain Node", PLAIN,
                                changed=["plain/package.json"])
    assert plan.commands == [vt.NPM_INSTALL_CMD, vt.TEST_STEP] and plan.finding is None
    assert "check_plain_node(g)" in (GATE / "plumbing.py").read_text()


def test_the_engines_gate_runs_every_scenario_on_every_engine_profile():
    """The engines tier (scripts/e2e-gate.sh engines): one expectation per scenario, met by every
    engine profile, and the scripted engine plays every fault a scenario names."""
    sys.path.insert(0, str(GATE))
    try:
        import engines
    finally:
        sys.path.remove(str(GATE))
    from boss_engine.services import scripted_engine

    assert set(engines.PROFILES) == {"poorest", "richest"}
    assert engines.PROFILES["poorest"] == ""
    doc = scripted_engine.__doc__ or ""
    for name, exp in engines.SCENARIOS.items():
        for fault in filter(None, exp["faults"].split(",")):
            assert fault in doc, f"{name}: the scripted engine does not play {fault!r}"
    assert '"$IMAGE" python3 "/gate-src/tests/e2e/gate/$TIER.py"' in (
        ROOT / "scripts" / "e2e-gate.sh").read_text()
