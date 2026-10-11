"""official.postgres against a real PostgreSQL 17 container: the full lifecycle, migrations,
backup with a tested restore into a disposable server, restore into the resource, and the
destroy refusals."""
from __future__ import annotations

import pytest

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import Harness, docker_ok

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")


def _req(**kw) -> ResourceRequest:
    body = {"name": "db", "capability": "database.relational", "engine": "postgres", "backups": True}
    body.update(kw)
    return ResourceRequest.model_validate(body)


def test_public_postgres_and_a_wrong_engine_are_invalid(tmp_path):
    h = Harness(load_plugin("postgres"), tmp_path)
    out = h.call("validate", _req(publicAccess=True, engineVersion="16"))
    assert out.status is OperationStatus.INVALID
    text = " ".join(e.message for e in out.errors)
    assert "never published publicly" in text and "PostgreSQL 17" in text


def test_plan_names_no_port_and_the_bindings(tmp_path):
    out = Harness(load_plugin("postgres"), tmp_path).call("plan", _req())
    assert out.plan.ports == [] and out.plan.publicAccess is False
    assert out.plan.bindings == ["DATABASE_URL"]


def test_attached_postgres_binds_existing_secrets_and_is_never_destroyed(tmp_path):
    h = Harness(load_plugin("postgres"), tmp_path)
    req = _req(mode="attached", attach={"bindings": {"DATABASE_URL": "LEGACY_DB_URL"}})
    prov = h.call("provision", req)
    assert prov.ok and prov.bindings["DATABASE_URL"].secretRef.endswith("/LEGACY_DB_URL")
    plan = h.call("plan", req, state={"desired": "absent"}).plan
    out = h.call("destroy", req, plan=plan)
    assert out.status is OperationStatus.REFUSED and out.errors[0].code == "attached_resource"


def test_destroy_is_refused_while_another_resource_depends_on_it(tmp_path):
    h = Harness(load_plugin("postgres"), tmp_path)
    plan = h.call("plan", _req(), state={"desired": "absent"}).plan
    out = h.call("destroy", _req(), state={"dependents": ["deploy/app/vectors"]}, plan=plan)
    assert out.status is OperationStatus.REFUSED and out.errors[0].code == "has_dependents"


@needs_docker
def test_postgres_lifecycle_migrations_backup_tested_restore(tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_tickets.sql").write_text(
        "CREATE TABLE tickets (id serial PRIMARY KEY, title text NOT NULL);\n"
        "INSERT INTO tickets (title) SELECT 'ticket ' || g FROM generate_series(1, 250) g;\n")
    (mig / "002_comments.sql").write_text(
        "CREATE TABLE comments (id serial PRIMARY KEY, ticket int REFERENCES tickets(id), body text);\n"
        "INSERT INTO comments (ticket, body) SELECT (g % 250) + 1, md5(g::text) FROM generate_series(1, 1000) g;\n")
    plugin = load_plugin("postgres")
    h = Harness(plugin, tmp_path)
    req = _req(config={"migrations": {"dir": "migrations"}})
    facts: dict = {}

    def between(state):
        again = h.call("configure", req, state=state)          # migrations are idempotent
        assert again.ok and "0 applied" in again.evidence[-1].detail
        b = h.call("backup", req, state=state)
        assert b.ok, b.errors
        tested = next(e for e in b.evidence if e.check == "tested_restore")
        assert tested.passed and "every row count and content checksum matches" in tested.detail
        assert "1000 rows" not in tested.detail and "1252 rows" in tested.detail  # 250 + 1000 + 2 migrations
        facts["backup"] = tested.detail
        state = {**state, **b.state}
        # lose data, then restore it from the backup
        url = h.broker.resolve(state["adminUrlRef"])
        from boss_engine.infra_plugins._runtime import Session
        ctx = h.ctx(plugin.manifest.spec.hooks[0], req, state=state)
        plugin.psql(ctx, Session(ctx, plugin.plugin_id), url, "DELETE FROM comments; DELETE FROM tickets;")
        r = h.call("restore", req, state=state)
        assert r.ok, (r.errors, r.evidence)
        assert "1252 rows match the backup" in r.evidence[-1].detail
        facts["restore"] = r.evidence[-1].detail
        return state

    summary = h.lifecycle(req, between=between, container_service="res-db")
    summary.update(facts)
    h.record("postgres", summary)
