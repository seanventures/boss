"""official.pgvector on a real PostgreSQL 17 + pgvector 0.8.1 provisioned by official.postgres."""
from __future__ import annotations

import pytest

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import Harness, docker_ok

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")

DB = {"name": "db", "capability": "database.relational", "engine": "postgres", "features": ["pgvector"]}
VEC = {"name": "vectors", "capability": "database.vector", "dependsOn": ["db"], "config": {"database": "db"}}


def test_pgvector_must_depend_on_its_database(tmp_path):
    h = Harness(load_plugin("pgvector"), tmp_path)
    out = h.call("validate", ResourceRequest.model_validate({**VEC, "dependsOn": []}))
    assert out.status is OperationStatus.INVALID and "dependsOn" in out.errors[0].message


def test_pgvector_refuses_a_database_without_the_extension_image(tmp_path):
    h = Harness(load_plugin("pgvector"), tmp_path)
    up = {"adminUrlRef": "app_secret://deploy/a/DB_ADMIN_URL", "image": "postgres:17.6-alpine@sha256:" + "0" * 64}
    out = h.call("provision", ResourceRequest.model_validate(VEC), state={"upstream": up})
    assert out.status is OperationStatus.FAILED and "features: [pgvector]" in out.errors[0].message


@needs_docker
def test_pgvector_lifecycle_on_a_real_database(tmp_path):
    pg = Harness(load_plugin("postgres"), tmp_path)
    db = ResourceRequest.model_validate(DB)
    first = pg.call("provision", db)
    assert first.ok, first.errors
    vec = Harness(load_plugin("pgvector"), tmp_path, app_key=pg.app_key, broker=pg.broker)
    try:
        summary = vec.lifecycle(ResourceRequest.model_validate(VEC), state={"upstream": first.state}, clean=False)
    finally:
        plan = pg.call("plan", db, state={**first.state, "desired": "absent"}).plan
        assert pg.call("destroy", db, state=first.state, plan=plan).ok
    pg.assert_clean()
    vec.assert_redacted()
    vec.record("pgvector", summary)
