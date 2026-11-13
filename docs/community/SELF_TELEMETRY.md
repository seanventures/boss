# BOSS's own telemetry

BOSS can send its own traces, errors and metrics to the tools your platform team already runs, such as Datadog, Grafana, Langfuse or Sentry. This is separate from the telemetry of the apps BOSS deploys.

Every part is off by default, and BOSS works the same with all of them off. Turn on only the parts you want by setting them in `.env` and restarting the API and worker.

## Traces (OpenTelemetry)

```bash
# .env
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer%20<token>   # if your collector needs it
OTEL_SERVICE_NAME=boss                                        # optional
```

BOSS exports over OTLP/HTTP and sends traces to `<endpoint>/v1/traces`. You get:

- the API's requests, database queries and outgoing HTTP calls, through the FastAPI, SQLAlchemy and httpx instrumentation;
- a GenAI `chat` span for every model call, with the model, the input and output token counts, the stage and the list-price cost (`boss.cost_usd_micros`);
- a GenAI `execute_tool` span for every MCP tool call, with the server, the tool, the run and the agent.

To send these spans to an MCP server as well, turn on `traceparent` under the server's **Upstream headers** in its drawer. See [MCP](MCP.md).

The shipped image does not include the OpenTelemetry SDK. Install it into your image with `pip install -r apps/api/requirements-observability.txt`. If the endpoint is set but the SDK is missing, BOSS writes one log line saying so and runs without traces.

## Errors (Sentry)

```bash
# .env
SENTRY_DSN=https://<key>@<org>.ingest.sentry.io/<project>
BOSS_SENTRY_TRACES_SAMPLE_RATE=0.0
```

BOSS starts Sentry with `send_default_pii` off. Before an event leaves BOSS, every string in it goes through BOSS's secret and PII redaction. `Authorization`, cookie and API-key headers are dropped. Sentry needs `sentry-sdk`, which `requirements-observability.txt` includes.

## Metrics (Prometheus)

```bash
# .env
BOSS_METRICS_PORT=9464
BOSS_METRICS_HOST=0.0.0.0     # the default, 127.0.0.1, is reachable only inside the container
```

The API and the worker each serve `GET /metrics` on that port. This listener is separate from the API and needs no BOSS sign-in, so expose it only on a network your scraper uses. Metrics need no extra package. They are:

- `boss_model_calls_total{engine,ok}`
- `boss_model_tokens_total{engine,direction}`
- `boss_model_cost_usd_micros_total{engine}`
- `boss_tool_calls_total{outcome}`
- `boss_process_start_time_seconds`

## Not yet

You cannot yet send one organisation's agent-run spans to a destination of its own. All of BOSS's traces go to the one endpoint above.
