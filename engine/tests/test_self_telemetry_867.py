"""BOSSAPP-867: OpenTelemetry, Sentry and Prometheus for BOSS itself — all opt-in, none a
dependency.

  * nothing configured: install() turns nothing on (no tracer, no listener, no Sentry);
  * an endpoint set without the SDK installed: said once in the log, never raised;
  * the Sentry filter runs every string through BOSS's redaction and drops auth headers/cookies;
  * BOSS_METRICS_PORT serves Prometheus text on its own listener, counting model and tool calls;
  * with a tracer, every model call recorded through llm_telemetry.record_llm_call becomes a GenAI
    `chat` span and every receipt-door tool call an `execute_tool` span;
  * the 873 traceparent forwarding reads the current span through the real OpenTelemetry API.
"""
from __future__ import annotations

import builtins
import contextlib
import socket
from types import SimpleNamespace

import httpx
import pytest

from boss_engine.services import self_telemetry as st


@pytest.fixture(autouse=True)
def clean_state():
    saved = dict(st._STATE)
    st._STATE.update(tracer=None, tracing=False, sentry=False, metrics=None, said=set())
    yield
    st.stop_metrics()
    st._STATE.clear()
    st._STATE.update(saved)


def _settings(**kw):
    base = dict(otel_exporter_otlp_endpoint=None, otel_service_name=None, sentry_dsn=None,
                sentry_traces_sample_rate=0.0, metrics_port=0, metrics_host="127.0.0.1",
                boss_edition="community")
    base.update(kw)
    return SimpleNamespace(**base)


def test_nothing_configured_turns_nothing_on():
    out = st.install(_settings(), service="boss-api")
    assert out == {"tracing": False, "instrumented": [], "missing": [], "sentry": False,
                   "metrics_port": None}
    assert st.metrics() is None and not st.tracing_on()
    # the hooks are no-ops
    st.record_model_span(engine_name="native", model="m", usage={}, latency_ms=5, ok=True,
                         error=None, stage=None, cost_usd_micros=1)
    st.metrics_count_tool_call("succeeded")
    with st.tool_span("srv", "echo") as span:
        assert span is None


def test_config_defaults_are_off():
    from boss_engine.config import Settings

    fields = Settings.model_fields
    assert fields["metrics_port"].default == 0
    assert fields["otel_exporter_otlp_endpoint"].default is None
    assert fields["sentry_traces_sample_rate"].default == 0.0


def test_an_endpoint_without_the_sdk_is_said_not_raised(monkeypatch, caplog):
    real = builtins.__import__

    def no_sdk(name, *a, **k):
        if name.startswith("opentelemetry.sdk") or name.startswith("opentelemetry.exporter") \
                or name == "sentry_sdk":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    out = st.install(_settings(otel_exporter_otlp_endpoint="http://collector:4318",
                               sentry_dsn="https://k@sentry.example/1"))
    assert out["tracing"] is False and out["sentry"] is False and out["missing"]
    st.install(_settings(otel_exporter_otlp_endpoint="http://collector:4318"))
    said = [r.message for r in caplog.records if "OpenTelemetry SDK is not installed" in r.message]
    assert len(said) == 1  # once, not per call


def test_the_sentry_filter_redacts():
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    event = {"message": f"push failed with {token}",
             "request": {"headers": {"Authorization": "Bearer x", "X-Ok": "fine"},
                         "cookies": {"boss_session": "s"}},
             "extra": {"nested": [f"token={token}"]}}
    out = st.before_send(event)
    flat = str(out)
    assert token not in flat
    assert out["request"]["headers"]["Authorization"] == "[redacted]"
    assert out["request"]["headers"]["X-Ok"] == "fine"
    assert out["request"]["cookies"] == {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_metrics_listener_counts_model_and_tool_calls():
    port = st.start_metrics(_settings(metrics_port=_free_port()))
    assert port
    st.record_model_span(engine_name="native", model="m", usage={"prompt_tokens": 10,
                         "completion_tokens": 3}, latency_ms=5, ok=True, error=None, stage="plan",
                         cost_usd_micros=250)
    st.record_model_span(engine_name="native", model="m", usage={}, latency_ms=5, ok=False,
                         error="boom", stage="plan", cost_usd_micros=0)
    st.metrics_count_tool_call("succeeded")
    st.metrics_count_tool_call("refused")
    body = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text
    assert 'boss_model_calls_total{engine="native",ok="true"} 1' in body
    assert 'boss_model_calls_total{engine="native",ok="false"} 1' in body
    assert 'boss_model_tokens_total{direction="input",engine="native"} 10' in body
    assert 'boss_model_cost_usd_micros_total{engine="native"} 250' in body
    assert 'boss_tool_calls_total{outcome="refused"} 1' in body
    assert "# TYPE boss_tool_calls_total counter" in body
    assert httpx.get(f"http://127.0.0.1:{port}/other", timeout=5).status_code == 404


class FakeSpan:
    def __init__(self, name, start_time=None):
        self.name, self.start_time, self.attrs, self.ended, self.status = name, start_time, {}, False, None

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def set_status(self, s):
        self.status = s

    def end(self, end_time=None):
        self.ended = True


class FakeTracer:
    def __init__(self):
        self.spans: list[FakeSpan] = []

    def start_span(self, name, start_time=None):
        span = FakeSpan(name, start_time)
        self.spans.append(span)
        return span

    @contextlib.contextmanager
    def start_as_current_span(self, name):
        span = FakeSpan(name)
        self.spans.append(span)
        yield span
        span.end()


def test_a_model_call_is_a_genai_chat_span():
    tracer = FakeTracer()
    st._STATE["tracer"] = tracer
    st.record_model_span(engine_name="anthropic", model="claude-x",
                         usage={"prompt_tokens": 7, "completion_tokens": 2}, latency_ms=1500,
                         ok=True, error=None, stage="implement", cost_usd_micros=42)
    span = tracer.spans[-1]
    assert span.name == "chat claude-x" and span.ended
    assert span.attrs["gen_ai.operation.name"] == "chat"
    assert span.attrs["gen_ai.system"] == "anthropic"
    assert span.attrs["gen_ai.request.model"] == "claude-x"
    assert span.attrs["gen_ai.usage.input_tokens"] == 7
    assert span.attrs["gen_ai.usage.output_tokens"] == 2
    assert span.attrs["boss.stage"] == "implement"


def test_the_recording_seam_feeds_the_span(monkeypatch):
    from boss_engine.services import llm_telemetry

    seen = []
    monkeypatch.setattr(st, "record_model_span", lambda **kw: seen.append(kw))

    class FakeConn:
        @contextlib.contextmanager
        def begin_nested(self):
            yield

        def execute(self, *a, **k):
            return SimpleNamespace(scalar=lambda: None, first=lambda: None,
                                   mappings=lambda: SimpleNamespace(first=lambda: None))

    monkeypatch.setattr(llm_telemetry, "_signed_by", lambda c, ctx, stage: (None, None))
    monkeypatch.setattr(llm_telemetry, "_ledger_columns", lambda c, ctx, act_id, row_id: {})
    row = llm_telemetry.record_llm_call(engine_name="native", usage={"model": "m",
                                        "prompt_tokens": 4}, cost_usd_micros=9, latency_ms=12,
                                        connection=FakeConn(), capacity=False, stage="plan")
    assert row and seen and seen[0]["engine_name"] == "native"
    assert seen[0]["cost_usd_micros"] == 9 and seen[0]["stage"] == "plan"


def test_a_tool_call_is_an_execute_tool_span():
    tracer = FakeTracer()
    st._STATE["tracer"] = tracer
    with st.tool_span("composio", "GMAIL_SEND_EMAIL", run_id="r1", agent="forge"):
        pass
    span = tracer.spans[-1]
    assert span.name == "execute_tool GMAIL_SEND_EMAIL"
    assert span.attrs == {"gen_ai.operation.name": "execute_tool",
                          "gen_ai.tool.name": "GMAIL_SEND_EMAIL", "boss.mcp.server": "composio",
                          "boss.run_id": "r1", "gen_ai.agent.name": "forge"}


def test_traceparent_forwarding_reads_the_current_span():
    otel = pytest.importorskip("opentelemetry")
    from opentelemetry import context, trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    from boss_engine.services import mcp_headers_policy as hp

    assert otel
    assert hp.current_traceparent() is None  # no span: nothing invented
    ctx = SpanContext(trace_id=0x0AF7651916CD43DD8448EB211C80319C, span_id=0xB7AD6B7169203331,
                      is_remote=False, trace_flags=TraceFlags(1))
    token = context.attach(trace.set_span_in_context(NonRecordingSpan(ctx)))
    try:
        got = hp.call_headers({"forward": ["traceparent"]})
    finally:
        context.detach(token)
    assert got == {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
