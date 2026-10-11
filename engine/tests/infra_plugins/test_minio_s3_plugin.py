"""official.minio-s3 against a real MinIO: a bucket, a key scoped to it, put/get as the app."""
from __future__ import annotations

import pytest

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import Harness, docker_ok

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")


def _req(**kw) -> ResourceRequest:
    return ResourceRequest.model_validate({"name": "files", "capability": "object-storage", "engine": "minio", **kw})


def test_bad_bucket_names_are_invalid_and_bindings_are_all_refs(tmp_path):
    h = Harness(load_plugin("minio-s3"), tmp_path)
    assert h.call("validate", _req(config={"bucket": "Bad_Name"})).status is OperationStatus.INVALID
    plan = h.call("plan", _req()).plan
    assert plan.bindings == ["S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"]
    assert plan.publicAccess is False


@needs_docker
def test_minio_lifecycle(tmp_path):
    h = Harness(load_plugin("minio-s3"), tmp_path)
    summary = h.lifecycle(_req(), container_service="res-files")
    prov = next(r for r in h.results if r.hook.value == "provision")
    assert set(prov.bindings) == {"S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"}
    assert all(b.secretRef.startswith("app_secret://") for b in prov.bindings.values())
    h.record("minio-s3", summary)
