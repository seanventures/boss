"""official.docker-compose: assemble a real resource into an override, then deploy it with an app
the way the stack deploy does, and check nothing is recreated and the app gets its binding."""
from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import WHOAMI, Harness, app_compose, compose_up, docker_ok

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")

ENV = ResourceRequest.model_validate({"name": "env", "capability": "environment.compose",
                                      "config": {"appServices": ["app"]}})


def test_no_resources_and_name_collisions_are_refused(tmp_path):
    h = Harness(load_plugin("docker-compose"), tmp_path)
    assert h.call("provision", ENV).errors[0].code == "no_resources"
    clash = {"cache": {"fragment": {"services": {"app": {"image": "x"}}}, "bindings": {}}}
    assert h.call("provision", ENV, state={"resources": clash}).errors[0].code == "conflict"


@needs_docker
def test_compose_environment_lifecycle(tmp_path):
    cache = Harness(load_plugin("valkey"), tmp_path)
    creq = ResourceRequest.model_validate({"name": "cache", "capability": "cache"})
    c = cache.call("provision", creq)
    assert c.ok, c.errors
    h = Harness(load_plugin("docker-compose"), tmp_path, app_key=cache.app_key)
    resources = {"cache": {"fragment": c.state["fragment"], "bindings": c.model_dump(mode="json")["bindings"]}}

    def between(state):
        override = yaml.safe_load(state["compose"])
        app = override["services"]["app"]
        assert app["environment"] == {"REDIS_URL": "${REDIS_URL}"}
        assert app["depends_on"] == {"res-cache": {"condition": "service_healthy"}}
        assert state["deploy"]["env"] == {"REDIS_URL": {"secret": "REDIS_URL"}}
        # What the stack deploy does: the app's compose + the override, the env file's values.
        files = [app_compose(h, {"app": {"image": WHOAMI}}), tmp_path / "override.yaml"]
        files[1].write_text(state["compose"])
        before = cache.container_id("res-cache")
        compose_up(h, files, env={"REDIS_URL": cache.broker.store[c.bindings["REDIS_URL"].secretRef]})
        assert cache.container_id("res-cache") == before, "deploying the override recreated the resource"
        env = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .Config.Env}}", h.container_id("app")],
                                        capture_output=True, text=True).stdout)
        assert any(e.startswith("REDIS_URL=redis://") for e in env)
        refused = h.call("destroy", ENV, state=state)
        assert refused.status is OperationStatus.REFUSED and refused.errors[0].code == "has_dependents"
        cplan = cache.call("plan", creq, state={**c.state, "desired": "absent"}).plan
        assert cache.call("destroy", creq, state=c.state, plan=cplan).ok
        return state

    summary = h.lifecycle(ENV, state={"resources": resources}, between=between)
    cache.assert_clean()
    cache.assert_redacted()
    h.record("docker-compose", summary)
