"""Every health probe asks the checks the target DECLARED — whatever its kind.

Before: an API gateway (no page at "/", 200 on /healthz and /readyz) read "Down" on two
targets at once. Its stack target was probed with a GET of "/" (404) because StackHealthProbe
ignored `health_checks`; its config_dir target had no connector, so every sweep recorded
"no connector for target_kind 'config_dir'" as a FAILED sample, forever.

Now one function (deployment_connectors.declared_health_probes) turns a target's declared checks
— both shapes — into what to ask, and every probe path uses it first. "/" is only the fallback
for a stack target that declared nothing; a target with no connector and no check is unknown
(ok None), which the sweep never records as a failure.

The server below is real: "/" answers 404, /healthz and /readyz answer 200.
"""
from __future__ import annotations

import http.server
import threading

import pytest

from boss_engine.config import Settings
from boss_engine.services import deployment_health
from boss_engine.services.deployment_connectors import (
    _check_kind,
    declared_health_probes,
    supported_kinds,
)

#: Every kind a target can have that the sweep probes: the connector table, plus the two kinds
#: probed WITHOUT a connector. A new connector is covered the moment it is registered.
ALL_KINDS = sorted(set(supported_kinds()) | {"stack", "config_dir"})


@pytest.fixture(scope="module")
def api_only():
    class Api(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code = 200 if self.path in ("/healthz", "/readyz") else 404
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"ok" if code == 200 else b"unmatched")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _path_shape(kind: str, port: int, path: str = "/healthz") -> dict:
    """The stack target's shape, verbatim from the live install (host made local)."""
    return {"target_kind": kind, "domains": [f"127.0.0.1:{port}"],
            "target_ref": "deploy@127.0.0.1:22",
            "health_checks": [{"kind": "http", "path": path, "port": port, "scheme": "http",
                               "expect_status": 200}]}


def _url_shape(kind: str, port: int, path: str = "/readyz") -> dict:
    """The config_dir target's shape, verbatim from the live install (host made local)."""
    url = f"http://127.0.0.1:{port}{path}"
    return {"target_kind": kind, "domains": [url],
            "health_checks": [{"url": url, "expected_status": 200}]}


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("shape", [_path_shape, _url_shape])
def test_a_declared_check_that_answers_is_healthy(kind, shape, api_only):
    sample = deployment_health.probe(shape(kind, api_only), Settings())
    assert sample["ok"] is True, f"{kind}: declared check ignored → {sample}"
    assert sample["status_code"] == 200


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("shape", [_path_shape, _url_shape])
def test_a_declared_check_is_what_is_asked(kind, shape, api_only):
    """The complement: the declared path is really asked — a missing one fails, and the sample
    names the declared URL, not '/'."""
    sample = deployment_health.probe(shape(kind, api_only, "/missing"), Settings())
    assert sample["ok"] is False
    assert sample["status_code"] == 404
    assert "expected 2xx-3xx" not in (sample["error"] or ""), "fell back to the '/' probe"


@pytest.mark.parametrize("kind", sorted(set(ALL_KINDS) - set(supported_kinds()) - {"stack"}))
def test_no_connector_and_no_check_is_unknown_not_a_failure(kind):
    sample = deployment_health.probe({"target_kind": kind, "domains": ["127.0.0.1:1"]},
                                     Settings())
    assert sample["ok"] is None, "an unknown target must not record a failure sample"
    assert "no health check declared" in sample["error"]


def test_an_unregistered_kind_is_probed_by_its_declared_checks(api_only):
    """Any kind added before its connector is asked through its checks, not refused."""
    assert deployment_health.probe(_path_shape("k8s_someday", api_only), Settings())["ok"] is True


def test_a_stack_target_with_no_declared_check_still_falls_back_to_its_front_door(api_only):
    sample = deployment_health.probe(
        {"target_kind": "stack", "public_url": f"http://127.0.0.1:{api_only}/"}, Settings())
    assert sample["ok"] is False and sample["status_code"] == 404


class TestOneResolver:
    def test_the_path_shape_resolves_against_the_domain_host(self):
        got = declared_health_probes({
            "domains": ["192.0.2.202:8080"],
            "health_checks": [{"kind": "http", "path": "/healthz", "port": 8080,
                               "scheme": "http", "expect_status": 200}]})
        assert got[0]["url"] == "http://192.0.2.202:8080/healthz"
        assert got[0]["expected"] == 200

    def test_the_url_shape_is_asked_as_written(self):
        got = declared_health_probes({
            "domains": ["http://192.0.2.202:8080/readyz"],
            "health_checks": [{"url": "http://192.0.2.202:8080/readyz", "expected_status": 204}]})
        assert got == [{"kind": "http", "url": "http://192.0.2.202:8080/readyz", "expected": 204,
                        "check": {"url": "http://192.0.2.202:8080/readyz",
                                  "expected_status": 204}}]

    def test_a_url_domain_gives_relative_checks_their_host_and_scheme(self):
        got = declared_health_probes({"domains": ["http://192.0.2.9:9000/readyz"],
                                      "health_checks": [{"path": "/healthz"}]})
        assert got[0]["url"] == "http://192.0.2.9:9000/healthz"

    def test_the_host_falls_back_to_the_public_url_then_the_target_ref(self):
        assert declared_health_probes({"public_url": "https://gw.example.test",
                                       "health_checks": [{"path": "/healthz"}]}
                                      )[0]["url"] == "https://gw.example.test/healthz"
        assert declared_health_probes({"target_ref": "deploy@192.0.2.5:22",
                                       "health_checks": [{"path": "/h", "scheme": "http",
                                                          "port": 8080}]}
                                      )[0]["url"] == "http://192.0.2.5:8080/h"

    def test_the_published_port_still_wins_over_a_stale_domain_port(self):
        got = declared_health_probes({"domains": ["192.0.2.1:8000"],
                                      "health_checks": [{"path": "/", "scheme": "http"}]},
                                     app_port=3020)
        assert got[0]["url"] == "http://192.0.2.1:3020/"

    def test_an_unrunnable_check_is_said_not_dropped(self):
        got = declared_health_probes({"domains": ["h"], "health_checks": [{"kind": "smtp"}]})
        assert got[0]["skipped"]

    def test_a_url_is_an_http_check(self):
        assert _check_kind({"url": "http://h/readyz", "expected_status": 200}) == "http"


class TestEveryOtherReaderUsesIt:
    """The other places that turn a target into "the URL to ask" go through the same resolver,
    so "Probe now", the blackbox scrape and the metrics origin cannot drift back to '/'."""

    def test_the_briefing_probe_asks_the_declared_check(self):
        from boss_engine.services.factory_briefing import _probe_url

        t = {"domains": ["192.0.2.202:8080"],
             "health_checks": [{"kind": "http", "path": "/healthz", "port": 8080,
                                "scheme": "http", "expect_status": 200}]}
        assert _probe_url(t, "192.0.2.202:8080") == "http://192.0.2.202:8080/healthz"
        u = {"domains": ["http://192.0.2.202:8080/readyz"],
             "health_checks": [{"url": "http://192.0.2.202:8080/readyz", "expected_status": 200}]}
        assert _probe_url(u, "http://192.0.2.202:8080/readyz") == "http://192.0.2.202:8080/readyz"
        assert _probe_url({"domains": ["h:1"]}, "h:1") == "http://h:1/"

    def test_the_blackbox_probe_asks_the_declared_path(self):
        from boss_engine.services.observability_stack import _declared_path

        assert _declared_path({"domains": ["h:8080"],
                               "health_checks": [{"path": "/healthz", "scheme": "http"}]}) \
            == "/healthz"
        assert _declared_path({"domains": ["h:8080"], "health_checks": []}) == "/"

    def test_the_metrics_origin_is_the_declared_checks(self):
        from boss_engine.services.app_metrics import public_origin

        assert public_origin({"target_kind": "config_dir",
                              "domains": ["http://192.0.2.202:8080/readyz"],
                              "health_checks": [{"url": "http://192.0.2.202:8080/readyz",
                                                 "expected_status": 200}]}) \
            == "http://192.0.2.202:8080"
