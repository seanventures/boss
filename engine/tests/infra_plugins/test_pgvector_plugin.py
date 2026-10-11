"""official.pgvector on a real PostgreSQL 17 + pgvector 0.8.1 that official.postgres provisioned."""
from __future__ import annotations

import pytest

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import Harness, docker_ok

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")

VEC = {"name": "vectors", "capability": "database.vector", "dependsOn": ["db"], "config": {"database": "db"}}


def _db(**kw) -> ResourceRequest:
    return ResourceRequest.model_validate({"name": "db", "capability": "database.relational", "engine": "postgres", **kw})


def test_pgvector_must_depend_on_its_database(tmp_path):
    out = Harness(load_plugin("pgvector"), tmp_path).call("validate", ResourceRequest.model_validate({**VEC, "dependsOn": []}))
    assert out.status is OperationStatus.INVALID and "dependsOn" in out.errors[0].message


def _with_db(tmp_path, features, body):
    pg = Harness(load_plugin("postgres"), tmp_path)
    db = _db(features=features)
    first = pg.call("provision", db)
    assert first.ok, first.errors
    vec = Harness(load_plugin("pgvector"), tmp_path, app_key=pg.app_key)
    try:
        return body(vec)
    finally:
        plan = pg.call("plan", db, state={**first.state, "desired": "absent"}).plan
        assert pg.call("destroy", db, state=first.state, plan=plan).ok
        pg.assert_clean()


@needs_docker
def test_pgvector_refuses_a_database_without_the_extension_image(tmp_path):
    def body(vec):
        out = vec.call("provision", ResourceRequest.model_validate(VEC))
        assert out.status is OperationStatus.FAILED and "features: [pgvector]" in out.errors[0].message
    _with_db(tmp_path, [], body)


@needs_docker
def test_pgvector_lifecycle_on_a_real_database(tmp_path):
    def body(vec):
        summary = vec.lifecycle(ResourceRequest.model_validate(VEC), clean=False)
        vec.record("pgvector", summary)
    _with_db(tmp_path, ["pgvector"], body)
