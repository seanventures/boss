"""official.caddy in front of a real app container: HTTP, and HTTPS with Caddy's local CA."""
from __future__ import annotations

import subprocess

import pytest

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import WHOAMI, Harness, app_compose, compose_up, docker_ok, free_port

needs_docker = pytest.mark.skipif(not docker_ok(), reason="BOSS_RUN_DOCKER_LIFECYCLE_TEST=1 and Docker needed")


def _req(**config) -> ResourceRequest:
    return ResourceRequest.model_validate({"name": "web", "capability": "network.ingress",
                                           "config": {"upstream": "app:80", **config}})


def test_binding_beyond_loopback_needs_public_access(tmp_path):
    h = Harness(load_plugin("caddy"), tmp_path)
    out = h.call("validate", _req(bindAddress="0.0.0.0"))
    assert out.status is OperationStatus.INVALID and "publicAccess" in out.errors[0].message
    req = ResourceRequest.model_validate({"name": "web", "capability": "network.ingress", "publicAccess": True,
                                          "config": {"upstream": "app:80", "hostname": "helpdesk.example.com"}})
    plan = h.call("plan", req).plan
    assert plan.publicAccess is True and {p.containerPort for p in plan.ports} == {80, 443}


def test_caddyfile_has_tls_only_with_a_hostname(tmp_path):
    plugin = load_plugin("caddy")
    h = Harness(plugin, tmp_path)
    ctx = h.ctx(plugin.manifest.spec.hooks[0], _req())
    assert plugin.caddyfile(ctx).split("\n\n")[1].startswith(":80 {")
    ctx = h.ctx(plugin.manifest.spec.hooks[0], _req(hostname="10.0.0.5"))
    assert "tls internal" in plugin.caddyfile(ctx)
    ctx = h.ctx(plugin.manifest.spec.hooks[0], _req(hostname="helpdesk.example.com"))
    text = plugin.caddyfile(ctx)
    assert "helpdesk.example.com {" in text and "tls internal" not in text  # ACME


@needs_docker
@pytest.mark.parametrize("tls", ["off", "internal"])
def test_caddy_routes_to_the_app(tmp_path, tls):
    h = Harness(load_plugin("caddy"), tmp_path)
    compose_up(h, [app_compose(h, {"app": {"image": WHOAMI}})], "app")
    config = {"httpPort": free_port(), "httpsPort": free_port()}
    if tls == "internal":
        config.update(hostname="helpdesk.boss.test", tls="internal")
    req = _req(**config)

    def between(state):
        # the app goes away: verify must now fail with a 502 through Caddy, not pass
        proj = f"bossapp-{h.app_key}-deploy"
        cid = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={proj}",
                              "--filter", "label=com.docker.compose.service=app"], capture_output=True, text=True).stdout.split()
        subprocess.run(["docker", "rm", "-f", *cid], capture_output=True)
        out = h.call("verify", req, state=state)
        assert out.status is OperationStatus.FAILED and "502" in out.evidence[0].detail
        return state

    summary = h.lifecycle(req, between=between, container_service="res-web")
    ver = next(r for r in h.results if r.hook.value == "verify")
    if tls == "internal":
        assert "certificate chain verified" in ver.evidence[0].detail
    h.record(f"caddy-{tls}", summary)
