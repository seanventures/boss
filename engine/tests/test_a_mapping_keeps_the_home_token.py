"""A project mapping does not keep its own copy of the home connection's token (BOSSAPP-1231).

Reads already resolve the home credential. Meeting ticket creation
(``tracker.issues.create``) used the per-project row, which is why the mapping
row was given a copy. The copy is dormant: the mapping row stores none, and a
meeting on that project still creates the ticket with the home credential.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services import skill_executors, ticket_intake
from boss_engine.services.work_tracker_adapters import IssueCreateResult, JiraWorkTrackerAdapter

FAKE_TOKEN = "fake-map-token-1231"
_URL = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    "postgresql+psycopg://boss:boss@127.0.0.1:55640/boss_slice_tokens_test",
)


@pytest.fixture(scope="module")
def eng():
    os.environ["DATABASE_URL"] = _URL
    os.environ["BOSS_COMMUNITY_SECRET_STORE"] = "database"
    get_settings.cache_clear()
    engine = create_engine(_URL, future=True)
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as error:  # pragma: no cover - no database in this process
        pytest.skip(f"test database unavailable: {error}")
    ensure_schema(engine, get_settings())
    return engine


@pytest.fixture()
def two_projects(eng):
    home = f"p_{uuid.uuid4().hex[:8]}"
    other = f"p_{uuid.uuid4().hex[:8]}"
    org = f"org_{uuid.uuid4().hex[:8]}"
    workspace = f"ws_{org}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'mapping')"), {"i": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'mapping')"),
                  {"i": workspace, "o": org})
        for pid, slug in ((home, f"home-{home}"), (other, f"other-{other}")):
            c.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, :n, :s)"),
                {"i": pid, "w": workspace, "n": slug, "s": slug})
    yield home, other
    with eng.begin() as c:
        refs = c.execute(text(
            "SELECT id, ref_key FROM secret_refs WHERE project_id IN (:h, :o)"),
            {"h": home, "o": other}).all()
        if refs:
            c.execute(text("DELETE FROM secret_material WHERE ref_key = ANY(:k)"),
                      {"k": [r[1] for r in refs]})
            c.execute(text("DELETE FROM secret_refs WHERE id = ANY(:i)"),
                      {"i": [r[0] for r in refs]})
        c.execute(text("DELETE FROM work_tracker_providers WHERE project_id IN (:h, :o)"),
                  {"h": home, "o": other})
        c.execute(text("DELETE FROM events WHERE project_id IN (:h, :o)"),
                  {"h": home, "o": other})
        c.execute(text("DELETE FROM projects WHERE id IN (:h, :o)"), {"h": home, "o": other})
        c.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": workspace})
        c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org})


def _connect_home(db, home: str, other: str) -> dict:
    return ticket_intake.store(
        db, get_settings(), project_id=home, adapter=ticket_intake.adapter_for("jira"),
        configuration={
            "base_url": "https://jira.example.test",
            "email": "ada@example.test",
            "project_key": "KAN",
            "settings": {"mappings": [
                {"external_project": "KAN", "boss_project_id": home, "default": True},
                {"external_project": "OPS", "boss_project_id": other, "default": False},
            ]},
        },
        token=FAKE_TOKEN, outcome={"account": "ada"}, actor="tester")


def _mirror(db, other: str) -> dict:
    row = db.execute(text(
        "SELECT id, project_id, configuration FROM work_tracker_providers "
        "WHERE project_id = :p AND provider_key = 'jira'"), {"p": other}).mappings().one()
    config = row["configuration"]
    return {"id": row["id"], "project_id": row["project_id"], "configuration": dict(config)}


def test_a_mapping_row_receives_no_token(eng, two_projects):
    home, other = two_projects
    with eng.begin() as db:
        stored = _connect_home(db, home, other)
        ticket_intake.sync_mirrors(db, get_settings(), stored, actor="tester",
                                   may_manage=lambda _project: None)
        mirror = _mirror(db, other)
        copied = db.execute(text(
            "SELECT count(*) FROM secret_refs WHERE project_id = :p"),
            {"p": other}).scalar()
        home_material = db.execute(text(
            "SELECT count(*) FROM secret_material sm JOIN secret_refs sr ON sr.ref_key = sm.ref_key "
            "WHERE sr.project_id = :p"), {"p": home}).scalar()
    config = mirror["configuration"]
    leftover = [key for key in (
        "secret_ref_id", "last4", "fingerprint", "api_token", "access_token",
        "token", "pat", "password", "oauth_client_secret", "oauth_client_secret_ref")
        if key in config]
    assert leftover == []
    assert config.get("mirror_of") == stored["id"]
    assert copied == 0
    assert home_material == 1


def test_meeting_ticket_creation_uses_the_home_credential(eng, two_projects, monkeypatch):
    home, other = two_projects
    for name in ("PLANE_API_KEY", "JIRA_API_TOKEN", "LINEAR_API_KEY", "GITHUB_TOKEN",
                 "BOSS_GITHUB_TOKEN", "AZURE_DEVOPS_PAT"):
        monkeypatch.delenv(name, raising=False)
    seen: dict = {}

    def capture(self, configuration, **_kwargs):
        seen["api_token"] = configuration.get("api_token")
        return IssueCreateResult(external_issue_id="99", external_key="OPS-1",
                                 external_url="https://jira.example.test/browse/OPS-1")

    monkeypatch.setattr(JiraWorkTrackerAdapter, "create_issue", capture)
    with eng.begin() as db:
        stored = _connect_home(db, home, other)
        ticket_intake.sync_mirrors(db, get_settings(), stored, actor="tester",
                                   may_manage=lambda _project: None)
        assert "secret_ref_id" not in _mirror(db, other)["configuration"]
        home_ref = stored["configuration"]["secret_ref_id"]
        before = db.execute(text(
            "SELECT count(*) FROM secret_access_events WHERE secret_ref_id = :r "
            "AND outcome = 'resolved'"), {"r": home_ref}).scalar()

        class Context:
            connection = db
            project_id = other

        made = skill_executors.execute_tracker_issues_create(
            Context(), {"actions": [{"title": "Send the notes"}]})
        after = db.execute(text(
            "SELECT count(*) FROM secret_access_events WHERE secret_ref_id = :r "
            "AND outcome = 'resolved'"), {"r": home_ref}).scalar()
    assert made["created_count"] == 1
    assert made["created"][0]["key"] == "OPS-1"
    assert seen["api_token"] == FAKE_TOKEN
    assert after == before + 1


def _migration_sql() -> str:
    schema = (get_settings().repo_root / "db" / "schema.sql").read_text(encoding="utf-8")
    start = schema.find("DO $bossapp_1231$")
    end = schema.find("$bossapp_1231$;", start)
    assert start != -1 and end != -1, "schema.sql has no BOSSAPP-1231 migration"
    return schema[start:end + len("$bossapp_1231$;")]


def test_the_migration_deletes_a_dormant_mapping_copy_and_keeps_the_home(eng, two_projects):
    """An install that already copied the token loses the copy and its vault material."""
    home, other = two_projects
    home_ref = f"sref_{uuid.uuid4().hex[:8]}"
    copy_ref = f"sref_{uuid.uuid4().hex[:8]}"
    home_key = f"BOSS_{home.upper()}_TICKET_JIRA_TOKEN"
    copy_key = f"BOSS_{other.upper()}_TICKET_JIRA_TOKEN"
    shared_mirror = f"p_{uuid.uuid4().hex[:8]}"
    with eng.begin() as db:
        db.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "SELECT :i, workspace_id, 'shared', :s FROM projects WHERE id = :h"),
            {"i": shared_mirror, "s": f"shared-{shared_mirror}", "h": home})
        for ref, project, key in ((home_ref, home, home_key), (copy_ref, other, copy_key)):
            db.execute(text(
                "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, "
                "ref_key, secret_kind, storage_backend, status, created_by) "
                "VALUES (:i, :p, 'project', :p, 'database', :k, 'api_token', 'database', "
                "'active', 'tester')"),
                {"i": ref, "p": project, "k": key})
            db.execute(text(
                "INSERT INTO secret_material (ref_key, ciphertext) VALUES (:k, :v) "
                "ON CONFLICT (ref_key) DO NOTHING"),
                {"k": key, "v": b"ciphertext-not-a-token"})
        db.execute(text(
            "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
            "status, configuration) VALUES (:i, :p, 'jira', 'Jira', 'active', CAST(:c AS JSONB))"),
            {"i": f"trk_{home}", "p": home, "c":
             '{"base_url": "https://jira.example.test", "email": "ada@example.test", '
             f'"secret_ref_id": "{home_ref}", "last4": "1231", "fingerprint": "home"}}'})
        db.execute(text(
            "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
            "status, configuration) VALUES (:i, :p, 'jira', 'Jira', 'active', CAST(:c AS JSONB))"),
            {"i": f"trk_{other}", "p": other, "c":
             '{"mirror_of": "trk_%s", "base_url": "https://jira.example.test", '
             '"email": "ada@example.test", "secret_ref_id": "%s", "last4": "1231", '
             '"fingerprint": "copy"}' % (home, copy_ref)})
        # The same reference as the home: the pointer goes, the vault item stays.
        db.execute(text(
            "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
            "status, configuration) VALUES (:i, :p, 'jira', 'Jira', 'active', CAST(:c AS JSONB))"),
            {"i": f"trk_{shared_mirror}", "p": shared_mirror, "c":
             '{"mirror_of": "trk_%s", "secret_ref_id": "%s", "last4": "1231"}' % (home, home_ref)})
        db.execute(text(_migration_sql()))
        mirror = db.execute(text(
            "SELECT configuration FROM work_tracker_providers WHERE project_id = :p"),
            {"p": other}).scalar()
        shared = db.execute(text(
            "SELECT configuration FROM work_tracker_providers WHERE project_id = :p"),
            {"p": shared_mirror}).scalar()
        home_row = db.execute(text(
            "SELECT configuration->>'secret_ref_id' FROM work_tracker_providers WHERE project_id = :p"),
            {"p": home}).scalar()
        copy_left = db.execute(text("SELECT count(*) FROM secret_refs WHERE id = :i"),
                               {"i": copy_ref}).scalar()
        copy_material = db.execute(text(
            "SELECT count(*) FROM secret_material WHERE ref_key = :k"), {"k": copy_key}).scalar()
        home_left = db.execute(text("SELECT count(*) FROM secret_refs WHERE id = :i"),
                               {"i": home_ref}).scalar()
        home_material = db.execute(text(
            "SELECT count(*) FROM secret_material WHERE ref_key = :k"), {"k": home_key}).scalar()
        db.execute(text("DELETE FROM work_tracker_providers WHERE project_id = :p"),
                   {"p": shared_mirror})
        db.execute(text("DELETE FROM projects WHERE id = :p"), {"p": shared_mirror})
    assert "secret_ref_id" not in mirror and "last4" not in mirror and "fingerprint" not in mirror
    assert "secret_ref_id" not in shared
    assert home_row == home_ref
    assert copy_left == 0 and copy_material == 0
    assert home_left == 1 and home_material == 1
