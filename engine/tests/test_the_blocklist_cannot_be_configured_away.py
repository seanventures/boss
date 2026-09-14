"""What the coding agent may run, at the boundary where it runs it.

BOSS's governance is strong at the workflow level — decisions, risk tiers, dual control,
budget states — and until BOSS-411 it stopped at the sandbox wall: the agent ran with the
harness's full toolset and whatever the sandbox allowlist happened to permit.

The ordering below is the whole design and is worth stating once:

    BLOCKLIST  beats every autonomy level, every allowlist, and every configuration
    denylist   beats the allowlist
    allowlist  extends the sandbox's own executable allowlist
    autonomy   decides which tools are offered at all

`rm` is on the blocklist because of 2026-09-02, when an agent's test run cleared 204
production tables. That is not a setting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services.code_agent_tools import (
    AUTONOMY_LEVELS,
    SandboxToolset,
    resolve_autonomy,
    tool_schemas,
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("x = 1\n")
    return tmp_path


def _s(**kw) -> Settings:
    kw.setdefault("BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS", "python3,npm,pytest")
    return Settings(**kw)


# --- the blocklist is not negotiable ------------------------------------------

@pytest.mark.parametrize("level", AUTONOMY_LEVELS)
def test_a_blocklisted_command_is_refused_at_every_autonomy_level(repo: Path, level: str) -> None:
    ts = SandboxToolset(repo, settings=_s(BOSS_AGENT_AUTONOMY_LEVEL=level), autonomy=level)
    out = ts.call("bash", {"command": "rm -rf ."})
    assert out.is_error
    assert "blocklist" in out.text or "read-only" in out.text or "not available" in out.text


def test_the_blocklist_beats_an_operator_allowlist(repo: Path) -> None:
    """An allowlist entry must not be a way to re-enable a blocked verb."""
    ts = SandboxToolset(repo, autonomy="full", settings=_s(
        BOSS_AGENT_AUTONOMY_LEVEL="full", BOSS_AGENT_COMMAND_ALLOWLIST="rm,curl"))
    assert "blocklist" in ts.call("bash", {"command": "rm -rf ."}).text
    assert "blocklist" in ts.call("bash", {"command": "curl http://x"}).text


@pytest.mark.parametrize("disguise", ["/bin/rm", "./rm", "/usr/bin/../bin/rm", "RM"])
def test_a_blocked_command_cannot_be_hidden_behind_a_path(repo: Path, disguise: str) -> None:
    ts = SandboxToolset(repo, autonomy="full", settings=_s(BOSS_AGENT_AUTONOMY_LEVEL="full"))
    out = ts.call("bash", {"command": f"{disguise} -rf ."})
    assert out.is_error, f"{disguise} slipped past the blocklist"


def test_shell_syntax_cannot_smuggle_a_blocked_command(repo: Path) -> None:
    ts = SandboxToolset(repo, autonomy="full", settings=_s(BOSS_AGENT_AUTONOMY_LEVEL="full"))
    out = ts.call("bash", {"command": "python3 --version && rm -rf /"})
    assert out.is_error and "WITHOUT a shell" in out.text


def test_the_blocklist_can_be_extended_but_the_defaults_remain(repo: Path) -> None:
    ts = SandboxToolset(repo, autonomy="full", settings=_s(
        BOSS_AGENT_AUTONOMY_LEVEL="full",
        BOSS_AGENT_COMMAND_BLOCKLIST="rm,curl,wget,sudo,ssh,npm"))
    assert ts.call("bash", {"command": "npm install"}).is_error
    assert ts.call("bash", {"command": "rm x"}).is_error


# --- denylist beats allowlist --------------------------------------------------

def test_the_denylist_beats_the_allowlist(repo: Path) -> None:
    ts = SandboxToolset(repo, autonomy="standard", settings=_s(
        BOSS_AGENT_COMMAND_ALLOWLIST="make", BOSS_AGENT_COMMAND_DENYLIST="make"))
    out = ts.call("bash", {"command": "make build"})
    assert out.is_error and "denylist" in out.text


def test_the_allowlist_extends_the_sandbox_allowlist(repo: Path) -> None:
    """`make` is not in the sandbox's validation allowlist for this fixture."""
    blocked = SandboxToolset(repo, autonomy="standard", settings=_s())
    assert "not allowlisted" in blocked.call("bash", {"command": "make build"}).text
    allowed = SandboxToolset(repo, autonomy="standard",
                             settings=_s(BOSS_AGENT_COMMAND_ALLOWLIST="make"))
    # It now gets past the allowlist check and fails on execution instead (no Makefile).
    assert "not allowlisted" not in allowed.call("bash", {"command": "make build"}).text


# --- autonomy levels shape the schema -----------------------------------------

def test_a_review_step_is_read_only_whatever_the_operator_configured() -> None:
    """The governance invariant does not bend to a config value."""
    assert resolve_autonomy(_s(BOSS_AGENT_AUTONOMY_LEVEL="full"), read_only=True) == "read_only"


def test_an_unknown_level_falls_back_to_standard_not_to_full() -> None:
    assert resolve_autonomy(_s(BOSS_AGENT_AUTONOMY_LEVEL="banana"), read_only=False) == "standard"
    assert resolve_autonomy(_s(BOSS_AGENT_AUTONOMY_LEVEL=""), read_only=False) == "standard"


def test_edit_level_may_write_but_never_run_commands(repo: Path) -> None:
    names = {t["function"]["name"] for t in tool_schemas(False, "edit")}
    assert "write_file" in names and "edit_file" in names
    assert "bash" not in names, "the schema is the boundary; a tool not offered cannot be tried"

    ts = SandboxToolset(repo, autonomy="edit", settings=_s(BOSS_AGENT_AUTONOMY_LEVEL="edit"))
    out = ts.call("bash", {"command": "python3 --version"})
    assert out.is_error and "autonomy level 'edit'" in out.text
    assert not ts.call("write_file", {"path": "b.py", "content": "y = 2\n"}).is_error


def test_read_only_level_offers_no_write_tools_at_all() -> None:
    assert {t["function"]["name"] for t in tool_schemas(True)} == {
        "read_file", "list_dir", "glob", "grep"}


def test_standard_is_the_default_rather_than_full() -> None:
    assert Settings().agent_autonomy_level == "standard"
    assert "bash" in {t["function"]["name"] for t in tool_schemas(False, "standard")}
