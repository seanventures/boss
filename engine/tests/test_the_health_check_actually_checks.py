"""Three apps read "up · 100% uptime · 0ms latency" for two days with nothing ever probed.

Asked what the green line on the health card was, the answer turned out to be: nothing.

    deployment_targets.health_checks = [{"path": "/", "scheme": "http", "expect_status": 200}]

    observe():  for check in checks:
                    if check.get("kind") != "http":
                        results.append({"check": check, "skipped": "unsupported kind"})
                        continue

There is no `kind` field. Nothing writes one. So every check was skipped — and `healthy` was
initialised to True and only ever set False by a check that FAILED, so a target whose every
check was skipped came back healthy. 2,486 samples, all `ok`, all `latency_ms = 0`, because the
timer measured a function that returned without doing anything.

A green light not wired to anything is worse than a red one: it is the same reassurance a
working system gives, and it is what an operator makes decisions on.

Three fixes, and the third is the one that matters most:
  * infer the kind from the shape, since the shape is unambiguous;
  * read `expect_status`, which is the field the writer actually used;
  * a check that could not run is UNKNOWN, never healthy.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services.deployment_connectors import (
    _check_kind,
    _expected_status,
    tcp_probe,
)


class TestTheCheckThatWasBeingSkipped:
    def test_the_shape_every_target_actually_stores_is_an_http_check(self):
        """Verbatim from deployment_targets on the live fleet."""
        assert _check_kind({"path": "/", "scheme": "http", "expect_status": 200}) == "http"

    def test_a_declared_kind_still_wins(self):
        assert _check_kind({"kind": "tcp", "path": "/"}) == "tcp"

    def test_a_bare_port_is_a_socket_check(self):
        assert _check_kind({"port": 3012}) == "tcp"

    def test_something_unrecognisable_is_not_guessed_at(self):
        """Better to report a check as unrunnable than to probe the wrong thing."""
        assert _check_kind({"note": "ask someone"}) == ""

    def test_the_status_field_the_writer_used_is_read(self):
        # The reader asked for `expect`; the writer wrote `expect_status`. Both work now.
        assert _expected_status({"expect_status": 204}) == 204
        assert _expected_status({"expect": 204}) == 204
        assert _expected_status({}) == 200

    def test_a_junk_status_falls_back_rather_than_raising(self):
        assert _expected_status({"expect_status": "soon"}) == 200


class TestASkippedCheckIsNotHealthy:
    """The line that turned an unprobed fleet green."""

    def test_nothing_probed_reports_unknown(self):
        body = inspect.getsource(
            __import__("boss_engine.services.deployment_connectors",
                       fromlist=["x"]).SSHDockerConnector.observe
            if hasattr(__import__("boss_engine.services.deployment_connectors",
                                  fromlist=["x"]), "SSHDockerConnector")
            else __import__("boss_engine.services.deployment_connectors", fromlist=["x"]))
        assert "if not probed:" in body
        assert '"healthy": None' in body

    def test_the_reason_says_what_happened(self):
        module = __import__("boss_engine.services.deployment_connectors", fromlist=["x"])
        body = inspect.getsource(module)
        assert "nothing was probed: no check on this target could be run" in body

    def test_a_target_with_no_check_gets_a_socket_probe_rather_than_a_pass(self):
        """"No check declared" was an unexplained blank. The socket always answers."""
        module = __import__("boss_engine.services.deployment_connectors", fromlist=["x"])
        body = inspect.getsource(module)
        assert 'checks = [{"kind": "tcp"}]' in body


class TestTheSocketProbe:
    """What the operator asked for by name: is the port open, not does the page render."""

    def test_a_closed_port_is_reported_closed(self):
        out = tcp_probe("127.0.0.1", 1, timeout=2)
        assert out["ok"] is False
        assert out["error"]

    def test_it_times_its_own_attempt(self):
        out = tcp_probe("127.0.0.1", 1, timeout=2)
        assert isinstance(out["latency_ms"], int)

    def test_an_open_port_is_reported_open(self):
        import socket
        import threading

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        def accept_once():
            # Closing the socket under a blocked accept() raises in the thread; swallowing it
            # keeps the test from emitting an unhandled-thread warning that reads like a defect
            # in the code under test.
            try:
                server.accept()
            except OSError:
                pass

        threading.Thread(target=accept_once, daemon=True).start()
        try:
            assert tcp_probe("127.0.0.1", port, timeout=3)["ok"] is True
        finally:
            server.close()

    def test_it_never_raises(self):
        """Unreachable IS the measurement — a probe that throws loses the sample."""
        for host, port in [("127.0.0.1", 1), ("no-such-host.invalid", 80)]:
            assert tcp_probe(host, port, timeout=2)["ok"] is False


class TestServiceHealthReachesGrafana:
    """The dashboard measured the app's /metrics only, so an uninstrumented app looked exactly
    like a dead one. `probe_success` comes from something that opened the front door."""

    def _dashboard(self):
        import json

        from boss_engine.services.observability_stack import render_dashboard

        return json.loads(render_dashboard({
            "app": "Meeting cost meter", "environment": "staging", "project": "p",
            "repo": "o/r", "port": 3012, "scrape_url": "host.docker.internal:3012",
            "probe_url": "http://host.docker.internal:3012/",
        }))

    def test_the_dashboard_answers_is_it_answering(self):
        titles = [p["title"] for p in self._dashboard()["panels"]]
        assert "Service reachable" in titles
        assert "Availability (24h)" in titles
        assert "Response time (s)" in titles

    def test_it_reads_the_probe_not_the_scrape(self):
        exprs = " ".join(
            t["expr"] for p in self._dashboard()["panels"] for t in p.get("targets", []))
        assert "probe_success" in exprs
        assert "probe_duration_seconds" in exprs

    def test_zero_says_not_answering_rather_than_showing_a_bare_zero(self):
        panel = next(p for p in self._dashboard()["panels"] if p["title"] == "Service reachable")
        mappings = panel["fieldConfig"]["defaults"]["mappings"][0]["options"]
        assert mappings["0"]["text"] == "NOT ANSWERING"
        assert mappings["1"]["text"] == "answering"

    def test_panel_ids_are_unique(self):
        panels = self._dashboard()["panels"]
        assert len({p["id"] for p in panels}) == len(panels)

    def test_the_prober_is_in_the_stack(self):
        from boss_engine.services.observability_stack import (
            DEFAULT_PORTS, render_compose, stack_files,
        )

        target = {"target_id": "t", "app": "a", "environment": "staging", "project": "p",
                  "repo": "o/r", "port": 3012, "scrape_url": "host.docker.internal:3012",
                  "public_url": "1.2.3.4:3012", "probe_url": "http://host.docker.internal:3012/"}
        files = stack_files([target], DEFAULT_PORTS)
        assert "blackbox.yml" in files
        assert "boss-blackbox" in render_compose(DEFAULT_PORTS)
        assert "service-health" in files["prometheus.yml"]

    def test_it_probes_the_front_door_not_the_metrics_endpoint(self):
        from boss_engine.services.observability_stack import DEFAULT_PORTS, render_prometheus_config

        cfg = render_prometheus_config(DEFAULT_PORTS, [{
            "app": "a", "environment": "staging", "project": "p",
            "probe_url": "http://host.docker.internal:3012/"}])
        assert "http://host.docker.internal:3012/" in cfg
        assert "/metrics" not in cfg.split("service-health")[1]

    def test_a_project_with_nothing_deployed_renders_no_probe_job(self):
        """An empty static_configs block is a Prometheus config error, not an empty dashboard."""
        from boss_engine.services.observability_stack import DEFAULT_PORTS, render_prometheus_config

        assert "service-health" not in render_prometheus_config(DEFAULT_PORTS, [])


@pytest.mark.parametrize("check,kind", [
    ({"path": "/", "scheme": "http", "expect_status": 200}, "http"),
    ({"scheme": "https"}, "http"),
    ({"path": "/healthz"}, "http"),
    ({"kind": "TCP"}, "tcp"),
    ({"port": 8080}, "tcp"),
    ({}, ""),
])
def test_the_inference_table(check, kind):
    assert _check_kind(check) == kind
