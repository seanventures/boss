from pathlib import Path
import os
import subprocess

import pytest

from boss_engine.services.agent_env import agent_environment, npm_registry_environment
from boss_engine.services.agent_shell import _shell_environment


def test_unset_preserves_existing_npm_configuration():
    assert npm_registry_environment({}) == {}
    assert agent_environment({"npm_config_registry": "https://registry.npmjs.org/"}) == {
        "npm_config_registry": "https://registry.npmjs.org/"}


def test_enterprise_registry_reaches_agents_with_tls_enabled(monkeypatch, tmp_path):
    registry = "https://packages.example.com/api/npm/team/"
    source = {"BOSS_NPM_REGISTRY": registry, "NPM_CONFIG_REGISTRY": "https://old.example.com/",
              "NPM_CONFIG_STRICT_SSL": "false", "npm_config_strict_ssl": "false",
              "DATABASE_URL": "private", "NPM_TOKEN": "private"}
    env = agent_environment(source)
    assert env == {
        "npm_config_registry": registry,
        "npm_config_strict_ssl": "true",
        "YARN_NPM_REGISTRY_SERVER": registry,
    }
    for name, value in source.items():
        monkeypatch.setenv(name, value)
    shell = _shell_environment(tmp_path)
    assert shell["npm_config_registry"] == registry
    assert shell["npm_config_strict_ssl"] == "true"
    assert shell["YARN_NPM_REGISTRY_SERVER"] == registry
    assert "DATABASE_URL" not in shell and "NPM_TOKEN" not in shell


@pytest.mark.parametrize("registry", ["http://packages.example.com/", "https://user:password@example.com/",
                                      "https://example.com/?token=secret", "https://example.com/#token",
                                      "https:///missing", "https://example.com/\nOTHER=value"])
def test_invalid_runtime_registry_fails_without_echoing_value(registry):
    with pytest.raises(ValueError) as error:
        npm_registry_environment({"BOSS_NPM_REGISTRY": registry})
    assert registry not in str(error.value)


def test_registry_survives_installer_rewrite(tmp_path):
    root = Path(__file__).resolve().parents[2]
    old = tmp_path / "old.env"
    new = tmp_path / "new.env"
    old.write_text("BOSS_NPM_REGISTRY=https://packages.example.com/npm/\n")
    new.write_text("BOSS_NPM_REGISTRY=\n")
    subprocess.run(["sh", str(root / "scripts/install/restore_preserved_env.sh"), str(old), str(new)],
                   check=True, env={**os.environ, "BOSS_NPM_REGISTRY": ""})
    assert "BOSS_NPM_REGISTRY=https://packages.example.com/npm/" in new.read_text()
