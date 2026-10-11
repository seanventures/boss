"""official.valkey against a real Valkey 8.1 container."""
from __future__ import annotations

import pytest

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import Harness, docker_ok

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")


def _req(**kw) -> ResourceRequest:
    return ResourceRequest.model_validate({"name": "cache", "capability": "cache", "engine": "valkey", **kw})


def test_a_cache_is_never_backed_up_or_public(tmp_path):
    h = Harness(load_plugin("valkey"), tmp_path)
    out = h.call("validate", _req(backups=True, publicAccess=True))
    text = " ".join(e.message for e in out.errors)
    assert out.status is OperationStatus.INVALID and "never a durable store" in text and "publicly" in text
    assert "not a durable store" in " ".join(h.plugin.manifest.spec.limitations)
    assert h.plugin.manifest.implements("backup") is False


@needs_docker
def test_valkey_lifecycle(tmp_path):
    h = Harness(load_plugin("valkey"), tmp_path)
    summary = h.lifecycle(_req(config={"maxMemoryMb": 64}), container_service="res-cache")
    h.record("valkey", summary)
