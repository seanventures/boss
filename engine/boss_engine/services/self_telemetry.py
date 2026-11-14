"""BOSS'S OWN TELEMETRY (BOSSAPP-867): OpenTelemetry traces, Sentry errors and Prometheus metrics
for BOSS itself — not for the apps it deploys (those have infra_plugins/opentelemetry.py).

EVERY PART IS OPT-IN AND NONE IS A DEPENDENCY. With nothing configured this module does nothing:
no import of an optional package, no thread, no socket, no middleware.

  Traces   OTEL_EXPORTER_OTLP_ENDPOINT set → the OTel SDK exports over OTLP/HTTP, FastAPI,
           SQLAlchemy and httpx are auto-instrumented when their instrumentation packages are
           installed, and BOSS adds GenAI spans (OTel GenAI semantic conventions) for every model
           call (llm_telemetry.record_llm_call, the one recording seam) and every MCP tool call
           (mcp_registry.call_tool_with_receipt). OTEL_EXPORTER_OTLP_HEADERS / OTEL_SERVICE_NAME
           are read by the SDK itself. Needs `opentelemetry-sdk` and
           `opentelemetry-exporter-otlp-proto-http` (plus `opentelemetry-instrumentation-*` for
           the auto-instrumentation): a missing package is said once in the log, never raised.
  Errors   SENTRY_DSN set and `sentry-sdk` installed → sentry_sdk.init with send_default_pii off
           and a before_send filter that runs every string through services/redaction (secrets
           and PII) and drops cookies and auth headers. BOSS_SENTRY_TRACES_SAMPLE_RATE (0.0).
  Metrics  BOSS_METRICS_PORT > 0 → a Prometheus text endpoint (GET /metrics) on its own small
           listener (BOSS_METRICS_HOST, default 127.0.0.1), outside the API and its sign-in, so a
           scraper needs no BOSS credential and the API's routes are unchanged. No dependency:
           the exposition format is written here. Counters: model calls, tokens, list-price
           cost, tool calls by outcome, and process start time.

  Run spans  (AP-P4) every kernel run's spans (kernel/trace.py) are exported with their own
           ids by `export_trace_span` when traces are on, parented to the run (traceparent).

NOT HERE (deferred, said in the ticket): a per-org export destination for agent-run spans.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Iterator, Optional

log = logging.getLogger("boss.self_telemetry")

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"tracer": None, "tracing": False, "sentry": False, "metrics": None,
                          "said": set()}


def _say_once(key: str, message: str, *args: Any) -> None:
    if key in _STATE["said"]:
        return
    _STATE["said"].add(key)
    log.warning(message, *args)


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    value = getattr(settings, name, default) if settings is not None else default
    return default if value in (None, "") else value


# ── traces ───────────────────────────────────────────────────────────────────────────────────

def init_tracing(settings: Any, *, app: Any = None, engine: Any = None,
                 service: str = "boss-api") -> dict[str, Any]:
    """Turn on OTel export and auto-instrumentation when OTEL_EXPORTER_OTLP_ENDPOINT is set.
    Answers what is on (``{"tracing": bool, "instrumented": [...], "missing": [...]}``)."""
    endpoint = _setting(settings, "otel_exporter_otlp_endpoint")
    out: dict[str, Any] = {"tracing": False, "instrumented": [], "missing": []}
    if not endpoint:
        return out
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter)
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]
    except Exception as error:  # noqa: BLE001 — optional: say it, never fail the process
        out["missing"].append("opentelemetry-sdk / opentelemetry-exporter-otlp-proto-http")
        _say_once("otel-missing", "self-telemetry: OTEL_EXPORTER_OTLP_ENDPOINT is set but the "
                  "OpenTelemetry SDK is not installed (%s); BOSS's own traces stay off.",
                  type(error).__name__)
        return out
    with _LOCK:
        if not _STATE["tracing"]:
            name = _setting(settings, "otel_service_name") or service
            provider = TracerProvider(resource=Resource.create({"service.name": name}),
                                      id_generator=_preset_id_generator())
            # The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS itself; traces go to
            # <endpoint>/v1/traces as the OTLP/HTTP spec says.
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
            _STATE["tracer"] = trace.get_tracer("boss")
            _STATE["tracing"] = True
    out["tracing"] = True
    for label, module, call in (
        ("fastapi", "opentelemetry.instrumentation.fastapi", lambda m: app is not None and (
            m.FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics") or True)),
        ("sqlalchemy", "opentelemetry.instrumentation.sqlalchemy", lambda m: engine is not None and (
            m.SQLAlchemyInstrumentor().instrument(engine=engine) or True)),
        ("httpx", "opentelemetry.instrumentation.httpx",
         lambda m: m.HTTPXClientInstrumentor().instrument() or True),
    ):
        try:
            import importlib

            if call(importlib.import_module(module)):
                out["instrumented"].append(label)
        except Exception:  # noqa: BLE001 — each instrumentation is optional on its own
            out["missing"].append(f"opentelemetry-instrumentation-{label}")
    log.info("self-telemetry: traces on (%s); instrumented %s", service,
             ", ".join(out["instrumented"]) or "nothing")
    return out


def tracing_on() -> bool:
    return bool(_STATE["tracing"])


@contextlib.contextmanager
def tool_span(server: str, tool: str, *, run_id: Optional[str] = None,
              agent: Optional[str] = None) -> Iterator[Any]:
    """An OTel GenAI ``execute_tool`` span around one MCP tool call; a no-op with tracing off.
    The span is current while the call runs, so mcp_headers_policy's ``traceparent`` (BOSSAPP-873)
    names it."""
    tracer = _STATE["tracer"]
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(f"execute_tool {tool}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", str(tool))
        span.set_attribute("boss.mcp.server", str(server))
        if run_id:
            span.set_attribute("boss.run_id", str(run_id))
        if agent:
            span.set_attribute("gen_ai.agent.name", str(agent))
        yield span


def record_model_span(*, engine_name: str, model: str, usage: dict, latency_ms: int, ok: bool,
                      error: Optional[str], stage: Optional[str], cost_usd_micros: int) -> None:
    """A finished model call as an OTel GenAI ``chat`` span (started ``latency_ms`` ago), and the
    metrics counters. Called from llm_telemetry.record_llm_call. Never raises."""
    try:
        metrics_count_model_call(engine_name, ok, usage, cost_usd_micros)
        tracer = _STATE["tracer"]
        if tracer is None:
            return
        end = time.time_ns()
        start = end - max(0, int(latency_ms or 0)) * 1_000_000
        span = tracer.start_span(f"chat {model or engine_name}", start_time=start)
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", str(engine_name))
        if model:
            span.set_attribute("gen_ai.request.model", str(model))
        for key, attr in (("prompt_tokens", "gen_ai.usage.input_tokens"),
                          ("completion_tokens", "gen_ai.usage.output_tokens")):
            if isinstance(usage.get(key), int):
                span.set_attribute(attr, int(usage[key]))
        if stage:
            span.set_attribute("boss.stage", str(stage))
        span.set_attribute("boss.cost_usd_micros", int(cost_usd_micros or 0))
        if not ok:
            from opentelemetry.trace import Status, StatusCode  # type: ignore[import-not-found]
            from .redaction import redact_secrets

            span.set_status(Status(StatusCode.ERROR, redact_secrets(str(error or ""))[:200]))
        span.end(end_time=end)
    except Exception:  # noqa: BLE001 — telemetry never breaks the call it describes
        log.debug("self-telemetry: model span not recorded", exc_info=True)


# ── run spans (AP-P4): the kernel's own spans, exported with their own ids ───────────────────

#: The (trace id, span id) the next span this thread starts must carry (export_trace_span sets
#: it), so an exported span keeps the ids BOSS stored and its children's parent links meet.
_PRESET_SPAN_ID: Any = None


def _preset_id_generator() -> Any:
    """A RandomIdGenerator that hands out a preset span id when export_trace_span asks."""
    import contextvars

    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator  # type: ignore[import-not-found]

    global _PRESET_SPAN_ID
    if _PRESET_SPAN_ID is None:
        _PRESET_SPAN_ID = contextvars.ContextVar("boss_preset_span_id", default=None)
    preset = _PRESET_SPAN_ID

    class PresetIds(RandomIdGenerator):
        def generate_span_id(self) -> int:
            wanted = preset.get()
            return int(wanted[1]) if wanted else super().generate_span_id()

        def generate_trace_id(self) -> int:
            wanted = preset.get()
            return int(wanted[0]) if wanted else super().generate_trace_id()

    return PresetIds()


def export_trace_span(span: Any) -> bool:
    """One finished kernel span (kernel/trace.Span) over OTLP, with BOSS's own trace id, span id
    and parent — so the run is the exported spans' parent (`traceparent`) and the tree an OTel
    backend draws is the tree the console draws. A no-op unless BOSS's tracing is on (OTEL_
    EXPORTER_OTLP_ENDPOINT set and the SDK installed: off by default, D8). Never raises."""
    tracer = _STATE["tracer"]
    if tracer is None or _PRESET_SPAN_ID is None or getattr(span, "ended_at", None) is None:
        return False
    try:
        from opentelemetry import trace as otel  # type: ignore[import-not-found]
        from opentelemetry.trace import (NonRecordingSpan, SpanContext,  # type: ignore[import-not-found]
                                         TraceFlags)

        trace_id = int(span.trace_id, 16)
        if span.parent_span_id:
            parent = otel.set_span_in_context(NonRecordingSpan(SpanContext(
                trace_id=trace_id, span_id=int(span.parent_span_id, 16), is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED))))
        else:
            # A root run: no parent; the id generator hands out the stored trace id.
            parent = otel.set_span_in_context(otel.INVALID_SPAN)
        token = _PRESET_SPAN_ID.set((trace_id, int(span.span_id, 16)))
        try:
            started = int(float(span.started_at) * 1e9)
            ended = int(float(span.ended_at) * 1e9)
            exported = tracer.start_span(f"{span.kind} {span.name}", context=parent,
                                         start_time=started)
        finally:
            _PRESET_SPAN_ID.reset(token)
        exported.set_attribute("boss.span.kind", str(span.kind))
        exported.set_attribute("boss.run_id", str(span.run_id))
        exported.set_attribute("boss.project_id", str(span.project_id))
        if span.act_id:
            exported.set_attribute("boss.act_id", str(span.act_id))
        if span.agent_key:
            exported.set_attribute("gen_ai.agent.name", str(span.agent_key))
        if span.kind == "model":
            exported.set_attribute("gen_ai.operation.name", "chat")
        elif span.kind in ("tool", "subagent", "handoff"):
            exported.set_attribute("gen_ai.operation.name", "execute_tool")
            exported.set_attribute("gen_ai.tool.name", str(span.name))
        if span.seq is not None:
            exported.set_attribute("boss.ledger.seq", int(span.seq))
        if span.status in ("error", "refused"):
            from opentelemetry.trace import Status, StatusCode  # type: ignore[import-not-found]

            exported.set_status(Status(StatusCode.ERROR, str(span.status)))
        exported.end(end_time=ended)
        return True
    except Exception:  # noqa: BLE001 — telemetry never breaks the call it describes
        log.debug("self-telemetry: run span not exported", exc_info=True)
        return False


# ── errors ───────────────────────────────────────────────────────────────────────────────────

_DROP_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "x-boss-csrf",
                 "proxy-authorization", "dpop"}


def scrub(value: Any, _depth: int = 0) -> Any:
    """Every string in a Sentry event run through BOSS's redaction (secrets and PII); auth
    headers and cookies dropped outright."""
    if _depth > 12:
        return "[truncated]"
    if isinstance(value, str):
        from .redaction import redact

        return redact(value)[0]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _DROP_HEADERS:
                out[k] = "[redacted]"
            elif isinstance(k, str) and k.lower() == "cookies":
                out[k] = {}
            else:
                out[k] = scrub(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(v, _depth + 1) for v in value)
    return value


def before_send(event: Any, _hint: Any = None) -> Any:
    try:
        return scrub(event)
    except Exception:  # noqa: BLE001 — an event that cannot be scrubbed is not sent
        return None


def init_sentry(settings: Any, *, service: str = "boss-api") -> bool:
    dsn = _setting(settings, "sentry_dsn")
    if not dsn:
        return False
    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        _say_once("sentry-missing", "self-telemetry: SENTRY_DSN is set but sentry-sdk is not "
                  "installed; BOSS's own errors are not reported.")
        return False
    with _LOCK:
        if not _STATE["sentry"]:
            sentry_sdk.init(dsn=str(dsn), send_default_pii=False, before_send=before_send,
                            traces_sample_rate=float(_setting(settings,
                                                              "sentry_traces_sample_rate", 0.0)),
                            server_name=service, environment=str(_setting(
                                settings, "boss_edition", "community")))
            _STATE["sentry"] = True
    log.info("self-telemetry: Sentry on (%s)", service)
    return True


# ── metrics ──────────────────────────────────────────────────────────────────────────────────

class Metrics:
    """Counters in process memory, written in the Prometheus text format (0.0.4)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = {}
        self.started = time.time()

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self.lock:
            self.counters[key] = self.counters.get(key, 0.0) + float(value)

    def render(self) -> str:
        helps = {
            "boss_model_calls_total": "Model calls recorded, by engine and outcome.",
            "boss_model_tokens_total": "Model tokens recorded, by engine and direction.",
            "boss_model_cost_usd_micros_total": "List-price cost of model calls, in micro-dollars.",
            "boss_tool_calls_total": "MCP tool calls through the receipt door, by outcome.",
            "boss_agent_runs_total": "Kernel runs finished, by agent and status (AP-P4 spans).",
            "boss_agent_run_duration_ms_total": "Wall time of finished kernel runs, by agent.",
        }
        lines = ["# HELP boss_process_start_time_seconds Start time of this BOSS process.",
                 "# TYPE boss_process_start_time_seconds gauge",
                 f"boss_process_start_time_seconds {self.started:.3f}"]
        with self.lock:
            items = sorted(self.counters.items())
        seen: set[str] = set()
        for (name, labels), value in items:
            if name not in seen:
                seen.add(name)
                lines.append(f"# HELP {name} {helps.get(name, name)}")
                lines.append(f"# TYPE {name} counter")
            said = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
            lines.append(f"{name}{{{said}}} {value:g}" if said else f"{name} {value:g}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metrics() -> Optional[Metrics]:
    return _STATE["metrics"]


def metrics_count_model_call(engine_name: str, ok: bool, usage: dict,
                             cost_usd_micros: int) -> None:
    m = _STATE["metrics"]
    if m is None:
        return
    m.inc("boss_model_calls_total", engine=engine_name or "unknown", ok=str(bool(ok)).lower())
    for key, direction in (("prompt_tokens", "input"), ("completion_tokens", "output")):
        if isinstance(usage.get(key), int) and usage[key] > 0:
            m.inc("boss_model_tokens_total", usage[key], engine=engine_name or "unknown",
                  direction=direction)
    if cost_usd_micros:
        m.inc("boss_model_cost_usd_micros_total", int(cost_usd_micros),
              engine=engine_name or "unknown")


def metrics_count_tool_call(outcome: str) -> None:
    m = _STATE["metrics"]
    if m is not None:
        m.inc("boss_tool_calls_total", outcome=outcome or "unknown")


def metrics_count_agent_run(agent: Optional[str], status: str, duration_ms: int) -> None:
    """A finished kernel run (kernel/trace.py's run span), for boss_agent_run_* (AP-P4)."""
    m = _STATE["metrics"]
    if m is None:
        return
    who = agent or "unnamed"
    m.inc("boss_agent_runs_total", agent=who, status=status or "unknown")
    m.inc("boss_agent_run_duration_ms_total", max(0, int(duration_ms or 0)), agent=who)


def start_metrics(settings: Any) -> Optional[int]:
    """Serve GET /metrics on BOSS_METRICS_PORT (0 = off). Answers the bound port, or None."""
    port = int(_setting(settings, "metrics_port", 0) or 0)
    if port <= 0:
        return None
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    with _LOCK:
        if _STATE["metrics"] is not None and _STATE.get("metrics_port"):
            return _STATE["metrics_port"]
        registry = Metrics()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 — scrapes are not news
                return

            def do_GET(self):  # noqa: N802
                if self.path.split("?", 1)[0] != "/metrics":
                    self.send_response(404)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                body = registry.render().encode()
                self.send_response(200)
                self.send_header("content-type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        host = str(_setting(settings, "metrics_host", "127.0.0.1"))
        try:
            server = ThreadingHTTPServer((host, port), Handler)
        except OSError as error:
            _say_once("metrics-bind", "self-telemetry: /metrics not served on %s:%s (%s)",
                      host, port, error)
            return None
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="boss-metrics", daemon=True).start()
        _STATE["metrics"] = registry
        _STATE["metrics_port"] = server.server_address[1]
        _STATE["metrics_server"] = server
    log.info("self-telemetry: Prometheus metrics on %s:%s/metrics", host, _STATE["metrics_port"])
    return _STATE["metrics_port"]


def stop_metrics() -> None:
    """Tests: stop the listener and forget the counters."""
    with _LOCK:
        server = _STATE.pop("metrics_server", None)
        _STATE["metrics"] = None
        _STATE.pop("metrics_port", None)
    if server is not None:
        server.shutdown()
        server.server_close()


# ── one call per process ─────────────────────────────────────────────────────────────────────

def install(settings: Any, *, app: Any = None, engine: Any = None,
            service: str = "boss-api") -> dict[str, Any]:
    """Everything configured, for one process (the API at import, the worker at start). With
    nothing configured: returns all-off and touches nothing. Never raises."""
    out: dict[str, Any] = {"tracing": False, "sentry": False, "metrics_port": None}
    try:
        out.update(init_tracing(settings, app=app, engine=engine, service=service))
    except Exception:  # noqa: BLE001
        log.warning("self-telemetry: tracing not started", exc_info=True)
    try:
        out["sentry"] = init_sentry(settings, service=service)
    except Exception:  # noqa: BLE001
        log.warning("self-telemetry: Sentry not started", exc_info=True)
    try:
        out["metrics_port"] = start_metrics(settings)
    except Exception:  # noqa: BLE001
        log.warning("self-telemetry: metrics not started", exc_info=True)
    return out


__all__ = ["Metrics", "before_send", "export_trace_span", "init_sentry", "init_tracing", "install",
           "metrics",
           "metrics_count_agent_run", "metrics_count_model_call", "metrics_count_tool_call",
           "record_model_span", "scrub",
           "start_metrics", "stop_metrics", "tool_span", "tracing_on"]
