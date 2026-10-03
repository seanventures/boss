"""`boss.deploy.yaml` is read strictly, and every refusal names the field and the fix.

A manifest that fails at minute nine of an install — on a literal `{targte.host}` written to a
host, or a secret someone hoped would be substituted — is the failure this exists to prevent.
So placeholders are a closed set, checked at plan time, and the shell's own `${VAR}` is left
alone.
"""
from __future__ import annotations

import pytest

from boss_engine.services.app_deploy_manifest import (
    DeployManifest,
    ManifestError,
    load_manifest,
    render,
)

CTX = {"commit": "a" * 40, "ref": "main", "target.host": "203.0.113.7",
       "target.public_url": "https://boss.example.test", "target.user": "deploy"}

FULL = """
version: 1
name: boss
kind: commands
env:
  PUBLIC_URL: "{target.public_url}"
  RELEASE: "{commit}"
  PORT: 8000
  DEBUG: false
  DB_PASSWORD: {generate: password}
  SIGNING_KEY: {generate: hex32}
  GITHUB_TOKEN: {secret: github-token}
install:
  - ./scripts/install.sh --dir {dir}
  - run: docker compose up -d
    name: start
    timeout_seconds: 900
update:
  - ./scripts/upgrade.sh
health:
  - url: http://127.0.0.1:8000/health
    expect: 200
    timeout_seconds: 300
open: {url: "{target.public_url}/console"}
teardown: ["docker compose down"]
requires: {sudo: true, docker: true}
"""


def test_a_full_manifest_parses_into_steps_env_and_health():
    manifest = load_manifest(FULL)
    assert isinstance(manifest, DeployManifest)
    assert manifest.name == "boss" and manifest.dir == "/opt/boss-apps/boss"
    assert [s.name for s in manifest.install] == ["install 1", "start"]
    assert manifest.install[1].timeout_seconds == 900
    assert [s.run for s in manifest.update] == ["./scripts/upgrade.sh"]
    assert manifest.requires_sudo and manifest.requires_docker
    sources = {e.key: (e.source, e.value) for e in manifest.env}
    assert sources["DB_PASSWORD"] == ("generate", "password")
    assert sources["GITHUB_TOKEN"] == ("secret", "github-token")
    assert sources["PORT"] == ("literal", "8000") and sources["DEBUG"] == ("literal", "false")


def test_rendering_fills_every_placeholder_and_leaves_secrets_as_names():
    plan = render(load_manifest(FULL), CTX)
    assert plan["install"][0]["run"] == "./scripts/install.sh --dir /opt/boss-apps/boss"
    assert plan["open_url"] == "https://boss.example.test/console"
    assert plan["env_literals"]["PUBLIC_URL"] == "https://boss.example.test"
    assert plan["env_literals"]["RELEASE"] == "a" * 40
    assert plan["env_generated"] == {"DB_PASSWORD": "password", "SIGNING_KEY": "hex32"}
    assert plan["env_secrets"] == {"GITHUB_TOKEN": "github-token"}
    assert "DB_PASSWORD" not in plan["env_literals"]


def test_the_summary_the_console_shows_carries_names_and_never_values():
    summary = load_manifest(FULL).summary()
    assert summary["env_keys"] == ["PUBLIC_URL", "RELEASE", "PORT", "DEBUG", "DB_PASSWORD",
                                   "SIGNING_KEY", "GITHUB_TOKEN"]
    # The NAME of the BOSS secret each key reads is shown, so an approver sees which secret
    # leaves for the host; no env value (a literal, a placeholder) is.
    assert summary["secret_env"] == {"GITHUB_TOKEN": "github-token"}
    assert "{commit}" not in str(summary)
    assert summary["steps"]["install"] == ["install 1", "start"]


def test_update_defaults_to_install():
    manifest = load_manifest("version: 1\nname: app\ninstall: [make up]\n")
    assert manifest.update == manifest.install


def test_shell_variables_and_brace_syntax_are_not_mistaken_for_placeholders():
    manifest = load_manifest(
        "version: 1\nname: app\ninstall:\n"
        "  - echo ${HOME} {a,b} && awk '{print $1}' f && find . -exec echo {} \\;\n")
    assert "${HOME}" in render(manifest, CTX)["install"][0]["run"]


def test_compose_kind_writes_install_and_teardown_with_the_deploy_env():
    manifest = load_manifest(
        "version: 1\nname: shop\nkind: compose\ncompose: {file: deploy/compose.yml}\n")
    plan = render(manifest, CTX)
    assert plan["install"][0]["run"] == (
        "docker compose -f deploy/compose.yml -p shop "
        "--env-file /opt/boss-apps/shop/.boss/deploy.env up -d --build")
    assert plan["update"] == plan["install"]
    assert plan["teardown"][0]["run"].endswith(" down")
    assert manifest.requires_docker


@pytest.mark.parametrize("text, names", [
    ("version: 1\nname: app\ninstall: ['echo {targte.host}']\n", ["install[1]", "{targte.host}"]),
    ("version: 1\nname: app\ninstall: [x]\nenv: {K: '{password}'}\n", ["env.K", "{password}"]),
    ("version: 2\nname: app\ninstall: [x]\n", ["version must be 1"]),
    ("version: 1\nname: My App\ninstall: [x]\n", ["name 'My App'"]),
    ("version: 1\nname: app\n", ["install must list at least one command"]),
    ("version: 1\nname: app\ninstall: [x]\nenv: {K: {generate: uuid}}\n", ["env.K", "generate"]),
    ("version: 1\nname: app\ninstall: [x]\nenv: {K: {secret: a, generate: password}}\n",
     ["exactly one"]),
    ("version: 1\nname: app\ninstall: [x]\nhealth: [{url: '127.0.0.1:8000'}]\n",
     ["health[1].url"]),
    ("version: 1\nname: app\nkind: compose\ncompose: {file: c.yml}\ninstall: [x]\n",
     ["kind: compose writes install"]),
    ("version: 1\nname: app\nkind: compose\n", ["compose: {file"]),
    ("version: 1\nname: app\ninstall: [x]\nbuild: [y]\n", ["unknown field", "build"]),
    ("version: 1\nname: app\ninstall: [x]\ndir: relative/path\n", ["dir 'relative/path'"]),
    ("version: 1\nname: app\ninstall: [{run: x, timeout_seconds: 0}]\n",
     ["install[1].timeout_seconds"]),
    ("version: 1\nname: app\ninstall: [x]\n  bad: [\n", ["not valid YAML"]),
])
def test_every_refusal_names_the_field_and_says_how_to_fix_it(text, names):
    with pytest.raises(ManifestError) as caught:
        load_manifest(text)
    for name in names:
        assert name in str(caught.value), str(caught.value)
