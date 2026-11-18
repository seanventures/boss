"""The claim page's `boss` command for THIS install (stranger test r2, 2026-11-17).

A second install on one machine keeps its launcher in BOSS_BIN_DIR; plain `boss` there is the
first install. Pure function; no database.
"""

from __future__ import annotations

import pytest

from boss_engine.identity.host_command import host_boss_command


@pytest.mark.parametrize("bin_dir", ["", "  ", "/usr/local/bin", "/home/dev/.local/bin",
                                     "/home/dev/.local/bin/", "/root/.local/bin"])
def test_a_default_install_is_plain_boss(bin_dir: str) -> None:
    assert host_boss_command(bin_dir) == "boss"


def test_a_second_install_is_its_launchers_full_path() -> None:
    assert host_boss_command("/home/dev/stranger-test/bin2") == "/home/dev/stranger-test/bin2/boss"
    assert host_boss_command("/home/dev/stranger-test/bin2/") == "/home/dev/stranger-test/bin2/boss"


@pytest.mark.parametrize("bin_dir", ["/tmp/x; rm -rf ~", "/tmp/$(id)", "relative/bin", "/a b"])
def test_anything_a_shell_would_read_differently_is_plain_boss(bin_dir: str) -> None:
    assert host_boss_command(bin_dir) == "boss"


def test_it_reads_the_containers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOSS_BIN_DIR", "/srv/boss-two/bin")
    assert host_boss_command() == "/srv/boss-two/bin/boss"
    monkeypatch.delenv("BOSS_BIN_DIR")
    assert host_boss_command() == "boss"


def test_the_api_container_is_given_the_bin_dir() -> None:
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "scripts/install/community-compose.yml").read_text()
    assert "BOSS_BIN_DIR: ${BOSS_BIN_DIR:-}" in compose
