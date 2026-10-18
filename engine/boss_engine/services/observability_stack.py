"""The observability stack BOSS deploys alongside the apps it ships.

BOSS could already READ Prometheus, Loki and Grafana — `telemetry_connectors` has had those
since RB-9, and `build_source` defaults to a 60-second step. What was missing is anything to
read FROM: no collector, no store, no dashboards. The read half has been waiting for a write
half.

    app /metrics ──► otel-collector ──► prometheus ──► grafana ──► console link
                     (scrape, 60s)      (store)        (dashboards)

Four decisions worth stating, because each closes a failure this codebase has already had:

  * **The scrape config is DERIVED from `deployment_targets`.** BOSS already knows every app,
    its host and its port; asking an operator to restate that in a YAML file would create a
    second source of truth that drifts the first time someone changes a port. Onboarding an app
    is what puts it on the dashboard.
  * **Prometheus is reached through the docker bridge gateway**, not the public IP, so metrics
    never traverse the internet and no firewall port is needed for collection. Only Grafana —
    the thing a human looks at — is exposed.
  * **The stack is a normal set of BOSS-managed containers**, labelled like any other, so the
    teardown path can prove ownership before removing them.
  * **Grafana is provisioned from files, not clicks.** A dashboard someone made by hand in the
    UI exists only in that Grafana's database; a provisioned one is reproducible on a fresh
    host, which is the difference between monitoring and a screenshot.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from .core import emit_event
from .deployment_health import DEPLOYED_PREDICATE as _DEPLOYED

# Where the stack lives on the host — beside the apps, not inside any of them.
STACK_DIR = "/opt/boss-observability"

# One pipeline and one Grafana for every app, as requested. Splitting per org means a collector
# per tenant and a Grafana per tenant; the shape below is deliberately the one that survives
# that change — every series already carries `org` and `project` labels, so separation later is
# a routing change rather than a re-instrumentation.
DEFAULT_PORTS = {"grafana": 3006, "prometheus": 9091, "otel_http": 4318, "otel_metrics": 8889,
                 # Probes the app's FRONT DOOR, not its /metrics. See render_blackbox_config.
                 "blackbox": 9115}

# The consolidation interval the operator asked for, and the same default `build_source` uses,
# so a graph in Grafana and a query BOSS runs cannot disagree about resolution.
SCRAPE_INTERVAL_SECONDS = 60


def scrape_targets(connection, project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every DEPLOYED app, as something to scrape.

    Derived rather than configured: an app that BOSS deploys is an app BOSS monitors, and a port
    changed through the console changes the scrape with it.

    A target that has never been deployed successfully is excluded, because a target is a
    DESTINATION and not a service. Scraping one produces `up=0` and a dashboard for something
    that has never existed — which reads as a broken app rather than an empty slot, and is
    exactly how a registered-but-never-deployed demo target ended up presented as a monitored
    service.
    """
    rows = connection.execute(
        sql("SELECT t.id, t.project_id, t.app_name, t.environment, t.repo, t.target_ref, "
            "       t.domains, t.required_resources "
            "FROM deployment_targets t "
            "WHERE t.status = 'active' AND " + _DEPLOYED.format(alias="t") + " "
            + (" AND t.project_id = :p" if project_id else "")
            + " ORDER BY t.app_name"),
        ({"p": project_id} if project_id else {})).mappings().all()

    out = []
    for r in rows:
        port = 8080
        for res in r["required_resources"] or []:
            if isinstance(res, dict) and res.get("kind") == "app_port":
                try:
                    port = int(res.get("name"))
                except (TypeError, ValueError):
                    pass
        out.append({
            "target_id": r["id"], "app": r["app_name"], "environment": r["environment"],
            "project": r["project_id"], "repo": r["repo"], "port": port,
            # host.docker.internal resolves to the host gateway inside the collector container
            # (Docker maps it with --add-host=host-gateway). Scraping the PUBLIC ip would send
            # every sample out to the internet and back, and would need a firewall hole per app.
            "scrape_url": f"host.docker.internal:{port}",
            "public_url": (r["domains"] or [None])[0],
            # WHAT A USER WOULD OPEN. The scrape url is the metrics endpoint on the host
            # gateway; this is the app's front door, and asking whether IT answers is the
            # question "is the website up" — a different one from "is it exporting metrics".
            "probe_url": f"http://host.docker.internal:{port}/",
        })
    return out


def render_otel_config(targets: list[dict], ports: dict) -> str:
    """OpenTelemetry Collector: scrape the apps, stamp dimensions, write to Prometheus.

    The collector is where DIMENSIONS are attached — app, environment, project, repo — so every
    series is sliceable without each app having to know its own deployment metadata. An app that
    reports its own environment is an app that lies about it after being promoted.
    """
    jobs = []
    for t in targets:
        jobs.append(f"""
          - job_name: {t['app']}
            scrape_interval: {SCRAPE_INTERVAL_SECONDS}s
            metrics_path: /metrics
            static_configs:
              - targets: ['{t['scrape_url']}']
                labels:
                  app: '{t['app']}'
                  environment: '{t['environment']}'
                  project: '{t['project']}'
                  repo: '{t['repo']}'
                  target_id: '{t['target_id']}'""".rstrip())

    return f"""# Generated by BOSS from deployment_targets — do not edit by hand.
# Regenerated on every stack deploy; local changes are lost, which is the point: the scrape
# config and the deployment registry must not be able to disagree.
receivers:
  prometheus:
    config:
      scrape_configs:{''.join(jobs) if jobs else ' []'}
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:{ports['otel_http']}

processors:
  batch:
    timeout: 10s
  # A sample with no idea which deployment produced it is a sample nobody can act on.
  resource:
    attributes:
      - key: collector
        value: boss
        action: upsert

exporters:
  prometheus:
    endpoint: 0.0.0.0:{ports['otel_metrics']}
    # Keep the labels above as real labels rather than folding them into the metric name.
    resource_to_telemetry_conversion:
      enabled: true

service:
  telemetry:
    logs:
      level: info
  pipelines:
    metrics:
      receivers: [prometheus, otlp]
      processors: [resource, batch]
      exporters: [prometheus]
"""


BLACKBOX_CONFIG = """# Generated by BOSS — do not edit by hand.
modules:
  # THE FRONT DOOR, not /metrics. `up` says whether the metrics endpoint answered, which is a
  # different question from whether the website works — and for an app with no /metrics it is 0
  # for ever, which reads as an outage that is not happening.
  http_2xx:
    prober: http
    timeout: 10s
    http:
      valid_status_codes: []          # any 2xx
      follow_redirects: true
      preferred_ip_protocol: ip4
  # The socket. No path, no status code, no cooperation from the app — the honest floor of a
  # health check, and the one that still answers when everything else is unconfigured.
  tcp_connect:
    prober: tcp
    timeout: 8s
"""


def render_blackbox_config() -> str:
    return BLACKBOX_CONFIG


def render_prometheus_config(ports: dict, targets: Optional[list[dict]] = None) -> str:
    """Prometheus scrapes the collector, and PROBES each app's front door.

    App discovery still belongs to the collector — two scrapers with two views of "which apps
    exist" is the duplicated-decision defect this codebase keeps paying for. What is added here
    is a different question about the same list: not "is it exporting metrics" but "does it
    answer at all", asked from outside via blackbox.

    That distinction was the bug. Three apps read "up · 100% · 0ms" for two days because the
    only check BOSS ran was skipped and a skipped check counted as healthy. `probe_success` is
    produced by something that actually connected.
    """
    probes = ""
    for t in (targets or []):
        url = t.get("probe_url") or ""
        if not url:
            continue
        probes += f"""
      - targets: ['{url}']
        labels:
          app: '{t['app']}'
          environment: '{t['environment']}'
          project: '{t['project']}'"""

    blackbox_job = f"""
  - job_name: service-health
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:{probes if probes else ' []'}
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: boss-blackbox:{ports.get('blackbox', 9115)}""" if probes else ""

    return f"""# Generated by BOSS — do not edit by hand.
global:
  scrape_interval: {SCRAPE_INTERVAL_SECONDS}s
  evaluation_interval: {SCRAPE_INTERVAL_SECONDS}s

scrape_configs:
  - job_name: otel-collector
    static_configs:
      - targets: ['boss-otel-collector:{ports['otel_metrics']}']
  - job_name: prometheus
    static_configs:
      - targets: ['localhost:9090']
  # THE HOST AND ITS CONTAINERS. Without these two jobs an app graph can show a latency spike
  # and say nothing about whether the box was out of memory or the container was throttled —
  # which is the question anybody asks second.
  - job_name: host
    static_configs:
      - targets: ['boss-node-exporter:9100']
  - job_name: containers
    static_configs:
      - targets: ['boss-cadvisor:8080']
    metric_relabel_configs:
      # cAdvisor emits a series per cgroup, most of them noise. Keep the named containers and
      # the four families actually plotted; a 15-day retention fills fast otherwise.
      - source_labels: [__name__]
        regex: 'container_(cpu_usage_seconds_total|memory_working_set_bytes|memory_rss|spec_memory_limit_bytes|network_.*_bytes_total|last_seen)'
        action: keep
      - source_labels: [name]
        regex: '.+'
        action: keep{blackbox_job}
"""


def render_grafana_datasource(ports: dict) -> str:
    return """# Generated by BOSS — do not edit by hand.
apiVersion: 1
datasources:
  - name: BOSS-Prometheus
    uid: boss-prometheus
    type: prometheus
    access: proxy
    url: http://boss-prometheus:9090
    isDefault: true
    jsonData:
      timeInterval: 60s
"""


def render_grafana_dashboard_provider() -> str:
    return """# Generated by BOSS — do not edit by hand.
apiVersion: 1
providers:
  - name: boss
    orgId: 1
    folder: BOSS
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    options:
      path: /etc/grafana/provisioning/dashboards/boss
"""


def dashboard_uid(app: str) -> str:
    """Stable per-app dashboard id, so the console can link straight to it.

    Derived from the app name rather than random, because a console link that changes every time
    the stack is redeployed is a bookmark that rots.
    """
    slug = "".join(c if c.isalnum() else "-" for c in app.lower()).strip("-")[:30]
    return f"boss-{slug}"


def _container_name_hint(target: dict) -> str:
    """A cAdvisor `name` matcher for this app's container.

    Deploys name the container after the app, lowercased and hyphenated. A regex rather than an
    equality so a suffix (`-staging`, a replica number) still matches; a wrong match here shows
    a neighbour's CPU, so it is anchored at the front.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(target.get("app") or "").lower()).strip("-")
    return f"{slug}.*" if slug else ".+"


def discover_feature_metrics(scrape_url: str, timeout: float = 4.0
                             ) -> list[tuple[str, str]]:
    """The app's OWN metric names, read from the app.

    Nothing in BOSS can know these in advance — that is the point of them. Change Calendar emits
    `collisions_detected_total`, Pricing Experiment Log emits `overlap_overrides_total`, and
    each was written to match one acceptance criterion of that app. So they are read from the
    exposition itself at dashboard-build time.

    Excludes the platform metrics every app is required to emit (`http_*`, `app_*`) because
    those already have dedicated panels, and the runtime families (`go_*`, `process_*`,
    `python_*`) because they describe the language, not the feature.
    """
    import urllib.request

    skip = ("http_request", "http_requests", "app_info", "app_uptime", "go_", "process_",
            "python_", "promhttp_", "scrape_")
    found: list[tuple[str, str]] = []
    try:
        url = scrape_url if scrape_url.startswith("http") else f"http://{scrape_url}"
        with urllib.request.urlopen(url.rstrip("/") + "/metrics", timeout=timeout) as r:
            body = r.read(400_000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — an app that is down still gets its other panels
        return []
    for line in body.splitlines():
        if not line.startswith("# HELP "):
            continue
        parts = line[len("# HELP "):].split(" ", 1)
        name = parts[0].strip()
        if not name or any(name.startswith(p) for p in skip):
            continue
        found.append((name, parts[1].strip() if len(parts) > 1 else ""))
    # Bounded: a dashboard with forty panels is one nobody reads at 3am.
    return found[:8]


def render_dashboard(target: dict,
                     feature_metrics: Optional[list[tuple[str, str]]] = None) -> str:
    """One dashboard per app: the four questions asked first during an incident.

    Deliberately small. A dashboard with forty panels is one nobody reads at 3am; these are
    request rate, error rate, latency and up/down — and every panel degrades to "No data"
    honestly if the app has not been instrumented yet, rather than drawing a flat zero line that
    reads like a healthy service with no traffic.
    """
    app = target["app"]
    sel = f'app="{app}"'

    # `up` is produced by the COLLECTOR, so it exists from the moment a target is registered —
    # before the app itself exposes anything. It is therefore the one panel that can distinguish
    # "not instrumented yet" from "down", and it leads the dashboard for that reason.
    #
    # Value mappings matter more than they look: a bare `0` in a stat panel reads as a broken
    # dashboard. Saying "no /metrics endpoint" turns the same number into an instruction.
    scrape = {
        "id": 1, "title": "Scrape status", "type": "stat",
        "gridPos": {"h": 5, "w": 6, "x": 0, "y": 0},
        "datasource": {"type": "prometheus", "uid": "boss-prometheus"},
        "targets": [{"expr": f'up{{{sel}}}', "refId": "A",
                     "datasource": {"type": "prometheus", "uid": "boss-prometheus"}}],
        "options": {"colorMode": "background", "graphMode": "none",
                    "reduceOptions": {"calcs": ["lastNotNull"]}},
        "fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {
            "0": {"text": "no /metrics endpoint", "color": "orange", "index": 0},
            "1": {"text": "scraping OK", "color": "green", "index": 1}}}]}, "overrides": []},
    }

    # A note panel, because a dashboard that is empty for a legitimate reason should say so.
    # Someone opening this at 3am should not have to work out whether the graphs are broken.
    note = {
        "id": 2, "title": "About this dashboard", "type": "text",
        "gridPos": {"h": 5, "w": 18, "x": 6, "y": 0},
        "options": {"mode": "markdown", "content": (
            f"**{app}** is registered with BOSS and is being scraped every "
            f"{SCRAPE_INTERVAL_SECONDS}s at `/metrics`.\n\n"
            "The traffic panels below stay empty until the app **exposes a `/metrics` "
            "endpoint**. `Scrape status` above tells you which situation you are in:\n\n"
            "- **no /metrics endpoint** — the app has not been instrumented yet. Expected for a "
            "newly onboarded app; the panels are not broken.\n"
            "- **scraping OK** — metrics are flowing and the panels below are live.\n\n"
            "_Generated by BOSS from the deployment registry._")},
    }

    # IS THE SERVICE ANSWERING? The question an operator asks first, and the one this dashboard
    # could not answer: every panel here measured the app's /metrics endpoint, so an app with no
    # instrumentation looked identical to one that was down. `probe_success` comes from blackbox
    # opening the app's FRONT DOOR — the same thing a user does.
    reachable = {
        "id": 3, "title": "Service reachable", "type": "stat",
        "gridPos": {"h": 5, "w": 6, "x": 0, "y": 5},
        "datasource": {"type": "prometheus", "uid": "boss-prometheus"},
        "targets": [{"expr": f'probe_success{{{sel}}}', "refId": "A",
                     "datasource": {"type": "prometheus", "uid": "boss-prometheus"}}],
        "options": {"colorMode": "background", "graphMode": "area",
                    "reduceOptions": {"calcs": ["lastNotNull"]}},
        "fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {
            "0": {"text": "NOT ANSWERING", "color": "red", "index": 0},
            "1": {"text": "answering", "color": "green", "index": 1}}}]}, "overrides": []},
    }
    uptime = {
        "id": 4, "title": "Availability (24h)", "type": "stat",
        "gridPos": {"h": 5, "w": 6, "x": 6, "y": 5},
        "datasource": {"type": "prometheus", "uid": "boss-prometheus"},
        "targets": [{"expr": f'avg_over_time(probe_success{{{sel}}}[24h]) * 100', "refId": "A",
                     "datasource": {"type": "prometheus", "uid": "boss-prometheus"}}],
        "options": {"colorMode": "value", "graphMode": "none",
                    "reduceOptions": {"calcs": ["lastNotNull"]}},
        "fieldConfig": {"defaults": {"unit": "percent", "decimals": 2}, "overrides": []},
    }
    responded = {
        "id": 5, "title": "Response time (s)", "type": "timeseries",
        "gridPos": {"h": 5, "w": 12, "x": 12, "y": 5},
        "datasource": {"type": "prometheus", "uid": "boss-prometheus"},
        "targets": [{"expr": f'probe_duration_seconds{{{sel}}}', "refId": "A",
                     "datasource": {"type": "prometheus", "uid": "boss-prometheus"}}],
        "options": {"legend": {"showLegend": False}},
        "fieldConfig": {"defaults": {"unit": "s"}, "overrides": []},
    }

    panels = [
        ("Request rate (req/s)", f'sum(rate(http_requests_total{{{sel}}}[5m]))', "timeseries"),
        ("Error rate (5xx/s)",
         f'sum(rate(http_requests_total{{{sel},status=~"5.."}}[5m]))', "timeseries"),
        ("p95 latency (s)",
         f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{{sel}}}[5m])) by (le))',
         "timeseries"),
        ("Scrape duration (s)", f'scrape_duration_seconds{{{sel}}}', "timeseries"),
    ]
    # THE APP'S OWN METRICS, which is the only reason it was instrumented.
    #
    # Every app BOSS builds is required to emit a counter per acceptance criterion — Change
    # Calendar has `changes_created_total`, `collisions_detected_total`, `freeze_overrides_total`;
    # Pricing Experiment Log has `experiments_launched_total`, `decisions_recorded_total`,
    # `overlap_overrides_total`. They were all being emitted and NONE of them was on a dashboard:
    # every panel here was generic HTTP traffic, identical for every app.
    #
    # Discovered from the app's own /metrics rather than listed here, because the whole point is
    # that they differ per app and nothing in BOSS can know them in advance. A dashboard is
    # regenerated on every stack deploy, so a metric added later appears on the next one.
    # Bounded HERE as well as at discovery: this function accepts whatever it is handed, and a
    # dashboard with forty panels is one nobody reads at 3am.
    for name, help_text in (feature_metrics or [])[:8]:
        panels.append((
            help_text or name.replace("_", " ").strip().capitalize(),
            # A counter is only meaningful as a rate; a gauge is meaningful as itself.
            f'sum(rate({name}{{{sel}}}[5m]))' if name.endswith("_total")
            else f'sum({name}{{{sel}}})',
            "timeseries"))

    # THE BOX UNDERNEATH IT. An app graph that shows a latency spike and cannot say whether the
    # host was out of memory or the container was throttled is half an answer.
    container = f'name=~"{_container_name_hint(target)}"'
    panels += [
        ("Container CPU (cores)",
         f'sum(rate(container_cpu_usage_seconds_total{{{container}}}[5m]))', "timeseries"),
        ("Container memory — working set / RSS (bytes)",
         f'sum(container_memory_working_set_bytes{{{container}}}) '
         f'or sum(container_memory_rss{{{container}}})', "timeseries"),
        ("Host memory available (bytes)", "node_memory_MemAvailable_bytes", "timeseries"),
        ("Host CPU busy (%)",
         '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)', "timeseries"),
    ]

    grid = [{"h": 8, "w": 12, "x": (i % 2) * 12, "y": 10 + (i // 2) * 8}
            for i in range(len(panels))]

    return json.dumps({
        "uid": dashboard_uid(app),
        "title": f"{app} — {target['environment']}",
        "tags": ["boss", target["environment"], target["project"]],
        "timezone": "browser",
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "panels": [scrape, note, reachable, uptime, responded] + [
            {
                "id": i + 6, "title": title, "type": kind, "gridPos": grid[i],
                "datasource": {"type": "prometheus", "uid": "boss-prometheus"},
                "targets": [{"expr": expr, "refId": "A",
                             "datasource": {"type": "prometheus", "uid": "boss-prometheus"}}],
                "options": {"legend": {"showLegend": True}},
                # "No data" rather than a flat zero line: a zero line reads as a healthy service
                # with no traffic, which is a different and much more comfortable lie.
                "fieldConfig": {"defaults": {"custom": {"drawStyle": "line",
                                                        "showPoints": "auto"}},
                                "overrides": []},
            }
            for i, (title, expr, kind) in enumerate(panels)
        ],
        "schemaVersion": 39,
        "description": (f"Generated by BOSS for {app}. Panels show 'No data' until the app "
                        f"exposes /metrics — that is honest, not broken."),
    }, indent=2)


def render_compose(ports: dict) -> str:
    """The stack itself. Labelled like every other BOSS deployment so teardown can prove
    ownership before removing anything."""
    return f"""# Generated by BOSS — do not edit by hand.
services:
  boss-otel-collector:
    image: otel/opentelemetry-collector-contrib:0.114.0
    container_name: boss-otel-collector
    restart: unless-stopped
    command: ["--config=/etc/otel/config.yaml"]
    volumes:
      - {STACK_DIR}/otel-config.yaml:/etc/otel/config.yaml:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
    labels:
      boss.managed: "true"
      boss.app: "observability"
    networks: [boss-obs]

  # THE HOST AND ITS CONTAINERS. Kept in step with `render_docker_run`: two hosts running
  # different sets of containers because one path was updated and the other was not is exactly
  # how blackbox came to be missing on hetzner-ci-01 for as long as it was.
  boss-node-exporter:
    image: prom/node-exporter:v1.8.2
    container_name: boss-node-exporter
    restart: unless-stopped
    command: ["--path.rootfs=/host"]
    pid: host
    volumes:
      - /:/host:ro,rslave
    labels:
      boss.managed: "true"
      boss.app: "observability"
    networks: [boss-obs]

  boss-cadvisor:
    image: gcr.io/cadvisor/cadvisor:v0.49.1
    container_name: boss-cadvisor
    restart: unless-stopped
    privileged: true
    volumes:
      - /:/rootfs:ro
      - /var/run:/var/run:ro
      - /sys:/sys:ro
      - /var/lib/docker/:/var/lib/docker:ro
      - /dev/disk/:/dev/disk:ro
    labels:
      boss.managed: "true"
      boss.app: "observability"
    networks: [boss-obs]

  boss-blackbox:
    image: prom/blackbox-exporter:v0.25.0
    container_name: boss-blackbox
    restart: unless-stopped
    # The apps are published on the HOST, so the prober reaches them the same way the collector
    # does — through the gateway, not out to the public internet and back.
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./blackbox.yml:/etc/blackbox_exporter/config.yml:ro
    networks: [boss-obs]

  boss-prometheus:
    image: prom/prometheus:v3.0.1
    container_name: boss-prometheus
    restart: unless-stopped
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=15d
    volumes:
      - {STACK_DIR}/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - boss-prometheus-data:/prometheus
    labels:
      boss.managed: "true"
      boss.app: "observability"
    networks: [boss-obs]

  boss-grafana:
    image: grafana/grafana:11.4.0
    container_name: boss-grafana
    restart: unless-stopped
    ports:
      - "{ports['grafana']}:3000"
    environment:
      # Anonymous VIEWER access: the console links straight to a dashboard, and a login wall
      # between an operator and a graph during an incident is friction with no security value
      # here — the instance is read-only and carries no secrets. Editing still requires admin.
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
      GF_USERS_DEFAULT_THEME: dark
      GF_SECURITY_ADMIN_PASSWORD: ${{GF_ADMIN_PASSWORD:-boss-admin}}
      GF_FEATURE_TOGGLES_ENABLE: ""
    volumes:
      - {STACK_DIR}/grafana/datasources:/etc/grafana/provisioning/datasources:ro
      - {STACK_DIR}/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
      - boss-grafana-data:/var/lib/grafana
    depends_on: [boss-prometheus]
    labels:
      boss.managed: "true"
      boss.app: "observability"
    networks: [boss-obs]

volumes:
  boss-prometheus-data:
  boss-grafana-data:

networks:
  boss-obs:
    name: boss-obs
"""


def render_docker_run(ports: dict) -> str:
    """The same three containers, without Compose.

    hetzner-ci-01 has Docker and NO Compose — neither the plugin nor the standalone binary:

        docker compose up -d  ->  unknown shorthand flag: 'd' in -d
        docker-compose        ->  command not found

    The apps on that host run perfectly well, because app deploys use plain `docker run`. The
    observability stack was the only thing that assumed Compose, so monitoring could never come
    up there — and the console went on linking to a Grafana dashboard on a Grafana that did not
    exist. An operator following that link got a connection refused and no explanation.

    Compose is still tried first: it handles ordering and volumes more tidily where it exists.
    This is the fallback, expressed in the one primitive every Docker host has.
    """
    return "\n".join([
        # Idempotent: re-running the deploy is how a newly onboarded app gets a dashboard, so
        # each container is replaced rather than refused as a duplicate name.
        "docker network create boss-obs 2>/dev/null || true",
        "docker volume create boss-prometheus-data >/dev/null 2>&1 || true",
        "docker volume create boss-grafana-data >/dev/null 2>&1 || true",
        ("docker rm -f boss-otel-collector boss-prometheus boss-grafana boss-blackbox "
         "boss-node-exporter boss-cadvisor >/dev/null 2>&1 || true"),
        # THE PROBER, MISSING FROM THIS PATH ENTIRELY.
        #
        # `render_compose` starts it and `render_prometheus_config` points the `service-health`
        # job at `boss-blackbox:9115` — but this fallback, which is what hetzner-ci-01 actually
        # uses because it has no Compose, started three containers and not this one. So every
        # `probe_success` panel on that host — "Service reachable", "Availability 24h",
        # "Response time" — had no data and no explanation, on a stack `stack_status` called
        # healthy because it only counts three containers.
        (f"docker run -d --name boss-blackbox --restart unless-stopped "
         f"--network boss-obs --add-host host.docker.internal:host-gateway "
         f"--label boss.managed=true --label boss.app=observability "
         f"-v {STACK_DIR}/blackbox.yml:/etc/blackbox_exporter/config.yml:ro "
         f"prom/blackbox-exporter:v0.25.0 --config.file=/etc/blackbox_exporter/config.yml"),
        # THE HOST ITSELF. Asked for directly: "expose some system metrics too.. like system
        # host and container memory (available and util), cpu, rss". An app graph that shows a
        # latency spike and cannot say whether the box was out of memory is half an answer.
        (f"docker run -d --name boss-node-exporter --restart unless-stopped "
         f"--network boss-obs --pid host "
         f"--label boss.managed=true --label boss.app=observability "
         f"-v /:/host:ro,rslave "
         f"prom/node-exporter:v1.8.2 --path.rootfs=/host"),
        # PER-CONTAINER cpu/memory/rss, which node-exporter does not break down. Read-only
        # mounts of the paths cAdvisor needs; it is given no ability to change anything.
        (f"docker run -d --name boss-cadvisor --restart unless-stopped "
         f"--network boss-obs --privileged "
         f"--label boss.managed=true --label boss.app=observability "
         f"-v /:/rootfs:ro -v /var/run:/var/run:ro -v /sys:/sys:ro "
         f"-v /var/lib/docker/:/var/lib/docker:ro -v /dev/disk/:/dev/disk:ro "
         f"gcr.io/cadvisor/cadvisor:v0.49.1"),
        (f"docker run -d --name boss-otel-collector --restart unless-stopped "
         f"--network boss-obs --add-host host.docker.internal:host-gateway "
         f"--label boss.managed=true --label boss.app=observability "
         f"-v {STACK_DIR}/otel-config.yaml:/etc/otel/config.yaml:ro "
         f"otel/opentelemetry-collector-contrib:0.114.0 --config=/etc/otel/config.yaml"),
        (f"docker run -d --name boss-prometheus --restart unless-stopped "
         f"--network boss-obs --label boss.managed=true --label boss.app=observability "
         f"-v {STACK_DIR}/prometheus.yml:/etc/prometheus/prometheus.yml:ro "
         f"-v boss-prometheus-data:/prometheus prom/prometheus:v3.0.1 "
         f"--config.file=/etc/prometheus/prometheus.yml "
         f"--storage.tsdb.retention.time=15d"),
        (f"docker run -d --name boss-grafana --restart unless-stopped "
         f"--network boss-obs -p {ports['grafana']}:3000 "
         f"--label boss.managed=true --label boss.app=observability "
         f"-e GF_AUTH_ANONYMOUS_ENABLED=true -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer "
         f"-e GF_USERS_DEFAULT_THEME=dark "
         f"-e GF_SECURITY_ADMIN_PASSWORD=\"${{GF_ADMIN_PASSWORD:-boss-admin}}\" "
         f"-v {STACK_DIR}/grafana/datasources:/etc/grafana/provisioning/datasources:ro "
         f"-v {STACK_DIR}/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro "
         f"-v boss-grafana-data:/var/lib/grafana grafana/grafana:11.4.0"),
    ])


def stack_files(targets: list[dict], ports: dict) -> dict[str, str]:
    """Every file the stack needs, keyed by path relative to STACK_DIR."""
    files = {
        "docker-compose.yml": render_compose(ports),
        "otel-config.yaml": render_otel_config(targets, ports),
        "prometheus.yml": render_prometheus_config(ports, targets),
        "blackbox.yml": render_blackbox_config(),
        "grafana/datasources/boss.yaml": render_grafana_datasource(ports),
        "grafana/dashboards/boss.yaml": render_grafana_dashboard_provider(),
    }
    for t in targets:
        # Read the app's own metric names now, while building the files. A dashboard is
        # regenerated on every stack deploy, so a metric added later appears on the next one.
        features = discover_feature_metrics(str(t.get("public_url") or t.get("scrape_url") or ""))
        files[f"grafana/dashboards/boss/{dashboard_uid(t['app'])}.json"] = render_dashboard(
            t, feature_metrics=features)
    return files


def grafana_url(base: str, app: str) -> str:
    """Where the console sends someone for this app."""
    return f"{base.rstrip('/')}/d/{dashboard_uid(app)}"


def provisioned_dashboards(base: str, timeout: float = 4.0) -> Optional[set[str]]:
    """The dashboard uids Grafana ACTUALLY has. None when Grafana cannot be asked.

    THE LINK WAS A GUESS. `grafana_url` builds `/d/boss-<slug>` by string concatenation, and the
    stack endpoint emitted one for every target that had ever deployed — but the dashboard JSON
    only exists on the host if somebody re-ran the stack deploy AFTER that app was onboarded.
    Nothing does that automatically, so a new app got a working-looking link immediately and a
    Grafana 404 for as long as nobody noticed.

    It is worse than an ordinary broken link: Grafana is a single-page app and answers 200 to
    ANY `/d/...` path, rendering "Dashboard not found" client-side. So a link to a dashboard
    that was never created is indistinguishable from one that works — to a person, and to any
    check that looks at the status code.

    Asking Grafana is authoritative and cannot drift from what was provisioned, which a flag
    stored on our side would. `None` (not asked) is never read as "no dashboards"; it is read as
    "Grafana did not answer", and then no link is shown at all (`check_endpoint`).
    """
    import urllib.request

    try:
        with urllib.request.urlopen(
                f"{base.rstrip('/')}/api/search?type=dash-db", timeout=timeout) as r:
            rows = json.loads(r.read(400_000).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — unreachable Grafana is not the same as an empty one
        return None
    if not isinstance(rows, list):
        return None
    return {str(x.get("uid") or "") for x in rows if isinstance(x, dict)}


# --------------------------------------------------------------------------- #
# Does it answer? Asked, recorded, and the only thing a console link is built from.
# --------------------------------------------------------------------------- #
#: A recorded check older than this is asked again by the monitor's sweep (never by a poll).
ENDPOINT_CHECK_TTL_SECONDS = 300
CHECK_TIMEOUT_SECONDS = 3.0
_KIND_NAMES = {"grafana": "Grafana", "prometheus": "Prometheus"}


def grafana_base(connection, project_id: str) -> Optional[str]:
    """Where this project's Grafana is: the registered boss-prometheus source's host (the stack
    BOSS deployed), else the address of the host its first active target deploys to. None when
    BOSS knows no host. ONE answer for every caller: the stack endpoint and the Observability
    views used to derive it two different ways."""
    from urllib.parse import urlparse

    row = connection.execute(sql(
        "SELECT config FROM telemetry_sources WHERE project_id = :p AND kind = 'prometheus' "
        "AND name = 'boss-prometheus'"), {"p": project_id}).mappings().first()
    host = ""
    if row:
        cfg = row["config"] or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except json.JSONDecodeError:
                cfg = {}
        host = urlparse(str(cfg.get("base_url") or "")).hostname or ""
    if not host:
        host = str(connection.execute(sql(
            "SELECT h.address FROM deployment_targets t "
            "JOIN deployment_hosts h ON h.id = t.host_id "
            "WHERE t.project_id = :p AND t.status = 'active' AND h.address <> '' "
            "ORDER BY t.created_at LIMIT 1"), {"p": project_id}).scalar() or "").strip()
    return f"http://{host}:{DEFAULT_PORTS['grafana']}" if host else None


def prometheus_base(connection, project_id: str) -> Optional[str]:
    """The registered boss-prometheus source's URL, or None when the stack was never deployed."""
    row = connection.execute(sql(
        "SELECT config FROM telemetry_sources WHERE project_id = :p AND kind = 'prometheus' "
        "AND name = 'boss-prometheus' AND enabled"), {"p": project_id}).mappings().first()
    if not row:
        return None
    cfg = row["config"] or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = {}
    return str(cfg.get("base_url") or "").rstrip("/") or None


def _why(exc: BaseException) -> str:
    import socket
    import urllib.error

    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"timed out after {CHECK_TIMEOUT_SECONDS:g}s"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, urllib.error.HTTPError):
        return f"answered {exc.code}"
    if isinstance(reason, socket.gaierror):
        return "the host name does not resolve"
    return f"{type(reason).__name__}"


def _get_json(url: str, timeout: float) -> Any:
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as r:   # noqa: S310 — our own stack
        return json.loads(r.read(400_000).decode("utf-8", "replace"))


def ask_endpoint(kind: str, base: str, *, timeout: float = CHECK_TIMEOUT_SECONDS
                 ) -> dict[str, Any]:
    """Ask Grafana or Prometheus whether it answers. Never raises; network I/O, so never inside
    a transaction and never on a console poll.

    Grafana answers if its search API answers with JSON (then the dashboards it has are known)
    or its health API reports a database. Prometheus answers if its build-info API answers."""
    name = _KIND_NAMES[kind]
    host = base.split("//", 1)[-1].rstrip("/")
    if kind == "grafana":
        dashboards = provisioned_dashboards(base, timeout=timeout)
        if dashboards is not None:
            return {"state": "answering", "reason": "", "detail": {
                "dashboards": sorted(dashboards)}}
        try:
            health = _get_json(f"{base.rstrip('/')}/api/health", timeout)
        except Exception as exc:  # noqa: BLE001
            return {"state": "not_answering",
                    "reason": f"{name} isn't answering on its host",
                    "detail": {"why": _why(exc), "at": host}}
        if isinstance(health, dict) and health.get("database"):
            return {"state": "answering", "reason": "", "detail": {"dashboards": None}}
        return {"state": "not_answering", "reason": f"Something else answers where {name} should",
                "detail": {"why": "not a Grafana health answer", "at": host}}
    try:
        info = _get_json(f"{base.rstrip('/')}/api/v1/status/buildinfo", timeout)
    except Exception as exc:  # noqa: BLE001
        return {"state": "not_answering", "reason": f"{name} isn't answering on its host",
                "detail": {"why": _why(exc), "at": host}}
    if isinstance(info, dict) and info.get("status") == "success":
        return {"state": "answering", "reason": "", "detail": {
            "version": str((info.get("data") or {}).get("version") or "")}}
    return {"state": "not_answering", "reason": f"Something else answers where {name} should",
            "detail": {"why": "not a Prometheus answer", "at": host}}


def record_check(connection, project_id: str, kind: str, base: str, result: dict[str, Any],
                 *, now: Any = None) -> dict[str, Any]:
    from datetime import datetime, timezone

    at = now or datetime.now(timezone.utc)
    connection.execute(sql(
        "INSERT INTO observability_endpoint_checks (project_id, kind, base_url, state, reason, "
        " detail, checked_at) VALUES (:p, :k, :b, :s, :r, CAST(:d AS JSONB), :at) "
        "ON CONFLICT (project_id, kind) DO UPDATE SET base_url = EXCLUDED.base_url, "
        " state = EXCLUDED.state, reason = EXCLUDED.reason, detail = EXCLUDED.detail, "
        " checked_at = EXCLUDED.checked_at"),
        {"p": project_id, "k": kind, "b": base, "s": result["state"],
         "r": result.get("reason") or "", "d": json.dumps(result.get("detail") or {}), "at": at})
    return {**result, "base_url": base, "checked_at": at}


def recorded_check(connection, project_id: str, kind: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql(
        "SELECT base_url, state, reason, detail, checked_at FROM observability_endpoint_checks "
        "WHERE project_id = :p AND kind = :k"), {"p": project_id, "k": kind}).mappings().first()
    if not row:
        return None
    out = dict(row)
    if isinstance(out.get("detail"), str):
        out["detail"] = json.loads(out["detail"])
    return out


def check_endpoint(engine, project_id: str, kind: str, *, base: Optional[str] = None,
                   only_if_stale: bool = False) -> Optional[dict[str, Any]]:
    """Ask now (outside any transaction) and record the answer. None when BOSS knows no address.
    `only_if_stale`: the sweep's call, which leaves a check younger than the TTL alone."""
    from datetime import datetime, timezone

    from ..db import begin

    with begin(engine) as c:
        if base is None:
            base = grafana_base(c, project_id) if kind == "grafana" else prometheus_base(
                c, project_id)
        if not base:
            return None
        if only_if_stale:
            last = recorded_check(c, project_id, kind)
            if last and last["base_url"] == base and (
                    datetime.now(timezone.utc) - last["checked_at"]).total_seconds()                     < ENDPOINT_CHECK_TTL_SECONDS:
                return last
    result = ask_endpoint(kind, base)
    with begin(engine) as c:
        return record_check(c, project_id, kind, base, result)


def endpoint_state(connection, project_id: str, kind: str = "grafana") -> dict[str, Any]:
    """What the console may say about Grafana (or Prometheus), from the RECORDED check only:

        answering      BOSS asked and it answered — the only state a link is shown in
        not_answering  BOSS asked and it did not; `reason` says so in words
        unchecked      BOSS has not asked since this address was known
        not_deployed   BOSS knows no address for it

    A recorded check for a different address than today's is `unchecked`: the host moved."""
    name = _KIND_NAMES[kind]
    base = grafana_base(connection, project_id) if kind == "grafana" else prometheus_base(
        connection, project_id)
    if not base:
        return {"state": "not_deployed", "url": None, "base_url": None,
                "reason": f"No monitoring stack: deploy it to get {name}",
                "checked_at": None, "dashboards": None, "why": None}
    last = recorded_check(connection, project_id, kind)
    if not last or last["base_url"] != base:
        return {"state": "unchecked", "url": None, "base_url": base,
                "reason": f"BOSS hasn't checked {name} yet", "checked_at": None,
                "dashboards": None, "why": None}
    detail = last.get("detail") or {}
    answering = last["state"] == "answering"
    return {"state": last["state"], "url": base if answering else None, "base_url": base,
            "reason": "" if answering else (last.get("reason") or f"{name} isn't answering"),
            "checked_at": last["checked_at"].isoformat() if last.get("checked_at") else None,
            "dashboards": detail.get("dashboards"), "why": detail.get("why")}


def dashboard_link(state: dict[str, Any], app: str) -> tuple[Optional[str], Optional[str]]:
    """(the app's dashboard URL, or None and why). A link only when the recorded check says
    Grafana answers and — when it could list them — has this app's dashboard."""
    if state.get("state") != "answering" or not state.get("base_url"):
        return None, state.get("reason") or "Grafana isn't answering"
    known = state.get("dashboards")
    if known is not None and dashboard_uid(app) not in set(known):
        return None, "No dashboard for this app yet: deploy the stack again"
    return grafana_url(state["base_url"], app), None


def explore_url(base: str, expr: str, start_ms: int, end_ms: int) -> str:
    """Grafana Explore with `expr` over [start, end], on the stack's Prometheus datasource."""
    from urllib.parse import quote

    panes = {"a": {"datasource": "boss-prometheus",
                   "queries": [{"refId": "A", "expr": expr, "range": True,
                                "datasource": {"type": "prometheus", "uid": "boss-prometheus"}}],
                   "range": {"from": str(int(start_ms)), "to": str(int(end_ms))}}}
    return (f"{base.rstrip('/')}/explore?schemaVersion=1&orgId=1&panes="
            + quote(json.dumps(panes, separators=(",", ":")), safe=""))


def deploy_stack(engine, settings: Settings, *, project_id: str, host_target_id: str,
                 ports: Optional[dict] = None, actor: str = "operator") -> dict[str, Any]:
    """Render the stack from the live target registry and put it on the host.

    `host_target_id` names an existing deployment target only to borrow its host and credential
    — the stack is not deployed "into" that app. Reusing a registered target means the stack
    authenticates through the same resolved secret as everything else rather than inventing a
    second way to reach the same machine.
    """
    from ..db import begin
    from .deploy_service import (_discard_key_file, _materialise_key, _ssh, _ssh_key_file,
                                 ensure_dir_sh)
    from .deployment_connectors import _split_ref
    from .secret_backends import SecretUnavailable, resolve_target_secret

    ports = {**DEFAULT_PORTS, **(ports or {})}

    with begin(engine) as c:
        row = c.execute(sql("SELECT * FROM deployment_targets WHERE id = :i"),
                        {"i": host_target_id}).mappings().first()
        if not row:
            return {"ok": False, "problems": [f"no deployment target {host_target_id}"]}
        host_target = dict(row)
        targets = scrape_targets(c, project_id)

    user, host, ssh_port = _split_ref(host_target.get("target_ref", ""))
    files = stack_files(targets, ports)

    ephemeral = None
    try:
        with begin(engine) as c:
            material = resolve_target_secret(c, host_target, "ssh_key", settings)
        key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)
    except (SecretUnavailable, Exception) as exc:  # noqa: BLE001
        return {"ok": False, "problems": [f"ssh key unusable: {exc}"]}

    try:
        # Written with a heredoc per file rather than scp: one ssh round trip, no temp files on
        # the BOSS host, and the content is identical to what was rendered and reviewed.
        #
        # /opt is not writable by the deploy user — the same wall the app deploy directory hit.
        # `sudo -n` so a host without passwordless sudo fails immediately with an instruction
        # rather than hanging on a prompt nobody can answer.
        parts = [
            # ensure_dir_sh: plain mkdir first (root needs no sudo, and may not have it).
            "case \"$(" + ensure_dir_sh(STACK_DIR, user, ok="DIR_OK", created="DIR_OK",
                                         need="DIR_NO") + ")\" "
            f"in DIR_NO) echo 'NEED_SUDO {STACK_DIR}' >&2; exit 90;; esac",
            f"mkdir -p {STACK_DIR}/grafana/datasources {STACK_DIR}/grafana/dashboards/boss",
            # Dashboards are FULLY generated, so the directory is cleared before rewriting.
            # Writing without removing leaves a dashboard for an app that is no longer
            # monitored — which is how a target that had never been deployed kept its dashboard
            # after being dropped from the scrape config. Generated config must be regenerated,
            # not merged.
            f"rm -f {STACK_DIR}/grafana/dashboards/boss/*.json",
        ]
        for i, (path, content) in enumerate(files.items()):
            # Index-based and alphanumeric: a marker derived from the filename produced
            # `BOSS_EOF_DOCKER-COMPOSE_YML`, and bash cannot use a hyphen there — the heredoc
            # ran to end-of-file and swallowed the rest of the script.
            marker = f"BOSS_EOF_{i}"
            parts.append(f"cat > {STACK_DIR}/{path} <<'{marker}'\n{content}\n{marker}")
        # Compose if the host has it; plain `docker run` if it does not. Both are attempted
        # because "no Compose on this box" is a fact about the host, not a failure to report
        # back — every other BOSS deployment already works there without it.
        parts.append(
            f"cd {STACK_DIR} && (docker compose up -d 2>&1 || docker-compose up -d 2>&1 || "
            f"{{ echo 'BOSS: no docker compose on this host, using docker run'; "
            f"{render_docker_run(ports)}; }} 2>&1)")
        # Joined with NEWLINES, not "; ". A heredoc's closing marker must be alone on its
        # line; joining with a semicolon put `BOSS_EOF_0; cat > next...` on one line, so the
        # first heredoc swallowed every following file into an 11KB blob and compose failed to
        # parse it. The shell was right; the command was malformed.
        res = _ssh(key, host, user, ssh_port, "set -e\n" + "\n".join(parts), timeout=300)
    finally:
        if ephemeral:
            _discard_key_file(ephemeral)

    grafana_base = f"http://{host}:{ports['grafana']}"
    # Checked on the SSH result, before observing: if the directory could not even be created
    # there is nothing to observe, and the operator needs the fix, not a container count.
    if res.returncode != 0 and "NEED_SUDO" in (res.stderr or ""):
        # Actionable rather than cryptic: this is host provisioning, the operator's step.
        return {"ok": False, "host": host, "grafana_url": grafana_base, "apps": [],
                "files": sorted(files),
                "problems": [f"{STACK_DIR} is not writable by {user} and passwordless sudo is "
                             f"unavailable; prepare the host once: "
                             f"sudo mkdir -p {STACK_DIR} && sudo chown {user} {STACK_DIR}"]}
    # OBSERVE, do not trust the exit status. `docker compose up -d` returned 0 while every
    # container failed to start, and this function happily reported success — the same defect
    # the connector contract exists to prevent ("observe() is the verdict, never apply()").
    running = stack_status(engine, settings, host_target_id=host_target_id) \
        if res.returncode == 0 else {"running": False, "containers": []}
    ok = bool(running.get("running"))

    if ok:
        with begin(engine) as c:
            # Prometheus is reachable from the BOSS host over the same host:port the operator
            # would use; it is NOT exposed publicly by the compose file, so this only resolves
            # from inside the host's network unless a port is opened deliberately.
            register_source(c, settings, project_id=project_id,
                            prometheus_url=f"http://{host}:{ports['prometheus']}", actor=actor)
            emit_event(c, project_id, "observability.stack_deployed", actor,
                       "deployment_target", host_target_id,
                       {"host": host, "grafana": grafana_base, "apps": len(targets)})

    return {
        "ok": ok, "host": host, "grafana_url": grafana_base,
        "apps": [{"app": t["app"], "environment": t["environment"],
                  "dashboard": grafana_url(grafana_base, t["app"])} for t in targets],
        "files": sorted(files),
        "containers": running.get("containers", []),
        "problems": [] if ok else [
            (res.stderr or res.stdout or "")[-600:]
            or f"compose exited 0 but only {len(running.get('containers', []))} of 3 "
               f"containers are running"],
        "detail": (res.stdout or "")[-400:] if ok else None,
    }


def stack_status(engine, settings: Settings, *, host_target_id: str) -> dict[str, Any]:
    """Is the stack actually up? Asked of the host, not inferred from a deploy having returned."""
    from ..db import begin
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .deployment_connectors import _split_ref
    from .secret_backends import SecretUnavailable, resolve_target_secret

    with begin(engine) as c:
        row = c.execute(sql("SELECT * FROM deployment_targets WHERE id = :i"),
                        {"i": host_target_id}).mappings().first()
        if not row:
            return {"running": False, "error": "no such target"}
        host_target = dict(row)
        try:
            material = resolve_target_secret(c, host_target, "ssh_key", settings)
        except SecretUnavailable as exc:
            return {"running": False, "error": str(exc)}

    user, host, ssh_port = _split_ref(host_target.get("target_ref", ""))
    ephemeral = None
    try:
        key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)
        res = _ssh(key, host, user, ssh_port,
                   "docker ps --filter label=boss.app=observability "
                   "--format '{{.Names}}|{{.Status}}'", timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"running": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        if ephemeral:
            _discard_key_file(ephemeral)

    containers = [dict(zip(("name", "status"), line.split("|", 1)))
                  for line in (res.stdout or "").splitlines() if "|" in line]
    # WHAT IS MISSING, BY NAME. `>= 3` called the stack healthy while blackbox was absent —
    # so every probe panel had no data and the status said everything was fine. A count cannot
    # tell you which container is gone; a set can.
    expected = {"boss-otel-collector", "boss-prometheus", "boss-grafana", "boss-blackbox",
                "boss-node-exporter", "boss-cadvisor"}
    present = {str(c.get("name") or "").strip() for c in containers}
    missing = sorted(expected - present)
    return {
        # Grafana and Prometheus are what "running" has always meant to the console; the rest
        # being absent degrades what you can see rather than stopping the stack.
        "running": {"boss-prometheus", "boss-grafana"} <= present,
        "containers": containers,
        "missing": missing,
        "degraded": bool(missing),
        "host": host,
    }


def register_source(connection, settings: Settings, *, project_id: str,
                    prometheus_url: str, actor: str = "operator") -> str:
    """Make the new Prometheus readable by everything BOSS already has.

    `telemetry_connectors`, `reliability` and the anomaly miner all consume a `telemetry_sources`
    row. Registering here means the stack feeds the existing machinery instead of becoming a
    second, parallel observability world.
    """
    source_id = f"tsrc_{project_id}_boss_prometheus"[:60]
    connection.execute(
        sql("""INSERT INTO telemetry_sources (id, project_id, kind, name, config, enabled,
                                              created_at)
               VALUES (:id,:p,'prometheus','boss-prometheus',CAST(:c AS JSONB),TRUE,now())
               ON CONFLICT (project_id, kind, name) DO UPDATE
                 SET config = EXCLUDED.config, enabled = TRUE"""),
        {"id": source_id, "p": project_id,
         "c": json.dumps({"base_url": prometheus_url,
                          "step_seconds": SCRAPE_INTERVAL_SECONDS})})
    emit_event(connection, project_id, "observability.source_registered", actor,
               "telemetry_source", source_id, {"base_url": prometheus_url})
    return source_id
