"""official.opentelemetry: an OpenTelemetry Collector on the app's private network.

The app's SDK hook is one binding, OTEL_EXPORTER_OTLP_ENDPOINT (OTLP/HTTP on 4318; gRPC 4317 is
also open on the network). The collector batches traces, metrics and logs and, when
`config.exportTo` names an OTLP/HTTP endpoint, forwards them there.

verify  sends a real test span with the app's binding (over the app's network) and proves it
        ARRIVED: the collector's own `otelcol_receiver_accepted_spans` counter goes up.
"""
from __future__ import annotations

import json
import re
import secrets as _secrets
import time

import yaml

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import HookFailure, OfficialPlugin, Session, evidence, labels, service_name

IMAGE = ("otel/opentelemetry-collector-contrib:0.135.0"
         "@sha256:89107a3a8f4636a396927edf7025bb9614b8da2d92f4cc3f43109e8d115736e2")
CURL = "curlimages/curl:8.16.0@sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6"
CONFIG_PATH = "/etc/otelcol-contrib/config.yaml"


class OpenTelemetryPlugin(OfficialPlugin):
    directory = "opentelemetry"

    def image(self, ctx: HookContext) -> str:
        return ctx.state.get("image") or ctx.resource.config.get("image") or IMAGE

    def _validate(self, ctx: HookContext) -> list[str]:
        to = ctx.resource.config.get("exportTo")
        if to and not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._/-]*)?", to):
            return ["config.exportTo must be an http(s) OTLP/HTTP endpoint with no credentials in it"]
        return []

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        out = [PlannedChange(action="create", target=f"service:{service_name(ctx)}",
                             detail=f"{self.image(ctx).split('@')[0]}, OTLP 4317/4318 on the app network only"),
               PlannedChange(action="create", target="secret:OTEL_EXPORTER_OTLP_ENDPOINT", detail="vault reference")]
        if to := ctx.resource.config.get("exportTo"):
            out.append(PlannedChange(action="create", target="egress", detail=f"forward telemetry to {to}"))
        return out

    def collector_config(self, ctx: HookContext) -> str:
        exporters = {"debug": {"verbosity": "basic"}}
        if to := ctx.resource.config.get("exportTo"):
            exporters["otlphttp/upstream"] = {"endpoint": to}
        names = list(exporters)
        pipe = {"receivers": ["otlp"], "processors": ["batch"], "exporters": names}
        return yaml.safe_dump({
            "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": "0.0.0.0:4317"},
                                                 "http": {"endpoint": "0.0.0.0:4318"}}}},
            "processors": {"batch": {}},
            "exporters": exporters,
            "extensions": {"health_check": {"endpoint": "0.0.0.0:13133"}},
            "service": {
                "extensions": ["health_check"],
                "pipelines": {"traces": pipe, "metrics": pipe, "logs": pipe},
                "telemetry": {"metrics": {"readers": [{"pull": {"exporter": {"prometheus": {
                    "host": "0.0.0.0", "port": 8888}}}}]}},
            },
        }, sort_keys=False)

    def fragment(self, ctx: HookContext) -> dict:
        svc, lab = service_name(ctx), labels(ctx, self.plugin_id)
        cfg = f"{svc}-config"
        return {"services": {svc: {"image": self.image(ctx), "restart": "unless-stopped",
                                   "command": [f"--config={CONFIG_PATH}"],
                                   "configs": [{"source": cfg, "target": CONFIG_PATH}],
                                   "labels": lab}},
                "configs": {cfg: {"content": self.collector_config(ctx)}}}

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        svc = service_name(ctx)
        ref = ctx.state.get("endpointRef") or s.put("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://{svc}:4318").secretRef
        s.write_fragment(self.fragment(ctx))
        s.compose_up(svc, {}, wait=True, timeout=120)
        state = {**ctx.state, "endpointRef": ref, "service": svc, "image": self.image(ctx),
                 "fragment": f"compose/{ctx.resource.name}.yaml"}
        return ctx.result(bindings=self.bindings(ctx, {"OTEL_EXPORTER_OTLP_ENDPOINT": ref}), state=state,
                          artifacts=[state["fragment"]], evidence=[evidence("container", True, "healthcheck", f"{svc} running")])

    def do_configure(self, ctx: HookContext, s: Session) -> OperationResult:
        if not ctx.state.get("endpointRef"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        return ctx.result(bindings=self.bindings(ctx, {"OTEL_EXPORTER_OTLP_ENDPOINT": ctx.state["endpointRef"]}),
                          state=ctx.state, evidence=[evidence("binding", True, "config",
                                                              "OTEL_EXPORTER_OTLP_ENDPOINT is a vault reference")])

    def accepted_spans(self, ctx: HookContext, s: Session) -> int:
        proc = s.probe(CURL, ["-fsS", f"http://{service_name(ctx)}:8888/metrics"], check=False, what="reading collector metrics")
        total = 0
        for line in s.text(proc).splitlines():
            if line.startswith("otelcol_receiver_accepted_spans") and 'receiver="otlp"' in line:
                total += int(float(line.rsplit(" ", 1)[-1]))
        return total

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        if not ctx.state.get("endpointRef"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        endpoint = s.get(ctx.state["endpointRef"])
        trace, span = _secrets.token_hex(16), _secrets.token_hex(8)
        now = time.time_ns()
        body = json.dumps({"resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "boss-verify"}}]},
            "scopeSpans": [{"scope": {"name": "boss.verify"}, "spans": [{
                "traceId": trace, "spanId": span, "name": "boss-verify", "kind": 1,
                "startTimeUnixNano": str(now - 1_000_000), "endTimeUnixNano": str(now)}]}]}]})
        before = self.accepted_spans(ctx, s)
        code, deadline = "", time.monotonic() + 45
        while time.monotonic() < deadline:
            proc = s.probe(CURL, ["-c", 'curl -sS -o /dev/null -w "%{http_code}" -H "Content-Type: application/json" '
                                  '--data-binary @- "$OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces"'],
                           entrypoint="sh", env={"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}, stdin=body.encode(),
                           check=False, what="sending a test span")
            code = s.text(proc).strip()
            if code == "200":
                break
            time.sleep(1.5)
        after = before
        for _ in range(20):
            after = self.accepted_spans(ctx, s)
            if after > before:
                break
            time.sleep(1)
        ev = [evidence("otlp_http_accepts_span", code == "200", "connection", f"POST /v1/traces with the app binding: HTTP {code}"),
              evidence("span_arrived", after > before, "connection",
                       f"otelcol_receiver_accepted_spans {before} -> {after}")]
        if code == "200" and after > before:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "verify_failed",
                                                                        "message": "the test span did not arrive at the collector"}])

    def do_observe(self, ctx: HookContext, s: Session) -> OperationResult:
        return ctx.result(observed={"healthy": True, "acceptedSpans": self.accepted_spans(ctx, s)},
                          evidence=[evidence("collector_metrics", True, "observe", "collector self-metrics read")])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        removed = s.compose_destroy(service_name(ctx))
        if ref := ctx.state.get("endpointRef"):
            ctx.secrets.delete(ref)
            removed.append("secret:OTEL_EXPORTER_OTLP_ENDPOINT")
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed))], state={})


Plugin = OpenTelemetryPlugin
