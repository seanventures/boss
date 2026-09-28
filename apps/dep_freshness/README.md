# Dependency Freshness Report

A tiny standalone service that renders a report of tracked dependencies, their
pinned and latest known versions, and a derived freshness status. Reads from a
static, checked-in snapshot (`data/dependencies.json`) — no live registry
calls at request time.

The checked-in versions are example data. The report does not establish what
the latest package versions are today.

## Run

```
pip install -r requirements.txt
HOST=0.0.0.0 PORT=8000 python -m app.main
```

## Endpoints

- `GET /` — HTML freshness report.
- `GET /healthz` — liveness check.
- `GET /metrics` — Prometheus text exposition (0.0.4) for the BOSS OTel collector.

## Tests

```
python3 -m unittest discover -v
```
