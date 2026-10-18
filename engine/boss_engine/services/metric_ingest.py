"""Metrics any app sends BOSS: OTLP/HTTP, Prometheus remote write, or a /metrics page BOSS reads.

  POST /v1/ingest/otlp/v1/metrics     OTLP/HTTP, protobuf or JSON, gzip allowed, Bearer <token>
  POST /v1/ingest/prom/remote_write   remote write v1 (snappy protobuf), Bearer <token>
  a /metrics URL                      scraped through the outbound guard (synthetics' rules)

AUTHENTICATED AND STAMPED AT BOSS. The token names the project; the resource's `service.name`
and `deployment.environment` (or the remote-write `job` / `service`) name the service, which is
created on its first data. BOSS's labels (`project`, `service_id`, `service`, `app`,
`environment`) overwrite anything the client sent. Points land on the Postgres floor (T0) and,
when a bundled Prometheus is configured (BOSS_MONITORING_PROMETHEUS_URL, the compose
`monitoring` profile), are forwarded to it by remote write (T2).

NAMES ARE PROMETHEUS' OTLP TRANSLATION (Prometheus 3, UnderscoreEscapingWithSuffixes): dots
become underscores, the unit becomes a suffix (s -> _seconds, ms -> _milliseconds, By -> _bytes),
a monotonic sum gets `_total`, a histogram is `_bucket{le}`, `_count`, `_sum`. So the stable
semconv `http.server.request.duration` (s) is `http_server_request_duration_seconds_*` with
`http_request_method`, `http_route`, `http_response_status_code` — the names the golden-signal
dialects in app_alerts read (spec §5.3).

Traces and logs are refused with a sentence (501), never dropped silently. No dependency beyond
the standard library: the protobuf wire format and snappy are small enough to read here.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import re
import struct
import zlib
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ..ports import sql
from . import monitoring as mon

log = logging.getLogger(__name__)

TRACES_WORD = ("Traces are not stored in this version of BOSS. Link your trace backend on the "
               "service page; send metrics to /v1/ingest/otlp/v1/metrics.")
LOGS_WORD = ("Logs are not stored in this version of BOSS: BOSS reads them where they live. "
             "Send metrics to /v1/ingest/otlp/v1/metrics.")


# --------------------------------------------------------------------------- #
# The protobuf wire format, read and written
# --------------------------------------------------------------------------- #
class WireError(ValueError):
    pass


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise WireError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise WireError("varint too long")


def fields(buf: bytes) -> dict[int, list[Any]]:
    """field number -> [values]: int for varint, bytes for length-delimited, raw 8/4 bytes for
    fixed64/fixed32."""
    out: dict[int, list[Any]] = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            n, i = _varint(buf, i)
            v, i = buf[i:i + n], i + n
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise WireError(f"unsupported wire type {wt}")
        if i > len(buf):
            raise WireError("truncated field")
        out.setdefault(num, []).append(v)
    return out


def _d(raw: bytes) -> float:
    return struct.unpack("<d", raw)[0]


def _u64(raw: bytes) -> int:
    return struct.unpack("<Q", raw)[0]


def _s64(raw: bytes) -> int:
    return struct.unpack("<q", raw)[0]


def _str(raw: Any) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)


def _packed_doubles(values: list[Any]) -> list[float]:
    out: list[float] = []
    for v in values:
        if isinstance(v, (bytes, bytearray)) and len(v) % 8 == 0 and len(v) != 8:
            out += [_d(v[j:j + 8]) for j in range(0, len(v), 8)]
        elif isinstance(v, (bytes, bytearray)) and len(v) == 8:
            out.append(_d(v))
    return out


def _packed_u64(values: list[Any]) -> list[int]:
    out: list[int] = []
    for v in values:
        if isinstance(v, (bytes, bytearray)):
            out += [_u64(v[j:j + 8]) for j in range(0, len(v) - len(v) % 8, 8)]
    return out


def _w_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _w_field(num: int, wt: int, payload: bytes) -> bytes:
    key = _w_varint((num << 3) | wt)
    if wt == 2:
        return key + _w_varint(len(payload)) + payload
    return key + payload


# --------------------------------------------------------------------------- #
# Snappy (block format), for remote write
# --------------------------------------------------------------------------- #
def snappy_decompress(buf: bytes) -> bytes:
    length, i = _varint(buf, 0)
    out = bytearray()
    while i < len(buf):
        tag = buf[i]
        i += 1
        kind = tag & 3
        if kind == 0:
            n = tag >> 2
            if n >= 60:
                extra = n - 59
                n = int.from_bytes(buf[i:i + extra], "little")
                i += extra
            n += 1
            out += buf[i:i + n]
            i += n
            continue
        if kind == 1:
            n = ((tag >> 2) & 7) + 4
            off = ((tag >> 5) << 8) | buf[i]
            i += 1
        elif kind == 2:
            n = (tag >> 2) + 1
            off = int.from_bytes(buf[i:i + 2], "little")
            i += 2
        else:
            n = (tag >> 2) + 1
            off = int.from_bytes(buf[i:i + 4], "little")
            i += 4
        if off == 0 or off > len(out):
            raise WireError("bad snappy offset")
        for _ in range(n):
            out.append(out[-off])
    if len(out) != length:
        raise WireError("snappy length mismatch")
    return bytes(out)


def snappy_compress(data: bytes) -> bytes:
    """A valid snappy block of literals only (correct, not small: for forwarding)."""
    out = bytearray(_w_varint(len(data)))
    for i in range(0, len(data), 65536):
        chunk = data[i:i + 65536]
        n = len(chunk) - 1
        if n < 60:
            out.append(n << 2)
        elif n < 256:
            out += bytes([60 << 2, n])
        else:
            out += bytes([61 << 2]) + n.to_bytes(2, "little")
        out += chunk
    return bytes(out)


# --------------------------------------------------------------------------- #
# OTLP: protobuf -> the JSON shape, then one reader
# --------------------------------------------------------------------------- #
def _any_value(raw: bytes) -> Any:
    f = fields(raw)
    if 1 in f:
        return {"stringValue": _str(f[1][0])}
    if 2 in f:
        return {"boolValue": bool(f[2][0])}
    if 3 in f:
        v = f[3][0]
        return {"intValue": str(v - (1 << 64) if v >= 1 << 63 else v)}
    if 4 in f:
        return {"doubleValue": _d(f[4][0])}
    return {}


def _kv(raw: bytes) -> dict[str, Any]:
    f = fields(raw)
    return {"key": _str(f.get(1, [b""])[0]), "value": _any_value(f[2][0]) if 2 in f else {}}


def _number_point(raw: bytes) -> dict[str, Any]:
    f = fields(raw)
    p: dict[str, Any] = {"attributes": [_kv(a) for a in f.get(7, [])],
                         "timeUnixNano": str(_u64(f[3][0])) if 3 in f else "0"}
    if 4 in f:
        p["asDouble"] = _d(f[4][0])
    elif 6 in f:
        p["asInt"] = str(_s64(f[6][0]))
    return p


def _hist_point(raw: bytes) -> dict[str, Any]:
    f = fields(raw)
    return {"attributes": [_kv(a) for a in f.get(9, [])],
            "timeUnixNano": str(_u64(f[3][0])) if 3 in f else "0",
            "count": str(_u64(f[4][0])) if 4 in f else "0",
            "sum": _d(f[5][0]) if 5 in f else None,
            "bucketCounts": [str(x) for x in _packed_u64(f.get(6, []))],
            "explicitBounds": _packed_doubles(f.get(7, []))}


def otlp_protobuf_to_json(buf: bytes) -> dict[str, Any]:
    out = []
    for rm in fields(buf).get(1, []):
        rmf = fields(rm)
        res = fields(rmf[1][0]) if 1 in rmf else {}
        scopes = []
        for sm in rmf.get(2, []):
            metrics = []
            for m in fields(sm).get(2, []):
                mf = fields(m)
                metric: dict[str, Any] = {"name": _str(mf.get(1, [b""])[0]),
                                          "unit": _str(mf.get(3, [b""])[0])}
                if 5 in mf:
                    metric["gauge"] = {"dataPoints": [_number_point(p)
                                                      for p in fields(mf[5][0]).get(1, [])]}
                if 7 in mf:
                    sf = fields(mf[7][0])
                    metric["sum"] = {"dataPoints": [_number_point(p) for p in sf.get(1, [])],
                                     "aggregationTemporality": sf.get(2, [2])[0],
                                     "isMonotonic": bool(sf.get(3, [0])[0])}
                if 9 in mf:
                    hf = fields(mf[9][0])
                    metric["histogram"] = {"dataPoints": [_hist_point(p) for p in hf.get(1, [])],
                                           "aggregationTemporality": hf.get(2, [2])[0]}
                metrics.append(metric)
            scopes.append({"metrics": metrics})
        out.append({"resource": {"attributes": [_kv(a) for a in res.get(1, [])]},
                    "scopeMetrics": scopes})
    return {"resourceMetrics": out}


def _attr_value(v: Mapping[str, Any]) -> Optional[str]:
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            x = v[k]
            if k == "doubleValue":
                x = float(x)
                return str(int(x)) if x == int(x) else repr(x)
            if k == "boolValue":
                return "true" if x else "false"
            return str(x)
    return None


def _attrs(items: Any) -> dict[str, str]:
    out = {}
    for kv in items or []:
        if isinstance(kv, Mapping) and kv.get("key"):
            v = _attr_value(kv.get("value") or {})
            if v is not None:
                out[str(kv["key"])] = v
    return out


_UNIT_SUFFIX = {"s": "seconds", "ms": "milliseconds", "us": "microseconds",
                "ns": "nanoseconds", "By": "bytes", "KBy": "kilobytes", "MBy": "megabytes",
                "min": "minutes", "h": "hours", "%": "percent"}


def prom_name(name: str, unit: str, *, counter: bool, gauge_ratio: bool = False) -> str:
    base = re.sub(r"[^a-zA-Z0-9_:]", "_", name or "metric")
    base = re.sub(r"_+", "_", base).strip("_") or "metric"
    if base[0].isdigit():
        base = "_" + base
    u = (unit or "").strip()
    suffix = _UNIT_SUFFIX.get(u)
    if not suffix and u == "1" and gauge_ratio:
        suffix = "ratio"
    if suffix and not base.endswith("_" + suffix):
        base += "_" + suffix
    if counter and not base.endswith("_total"):
        base += "_total"
    return base


def label_name(key: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_]", "_", key)
    return ("key_" + out) if out[:1].isdigit() else out


def _t(nanos: Any, now: datetime) -> datetime:
    try:
        n = int(nanos or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return now
    return datetime.fromtimestamp(n / 1e9, tz=timezone.utc)


def decode_otlp(raw: bytes, content_type: str, content_encoding: str) -> dict[str, Any]:
    body = _decompress(raw, content_encoding)
    if "json" in (content_type or "").lower():
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise mon.IngestRefused(400, "The body is not OTLP JSON") from None
    try:
        return otlp_protobuf_to_json(body)
    except (WireError, struct.error, IndexError):
        raise mon.IngestRefused(400, "The body is not OTLP protobuf "
                                     "(ExportMetricsServiceRequest)") from None


def _decompress(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower().strip()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            return zlib.decompress(raw)
    except (OSError, zlib.error, EOFError):
        raise mon.IngestRefused(400, f"The body is not valid {enc}") from None
    if enc in ("", "identity"):
        return raw
    raise mon.IngestRefused(415, f"Content-Encoding {enc} is not read; send gzip or none")


def otlp_points(doc: Mapping[str, Any], now: datetime
                ) -> list[tuple[dict[str, str], dict[str, str], datetime, float, bool]]:
    """[(resource attributes, labels incl. __name__, t, value, is_delta)] in Prometheus names."""
    out = []
    for rm in doc.get("resourceMetrics") or []:
        res = _attrs(((rm.get("resource") or {}).get("attributes")))
        for sm in rm.get("scopeMetrics") or rm.get("instrumentationLibraryMetrics") or []:
            for m in sm.get("metrics") or []:
                name, unit = str(m.get("name") or ""), str(m.get("unit") or "")
                if "sum" in m:
                    s = m["sum"] or {}
                    mono = bool(s.get("isMonotonic"))
                    delta = int(s.get("aggregationTemporality") or 2) == 1
                    pname = prom_name(name, unit, counter=mono)
                    for p in s.get("dataPoints") or []:
                        v = p.get("asDouble", p.get("asInt"))
                        lab = {label_name(k): v2 for k, v2 in _attrs(p.get("attributes")).items()}
                        out.append((res, {"__name__": pname, **lab},
                                    _t(p.get("timeUnixNano"), now), float(v or 0), delta and mono))
                if "gauge" in m:
                    pname = prom_name(name, unit, counter=False, gauge_ratio=True)
                    for p in (m["gauge"] or {}).get("dataPoints") or []:
                        v = p.get("asDouble", p.get("asInt"))
                        lab = {label_name(k): v2 for k, v2 in _attrs(p.get("attributes")).items()}
                        out.append((res, {"__name__": pname, **lab},
                                    _t(p.get("timeUnixNano"), now), float(v or 0), False))
                if "histogram" in m:
                    h = m["histogram"] or {}
                    delta = int(h.get("aggregationTemporality") or 2) == 1
                    base = prom_name(name, unit, counter=False)
                    for p in h.get("dataPoints") or []:
                        lab = {label_name(k): v2 for k, v2 in _attrs(p.get("attributes")).items()}
                        t = _t(p.get("timeUnixNano"), now)
                        counts = [float(c) for c in p.get("bucketCounts") or []]
                        bounds = [float(b) for b in p.get("explicitBounds") or []]
                        cum = 0.0
                        for i, c in enumerate(counts):
                            cum += c
                            le = "+Inf" if i >= len(bounds) else _le(bounds[i])
                            out.append((res, {"__name__": base + "_bucket", **lab, "le": le},
                                        t, cum, delta))
                        out.append((res, {"__name__": base + "_count", **lab}, t,
                                    float(p.get("count") or 0), delta))
                        if p.get("sum") is not None:
                            out.append((res, {"__name__": base + "_sum", **lab}, t,
                                        float(p["sum"]), delta))
    return out


def _le(b: float) -> str:
    if math.isinf(b):
        return "+Inf"
    return str(int(b)) if b == int(b) and abs(b) < 1e15 else repr(b)


# --------------------------------------------------------------------------- #
# Remote write v1
# --------------------------------------------------------------------------- #
def decode_remote_write(raw: bytes, content_encoding: str = "snappy"
                        ) -> list[tuple[dict[str, str], datetime, float]]:
    try:
        body = snappy_decompress(raw) if (content_encoding or "snappy").lower() == "snappy" \
            else raw
        out = []
        for ts in fields(body).get(1, []):
            tf = fields(ts)
            labels = {}
            for lab in tf.get(1, []):
                lf = fields(lab)
                labels[_str(lf.get(1, [b""])[0])] = _str(lf.get(2, [b""])[0])
            for smp in tf.get(2, []):
                sf = fields(smp)
                v = _d(sf[1][0]) if 1 in sf else 0.0
                tms = sf.get(2, [0])[0]
                tms = tms - (1 << 64) if tms >= 1 << 63 else tms
                out.append((labels, datetime.fromtimestamp(tms / 1000.0, tz=timezone.utc), v))
        return out
    except (WireError, struct.error, IndexError, ValueError):
        raise mon.IngestRefused(400, "The body is not a snappy-compressed remote write "
                                     "WriteRequest (v1)") from None


def encode_remote_write(series: list[tuple[dict[str, str], list[tuple[int, float]]]]) -> bytes:
    body = b""
    for labels, samples in series:
        ts = b"".join(_w_field(1, 2, _w_field(1, 2, k.encode()) + _w_field(2, 2, v.encode()))
                      for k, v in sorted(labels.items()))
        ts += b"".join(_w_field(2, 2, _w_field(1, 1, struct.pack("<d", v))
                                + _w_field(2, 0, _w_varint(int(t) & ((1 << 64) - 1))))
                       for t, v in samples)
        body += _w_field(1, 2, ts)
    return snappy_compress(body)


# --------------------------------------------------------------------------- #
# Recognising what a service speaks
# --------------------------------------------------------------------------- #
DIALECT_MARKERS = {
    "otel": "http_server_request_duration_seconds_count",
    "otel_legacy": "http_server_duration_milliseconds_count",
    "boss": "http_requests_total",
}


def recognise(names: set[str]) -> tuple[Optional[str], str]:
    """(dialect, words): which HTTP server metrics the names are, if any."""
    for dialect, marker in DIALECT_MARKERS.items():
        if marker in names:
            word = {"otel": "HTTP server metrics recognised (OpenTelemetry semconv)",
                    "otel_legacy": "HTTP server metrics recognised (older OpenTelemetry names)",
                    "boss": "HTTP server metrics recognised (BOSS's metrics standard)"}[dialect]
            return dialect, word
    return None, "No HTTP server metrics recognised yet; custom rules can read these series"


# --------------------------------------------------------------------------- #
# Store: stamp, write, recognise, forward
# --------------------------------------------------------------------------- #
def _service_for(connection, source: Mapping[str, Any], name: Optional[str],
                 env: Optional[str], path: str) -> dict[str, Any]:
    if source.get("service_id"):
        svc = mon.service(connection, source["project_id"], source["service_id"])
        if svc:
            return svc
    return mon.find_or_create_service(connection, source["project_id"],
                                      name or "unknown_service", env or "", path=path)


def store(connection, settings: Any, source: Mapping[str, Any], by_service: dict, *,
          path: str, now: datetime) -> dict[str, Any]:
    """by_service: {(name, env): [(labels, t, v, is_delta)]}. Returns what arrived, in words."""
    from . import app_alerts

    total_series = total_points = 0
    words, services = [], []
    forward: list[tuple[dict[str, str], list[tuple[int, float]]]] = []
    for (name, env), pts in by_service.items():
        svc = _service_for(connection, source, name, env, path)
        stamped = []
        delta = set()
        for labels, t, v, is_delta in pts:
            lab = mon.stamp(labels, project_id=source["project_id"], svc=svc)
            stamped.append((lab, t, v))
            if is_delta:
                delta.add(mon.labels_hash(lab))
        got = mon.write_points(connection, source["project_id"], svc, stamped, now=now,
                               cumulative_delta=delta)
        names = {lab["__name__"] for lab, _t, _v in stamped}
        dialect, word = recognise(names | _known_names(connection, svc["id"]))
        if dialect and dialect != svc.get("dialect"):
            connection.execute(sql("UPDATE monitored_services SET dialect = :d WHERE id = :s"),
                               {"d": dialect, "s": svc["id"]})
            svc["dialect"] = dialect
        proposed = 0
        if dialect and not svc.get("deployment_target_id"):
            proposed = len(app_alerts.ensure_service_rules(connection, mon.subject_of(svc)))
        mon.note_data(connection, svc["id"], now, series=got["series"], path=path,
                      recognised=word + (f"; {proposed} rules proposed" if proposed else ""))
        total_series += got["series"]
        total_points += got["points"]
        services.append(svc["name"])
        words.append(f"{got['series']} series from {svc['name']}; {word}"
                     + (f"; {proposed} rules proposed" if proposed else ""))
        if getattr(settings, "monitoring_prometheus_url", None):
            series: dict[str, tuple[dict, list]] = {}
            for lab, t, v in stamped:
                series.setdefault(mon.labels_hash(lab), (lab, []))[1].append(
                    (int(t.timestamp() * 1000), v))
            forward += list(series.values())
    if forward:
        _forward(settings, forward)
    return {"series": total_series, "points": total_points, "services": services,
            "word": "; ".join(words) or "Nothing in the body"}


def _known_names(connection, service_id: str) -> set[str]:
    return set(connection.execute(sql(
        "SELECT DISTINCT name FROM metric_series WHERE service_id = :s LIMIT 500"),
        {"s": service_id}).scalars().all())


def _forward(settings: Any, series) -> None:
    """Best effort to the bundled Prometheus (its remote-write receiver); the floor has it."""
    import urllib.request

    url = str(settings.monitoring_prometheus_url).rstrip("/") + "/api/v1/write"
    req = urllib.request.Request(url, data=encode_remote_write(series), method="POST", headers={
        "Content-Type": "application/x-protobuf", "Content-Encoding": "snappy",
        "X-Prometheus-Remote-Write-Version": "0.1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — BOSS's own Prometheus
            r.read(1000)
    except Exception:  # noqa: BLE001 — the floor holds the data; forwarding is best effort
        log.info("metric_ingest: forward to the bundled Prometheus failed", exc_info=True)


def ingest_otlp(connection, settings: Any, source: Mapping[str, Any], raw: bytes, *,
                content_type: str, content_encoding: str, now: datetime) -> dict[str, Any]:
    doc = decode_otlp(raw, content_type, content_encoding)
    by: dict[tuple, list] = {}
    for res, labels, t, v, delta in otlp_points(doc, now):
        name = res.get("service.name") or "unknown_service"
        env = (res.get("deployment.environment.name") or res.get("deployment.environment")
               or "")
        lab = dict(labels)
        lab.setdefault("job", name)
        if res.get("service.instance.id"):
            lab.setdefault("instance", res["service.instance.id"])
        by.setdefault((name, env), []).append((lab, t, v, delta))
    return store(connection, settings, source, by, path="otlp", now=now)


def ingest_remote_write(connection, settings: Any, source: Mapping[str, Any], raw: bytes, *,
                        content_encoding: str, now: datetime) -> dict[str, Any]:
    by: dict[tuple, list] = {}
    for labels, t, v in decode_remote_write(raw, content_encoding):
        name = labels.get("service") or labels.get("job") or labels.get("app") or "unknown"
        env = labels.get("environment") or labels.get("env") or ""
        by.setdefault((name, env), []).append((labels, t, v, False))
    return store(connection, settings, source, by, path="remote_write", now=now)


def ingest_exposition(connection, settings: Any, svc: Mapping[str, Any], body: str,
                      now: datetime) -> dict[str, Any]:
    """A scraped /metrics page for a service BOSS did not deploy (synthetics' fetch)."""
    from .app_metrics import parse_exposition

    samples = parse_exposition(body)
    pts = [({"__name__": s.name, **{k: str(v) for k, v in s.labels.items()}}, now,
            float(s.value), False) for s in samples]
    source = {"project_id": svc["project_id"], "service_id": svc["id"]}
    return store(connection, settings, source, {(svc["name"], svc["environment"]): pts},
                 path="scrape", now=now)


__all__ = ["LOGS_WORD", "TRACES_WORD", "decode_otlp", "decode_remote_write",
           "encode_remote_write", "ingest_exposition", "ingest_otlp", "ingest_remote_write",
           "otlp_points", "otlp_protobuf_to_json", "prom_name", "recognise",
           "snappy_compress", "snappy_decompress"]
