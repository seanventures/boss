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

- every agent run's own spans: the run, each step, model call, tool call, subagent, approval wait and handoff, exported with the ids BOSS stores, so the run is the parent of everything it did.

BOSS keeps these run spans itself whether or not export is on: they are what the console's run trace reads (`GET /v1/runs/{run_id}/trace`). Each span's full input and output is stored after secret and PII redaction, for 14 days, or 180 days when the run met an evaluator, feedback or a dataset. With `S3_BUCKET` set, payloads older than a week move to that store.

To send these spans to an MCP server as well, turn on `traceparent` under the server's **Upstream headers** in its drawer. See [MCP](MCP.md). Inside an agent run's tool call, the `traceparent` names that tool call's span.

The shipped image includes the OpenTelemetry SDK and the OTLP/HTTP exporter, so setting the endpoint is enough. The FastAPI, SQLAlchemy and httpx instrumentation is not in the image: install it with `pip install -r apps/api/requirements-observability.txt`.

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

The API and the worker each serve `GET /metrics` on that port. This listener is separate from the API and needs no BOSS sign-in, so expose it only on a network your scraper uses. Metrics need no extra package.

With Mission Control installed (the bundled Prometheus), `install.sh` sets these two for you and the bundled Prometheus scrapes the API and the worker on the compose network. The port is not published on the host.

The metrics are:

- `boss_model_calls_total{engine,ok}`
- `boss_model_tokens_total{engine,direction}`
- `boss_model_cost_usd_micros_total{engine}`
- `boss_tool_calls_total{outcome}`
- `boss_agent_runs_total{agent,status}`
- `boss_agent_run_duration_ms_total{agent}`
- `boss_process_start_time_seconds`

## Not yet

You cannot yet send one organisation's agent-run spans to a destination of its own. All of BOSS's traces go to the one endpoint above.
