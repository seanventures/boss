"""A preview runs the app as its boss.deploy.yaml says (prev_e3a41482afaa4b228abe).

HelpDesk AI declares `ALLOW_DEMO_SEED: "true"` and `DEMO_PASSWORD: {generate: password}`. Its
preview got neither the setting nor a password the reviewer could read, so the demo seed never ran
and nobody could sign in to review the change.
"""
from pathlib import Path

from boss_engine.templates.run_env import needs_from_app_dir, resolve

MANIFEST = """version: 1
name: helpdesk-ai
kind: compose
compose:
  file: docker-compose.yml
health: [{url: "{target.public_url}/auth/providers", expect: 200}]
env:
  ALLOW_DEMO_SEED: "true"
  DEMO_PASSWORD: {generate: password}
  APP_ENV: staging
  APP_BASE_URL: "{target.public_url}"
"""


def _app(tmp_path: Path) -> Path:
    (tmp_path / "boss.deploy.yaml").write_text(MANIFEST)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    return tmp_path


def test_a_preview_gets_the_apps_plain_settings(tmp_path):
    env = resolve(needs_from_app_dir(_app(tmp_path)), purpose="preview")
    assert env.values["ALLOW_DEMO_SEED"] == "true" and env.values["APP_ENV"] == "staging"
    # A setting with something to fill in is the deploy's: a preview is not given `{target.…}`.
    assert "APP_BASE_URL" not in env.values


def test_the_generated_demo_password_is_shown_to_the_reviewer(tmp_path):
    needs = needs_from_app_dir(_app(tmp_path))
    assert needs["DEMO_PASSWORD"].kind == "demo"
    env = resolve(needs, purpose="preview")
    assert env.values["DEMO_PASSWORD"] and "DEMO_PASSWORD" in env.shown


def test_a_bound_value_beats_the_manifests_setting(tmp_path):
    env = resolve(needs_from_app_dir(_app(tmp_path)), purpose="preview", bound={"APP_ENV": "review"})
    assert env.values["APP_ENV"] == "review"


def test_a_plain_setting_never_waits_for_a_person(tmp_path):
    env = resolve(needs_from_app_dir(_app(tmp_path)), purpose="deploy")
    assert not [n for n in env.needs if n.name in ("ALLOW_DEMO_SEED", "APP_ENV")]


COMPOSE = """services:
  app:
    environment:
      PORT: "3000"
      SESSION_SECRET: ${SESSION_SECRET}
      DATABASE_URL: ${DATABASE_URL:-postgresql://x}
      GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET:-}
"""


def test_a_session_key_the_compose_file_reads_is_generated_for_a_preview(tmp_path):
    """prev_ec5be7a0201a47ad9143: "SESSION_SECRET must be set ... (BOSS generates one)"."""
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    env = resolve(needs_from_app_dir(tmp_path), purpose="preview")
    assert len(env.values.get("SESSION_SECRET", "")) >= 32
    # A provider's credential or the database is not BOSS's to mint for a preview.
    assert "GOOGLE_CLIENT_SECRET" not in env.values and "DATABASE_URL" not in env.values


PINNED_PORT = """version: 1
name: helpdesk-mini
kind: commands
env:
  PORT: "3000"
  HOST: "127.0.0.1"
  APP_ENV: staging
install: ["true"]
health: [{url: "{target.public_url}/healthz", expect: 200}]
"""


def test_a_port_the_manifest_pins_never_overrides_the_launchers(tmp_path):
    """The release gate's known-good app (tests/e2e/fixtures/helpdesk-mini) declares the port its
    DEPLOY listens on, `PORT: "3000"`. Since a literal became a setting every run gets (1aee54c41),
    the app-start proof handed it to the app over the PORT launch_app chose: the app bound 3000
    and BOSS probed its own port for 90 s (2026-11-07). The launcher's PORT and HOST always win."""
    from boss_engine.services.preview_service import app_environment

    (tmp_path / "boss.deploy.yaml").write_text(PINNED_PORT)
    env = resolve(needs_from_app_dir(tmp_path), purpose="proof", bound={"PORT": "4000"})
    assert "PORT" not in env.values and "HOST" not in env.values
    assert {"PORT", "HOST"} <= set(env.supplied)
    assert env.values["APP_ENV"] == "staging"          # the other literals still reach the app
    launched = app_environment(tmp_path, 41234, {**env.values, "PORT": "3000", "HOST": "x"})
    assert launched["PORT"] == "41234" and launched["HOST"] == "0.0.0.0"
