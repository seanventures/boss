"""Archiving a project (BOSSAPP-768 S5; KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.7).

What archived means, end to end on a real database: hidden from GET /v1/projects unless asked,
new work refused with 409 project_archived in words, every scheduler skips it (each one asked
directly, not trusted), reads keep working, nothing is deleted, and a restore brings all of it
back. The archive and restore are owner-only (project:delete, step-up), confirmed, audited and
evented. engine/tests/test_every_scheduler_skips_archived_projects.py keeps a new scheduler from
forgetting; this file proves the ones that exist do skip.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import projects_archive


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def owner(world):
    person = world.person("archive-owner")
    world.bind(person, "owner")
    return person


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _project(world, owner, client) -> str:
    slug = f"archive-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(owner)):
        made = client.post("/v1/projects", json={"name": f"Archive {slug}", "slug": slug})
    assert made.status_code == 201, made.text
    return slug


def _archive(world, owner, client, project_id: str, path: str = "archive", **body):
    with world.acting_as(world.ctx(owner)):
        return client.post(f"/v1/projects/{project_id}/{path}",
                           json={"step_up": "confirmed", **body})


def _listed(world, owner, client, **params) -> dict[str, dict]:
    with world.acting_as(world.ctx(owner)):
        got = client.get("/v1/projects", params=params)
    assert got.status_code == 200, got.text
    return {p["id"]: p for p in got.json()["items"]}


def _code_task(world, owner, client, project_id: str):
    """A build on the scripted engine (no model), as the legacy actor, as
    test_creating_a_project starts one."""
    return client.post("/v1/code-tasks", json={
        "project_id": project_id, "title": f"probe {uuid.uuid4().hex[:4]}",
        "instructions": "slugify", "owner": "northpeak", "repo": "bank-a",
        "model_config": {"engine": "scripted"}, "open_pr": False, "actor": "ceo"})


def test_archive_hides_refuses_and_restore_brings_it_back(world, owner, client, engine):
    slug = _project(world, owner, client)
    assert slug in _listed(world, owner, client)

    with world.acting_as(world.ctx(owner)):
        preview = client.get(f"/v1/projects/{slug}/archive")
    assert preview.status_code == 200, preview.text
    assert preview.json()["archived"] is False
    assert preview.json()["stops"][0].startswith("New builds")      # the dialog's sentences

    done = _archive(world, owner, client, slug, note="case study finished")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["archived"] is True and body["archived_at"] and body["changed"] is True
    assert body["archived_by"]

    # HIDDEN unless asked; listed with its archive columns when asked.
    assert slug not in _listed(world, owner, client)
    shown = _listed(world, owner, client, include_archived="true")
    assert shown[slug]["archived_at"] and shown[slug]["archived_by"] == body["archived_by"]

    # NEW WORK IS REFUSED, in words, as 409.
    refused = _code_task(world, owner, client, slug)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "project_archived" and detail["project_id"] == slug
    assert "is archived" in detail["message"] and "Restore" in detail["message"]
    assert not engine.connect().execute(text(
        "SELECT 1 FROM code_task_runs WHERE project_id = :p"), {"p": slug}).first()

    # READS KEEP WORKING, and archiving again is a no-op.
    with world.acting_as(world.ctx(owner)):
        assert client.get("/v1/github/repos", params={"project_id": slug}).status_code == 200
    again = _archive(world, owner, client, slug)
    assert again.status_code == 200 and again.json()["changed"] is False

    # AUDITED AND EVENTED.
    with engine.connect() as db:
        events = db.execute(text(
            "SELECT event_type, actor, data->>'note' FROM events WHERE project_id = :p "
            "AND event_type IN ('project.archived', 'project.restored')"), {"p": slug}).all()
        assert [(e[0], e[2]) for e in events] == [("project.archived", "case study finished")]
        audited = db.execute(text(
            "SELECT event_type, actor_principal_id FROM identity_security_events "
            "WHERE org_id = :o AND target_id = :p AND event_type LIKE 'project.%'"),
            {"o": world.org, "p": slug}).all()
        assert ("project.archived", owner.id) in [tuple(a) for a in audited]

    # RESTORE: listed again, takes work again.
    back = _archive(world, owner, client, slug, path="unarchive")
    assert back.status_code == 200, back.text
    assert back.json()["archived"] is False and back.json()["changed"] is True
    assert slug in _listed(world, owner, client)
    started = _code_task(world, owner, client, slug)
    assert started.status_code == 200, started.text
    with engine.connect() as db:
        assert db.execute(text(
            "SELECT count(*) FROM identity_security_events WHERE org_id = :o AND target_id = :p "
            "AND event_type = 'project.restored'"), {"o": world.org, "p": slug}).scalar() == 1


def test_who_may_archive_and_what_cannot_be_archived(world, owner, client, engine):
    slug = _project(world, owner, client)

    unconfirmed = _archive(world, owner, client, slug, step_up=None)
    assert unconfirmed.status_code == 428, unconfirmed.text

    admin = world.person(f"archive-admin-{uuid.uuid4().hex[:4]}")
    world.bind(admin, "admin")                # project:delete is the owner's
    with world.acting_as(world.ctx(admin)):
        denied = client.post(f"/v1/projects/{slug}/archive", json={"step_up": "confirmed"})
    assert denied.status_code == 403, denied.text

    with world.acting_as(world.ctx(owner, mfa=False)):
        stale = client.post(f"/v1/projects/{slug}/archive", json={"step_up": "confirmed"})
    assert stale.status_code == 401 and stale.json().get("error") == "step_up_required", stale.text

    with world.acting_as(world.ctx(owner)):
        missing = client.post("/v1/projects/no-such-project/archive", json={"step_up": "confirmed"})
    assert missing.status_code == 404, missing.text

    with engine.connect() as db:
        assert db.execute(text("SELECT archived_at FROM projects WHERE id = :p"),
                          {"p": slug}).scalar() is None

    # The install's home project cannot be archived (the worker's ticks and BOSS's agent live there).
    with engine.begin() as db:
        with pytest.raises(projects_archive.ArchiveRefused, match="home project"):
            projects_archive.archive(db, get_settings(), project_id=get_settings().default_project_id,
                                     actor="test")


def test_an_orgs_last_open_project_cannot_be_archived(engine):
    world = IdentityWorld.create(engine)
    with engine.begin() as db:
        projects_archive.archive(db, get_settings(), project_id=world.project_a, actor="test")
        with pytest.raises(projects_archive.ArchiveRefused, match="only open project"):
            projects_archive.archive(db, get_settings(), project_id=world.project_b, actor="test")


@pytest.fixture
def pair(engine):
    """Two bare projects of one org: `archived` (archived) and `live` (not)."""
    world = IdentityWorld.create(engine)
    with engine.begin() as db:
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                   {"p": f"third-{world.org}", "w": world.workspace})
        projects_archive.archive(db, get_settings(), project_id=world.project_a, actor="test")
    return world.project_a, world.project_b


def test_every_scheduler_skips_the_archived_project(engine, pair):
    archived, live = pair
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=5)
    with engine.begin() as db:
        for pid in (archived, live):
            db.execute(text(
                "INSERT INTO workflow_schedules (id, project_id, workflow_key, name, cadence, minute, "
                "actor, next_run_at) VALUES (:i, :p, 'nope', 'nightly', 'daily', 0, 'test', :t)"),
                {"i": f"wsch_{uuid.uuid4().hex[:8]}", "p": pid, "t": past})
            db.execute(text(
                "INSERT INTO agent_wakeups (id, project_id, kind, actor, prompt, next_wake_at, "
                "created_by) VALUES (:i, :p, 'schedule', 'test', 'hello', :t, 'test')"),
                {"i": f"wake_{pid}", "p": pid, "t": past})
            db.execute(text(
                "INSERT INTO rule_groups (id, project_id, subject_id, subject_kind, name, next_eval_at) "
                "VALUES (:i, :p, :s, 'target', 'g', :t)"),
                {"i": f"rg_{pid}", "p": pid, "s": f"subj_{pid}", "t": past})
            db.execute(text(
                "INSERT INTO kg_ingest_queue (project_id, source_kind, source_id) "
                "VALUES (:p, 'run', :i)"), {"p": pid, "i": f"ctr_gone_{pid}"})

    from boss_engine.services import agent_wakeups, monitor_scheduler, workflow_schedules
    from boss_engine.services.knowledge import ingest

    # Workflow schedules: the archived project's does not fire and is not advanced.
    with engine.begin() as db:
        assert workflow_schedules.run_due(db, project_id=archived, now=now) == []
        assert db.execute(text("SELECT next_run_at FROM workflow_schedules WHERE project_id = :p"),
                          {"p": archived}).scalar() == past
        fired = workflow_schedules.run_due(db, project_id=live, now=now)
        assert len(fired) == 1          # it fails (no workflow 'nope'), but it was asked to fire

    # Agent wakeups: only the live project's is due.
    with engine.begin() as db:
        due = {r["project_id"] for r in agent_wakeups.due(db, limit=500)}
    assert live in due and archived not in due

    # Monitor scheduler: only the live project's rule group is leased.
    claimed = {g["project_id"] for g in monitor_scheduler.claim(engine, "test-owner", now, limit=500)}
    assert live in claimed and archived not in claimed

    # Lifecycle ingest: the archived project's row stays queued; the live one's is drained.
    ingest.ingest_queued(engine, limit=500)
    with engine.connect() as db:
        left = {r[0] for r in db.execute(text(
            "SELECT project_id FROM kg_ingest_queue WHERE project_id IN (:a, :l)"),
            {"a": archived, "l": live}).all()}
    assert left == {archived}
    assert ingest.tick(engine, project_id=archived)["archived"] is True

    # Jobs: one queued for the archived project waits (never leased, never dropped); after a
    # restore the same job is leased.
    from boss_engine.services.jobs import JobLeaseService, enqueue_job

    kind = f"test.archive.{uuid.uuid4().hex[:6]}"
    with engine.begin() as db:
        queued = enqueue_job(db, archived, kind, {"n": 1})
    assert queued["id"]
    with engine.begin() as db:
        leased = JobLeaseService(get_settings()).claim_job(db, "test-worker", [kind], 60)
    assert leased is None, leased
    with engine.begin() as db:
        projects_archive.restore(db, get_settings(), project_id=archived, actor="test")
    with engine.begin() as db:
        leased = JobLeaseService(get_settings()).claim_job(db, "test-worker", [kind], 60)
    assert leased is not None and leased["id"] == queued["id"], leased
    with engine.begin() as db:
        projects_archive.archive(db, get_settings(), project_id=archived, actor="test")

    # The live-project helpers agree.
    with engine.connect() as db:
        assert projects_archive.live_ids(db, [archived, live]) == [live]
        assert projects_archive.is_archived(db, archived) and not projects_archive.is_archived(db, live)


def test_new_work_doors_refuse_the_archived_project(engine, pair):
    archived, _live = pair
    from boss_engine.services import agent_wakeups, workflow_schedules, workflows
    from boss_engine.services.knowledge import repo_index

    doors = {
        "workflow run": lambda db: workflows.start_run(
            db, project_id=archived, actor="test", input_data={}, dedupe_key="d"),
        "schedule": lambda db: workflow_schedules.create(
            db, project_id=archived, workflow_key="w", name="n", cadence="daily", minute=0,
            actor="test"),
        "agent schedule": lambda db: agent_wakeups.schedule(
            db, project_id=archived, actor="test", prompt="p", at_time="09:00"),
        "index": lambda db: repo_index.request(db, get_settings(), project_id=archived, actor="t"),
    }
    for name, door in doors.items():
        with engine.begin() as db:
            with pytest.raises(projects_archive.ProjectArchived) as refused:
                door(db)
        assert refused.value.detail()["error"] == "project_archived", name
        assert "is archived" in str(refused.value), name


def test_the_agent_tool_offers_archive_as_a_button_and_uses_the_same_service(engine, pair):
    """archive_project is a governed write: never pressed by the model, and pressed it archives
    through projects_archive exactly as the route does."""
    from boss_engine.tools.builtin import build_default_registry

    reg = build_default_registry()
    for name in ("archive_project", "restore_project"):
        spec = reg.get(name)
        assert spec.kind == "write" and not spec.self_executable and spec.operator_only
        assert spec.permission == "project:delete" and spec.org_scope is True
    archived, live = pair

    class Ctx:
        settings = get_settings()

        def begin(self):
            return engine.begin()

    out = reg.get("archive_project").handler(Ctx(), project_id=archived, actor="ceo",
                                             args={"target": live, "note": "asked in chat"})
    assert out["ok"] is True and out["archived_at"] and out["stops"], out
    with engine.connect() as db:
        assert projects_archive.is_archived(db, live)
    restored = reg.get("restore_project").handler(Ctx(), project_id=live, actor="ceo",
                                                  args={"target": archived})
    assert restored["ok"] is True and restored["archived_at"] is None
    with engine.connect() as db:
        assert not projects_archive.is_archived(db, archived)
        assert db.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                               "event_type = 'project.restored'"), {"p": archived}).scalar() == 1
