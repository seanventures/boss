"""official.docker-compose assembles a real app with a real resource and brings it up."""
from __future__ import annotations

import pytest
import yaml

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import WHOAMI, Harness, app_compose, docker_ok

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")

ENV = ResourceRequest.model_validate({"name": "env", "capability": "environment.compose",
                                      "config": {"appCompose": "app/docker-compose.yml"}})


def test_a_resource_colliding_with_an_app_service_is_refused(tmp_path):
    h = Harness(load_plugin("docker-compose"), tmp_path)
    app_compose(h, {"res-cache": {"image": WHOAMI}})
    (tmp_path / "compose").mkdir()
    (tmp_path / "compose" / "cache.yaml").write_text(yaml.safe_dump({"services": {"res-cache": {"image": "x"}}}))
    out = h.call("provision", ENV, state={"resources": {"cache": {"fragment": "compose/cache.yaml"}}})
    assert out.status is OperationStatus.FAILED and out.errors[0].code == "conflict"


@needs_docker
def test_compose_environment_lifecycle(tmp_path):
    cache = Harness(load_plugin("valkey"), tmp_path)
    creq = ResourceRequest.model_validate({"name": "cache", "capability": "cache"})
    c = cache.call("provision", creq)
    assert c.ok, c.errors
    h = Harness(load_plugin("docker-compose"), tmp_path, app_key=cache.app_key, broker=cache.broker)
    app_compose(h, {"app": {"image": WHOAMI}})
    resources = {"cache": {**c.state, "bindings": c.model_dump(mode="json")["bindings"]}}

    def between(state):
        assembled = yaml.safe_load((tmp_path / ".boss" / "compose.yaml").read_text())
        app = assembled["services"]["app"]
        assert app["environment"]["REDIS_URL"] == "${REDIS_URL}"
        assert app["depends_on"]["res-cache"]["condition"] == "service_healthy"
        assert state["deploy"]["env"]["REDIS_URL"] == {"secret": "CACHE_REDIS_URL"}
        plan = h.call("plan", ENV, state={**state, "desired": "absent"}).plan
        refused = h.call("destroy", ENV, state=state, plan=plan)
        assert refused.status is OperationStatus.REFUSED and refused.errors[0].code == "has_dependents"
        cplan = cache.call("plan", creq, state={**c.state, "desired": "absent"}).plan
        assert cache.call("destroy", creq, state=c.state, plan=cplan).ok
        return state

    summary = h.lifecycle(ENV, state={"resources": resources}, between=between, container_service="app")
    h.record("docker-compose", summary)
