"""The monitoring stack assumed Compose; the host that runs the apps does not have it.

The console linked to http://2.29.2.202:3006/d/boss-hookwatch and the link answered connection
refused, because no Grafana was ever running there. The deploy had been failing all along with:

    docker compose up -d   ->  unknown shorthand flag: 'd' in -d
    docker-compose         ->  command not found

Docker itself is fine on that box — three application containers are up on it right now. App
deploys use plain `docker run`; the observability stack was the only thing that required
Compose, so monitoring could never start there and nothing said so.

Compose is still preferred where it exists. This is the fallback, in the one primitive every
Docker host has.
"""
from __future__ import annotations

import re

from boss_engine.services.observability_stack import render_docker_run, stack_files


# The real defaults, imported rather than retyped: a test that invents its own port
# names passes while the code raises KeyError, which is how this one first "passed".
from boss_engine.services.observability_stack import DEFAULT_PORTS

PORTS = dict(DEFAULT_PORTS)


class TestTheFallbackStartsTheWholeStack:
    def test_all_three_containers_are_started(self):
        script = render_docker_run(PORTS)
        for name in ("boss-otel-collector", "boss-prometheus", "boss-grafana"):
            assert f"--name {name}" in script, name

    def test_grafana_is_published_on_the_configured_port(self):
        assert "-p 3006:3000" in render_docker_run(PORTS)

    def test_they_share_a_network_so_prometheus_is_reachable_by_name(self):
        script = render_docker_run(PORTS)
        assert "docker network create boss-obs" in script
        assert script.count("--network boss-obs") == 3

    def test_the_collector_can_still_reach_the_host(self):
        """Scrape targets are on the host, not in the network — the compose file used
        extra_hosts for exactly this."""
        assert "--add-host host.docker.internal:host-gateway" in render_docker_run(PORTS)

    def test_data_survives_a_restart(self):
        script = render_docker_run(PORTS)
        assert "-v boss-prometheus-data:/prometheus" in script
        assert "-v boss-grafana-data:/var/lib/grafana" in script

    def test_the_generated_config_is_mounted(self):
        script = render_docker_run(PORTS)
        assert "/etc/otel/config.yaml:ro" in script
        assert "/etc/prometheus/prometheus.yml:ro" in script
        assert "/etc/grafana/provisioning/dashboards:ro" in script

    def test_it_is_labelled_like_every_other_boss_deployment(self):
        """Teardown proves ownership by label before removing anything."""
        script = render_docker_run(PORTS)
        assert script.count("--label boss.app=observability") == 3

    def test_rerunning_replaces_rather_than_collides(self):
        """Re-running the deploy is how a newly onboarded app gets a dashboard."""
        script = render_docker_run(PORTS)
        assert "docker rm -f boss-otel-collector boss-prometheus boss-grafana" in script
        assert re.search(r"docker network create boss-obs.*\|\| true", script)


class TestComposeIsStillPreferred:
    def test_compose_is_tried_before_the_fallback(self):
        import inspect

        from boss_engine.services import observability_stack

        body = inspect.getsource(observability_stack.deploy_stack)
        assert body.index("docker compose up -d") < body.index("no docker compose on this host")

    def test_the_fallback_announces_itself(self):
        """So an operator reading the log knows which path ran."""
        import inspect

        from boss_engine.services import observability_stack

        assert "no docker compose on this host, using docker run" in inspect.getsource(
            observability_stack.deploy_stack)


class TestTheFilesAreUnchanged:
    def test_compose_file_is_still_written(self):
        """The fallback changes how the stack is STARTED, not what it is."""
        files = stack_files([], PORTS)
        assert "docker-compose.yml" in files
        assert "prometheus.yml" in files
        assert "otel-config.yaml" in files
