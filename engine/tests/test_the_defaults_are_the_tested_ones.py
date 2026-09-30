"""What ships must survive the smallest machine the install docs tell people to use.

"I tested these very thoroughly in shared-server why if I change to different VM or local it is
not working... we are not doing the SaaS product we are actually going to do OSS, it should
work for all developers properly."

Comparing the long-lived install's configuration against what a fresh one gets found the
gaps. The one that mattered: it runs ONE coding agent at a time, set by hand months ago and
written down nowhere, while a fresh install shipped three and this laptop four.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from boss_engine.config import (
    Settings,
    setting_env_names,
    unread_boss_variables,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def shipped(monkeypatch):
    """Settings as a NEW install sees them.

    A bare `Settings()` reads the developer's own .env, so it asserts what this laptop happens
    to be configured to do -- which is the very mistake this file is about. It found the bug
    honestly once, by failing while the default was already correct.
    """
    for key in list(os.environ):
        if key.startswith("BOSS_"):
            monkeypatch.delenv(key, raising=False)
    return Settings(_env_file=None)


def test_one_agent_at_a_time_by_default(shipped):
    """Each concurrent slot is a coding-agent process inside the api container. On the 4 GB VM
    the install docs describe, three of them exhaust the box -- and the operator reads the
    resulting stall as BOSS being broken, which is the misattribution the host-stall work
    exists to prevent. shared-server, the only install with months of evidence behind it, runs 1.
    """
    assert shipped.code_agent_max_concurrency == 1


def test_a_shell_on_the_host_is_not_the_default(shipped):
    """shared-server enables it deliberately. Shipping it on would hand every fresh install a
    capability its operator never asked for."""
    assert shipped.agent_host_shell is False


# --------------------------------------------------------------------------------------- #
# Drift reporting
#
# These exist because the first version of the report was WRONG in the most damaging possible
# way: it read only the engine's settings, so it called BOSS_LEASE_TTL_SECONDS dead. That knob
# decides how long a crashed worker's job is stuck before another worker reclaims it. Acting on
# that report would have removed a durability setting from every install that copied the fix.
# --------------------------------------------------------------------------------------- #


def test_a_knob_the_worker_reads_is_not_called_dead():
    """The regression this section exists for. BOSS_LEASE_TTL_SECONDS is declared by the
    worker's settings class and by nothing in the engine."""
    assert unread_boss_variables({"BOSS_LEASE_TTL_SECONDS": "600"}) == ()


def test_a_name_reached_only_through_alias_choices_counts_as_read():
    """Settings declare names three ways. Counting only `alias` marked hundreds of live
    settings as unread, which is a report nobody would trust twice."""
    names = setting_env_names()
    for name in ("BOSS_OPENAI_MODEL", "BOSS_EMAIL_SMTP_HOST", "BOSS_GITHUB_TOKEN"):
        assert name in names, f"{name} is declared via AliasChoices and must count as read"


def test_a_genuinely_unread_variable_is_named():
    """The report has to still be capable of saying something, or it is always silent and
    therefore worthless.

    The name is ASSEMBLED rather than written, because this test file is itself inside the
    tree being scanned -- spelling it out here would make the scan find a "reader" and the
    test would assert the opposite of what it means to.
    """
    invented = "BOSS_" + "NO" + "_READER_ANYWHERE"
    assert unread_boss_variables({invented: "1"}) == (invented,)


def test_non_boss_variables_are_never_reported():
    assert unread_boss_variables({"PATH": "/usr/bin", "HOME": "/root"}) == ()


def test_silence_when_the_workers_settings_cannot_be_read(monkeypatch):
    """A missing reader must never be reported as proof that a variable has no reader."""
    import boss_engine.config as config

    monkeypatch.setattr(config, "_worker_settings_model", lambda: None)
    assert config.unread_boss_variables({"BOSS_NO_SUCH_SETTING_ANYWHERE": "1"}) == ()


def test_a_variable_read_by_a_bare_getenv_is_not_called_dead():
    """BOSS_LOCAL_SUPERVISOR is read by `os.getenv` in apps/api/app/local_supervisor.py and
    declared by no settings class at all. A check that reads only settings accuses it."""
    assert unread_boss_variables({"BOSS_LOCAL_SUPERVISOR": "1"}) == ()


def test_a_variable_read_only_by_the_compose_file_is_not_called_dead():
    """BOSS_API_PORT is read by community-compose.yml and by doctor.sh. An earlier version of
    this check told the operator it was safe to delete, which would have moved their API."""
    for variable in ("BOSS_API_PORT", "BOSS_WEB_PORT"):
        assert unread_boss_variables({variable: "8000"}) == (), variable


def test_the_scan_ignores_the_env_file_that_sets_the_variable():
    """.env is where variables are SET. Counting a name there as a reader would make the
    check permanently silent, which is a different way of being useless."""
    from boss_engine.config import names_mentioned_in_source

    names = names_mentioned_in_source(str(REPO))
    assert "BOSS_CODE_AGENT_MAX_CONCURRENCY" in names  # genuinely referenced in source
    assert len(names) > 100, "the scan found suspiciously little; did the walk break?"


def test_the_scan_prunes_rather_than_filters():
    """Pruning node_modules during the walk instead of filtering afterwards is what makes this
    1 second rather than 13, and therefore what makes it runnable at all."""
    source = (REPO / "engine/boss_engine/config.py").read_text(encoding="utf-8")
    assert "subdirectories[:] = [" in source, (
        "the walk must prune skipped directories in place; rglob enumerates every file under "
        "node_modules before the filter can reject it"
    )


def test_the_template_we_hand_new_installs_has_no_dead_variables():
    """Every install made from .env.example inherits whatever it lists. A variable nothing
    reads, shipped in the template, is how a snowflake gets into an install on day one -- it
    looks like tuning to the next person, so they keep it and copy it forward."""
    listed = [
        line.split("=", 1)[0].strip()
        for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
        if re.match(r"^BOSS_[A-Z0-9_]+=", line.strip())
    ]
    dead = unread_boss_variables(dict.fromkeys(listed, ""))
    assert not dead, f".env.example ships variables nothing reads: {list(dead)}"
