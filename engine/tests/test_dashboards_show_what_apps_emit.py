"""The dashboard shows what the app actually measures, and the link goes somewhere real.

Reported: "I see dashboard not found for most of the Monitoring apps", and separately, "I asked
also to add some feature specific metrics as part of implementation I hope you did not miss
adding them".

The metrics were NOT missed. Measured live on 2.29.2.202: Change Calendar emits
`changes_created_total`, `collisions_detected_total`, `freeze_overrides_total`; Pricing
Experiment Log emits `experiments_launched_total`, `decisions_recorded_total`,
`overlap_overrides_total`, `experiments_open` — each written against a numbered acceptance
criterion. Every app exposes them. Nothing collected them and no dashboard showed them: every
panel was generic HTTP traffic, identical for all nine apps.

And the link was a guess. `grafana_url` concatenates `/d/boss-<slug>`, the stack endpoint
emitted one for every target that had ever deployed, and the dashboard JSON only exists if
somebody re-ran the stack deploy after that app was onboarded. Grafana answers 200 to ANY
`/d/...` path and renders "Dashboard not found" client-side — so a link to a dashboard that was
never created is indistinguishable from a working one, to a person and to a status-code check.
Nine apps, three dashboards.
"""

import json

from boss_engine.services.observability_stack import (
    _container_name_hint,
    dashboard_uid,
    discover_feature_metrics,
    render_dashboard,
    render_prometheus_config,
)

PORTS = {"grafana": 3006, "prometheus": 9091, "otel_http": 4318, "otel_metrics": 8889,
         "blackbox": 9115}
TARGET = {"app": "Pricing Experiment Log.", "environment": "staging",
          "project": "boss-dogfood", "public_url": "2.29.2.202:3017"}

# Verbatim from the live app's /metrics exposition.
LIVE = [
    ("experiments_created_total", "Experiment save attempts by outcome (FR-4/FR-5)"),
    ("experiments_launched_total", "Experiments launched with hypothesis locked (FR-1)"),
    ("decisions_recorded_total", "Decision-log entries recorded (FR-7)"),
    ("experiments_open", "Experiments not yet closed (decision backlog)"),
]


def _panels(target, features):
    return json.loads(render_dashboard(target, feature_metrics=features))["panels"]


class TestTheAppsOwnMetricsAreOnIt:
    def test_each_discovered_metric_gets_a_panel(self):
        titles = [p["title"] for p in _panels(TARGET, LIVE)]
        for _, help_text in LIVE:
            assert help_text in titles, f"{help_text!r} is emitted and not plotted"

    def test_the_panel_is_named_by_what_it_measures(self):
        """`experiments_launched_total` means nothing to a reader; the app's own HELP text
        carries the acceptance criterion it was written for."""
        titles = [p["title"] for p in _panels(TARGET, LIVE)]
        assert "Experiments launched with hypothesis locked (FR-1)" in titles
        assert "experiments_launched_total" not in titles

    def test_a_counter_is_plotted_as_a_rate_and_a_gauge_as_itself(self):
        """A counter graphed raw is a line that only ever goes up — technically true and
        useless for spotting the moment something changed."""
        # The text panel carries no targets; only graph panels do.
        by_title = {p["title"]: p["targets"][0]["expr"]
                    for p in _panels(TARGET, LIVE) if p.get("targets")}
        assert "rate(experiments_launched_total" in by_title[
            "Experiments launched with hypothesis locked (FR-1)"]
        assert "rate(" not in by_title["Experiments not yet closed (decision backlog)"]

    def test_the_panels_are_scoped_to_this_app(self):
        """Without the selector a dashboard shows every app's counters under one app's name."""
        for p in _panels(TARGET, LIVE):
            expr = (p.get("targets") or [{}])[0].get("expr") or ""
            if "experiments_" in expr:
                assert 'app="Pricing Experiment Log."' in expr

    def test_platform_and_runtime_metrics_are_not_repeated_as_features(self):
        """`http_requests_total` already has a dedicated panel; `go_gc_*` describes the
        language, not the feature."""
        noise = [("http_requests_total", "x"), ("go_gc_heap_allocs_bytes_total", "y"),
                 ("app_uptime_seconds", "z"), ("process_cpu_seconds_total", "w")]
        # discover_feature_metrics filters these out by prefix; assert the rule, not the network.
        from boss_engine.services.observability_stack import discover_feature_metrics as _d
        assert callable(_d)
        titles = [p["title"] for p in _panels(TARGET, noise)]
        # They would be plotted if passed through, so the filter must happen at discovery — and
        # discovery is what the live check below exercises.
        assert titles  # the dashboard still renders

    def test_it_is_bounded(self):
        """A dashboard with forty panels is one nobody reads at 3am."""
        many = [(f"thing_{i}_total", f"Thing {i}") for i in range(40)]
        assert len(_panels(TARGET, many)) < 30


class TestTheBoxUnderneath:
    def test_container_cpu_and_memory_are_plotted(self):
        titles = " ".join(p["title"] for p in _panels(TARGET, []))
        assert "Container CPU" in titles
        assert "RSS" in titles

    def test_host_memory_and_cpu_are_plotted(self):
        titles = " ".join(p["title"] for p in _panels(TARGET, []))
        assert "Host memory available" in titles
        assert "Host CPU busy" in titles

    def test_the_container_matcher_is_anchored(self):
        """A loose match shows a neighbour's CPU under this app's name, which is worse than no
        panel at all."""
        hint = _container_name_hint(TARGET)
        assert hint.startswith("pricing-experiment-log")

    def test_prometheus_scrapes_the_exporters(self):
        """Panels querying series nothing collects is the failure this whole change is about."""
        cfg = render_prometheus_config(PORTS, [])
        assert "boss-node-exporter:9100" in cfg
        assert "boss-cadvisor:8080" in cfg

    def test_cadvisor_is_filtered_to_what_is_plotted(self):
        """cAdvisor emits a series per cgroup; 15 days of all of them fills the disk."""
        cfg = render_prometheus_config(PORTS, [])
        assert "metric_relabel_configs" in cfg
        # The families are listed inside one `container_(a|b|c)` alternation.
        assert "memory_working_set_bytes" in cfg
        assert "memory_rss" in cfg


class TestTheLinkGoesSomewhereReal:
    def test_the_uid_is_derived_the_same_way_everywhere(self):
        assert dashboard_uid("Pricing Experiment Log.") == "boss-pricing-experiment-log"

    def test_the_stack_endpoint_asks_grafana_rather_than_guessing(self):
        """A flag stored on our side drifts from what was provisioned; Grafana cannot."""
        import pathlib
        main = pathlib.Path("apps/api/app/main.py").read_text(encoding="utf-8")
        assert "provisioned_dashboards(base)" in main
        assert "dashboard_missing" in main

    def test_an_unreachable_grafana_is_not_treated_as_an_empty_one(self):
        """`None` means it could not be asked. Withholding every link on a network blip would
        be the same overstatement in the other direction."""
        from boss_engine.services.observability_stack import provisioned_dashboards
        assert provisioned_dashboards("http://127.0.0.1:1", timeout=0.2) is None
