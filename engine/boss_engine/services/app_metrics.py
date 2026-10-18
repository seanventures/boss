"""What a deployed app says about its own traffic: request count, error rate, p50/p95.

The health probe (deployment_health) answers "is it serving?" from outside — uptime, probe
latency, an incident after three failed probes. It cannot answer "how many requests did it serve,
how many failed, how slow were they for real users", because those live INSIDE the app. An app
that declares `metrics: {path: /metrics}` in its boss.deploy.yaml exposes them in the Prometheus
text format, and this module scrapes, parses, windows, stores and alerts on them.

Layers, kept apart for the same reason deployment_health keeps them apart:

  * PURE  `parse_exposition`, `extract`, `window_delta`, `histogram_quantile`, `aggregate` —
          functions of their arguments; every number on the screen is testable without a clock.
  * FETCH `fetch` — through the SAME route the health probe uses for that deployment: the
          public URL the StackHealthProbe asks, or (when the app is only reachable from its
          host) the host channel stack_deploy's health checks already run over. No new network
          path: the scrape URL is ALWAYS the app's own origin plus a validated path, redirects
          are never followed, the body is capped, and the host route is a read-only
          `stack_exec` that honours BOSS_SSH_OUTWARD_ALLOWED.
  * SWEEP `sweep` / `scrape_target` — records one row per scrape and evaluates the error-rate
          alert. Never raises: monitoring that can fail its caller turns observation into an
          outage.
  * READ  `metrics_view` — the block Observability's app drawer shows.

NAMING VARIANTS ACCEPTED (first family present wins; families are never summed together):

  requests   http_requests_total{status|code|status_code}      prometheus_client convention,
                                                               prometheus-fastapi-instrumentator
                                                               (status="2xx"/"5xx" grouped)
             starlette_requests_total{status_code}             starlette-exporter
  latency    http_request_duration_highr_seconds_bucket        instrumentator, fine buckets
             http_request_duration_seconds_bucket              prometheus_client convention
             starlette_request_duration_seconds_bucket         starlette-exporter

A request counter with no status label yields a request rate but no error rate (said in words).
With no request counter at all, the latency histogram's `_count` stands in for the count.
"""
from __future__ import annotations

import json
import math
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from ..ports import sql
from .core import make_id

#: Bounds an app may ask for in boss.deploy.yaml. Below 15 s a scrape is load on the thing being
#: measured; above an hour the alert window can never fill.
MIN_INTERVAL_SECONDS = 15
MAX_INTERVAL_SECONDS = 3600
#: The scrape rides the health sweep, so by default it runs on the probe's cadence.
DEFAULT_INTERVAL_SECONDS = 120

#: Seven days of scrapes per app, then they go. The drawer's longest useful window is 7d.
RETENTION_DAYS = 7
#: A metrics page bigger than this is a cardinality problem, not something to store.
MAX_BODY_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 5

#: The default alert: more than 5% of requests failing (5xx) over 5 minutes, judged only once
#: at least 20 requests were seen — two failures out of three requests is not an outage.
DEFAULT_ALERT = {"enabled": True, "error_rate_percent": 5.0, "window_minutes": 5,
                 "min_requests": 20}

REQUEST_FAMILIES = ("http_requests_total", "starlette_requests_total")
HISTOGRAM_FAMILIES = ("http_request_duration_highr_seconds", "http_request_duration_seconds",
                      "starlette_request_duration_seconds")
STATUS_LABELS = ("status", "code", "status_code")
#: The labels a request counter names its route by (the platform standard says `route`, always
#: a TEMPLATE). The per-route split is what says WHICH endpoint is failing — "POST /orders 31%"
#: is a lead; "9% of all requests" is only an alarm.
ROUTE_LABELS = ("route", "handler", "path", "endpoint")
#: An app that labels by raw path (against the standard) would give one key per order id; past
#: this many the split is dropped to the busiest routes rather than stored without bound.
MAX_ROUTES = 50
NAMING = {
    "http_requests_total": "prometheus",
    "starlette_requests_total": "starlette-exporter",
    "http_request_duration_highr_seconds": "fastapi-instrumentator",
}

#: The incident this module opens is keyed by target, so a re-fire reopens the same row.
INCIDENT_PREFIX = "app_error_rate:"


# --------------------------------------------------------------------------- #
# Pure: the Prometheus text exposition format (0.0.4).
# --------------------------------------------------------------------------- #
class MetricsFormatError(ValueError):
    """The page is not Prometheus text. The message says which line and why."""


@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict = field(hash=False)
    value: float


_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _float(raw: str, where: str) -> float:
    low = raw.lower()
    if low in ("+inf", "inf"):
        return math.inf
    if low == "-inf":
        return -math.inf
    if low == "nan":
        return math.nan
    try:
        return float(raw)
    except ValueError:
        raise MetricsFormatError(f"{where}: {raw[:40]!r} is not a number") from None


def _labels(line: str, i: int, where: str) -> tuple[dict, int]:
    """Parse `{a="x",b="y"}` starting at line[i] == '{'. Returns (labels, index after '}')."""
    out: dict[str, str] = {}
    i += 1
    while True:
        while i < len(line) and line[i] in " \t":
            i += 1
        if i < len(line) and line[i] == "}":
            return out, i + 1
        m = _LABEL_NAME.match(line, i)
        if not m:
            raise MetricsFormatError(f"{where}: expected a label name")
        name = m.group(0)
        i = m.end()
        while i < len(line) and line[i] in " \t":
            i += 1
        if i >= len(line) or line[i] != "=":
            raise MetricsFormatError(f"{where}: expected '=' after label {name}")
        i += 1
        while i < len(line) and line[i] in " \t":
            i += 1
        if i >= len(line) or line[i] != '"':
            raise MetricsFormatError(f"{where}: label {name} value must be quoted")
        i += 1
        buf = []
        while True:
            if i >= len(line):
                raise MetricsFormatError(f"{where}: label {name} value is not closed")
            ch = line[i]
            if ch == "\\":
                nxt = line[i + 1] if i + 1 < len(line) else ""
                buf.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                i += 2
                continue
            if ch == '"':
                i += 1
                break
            buf.append(ch)
            i += 1
        out[name] = "".join(buf)
        while i < len(line) and line[i] in " \t":
            i += 1
        if i < len(line) and line[i] == ",":
            i += 1
            continue
        if i < len(line) and line[i] == "}":
            return out, i + 1
        raise MetricsFormatError(f"{where}: expected ',' or '}}' after label {name}")


def parse_exposition(body: str) -> list[Sample]:
    """Every sample on a Prometheus text page. Strict enough to refuse what is not one.

    A malformed line refuses the whole page, as Prometheus itself does: a half-read page would
    turn into a counter that seems to drop to zero (a false reset) on the next scrape.
    """
    head = body.lstrip()[:200].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        raise MetricsFormatError("the page is HTML, not Prometheus text")
    samples: list[Sample] = []
    for n, raw in enumerate(body.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"line {n}"
        m = _NAME.match(line)
        if not m:
            raise MetricsFormatError(f"{where}: does not start with a metric name")
        name, i = m.group(0), m.end()
        labels: dict[str, str] = {}
        if i < len(line) and line[i] == "{":
            labels, i = _labels(line, i, where)
        rest = line[i:].split()
        if not rest:
            raise MetricsFormatError(f"{where}: {name} has no value")
        if len(rest) > 2:
            raise MetricsFormatError(f"{where}: unexpected text after the value")
        value = _float(rest[0], where)
        if len(rest) == 2:
            _float(rest[1], where)  # a timestamp; validated, not used
        samples.append(Sample(name, labels, value))
    if not samples:
        raise MetricsFormatError("the page has no samples")
    return samples


def _is_5xx(value: str) -> bool:
    v = str(value).strip().lower()
    return v.startswith("5") and (v == "5xx" or (len(v) == 3 and v.isdigit()))


def _le_key(le: str) -> float:
    return math.inf if le.lower() in ("+inf", "inf") else float(le)


def _le_text(le: str) -> str:
    """One spelling per boundary: '0.1', '0.10' and '1e-1' are the same bucket."""
    v = _le_key(le)
    return "+Inf" if v == math.inf else repr(float(v))


def extract(samples: list[Sample]) -> dict[str, Any]:
    """The cumulative counters this module windows, out of a parsed page.

    {requests, errors (None: no status label), buckets {le: cumulative}, hist_count, hist_sum,
     naming, request_family, histogram_family}. Raises MetricsFormatError when the page carries
    no request metrics at all — that is a page this module cannot use, said in words.
    """
    by_name: dict[str, list[Sample]] = {}
    for s in samples:
        by_name.setdefault(s.name, []).append(s)

    req_family = next((f for f in REQUEST_FAMILIES if f in by_name), None)
    hist_family = next((f for f in HISTOGRAM_FAMILIES if f + "_bucket" in by_name), None)

    requests = errors = None
    routes: dict[str, dict[str, float]] = {}
    if req_family:
        rows = by_name[req_family]
        requests = sum(s.value for s in rows if not math.isnan(s.value))
        status_key = next((k for k in STATUS_LABELS if any(k in s.labels for s in rows)), None)
        if status_key:
            errors = sum(s.value for s in rows
                         if _is_5xx(s.labels.get(status_key, "")) and not math.isnan(s.value))
        route_key = next((k for k in ROUTE_LABELS if any(k in s.labels for s in rows)), None)
        if route_key:
            for s in rows:
                if math.isnan(s.value):
                    continue
                key = " ".join(x for x in (str(s.labels.get("method") or "").upper(),
                                           str(s.labels.get(route_key) or "")) if x)[:120]
                r = routes.setdefault(key or "?", {"requests": 0.0, "errors": 0.0})
                r["requests"] += s.value
                if status_key and _is_5xx(s.labels.get(status_key, "")):
                    r["errors"] += s.value
            if len(routes) > MAX_ROUTES:
                routes = dict(sorted(routes.items(), key=lambda kv: -kv[1]["requests"])
                              [:MAX_ROUTES])

    buckets: dict[str, float] = {}
    hist_count = hist_sum = None
    if hist_family:
        for s in by_name[hist_family + "_bucket"]:
            if "le" not in s.labels:
                continue
            try:
                key = _le_text(s.labels["le"])
            except ValueError:
                raise MetricsFormatError(
                    f"{hist_family}_bucket has le={s.labels['le']!r}, which is not a number"
                ) from None
            buckets[key] = buckets.get(key, 0.0) + s.value
        hist_count = sum(s.value for s in by_name.get(hist_family + "_count", []))
        hist_sum = sum(s.value for s in by_name.get(hist_family + "_sum", []))
        if not by_name.get(hist_family + "_count") and "+Inf" in buckets:
            hist_count = buckets["+Inf"]

    if requests is None and hist_count is not None:
        requests = hist_count          # the histogram counts every request it timed
    if requests is None:
        raise MetricsFormatError(
            "no request metrics on the page (looked for "
            + ", ".join(REQUEST_FAMILIES + tuple(f + "_bucket" for f in HISTOGRAM_FAMILIES))
            + ")")
    return {
        "requests": requests, "errors": errors, "buckets": buckets,
        "hist_count": hist_count, "hist_sum": hist_sum,
        "naming": NAMING.get(hist_family or "", None) or NAMING.get(req_family or "", "prometheus"),
        "request_family": req_family, "histogram_family": hist_family,
        "routes": routes,
    }


def window_delta(prev: Optional[dict], cur: dict) -> dict[str, Any]:
    """What happened between two scrapes: requests, errors, bucket deltas, and whether the app
    restarted in between (a counter reset).

    A counter that went DOWN means the process restarted and counting began again from zero —
    Prometheus' `increase()` rule: the new value IS the delta. Treating the drop as negative
    traffic would draw a cliff; ignoring it would lose the requests served since the restart.
    """
    if not prev:
        return {"requests": None, "errors": None, "buckets": {}, "reset": False, "routes": {}}
    pb, cb = prev.get("buckets") or {}, cur.get("buckets") or {}
    reset = (cur["requests"] < prev["requests"]
             or (cur.get("errors") is not None and prev.get("errors") is not None
                 and cur["errors"] < prev["errors"])
             or any(cb.get(k, 0.0) < v for k, v in pb.items() if k in cb)
             or (set(pb) and set(cb) and set(pb) != set(cb)))
    if reset:
        return {"requests": cur["requests"], "errors": cur.get("errors"),
                "buckets": dict(cb), "reset": True,
                "routes": {k: dict(v) for k, v in (cur.get("routes") or {}).items()}}
    errors = None
    if cur.get("errors") is not None:
        errors = cur["errors"] - (prev.get("errors") or 0.0)
    return {"requests": cur["requests"] - prev["requests"], "errors": errors,
            "buckets": {k: v - pb.get(k, 0.0) for k, v in cb.items()}, "reset": False,
            "routes": route_delta(prev.get("routes") or {}, cur.get("routes") or {})}


def route_delta(prev: dict, cur: dict) -> dict[str, dict[str, float]]:
    """Per-route requests/errors between two scrapes. A route new since the last scrape counts
    from zero; one whose counter went down restarted (the same `increase()` rule as the total)."""
    out: dict[str, dict[str, float]] = {}
    for key, c in cur.items():
        p = prev.get(key) or {"requests": 0.0, "errors": 0.0}
        req, err = c["requests"] - p["requests"], c["errors"] - p["errors"]
        if req < 0 or err < 0:
            req, err = c["requests"], c["errors"]
        if req or err:
            out[key] = {"requests": req, "errors": err}
    return out


def histogram_quantile(q: float, buckets: dict[str, float]) -> Optional[float]:
    """Prometheus' histogram_quantile over cumulative bucket counts, in SECONDS.

    Linear interpolation inside the bucket the rank falls in; the lowest bucket's lower bound is
    0; a rank in the +Inf bucket answers the highest finite bound. None (never 0) when there is
    nothing to measure — no +Inf bucket, or no observations in the window.
    """
    if not buckets:
        return None
    items = sorted(((_le_key(k), float(v)) for k, v in buckets.items()), key=lambda kv: kv[0])
    if items[-1][0] != math.inf:
        return None
    # Cumulative counts must not decrease; scrape races can make them, Prometheus clamps too.
    fixed, running = [], 0.0
    for le, c in items:
        running = max(running, c)
        fixed.append((le, running))
    total = fixed[-1][1]
    if total <= 0:
        return None
    rank = q * total
    for i, (le, cum) in enumerate(fixed):
        if cum >= rank:
            if le == math.inf:
                return fixed[i - 1][0] if i > 0 else None
            lower, below = (0.0, 0.0) if i == 0 else fixed[i - 1]
            if i == 0 and le <= 0:
                return le
            in_bucket = cum - below
            if in_bucket <= 0:
                return le
            return lower + (le - lower) * ((rank - below) / in_bucket)
    return None


def _ms(seconds: Optional[float]) -> Optional[float]:
    return None if seconds is None else round(seconds * 1000.0, 1)


def aggregate(rows: list[dict]) -> dict[str, Any]:
    """Sum of windowed scrapes: requests, errors, error rate, rate/min, p50/p95 (ms).

    Bucket DELTAS are summed before the quantile is taken: a p95 of p95s is not a p95.
    """
    got = [r for r in rows if r.get("requests") is not None]
    requests = sum(float(r["requests"]) for r in got)
    with_err = [r for r in got if r.get("errors") is not None]
    errors = sum(float(r["errors"]) for r in with_err) if with_err else None
    seconds = sum(float(r.get("interval_s") or 0) for r in got)
    buckets: dict[str, float] = {}
    routes: dict[str, dict[str, float]] = {}
    for r in got:
        for k, v in (r.get("bucket_deltas") or {}).items():
            buckets[k] = buckets.get(k, 0.0) + float(v)
        for k, v in (r.get("route_deltas") or {}).items():
            acc = routes.setdefault(k, {"requests": 0.0, "errors": 0.0})
            acc["requests"] += float(v.get("requests") or 0)
            acc["errors"] += float(v.get("errors") or 0)
    return {
        "routes": [{"route": k, "requests": round(v["requests"], 3),
                    "errors": round(v["errors"], 3),
                    "error_rate": round(v["errors"] / v["requests"], 4) if v["requests"] else None}
                   for k, v in sorted(routes.items(),
                                      key=lambda kv: (-kv[1]["errors"], -kv[1]["requests"]))],
        "requests": round(requests, 3) if got else None,
        "errors": round(errors, 3) if errors is not None else None,
        "error_rate": (round(errors / requests, 4) if errors is not None and requests > 0
                       else None),
        "rate_per_min": round(requests / seconds * 60.0, 2) if got and seconds > 0 else None,
        "p50_ms": _ms(histogram_quantile(0.5, buckets)),
        "p95_ms": _ms(histogram_quantile(0.95, buckets)),
    }


# --------------------------------------------------------------------------- #
# The declaration: boss.deploy.yaml `metrics:` as recorded on the healthy deploy.
# --------------------------------------------------------------------------- #
def alert_rule(spec: Optional[dict]) -> dict[str, Any]:
    return {**DEFAULT_ALERT, **((spec or {}).get("alert") or {})}


def declarations(connection, target_ids: list[str]) -> dict[str, dict[str, Any]]:
    """target → {metrics spec, host_id, manifest health} from its newest HEALTHY stack deploy.

    The deploy that is serving is the one whose declaration counts: a later failed deploy that
    added `metrics:` describes an app that is not running.
    """
    if not target_ids:
        return {}
    rows = connection.execute(sql(
        "SELECT DISTINCT ON (target_id) target_id, host_id, manifest "
        "FROM stack_deployments WHERE target_id = ANY(:ids) AND status = 'healthy' "
        "ORDER BY target_id, created_at DESC"), {"ids": list(target_ids)}).mappings().all()
    out = {}
    for r in rows:
        manifest = r["manifest"] if isinstance(r["manifest"], dict) else \
            json.loads(r["manifest"] or "{}")
        spec = manifest.get("metrics")
        if spec and spec.get("path"):
            out[r["target_id"]] = {"spec": spec, "host_id": r["host_id"],
                                   "health": manifest.get("health") or []}
    return out


# --------------------------------------------------------------------------- #
# Fetch: the app's own origin, by the route the health probe uses.
# --------------------------------------------------------------------------- #
def public_origin(target: dict) -> Optional[str]:
    """The origin the health probe asks, for this target (scheme://host[:port]), or None."""
    if str(target.get("target_kind") or "").lower() == "stack":
        url = str(target.get("public_url") or target.get("stack_open_url")
                  or (target.get("domains") or [""])[0] or "").strip()
        if not url:
            return None
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
    else:
        from .deployment_connectors import _app_port, _health_url
        domains = target.get("domains") or []
        if not domains:
            return None
        checks = [c for c in (target.get("health_checks") or []) if isinstance(c, dict)]
        url = _health_url(domains[0], checks[0] if checks else {"scheme": "http"},
                          _app_port(target, default=0) or None)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def host_origin(decl: dict, host: Optional[dict]) -> Optional[str]:
    """The origin the deploy's health checks used ON THE HOST (e.g. http://127.0.0.1:8000)."""
    for check in decl.get("health") or []:
        url = str((check or {}).get("url") or "")
        if host:
            url = url.replace("{target.host}", str(host.get("address") or ""))
        if "{" in url:
            continue
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None


class _NoRedirect:
    """Built lazily: urllib's redirect handler, refusing every redirect."""

    @staticmethod
    def opener():
        import urllib.request

        class Refuse(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # noqa: D401
                return None
        return urllib.request.build_opener(Refuse)


def _fetch_public(url: str) -> dict[str, Any]:
    import socket
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "boss-metrics", "Accept": "text/plain;version=0.0.4"})
    try:
        with _NoRedirect.opener().open(req, timeout=FETCH_TIMEOUT_SECONDS) as r:
            status = int(r.status)
            body = r.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if 300 <= code < 400:
            return {"status": code, "transport": False,
                    "word": f"Metrics endpoint redirected ({code}); BOSS does not follow it"}
        return {"status": code, "transport": False,
                "word": f"Metrics endpoint returned {code}"}
    except (socket.timeout, TimeoutError):
        return {"status": None, "transport": True,
                "word": f"Metrics endpoint timed out after {FETCH_TIMEOUT_SECONDS}s"}
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return {"status": None, "transport": True,
                    "word": f"Metrics endpoint timed out after {FETCH_TIMEOUT_SECONDS}s"}
        if isinstance(reason, ConnectionRefusedError):
            return {"status": None, "transport": True,
                    "word": "Metrics endpoint refused the connection"}
        return {"status": None, "transport": True,
                "word": f"Metrics endpoint unreachable ({str(reason)[:80]})"}
    except Exception as exc:  # noqa: BLE001 — unreachable IS the measurement
        return {"status": None, "transport": True,
                "word": f"Metrics endpoint unreachable ({type(exc).__name__})"}
    if len(body) > MAX_BODY_BYTES:
        return {"status": status, "transport": False,
                "word": "Metrics page is larger than 2 MB; too many series"}
    return {"status": status, "transport": False, "body": body.decode("utf-8", "replace")}


_CODE_MARK = "BOSS_METRICS_HTTP:"


def _fetch_via_host(access: Any, url: str) -> dict[str, Any]:
    """GET the URL from the host itself, over the read-only stack channel."""
    cmd = (f"curl -sS -m {FETCH_TIMEOUT_SECONDS} --max-filesize {MAX_BODY_BYTES} "
           f"-w '\\n{_CODE_MARK}%{{http_code}}' {shlex.quote(url)}")
    try:
        res = access.run(cmd, timeout=FETCH_TIMEOUT_SECONDS + 20, write=False)
    except Exception as exc:  # noqa: BLE001
        return {"status": None, "transport": True,
                "word": f"Could not reach the host to scrape ({type(exc).__name__})"}
    out = str(res.stdout or "")
    body, _, code = out.rpartition(_CODE_MARK)
    code = code.strip()
    if res.returncode == 28:
        return {"status": None, "transport": True,
                "word": f"Metrics endpoint timed out after {FETCH_TIMEOUT_SECONDS}s"}
    if res.returncode == 63:
        return {"status": None, "transport": False,
                "word": "Metrics page is larger than 2 MB; too many series"}
    if not code.isdigit() or code == "000":
        return {"status": None, "transport": True,
                "word": "Metrics endpoint refused the connection on the host"}
    status = int(code)
    if status != 200:
        return {"status": status, "transport": False,
                "word": f"Metrics endpoint returned {status}"}
    return {"status": status, "transport": False, "body": body[:-1] if body.endswith("\n")
            else body}


def fetch(target: dict, decl: dict, settings: Any, *, connection=None,
          host_access: Any = None) -> dict[str, Any]:
    """One GET of the app's metrics page. Never raises.

    via=public  the health probe's own origin + path
    via=host    the origin the deploy's health checks used on the host, over the host channel
    via=auto    public first; the host channel only when public gave no HTTP answer at all
    """
    spec = decl["spec"]
    path = spec["path"]
    via = spec.get("via") or "auto"
    out: dict[str, Any] = {"status": None, "word": "No address to scrape"}
    if via in ("public", "auto"):
        origin = public_origin(target)
        if origin:
            out = {**_fetch_public(origin + path), "route": "public", "url": origin + path}
            if via == "public" or not out.get("transport"):
                return out
        elif via == "public":
            return {"status": None, "route": "public", "url": None,
                    "word": "No public URL to scrape: set the target's public URL"}
    host = None
    if connection is not None and decl.get("host_id"):
        host = connection.execute(sql("SELECT * FROM deployment_hosts WHERE id = :h"),
                                  {"h": decl["host_id"]}).mappings().first()
        host = dict(host) if host else None
    origin = host_origin(decl, host)
    if not origin or not host:
        if via == "host":
            return {"status": None, "route": "host", "url": None,
                    "word": "No host address to scrape: declare a health url in boss.deploy.yaml"}
        return out
    try:
        access = host_access or _host_access(connection, settings, host)
    except Exception as exc:  # noqa: BLE001
        return {"status": None, "route": "host", "url": origin + path,
                "word": f"Could not reach the host to scrape: {str(exc)[:100]}"}
    return {**_fetch_via_host(access, origin + path), "route": "host", "url": origin + path}


def _host_access(connection, settings, host):
    from .stack_deploy import host_access_for
    return host_access_for(connection, settings, host)


# --------------------------------------------------------------------------- #
# Sweep: one row per scrape, then the alert.
# --------------------------------------------------------------------------- #
def _last(connection, target_id: str, *, ok_only: bool = False) -> Optional[dict]:
    row = connection.execute(sql(
        "SELECT scraped_at, ok, snapshot FROM app_metrics_scrapes WHERE deployment_target_id = :t"
        + (" AND ok AND snapshot IS NOT NULL" if ok_only else "")
        + " ORDER BY scraped_at DESC LIMIT 1"), {"t": target_id}).mappings().first()
    if not row:
        return None
    out = dict(row)
    if isinstance(out.get("snapshot"), str):
        out["snapshot"] = json.loads(out["snapshot"])
    return out


def due(connection, target_id: str, spec: dict, now: datetime) -> bool:
    last = _last(connection, target_id)
    if not last:
        return True
    interval = int(spec.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS)
    return (now - last["scraped_at"]).total_seconds() >= interval


def _serialize(connection, target_id: str) -> None:
    """One writer per app at a time, for the length of the writing transaction.

    Two scrapers windowing against the same previous row both count the same requests: the
    laptop's three lease slots each swept on their idle branch and HelpDesk AI's traffic was
    stored three times over (21:10:43.061/.064/.086, 3 requests each against one 122 s window).
    The lock makes the second writer see the first one's row, so its delta is measured from it.
    """
    connection.execute(sql("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": "app_metrics:" + target_id})


def scrape_target(engine, settings: Any, target: dict, decl: dict, *,
                  now: Optional[datetime] = None, host_access: Any = None,
                  only_if_due: bool = False) -> dict[str, Any]:
    """Scrape one app, store what it said (or why it could not), evaluate its alert.

    The fetch is network I/O and runs outside any transaction; the write is serialized per app
    (`_serialize`). Under that lock a scrape is DISCARDED, not stored, when a newer one already
    landed (storing an older snapshot after a newer one reads as a counter reset — a spike of
    the whole counter), or — `only_if_due`, the sweep's own call — when another sweep took
    this slot while this one was fetching.
    """
    from ..db import begin

    now = now or datetime.now(timezone.utc)
    started = time.monotonic()
    with begin(engine) as c:
        got = fetch(target, decl, settings, connection=c, host_access=host_access)
    took_ms = int((time.monotonic() - started) * 1000)

    snapshot = None
    word = got.get("word") or ""
    if "body" in got:
        try:
            snapshot = extract(parse_exposition(got["body"]))
            word = "Scraped"
        except MetricsFormatError as exc:
            word = f"Metrics page is not usable: {exc}"[:200]
    ok = snapshot is not None

    with begin(engine) as c:
        _serialize(c, target["id"])
        latest = _last(c, target["id"])
        if latest and latest["scraped_at"] >= now:
            return {"ok": ok, "word": "A newer scrape is already stored", "skipped": True,
                    "route": got.get("route"), "url": got.get("url"), "requests": None,
                    "errors": None, "reset": False, "alert": None}
        if only_if_due and not due(c, target["id"], decl["spec"], now):
            return {"ok": ok, "word": "Another sweep scraped this app first", "skipped": True,
                    "route": got.get("route"), "url": got.get("url"), "requests": None,
                    "errors": None, "reset": False, "alert": None}
        prev = _last(c, target["id"], ok_only=True) if ok else None
        delta = window_delta(prev["snapshot"] if prev else None, snapshot) if ok else \
            {"requests": None, "errors": None, "buckets": {}, "reset": False, "routes": {}}
        interval_s = ((now - prev["scraped_at"]).total_seconds() if prev and ok else None)
        if ok and not prev:
            word = "Scraped; rates need a second scrape"
        c.execute(sql(
            "INSERT INTO app_metrics_scrapes (id, project_id, deployment_target_id, scraped_at, "
            " ok, route, url, status_code, word, naming, took_ms, snapshot, interval_s, "
            " requests, errors, counter_reset, bucket_deltas, p50_ms, p95_ms, route_deltas) "
            "VALUES (:id, :p, :t, :at, :ok, :route, :url, :sc, :word, :naming, :took, "
            " CAST(:snap AS JSONB), :iv, :req, :err, :reset, CAST(:bd AS JSONB), :p50, :p95, "
            " CAST(:rd AS JSONB))"),
            {"id": make_id("ams"), "p": target["project_id"], "t": target["id"], "at": now,
             "ok": ok, "route": got.get("route"), "url": got.get("url"),
             "sc": got.get("status"), "word": word[:200],
             "naming": snapshot["naming"] if snapshot else None, "took": took_ms,
             "snap": json.dumps(snapshot) if snapshot else None, "iv": interval_s,
             "req": delta["requests"], "err": delta["errors"], "reset": bool(delta["reset"]),
             "bd": json.dumps(delta["buckets"]) if delta["buckets"] else None,
             "p50": _ms(histogram_quantile(0.5, delta["buckets"])),
             "p95": _ms(histogram_quantile(0.95, delta["buckets"])),
             "rd": json.dumps(delta.get("routes")) if delta.get("routes") else None})
        c.execute(sql("DELETE FROM app_metrics_scrapes WHERE deployment_target_id = :t "
                      "AND scraped_at < :cut"),
                  {"t": target["id"], "cut": now - timedelta(days=RETENTION_DAYS)})
        alert = evaluate_alert(c, target, decl["spec"], now)
    return {"ok": ok, "word": word, "route": got.get("route"), "url": got.get("url"),
            "requests": delta["requests"], "errors": delta["errors"],
            "reset": bool(delta["reset"]), "alert": alert}


def sweep(engine, settings: Any, targets: list[dict], *, now: Optional[datetime] = None,
          force: bool = False) -> dict[str, Any]:
    """Scrape every given (deployed) target that declares metrics and is due. Never raises."""
    from ..db import begin

    now = now or datetime.now(timezone.utc)
    scraped = failed = 0
    try:
        with begin(engine) as c:
            decls = declarations(c, [t["id"] for t in targets])
        for t in targets:
            decl = decls.get(t["id"])
            if not decl:
                continue
            with begin(engine) as c:
                if not force and not due(c, t["id"], decl["spec"], now):
                    continue
            res = scrape_target(engine, settings, t, decl, now=now, only_if_due=not force)
            if res.get("skipped"):
                continue
            scraped += 1
            failed += 0 if res["ok"] else 1
    except Exception as exc:  # noqa: BLE001
        return {"scraped": scraped, "scrape_failed": failed,
                "scrape_error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"scraped": scraped, "scrape_failed": failed}


# --------------------------------------------------------------------------- #
# Alert: error rate over a window, through the incidents ledger.
# --------------------------------------------------------------------------- #
def _window_rows(connection, target_id: str, start: datetime, end: datetime) -> list[dict]:
    rows = connection.execute(sql(
        "SELECT scraped_at, ok, requests, errors, interval_s, bucket_deltas, counter_reset, "
        "       route_deltas "
        "FROM app_metrics_scrapes WHERE deployment_target_id = :t AND scraped_at > :a "
        "AND scraped_at <= :z ORDER BY scraped_at"),
        {"t": target_id, "a": start, "z": end}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("bucket_deltas", "route_deltas"):
            if isinstance(d.get(k), str):
                d[k] = json.loads(d[k])
        out.append(d)
    return out


def judge(window: dict, rule: dict) -> str:
    """firing | ok | quiet (too few requests to judge) | off."""
    if not rule.get("enabled", True):
        return "off"
    if window["requests"] is None or window["requests"] < float(rule["min_requests"]):
        return "quiet"
    if window["error_rate"] is None:
        return "quiet"
    return "firing" if window["error_rate"] * 100.0 > float(rule["error_rate_percent"]) else "ok"


def _incident(connection, project_id: str, target_id: str) -> Optional[dict]:
    row = connection.execute(sql(
        "SELECT id, status, title, first_seen, last_seen, "
        "       (finalize_ctx->'alert') IS NOT NULL AND finalize_ctx->'alert' <> 'null'::jsonb "
        "         AS alerted "
        "FROM incidents "
        "WHERE project_id = :p AND dedupe_key = :k"),
        {"p": project_id, "k": INCIDENT_PREFIX + target_id}).mappings().first()
    return dict(row) if row else None


_CLOSED = ("resolved", "dismissed")


def _being_answered(connection, project_id: str, incident_id: str) -> bool:
    """Is an Incident Response running on this incident? Then IT closes the incident, by its
    verify step, on the pre-incident baseline over a stable window. A dip under the threshold
    is not recovery: D2's monitor resolved the incident mid-fix at 07:12:43 and reopened it at
    07:13:04, and the reopen reset the incident's opening time in the report."""
    from .incident_response import _response_in_flight

    return bool(_response_in_flight(connection, project_id, incident_id))


def evaluate_alert(connection, target: dict, spec: dict, now: datetime) -> dict[str, Any]:
    """Open (or reopen) the app's error-rate incident when the rule fires; resolve it once the
    rate is back under the threshold on enough traffic. Too little traffic HOLDS the current
    state: silence is not evidence of recovery."""
    rule = alert_rule(spec)
    window = aggregate(_window_rows(connection, target["id"],
                                    now - timedelta(minutes=float(rule["window_minutes"])), now))
    verdict = judge(window, rule)
    inc = _incident(connection, target["project_id"], target["id"])
    is_open = bool(inc and inc["status"] not in _CLOSED)
    # An incident an APP REPORTED (incident_signals) is open before the monitor says anything.
    # When the monitor then fires, that is the alert opening it — the release, the routes and
    # the event the response starts from are recorded now. And the monitor never closes an
    # incident it did not raise: reports wait for the alert, or for a person.
    alerted = bool(inc and inc.get("alerted"))
    action = None
    if verdict == "firing":
        pct = round(window["error_rate"] * 100.0, 1)
        title = (f"{target.get('app_name')} ({target.get('environment')}): {pct}% of requests "
                 f"failing over {rule['window_minutes']} min (alert above "
                 f"{rule['error_rate_percent']}%)")[:200]
        if inc is None:
            inc_id = make_id("inc")
            connection.execute(sql(
                "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, "
                " status, signal_count, first_seen, last_seen) VALUES "
                "(:id, :p, :k, :title, :svc, 'warning', 'open', 1, :now, :now)"),
                {"id": inc_id, "p": target["project_id"], "k": INCIDENT_PREFIX + target["id"],
                 "title": title, "svc": target.get("app_name") or "", "now": now})
            action = "opened"
        else:
            inc_id = inc["id"]
            connection.execute(sql(
                "UPDATE incidents SET title = :title, last_seen = :now, "
                " signal_count = signal_count + 1, "
                " status = CASE WHEN status IN ('resolved', 'dismissed') THEN 'open' "
                "               ELSE status END, "
                " first_seen = CASE WHEN status IN ('resolved', 'dismissed') THEN :now "
                "                   ELSE first_seen END "
                "WHERE id = :id"), {"id": inc_id, "title": title, "now": now})
            action = ("reopened" if not is_open else
                      "still_firing" if alerted else "opened")
        connection.execute(sql(
            "INSERT INTO anomaly_signals (id, project_id, incident_id, kind, signature, service, "
            " score, count, sample, ts) VALUES (:id, :p, :inc, 'error_surge', 'http_error_rate', "
            " :svc, :score, :count, CAST(:sample AS JSONB), :now)"),
            {"id": make_id("sig"), "p": target["project_id"], "inc": inc_id,
             "svc": target.get("app_name") or "", "score": window["error_rate"],
             "count": int(window["errors"] or 0),
             "sample": json.dumps({"target_id": target["id"], "window": window, "rule": rule}),
             "now": now})
    elif verdict in ("ok", "off") and is_open and alerted and not _being_answered(
            connection, target["project_id"], inc["id"]):
        connection.execute(sql(
            "UPDATE incidents SET status = 'resolved', last_seen = :now WHERE id = :id"),
            {"id": inc["id"], "now": now})
        action, inc_id = "resolved", inc["id"]
    event_id = None
    if action in ("opened", "reopened", "resolved"):
        # WHAT WAS SERVING, AND SAY SO. The row above says "x% failing"; the response needs the
        # release, the routes and the app's build lineage, and something has to be told. Its
        # own savepoint: evidence that cannot be written never costs the scrape or the alert.
        from . import incident_response

        point = connection.begin_nested()
        try:
            event_id = incident_response.on_alert(connection, target, inc_id, action=action,
                                                  window=window, rule=rule, now=now)
            point.commit()
        except Exception:  # noqa: BLE001
            point.rollback()
    return {"verdict": verdict, "action": action, "window": window, "rule": rule,
            "event_id": event_id}


# --------------------------------------------------------------------------- #
# Read: the drawer's metrics block.
# --------------------------------------------------------------------------- #
_VERDICT_WORDS = {"firing": "Alerting", "ok": "Within limit", "quiet": "Too little traffic",
                  "off": "Alert off"}


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v else None


def metrics_view(connection, target: dict, *, now: Optional[datetime] = None,
                 since: Optional[datetime] = None, points: int = 60) -> dict[str, Any]:
    """What Observability shows about an app's own metrics. `word` is stable between polls
    (the console adds the age of `last_scrape_at`), so an unchanged view still answers 304."""
    now = now or datetime.now(timezone.utc)
    decl = declarations(connection, [target["id"]]).get(target["id"])
    if not decl:
        return {"declared": False, "word": "Not declared",
                "tip": "Add metrics: {path: /metrics} to boss.deploy.yaml and redeploy"}
    spec = decl["spec"]
    rule = alert_rule(spec)
    latest = connection.execute(sql(
        "SELECT scraped_at, ok, route, url, status_code, word, naming FROM app_metrics_scrapes "
        "WHERE deployment_target_id = :t ORDER BY scraped_at DESC LIMIT 1"),
        {"t": target["id"]}).mappings().first()
    last_ok = connection.execute(sql(
        "SELECT max(scraped_at) FROM app_metrics_scrapes WHERE deployment_target_id = :t "
        "AND ok"), {"t": target["id"]}).scalar()
    window = aggregate(_window_rows(connection, target["id"],
                                    now - timedelta(minutes=float(rule["window_minutes"])), now))
    series_rows = connection.execute(sql(
        "SELECT scraped_at, ok, requests, errors, interval_s, p50_ms, p95_ms "
        "FROM app_metrics_scrapes WHERE deployment_target_id = :t AND scraped_at >= :a "
        "ORDER BY scraped_at DESC LIMIT :n"),
        {"t": target["id"], "a": since or now - timedelta(hours=24), "n": points}
    ).mappings().all()
    series = []
    for r in reversed(series_rows):
        req, iv = r["requests"], r["interval_s"]
        series.append({
            "at": _iso(r["scraped_at"]), "ok": r["ok"],
            "rpm": round(req / iv * 60.0, 2) if req is not None and iv else None,
            "err": (round(r["errors"] / req, 4) if r["errors"] is not None and req else None),
            "p50": r["p50_ms"], "p95": r["p95_ms"]})
    inc = _incident(connection, target["project_id"], target["id"])
    # An incident only an app REPORTED is open without the monitor alerting: it is shown on its
    # card, never as this monitor's "Alerting".
    firing = bool(inc and inc["status"] not in _CLOSED and inc.get("alerted"))
    verdict = "firing" if firing else judge(window, rule)
    state = ("waiting" if not latest else "ok" if latest["ok"] else "failing")
    return {
        "declared": True, "path": spec["path"], "via": spec.get("via") or "auto",
        "interval_s": int(spec.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS),
        "state": state,
        "word": latest["word"] if latest else "Waiting for the first scrape",
        "last_scrape_at": _iso(latest["scraped_at"]) if latest else None,
        "last_ok_at": _iso(last_ok),
        "route": latest["route"] if latest else None,
        "url": latest["url"] if latest else None,
        "status_code": latest["status_code"] if latest else None,
        "naming": latest["naming"] if latest else None,
        **{k: window[k] for k in ("requests", "errors", "error_rate", "rate_per_min",
                                  "p50_ms", "p95_ms")},
        "window_minutes": rule["window_minutes"],
        "series": series,
        "alert": {"rule": rule, "state": verdict, "word": _VERDICT_WORDS[verdict],
                  "incident_id": inc["id"] if inc else None,
                  "incident_status": inc["status"] if inc else None,
                  "since": _iso(inc["first_seen"]) if firing else None},
    }
