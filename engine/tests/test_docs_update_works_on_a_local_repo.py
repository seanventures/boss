"""A DOCS UPDATE USES THE PROJECT'S OWN REPOSITORY, AND A PROJECT WHOSE CODE IS ON THIS MACHINE
NEEDS NO GITHUB FOR IT.

THE CASE (laptop, 2026-11-18, Easy Mode project park-bench-map, repository
local_park-bench-map-…/park-bench-map). After the first change merged, the background docs update
failed "No GitHub credentials for /: the App has no installation for it, BOSS_GITHUB_TOKEN is not
set…" — the updater wrote to the install-wide BOSS_TARGET_GITHUB_* repository (empty: "/"), not
the project's — and the failure-owner loop made it a person's action item ("Only a person can
connect a repository"). 202 such dead letters, 72 on local-repo projects. Now:

  - a project whose repository is on this machine (or that has none) never queues a docs update;
  - a queued one is skipped with a recorded note, and never asks GitHub;
  - a project with a GitHub repository binds the update to THAT repository;
  - "No GitHub credentials for /" is BOSS's own side, never a person's item, and the existing
    dead letters of a local-repo project are resolved by the owner loop's next sweep.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import docs_agent, failure_owner as fo
from boss_engine.services.docs_agent import (DocsNotApplicable, DocsUpdateService,
                                             docs_repository, enqueue_docs_update_job)
from boss_engine.services.event_subscription_service import seed_default_event_subscriptions

from ._ambient_support import eng, make_project, purge  # noqa: F401

CRED_SLASH = ("No GitHub credentials for /: the App has no installation for it, BOSS_GITHUB_TOKEN "
              "is not set, and this project has no repository connected in Setup.")


def _bind(c, pid, owner, name, *, host="github"):
    c.execute(text(
        "INSERT INTO github_repository_bindings (id, project_id, owner, name, full_name, role, "
        "default_branch, created_by, host) VALUES (:i, :p, :o, :n, :f, 'primary', 'main', "
        "'test', :h)"),
        {"i": f"ghb_{uuid4().hex[:12]}", "p": pid, "o": owner, "n": name,
         "f": f"{owner}/{name}", "h": host})


@pytest.fixture()
def local_project(eng):
    pid = make_project(eng)
    with eng.begin() as c:
        _bind(c, pid, f"local_{pid}", "park-bench-map", host="local")
    yield pid
    _cleanup(eng, pid)


@pytest.fixture()
def github_project(eng, monkeypatch):
    """A project whose primary repository is acme/web on GitHub (the registry's answer)."""
    pid = make_project(eng)
    monkeypatch.setattr(
        "boss_engine.services.repo_registry.resolve_repository_target",
        lambda c, settings, project_id, **kw: {
            "owner": "acme", "repo": "web", "full_name": "acme/web", "binding_id": None,
            "connection_id": None, "default_branch": "main", "role": "primary",
            "source": "registry:primary"} if project_id == pid else None)
    yield pid
    _cleanup(eng, pid)


def _cleanup(eng, pid):
    with eng.begin() as c:
        for table in ("failure_owners", "deadletters", "jobs", "github_repository_bindings",
                      "event_subscriptions"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
            except Exception:  # noqa: BLE001
                pass
    purge(eng, pid)


def _no_github(*_a, **_kw):
    raise AssertionError("a repository on this machine must never ask GitHub")


def test_a_local_repo_project_has_no_docs_repository(eng, local_project):
    with eng.begin() as c:
        target, why = docs_repository(c, get_settings(), local_project)
    assert target is None and "on this machine" in why


def test_a_github_project_resolves_its_own_repository(eng, github_project):
    with eng.begin() as c:
        target, why = docs_repository(c, get_settings(), github_project)
        adapter = DocsUpdateService(get_settings())._adapter(c, github_project, target)
    assert why == "" and (target["owner"], target["repo"]) == ("acme", "web")
    assert adapter.repository_full_name == "acme/web"


def test_a_local_repo_project_never_queues_a_docs_update(eng, local_project):
    settings = get_settings()
    event_type = sorted(docs_agent.docs_trigger_events(settings))[0]
    with eng.begin() as c:
        with pytest.raises(DocsNotApplicable):
            enqueue_docs_update_job(c, settings, project_id=local_project, event_id="evt_x",
                                    event_type=event_type, entity_type="code_task_run",
                                    entity_id="crun_x")
        seed_default_event_subscriptions(c, local_project)
        from boss_engine.services.event_dispatch import dispatch_event

        out = dispatch_event(c, event_id=f"evt_{uuid4().hex[:8]}", project_id=local_project,
                             event_type=event_type, actor="boss", entity_type="code_task_run",
                             entity_id="crun_x", data={"data_boundary": "internal"})
        queued = c.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p "
                                "AND kind = 'docs.update'"), {"p": local_project}).scalar()
    assert queued == 0
    assert any(o.get("skipped") and o.get("sink_ref") == "docs.update" for o in out)


def test_a_queued_docs_update_on_a_local_repo_is_skipped_without_github(
        eng, local_project, monkeypatch):
    monkeypatch.setattr("boss_engine.services.github_app_adapter.repo_token", _no_github)
    monkeypatch.setattr(docs_agent.GitHubAdapter, "_auth_token", _no_github)
    settings = get_settings()
    event_type = sorted(docs_agent.docs_trigger_events(settings))[0]
    with eng.begin() as c:
        out = DocsUpdateService(settings).run_update(
            c, project_id=local_project, event_id="evt_y", event_type=event_type,
            entity_type="code_task_run", entity_id="crun_y", created_by_job_id=None)
        noted = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p "
                               "AND event_type = 'docs.update_skipped'"),
                          {"p": local_project}).scalar()
    assert out["status"] == "skipped" and out["retryable"] is False
    assert noted == 1


def test_no_credentials_for_slash_is_boss_side_not_a_person_item():
    cls, code, _action = fo.classify(CRED_SLASH)
    assert cls == fo.PLATFORM and code != "no_repository"
    # A real repository with no credential is still the person's to connect.
    cls, code, _ = fo.classify(CRED_SLASH.replace("for /:", "for acme/web:"))
    assert (cls, code) == (fo.PERSON, "no_repository")


def test_existing_docs_dead_letters_on_a_local_repo_are_resolved(eng, local_project):
    at = datetime.now(timezone.utc) - timedelta(minutes=5)
    dls = []
    with eng.begin() as c:
        for n in range(3):
            jid, dl = f"job_{uuid4().hex[:20]}", f"dlq_{uuid4().hex[:20]}"
            c.execute(text(
                "INSERT INTO jobs (id, project_id, kind, status, payload, last_error, dedupe_key, "
                "on_behalf_of, on_behalf_system, created_at, updated_at) VALUES (:i, :p, "
                "'docs.update', 'deadlettered', CAST(:pl AS JSONB), CAST(:e AS JSONB), :d, "
                "'sys_worker', false, :at, :at)"),
                {"i": jid, "p": local_project, "at": at, "d": f"docs.update:{jid}",
                 "pl": json.dumps({"event_id": f"evt_{n}", "subscription_id": f"sub_{n % 2}",
                                   "entity_id": "crun_z"}),
                 "e": json.dumps({"type": "RuntimeError", "message": CRED_SLASH})})
            c.execute(text(
                "INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, "
                "created_at) VALUES (:d, :p, 'job', :j, :r, :at)"),
                {"d": dl, "p": local_project, "j": jid, "r": CRED_SLASH[:200], "at": at})
            dls.append(dl)

    report = fo.sweep(eng, get_settings(), only=["job"], project_id=local_project, max_acts=0)

    with eng.begin() as c:
        open_left = c.execute(text("SELECT count(*) FROM deadletters WHERE id = ANY(:d) "
                                   "AND status = 'open'"), {"d": dls}).scalar()
        states = {r[0] for r in c.execute(text(
            "SELECT state FROM failure_owners WHERE project_id = :p"), {"p": local_project})}
        person_cards = c.execute(text(
            "SELECT count(*) FROM decisions WHERE project_id = :p AND status = 'pending'"),
            {"p": local_project}).scalar()
    assert open_left == 0
    assert report.get(fo.NEEDS_PERSON, 0) == 0 and states <= {fo.RESOLVED}
    assert person_cards == 0
