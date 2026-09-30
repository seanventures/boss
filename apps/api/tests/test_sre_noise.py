"""AIOps noise-reduction view + NL ask plan.

Seams:
  GET  /v1/ai-sre/noise?project_id=
  POST /v1/ai-sre/ask
  boss_engine.services.sre_noise.noise_report

Never invent a 95% reduction. Never execute live PromQL.
"""
from __future__ import annotations

from apps.api.app.db import begin
from apps.api.app.services.reliability import ReliabilityService
from boss_engine.services.sre_noise import noise_report

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService()


def _errors(service: str, n: int, tag: str = "err"):
    return [
        {"ts": i, "message": f"{tag} boom {i}", "service": service, "level": "error"}
        for i in range(n)
    ]


def test_empty_project_noise_report_is_honest_zeros(engine):
    with begin(engine) as connection:
        report = noise_report(connection, PID)
    assert report["project_id"] == PID
    assert report["public_ga"] is False
    assert report["raw_signal_count"] == 0
    assert report["clustered_count"] == 0
    assert report["suppressed_duplicates"] == 0
    assert report["top_signatures"] == []
    ratio = report["reduction"]["ratio"]
    assert ratio in (None, 0, 0.0)
    assert report["reduction"].get("percent") not in (95, 95.0, "95")
    assert "0.95" not in str(report)


def test_empty_project_http_noise_is_honest_zeros(client):
    r = client.get("/v1/ai-sre/noise", params={"project_id": PID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["raw_signal_count"] == 0
    assert body["clustered_count"] == 0
    assert body["suppressed_duplicates"] == 0
    assert body["public_ga"] is False
    assert body["reduction"]["ratio"] in (None, 0, 0.0)
    assert "95" not in str(body.get("reduction"))


def test_clustering_reduces_displayed_items_vs_raw_signals(engine):
    spec = {"kind": "memory", "logs": _errors("noise-svc", 15, tag="duperr")}
    window = {"start": 0, "end": 10_000}
    with begin(engine) as connection:
        first = RS.scan(connection, PID, spec, window, persist=True)
        assert first["incidents"]
        second = RS.scan(connection, PID, spec, window, persist=True)
        assert any(i["id"] == first["incidents"][0]["id"] for i in second["incidents"])
        report = noise_report(connection, PID)

    assert report["raw_signal_count"] >= 2
    assert report["clustered_count"] >= 1
    assert report["clustered_count"] < report["raw_signal_count"]
    assert report["suppressed_duplicates"] == (
        report["raw_signal_count"] - report["clustered_count"]
    )
    expected_ratio = report["suppressed_duplicates"] / report["raw_signal_count"]
    assert report["reduction"]["ratio"] == expected_ratio
    assert report["reduction"]["ratio"] != 0.95 or expected_ratio == 0.95
    assert report["top_signatures"]
    assert report["public_ga"] is False


def test_http_noise_clusters_and_http_ask_unknown(client):
    scan = client.post(
        "/v1/reliability/scan",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "persist": True,
            "source": {"kind": "memory", "logs": _errors("http-noise", 14)},
            "window": {"start": 0, "end": 10_000},
        },
    )
    assert scan.status_code == 200, scan.text
    client.post(
        "/v1/reliability/scan",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "persist": True,
            "source": {"kind": "memory", "logs": _errors("http-noise", 14)},
            "window": {"start": 0, "end": 10_000},
        },
    )

    noise = client.get("/v1/ai-sre/noise", params={"project_id": PID})
    assert noise.status_code == 200, noise.text
    body = noise.json()
    assert body["clustered_count"] < body["raw_signal_count"]
    assert body["top_signatures"]

    ask = client.post(
        "/v1/ai-sre/ask",
        json={"project_id": PID, "question": "what should I have for lunch"},
    )
    assert ask.status_code == 200, ask.text
    plan = ask.json()
    assert plan["status"] == "unknown"
    assert plan["executed"] is False
    assert plan.get("suggested_query") in (None, "")
    assert "sum(rate(" not in str(plan)
