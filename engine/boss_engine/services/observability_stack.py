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
DEFAULT_PORTS = {"grafana": 3006, "prometheus": 9091, "otel_http": 4318, "otel_metrics": 8889}

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


def render_prometheus_config(ports: dict) -> str:
    """Prometheus scrapes ONE place: the collector. The collector owns app discovery.

    Two scrapers with two views of "which apps exist" is the duplicated-decision defect this
    codebase keeps paying for, so Prometheus deliberately knows nothing about the apps.
    """
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


def render_dashboard(target: dict) -> str:
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

    panels = [
        ("Request rate (req/s)", f'sum(rate(http_requests_total{{{sel}}}[5m]))', "timeseries"),
        ("Error rate (5xx/s)",
         f'sum(rate(http_requests_total{{{sel},status=~"5.."}}[5m]))', "timeseries"),
        ("p95 latency (s)",
         f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{{sel}}}[5m])) by (le))',
         "timeseries"),
        ("Scrape duration (s)", f'scrape_duration_seconds{{{sel}}}', "timeseries"),
    ]
    grid = [{"h": 8, "w": 12, "x": 0, "y": 5}, {"h": 8, "w": 12, "x": 12, "y": 5},
            {"h": 8, "w": 12, "x": 0, "y": 13}, {"h": 8, "w": 12, "x": 12, "y": 13}]

    return json.dumps({
        "uid": dashboard_uid(app),
        "title": f"{app} — {target['environment']}",
        "tags": ["boss", target["environment"], target["project"]],
        "timezone": "browser",
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "panels": [scrape, note] + [
            {
                "id": i + 3, "title": title, "type": kind, "gridPos": grid[i],
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
        "docker rm -f boss-otel-collector boss-prometheus boss-grafana >/dev/null 2>&1 || true",
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
        "prometheus.yml": render_prometheus_config(ports),
        "grafana/datasources/boss.yaml": render_grafana_datasource(ports),
        "grafana/dashboards/boss.yaml": render_grafana_dashboard_provider(),
    }
    for t in targets:
        files[f"grafana/dashboards/boss/{dashboard_uid(t['app'])}.json"] = render_dashboard(t)
    return files


def grafana_url(base: str, app: str) -> str:
    """Where the console sends someone for this app."""
    return f"{base.rstrip('/')}/d/{dashboard_uid(app)}"


def deploy_stack(engine, settings: Settings, *, project_id: str, host_target_id: str,
                 ports: Optional[dict] = None, actor: str = "operator") -> dict[str, Any]:
    """Render the stack from the live target registry and put it on the host.

    `host_target_id` names an existing deployment target only to borrow its host and credential
    — the stack is not deployed "into" that app. Reusing a registered target means the stack
    authenticates through the same resolved secret as everything else rather than inventing a
    second way to reach the same machine.
    """
    from ..db import begin
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
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
            f"if [ ! -d {STACK_DIR} ] || [ ! -w {STACK_DIR} ]; then "
            f"  sudo -n mkdir -p {STACK_DIR} && sudo -n chown {user} {STACK_DIR} "
            f"    || {{ echo 'NEED_SUDO {STACK_DIR}' >&2; exit 90; }}; fi",
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
    return {"running": len(containers) >= 3, "containers": containers, "host": host}


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
