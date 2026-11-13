"""BOSSAPP-865: every secret resolution is one append-only access row (run, person, agent,
version; resolved or refused), each write is a version, and a person- or agent-scoped secret
resolves only for that person or agent."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")

VALUE = "correct-horse-staple-865"


class _Mem:
    store: dict = {}

    def get(self, key):
        from boss_engine.services.secret_backends import SecretNotFound

        if key not in self.store:
            raise SecretNotFound(f"{key} is not stored")
        return self.store[key]

    def put(self, key, value):
        self.store[key] = value


@pytest.fixture(scope="module")
def engine():
    from boss_engine.db import ensure_schema

    eng = create_engine(URL, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def project(engine, monkeypatch):
    from boss_engine.services import run_secret_redaction, secret_access_log, secret_backends, secrets_service

    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings: _Mem())
    monkeypatch.setattr(secrets_service, "backend_for", lambda kind, settings: _Mem())
    secret_access_log.reset_for_tests()
    run_secret_redaction.reset_for_tests()
    pid = f"p_865_{uuid4().hex[:8]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_865', 'o') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_865', 'org_865', 'w') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_865', :p, :p)"),
                  {"p": pid})
    yield pid
    run_secret_redaction.reset_for_tests()


def _put(engine, pid, name="stripe_key", **kw):
    from boss_engine.services.secrets_service import put_secret

    with engine.begin() as c:
        return put_secret(c, None, project_id=pid, name=name, value=VALUE, backend="database",
                          actor="usr_writer", **kw)


def _events(engine, ref):
    with engine.begin() as c:
        return c.execute(text("SELECT * FROM secret_access_events WHERE secret_ref_id = :r "
                              "ORDER BY accessed_at, id"), {"r": ref}).mappings().all()


def test_each_write_is_a_version(engine, project):
    first = _put(engine, project)
    second = _put(engine, project)
    assert first["secret_ref_id"] == second["secret_ref_id"]
    assert (first["version"], second["version"]) == (1, 2)
    with engine.begin() as c:
        rows = c.execute(text("SELECT version, last4, created_by FROM secret_versions "
                              "WHERE secret_ref_id = :r ORDER BY version"),
                         {"r": first["secret_ref_id"]}).all()
        assert [r[0] for r in rows] == [1, 2]
        assert all(VALUE not in str(tuple(r)) for r in rows)        # never the material
        assert c.execute(text("SELECT version FROM secret_refs WHERE id = :r"),
                         {"r": first["secret_ref_id"]}).scalar() == 2


def test_a_resolution_names_the_run_the_agent_and_the_version(engine, project):
    from boss_engine.services.connection_credentials import acting_agent
    from boss_engine.services.harness_failures import run_scope
    from boss_engine.services.secret_backends import resolve_secret_ref

    ref = _put(engine, project)["secret_ref_id"]
    with run_scope("ctr_run_865"), acting_agent("deployer"), engine.begin() as c:
        assert resolve_secret_ref(c, ref, None, project_id=project) == VALUE
    (row,) = _events(engine, ref)
    assert row["outcome"] == "resolved"
    assert row["run_id"] == "ctr_run_865"
    assert row["agent_role"] == "deployer"
    assert row["version"] == 1
    assert VALUE not in str(dict(row))


def test_the_log_is_append_only(engine, project):
    from boss_engine.services.secret_backends import resolve_secret_ref

    ref = _put(engine, project)["secret_ref_id"]
    with engine.begin() as c:
        resolve_secret_ref(c, ref, None)
    with pytest.raises(Exception, match="append-only"), engine.begin() as c:
        c.execute(text("UPDATE secret_access_events SET outcome = 'refused' WHERE secret_ref_id = :r"),
                  {"r": ref})
    with pytest.raises(Exception, match="append-only"), engine.begin() as c:
        c.execute(text("DELETE FROM secret_access_events WHERE secret_ref_id = :r"), {"r": ref})


def test_an_identical_access_inside_the_window_is_one_row(engine, project):
    from boss_engine.services.secret_backends import resolve_secret_ref

    ref = _put(engine, project)["secret_ref_id"]
    for _ in range(3):
        with engine.begin() as c:
            resolve_secret_ref(c, ref, None)
    assert len(_events(engine, ref)) == 1


def test_an_agent_scoped_secret_resolves_only_for_that_agent(engine, project):
    from boss_engine.services.connection_credentials import acting_agent
    from boss_engine.services.secret_backends import SecretUnavailable, resolve_secret_ref

    ref = _put(engine, project, name="deployer_key", scope_type="agent",
               scope_id="deployer")["secret_ref_id"]
    with acting_agent("reviewer"), engine.begin() as c, pytest.raises(SecretUnavailable,
                                                                       match="another agent"):
        resolve_secret_ref(c, ref, None)
    with acting_agent("deployer"), engine.begin() as c:
        assert resolve_secret_ref(c, ref, None) == VALUE
    outcomes = [(e["outcome"], e["agent_role"]) for e in _events(engine, ref)]
    assert outcomes == [("refused", "reviewer"), ("resolved", "deployer")]


def test_a_user_scoped_secret_resolves_only_for_that_person(engine, project, monkeypatch):
    from boss_engine.services import connection_credentials as cc
    from boss_engine.services.secret_backends import SecretUnavailable, resolve_secret_ref

    ref = _put(engine, project, name="my_token", scope_type="user",
               scope_id="usr_alice")["secret_ref_id"]
    monkeypatch.setattr(cc, "principal_from_context",
                        lambda c, p: cc.CredentialPrincipal(person_id="usr_bob"))
    with engine.begin() as c, pytest.raises(SecretUnavailable, match="another person"):
        resolve_secret_ref(c, ref, None)
    monkeypatch.setattr(cc, "principal_from_context",
                        lambda c, p: cc.CredentialPrincipal(person_id="usr_alice"))
    with engine.begin() as c:
        assert resolve_secret_ref(c, ref, None) == VALUE
    assert [e["person_id"] for e in _events(engine, ref)] == ["usr_bob", "usr_alice"]


def test_a_store_failure_is_logged_and_keeps_its_own_exception(engine, project):
    from boss_engine.services.secret_backends import SecretNotFound, resolve_secret_ref

    ref = _put(engine, project, name="gone")["secret_ref_id"]
    _Mem.store.clear()
    # The caller handles the failure and commits (pytest.raises is inside the transaction).
    with engine.begin() as c, pytest.raises(SecretNotFound):
        resolve_secret_ref(c, ref, None)
    (row,) = _events(engine, ref)
    assert (row["outcome"], row["reason"]) == ("refused", "SecretNotFound")


def test_access_log_reads_versions_and_events_newest_first(engine, project):
    from boss_engine.services.secret_access_log import access_log, reset_for_tests
    from boss_engine.services.secret_backends import resolve_secret_ref

    ref = _put(engine, project)["secret_ref_id"]
    _put(engine, project)
    with engine.begin() as c:
        resolve_secret_ref(c, ref, None)
    reset_for_tests()
    from boss_engine.services.harness_failures import run_scope

    with run_scope("ctr_second"), engine.begin() as c:
        resolve_secret_ref(c, ref, None)
    with engine.begin() as c:
        out = access_log(c, ref)
    assert out["version"] == 2
    assert [v["version"] for v in out["versions"]] == [2, 1]
    assert [e["run_id"] for e in out["events"]] == ["ctr_second", None]
