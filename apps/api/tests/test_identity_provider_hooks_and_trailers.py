"""P1-14: the vault's provider hooks exist in every process, and a build's PR names who it was for.

  * vault.REFRESHERS/REVOKERS had no "github" entry in production (install_provider_hooks had no
    caller): a person's GitHub token was never refreshed, and a disconnect "confirmed" a revocation
    that never reached GitHub before shredding the token. Every provider a person can connect now
    has its hooks on first use, whatever process opens the vault.
  * IDENTITY.md §7.9: a build's PR body carries `On-behalf-of:` and `Boss-Lineage:` from the
    authority the build was started under (commit_trailers had no production caller).
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture
def no_hooks(monkeypatch):
    from boss_engine.identity import vault

    monkeypatch.setattr(vault, "REFRESHERS", {})
    monkeypatch.setattr(vault, "REVOKERS", {})
    return vault


def test_every_connectable_provider_has_its_hooks_without_a_boot_step(no_hooks):
    from apps.api.app.routers.identity_agents import PROVIDER_LABELS
    from boss_engine.identity import github_app

    vault = no_hooks
    for provider in PROVIDER_LABELS:
        assert provider in vault.BUILTIN_PROVIDER_HOOKS, provider
        assert vault.refresher_for(provider) is not None, provider
        assert vault.revoker_for(provider) is not None, provider
    assert vault.refresher_for("github") is github_app._oauth_refresh
    assert vault.revoker_for("github") is github_app._oauth_revoke


def test_a_hook_a_test_or_plugin_installed_first_is_kept(no_hooks):
    vault = no_hooks

    def mine(_access, _refresh):
        return None

    vault.REVOKERS["github"] = mine
    assert vault.revoker_for("github") is mine
    assert vault.revoker_for("no-such-provider") is None


def test_the_vault_reads_hooks_only_through_the_lazy_lookup():
    """The class: no code path reads the registries directly (which is how the boot step was missed)."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[3] / "engine/boss_engine/identity/vault.py").read_text()
    assert "REFRESHERS.get(" not in source.replace("return REFRESHERS.get(", "")
    assert "REVOKERS.get(" not in source.replace("return REVOKERS.get(", "")


@pytest.fixture
def build(engine):
    world = IdentityWorld.create(engine)
    person = world.person("Alice Builder")
    run_id, grant_id = f"crun_{uuid.uuid4().hex[:10]}", f"dlg_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_users (principal_id, email) VALUES (:p, 'alice@acme.test')"),
                   {"p": person.id})
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title) VALUES (:r, :p, 't')"),
                   {"r": run_id, "p": world.project_a})
        db.execute(text(
            "INSERT INTO jobs (id, project_id, kind, payload, on_behalf_of, grant_id, chain) VALUES "
            "(:j, :p, 'code.task', CAST(:pl AS jsonb), :u, :g, '[]'::jsonb)"),
            {"j": f"job_{uuid.uuid4().hex[:10]}", "p": world.project_a, "u": person.id, "g": grant_id,
             "pl": json.dumps({"run_id": run_id})})
    return {"run_id": run_id, "grant_id": grant_id, "engine": engine}


def test_a_builds_pr_names_who_it_was_for_and_the_grant(build, monkeypatch):
    from boss_engine.services import agent_acts

    monkeypatch.setenv("BOSS_CONSOLE_URL", "https://boss.example.test")
    with build["engine"].begin() as db:
        footer = agent_acts.pr_trailers(db, build["run_id"])
    assert "On-behalf-of: Alice Builder <alice@acme.test>" in footer, footer
    assert f"Boss-Lineage: {build['grant_id']}/{build['run_id']}" in footer, footer
    assert f"/identity/agents/delegations/{build['grant_id']}" in footer


def test_boss_own_work_carries_no_person(engine):
    from boss_engine.services import agent_acts

    world = IdentityWorld.create(engine)
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title) VALUES (:r, :p, 't')"),
                   {"r": run_id, "p": world.project_a})
        db.execute(text(
            "INSERT INTO jobs (id, project_id, kind, payload, on_behalf_of, on_behalf_system) VALUES "
            "(:j, :p, 'code.task', CAST(:pl AS jsonb), 'sys_worker', true)"),
            {"j": f"job_{uuid.uuid4().hex[:10]}", "p": world.project_a, "pl": json.dumps({"run_id": run_id})})
        assert "On-behalf-of" not in agent_acts.pr_trailers(db, run_id)
