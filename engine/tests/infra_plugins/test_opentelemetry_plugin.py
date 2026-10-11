"""official.opentelemetry: a real collector; a test span sent with the app's binding must arrive."""
from __future__ import annotations

import pytest

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import Harness, docker_ok

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")


def _req(**kw) -> ResourceRequest:
    return ResourceRequest.model_validate({"name": "otel", "capability": "observability.telemetry", **kw})


def test_export_target_may_not_carry_credentials(tmp_path):
    h = Harness(load_plugin("opentelemetry"), tmp_path)
    assert h.call("validate", _req(config={"exportTo": "https://user:pw@otel.example/v1"})).status is OperationStatus.INVALID
    assert h.call("validate", _req(config={"exportTo": "https://otel.example:4318"})).status is OperationStatus.VALID


@needs_docker
def test_opentelemetry_lifecycle(tmp_path):
    h = Harness(load_plugin("opentelemetry"), tmp_path)
    summary = h.lifecycle(_req(), container_service="res-otel")
    ver = next(r for r in h.results if r.hook.value == "verify")
    assert any(e.check == "span_arrived" and e.passed for e in ver.evidence)
    h.record("opentelemetry", summary)
