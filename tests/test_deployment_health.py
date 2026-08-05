"""Health metrics for deployed apps.

`deployment_runs` records the ACT of deploying — a run that finished "succeeded" stays
succeeded forever, so the console could report "deployed to staging" about an app that fell
over an hour ago. These are the functions that answer the other question, and they are pure
with the clock passed IN so every case here is exact rather than timing-dependent.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_deployment_health.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "engine")

from boss_engine.services import deployment_health as h  # noqa: E402

T0 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
_failures: list[str] = []


def check(name: str, got, want, note: str = "") -> None:
    if got == want:
        print(f"  PASS  {name}")
    else:
        _failures.append(f"{name}: got {got!r}, want {want!r} {note}")
        print(f"  FAIL  {name} — got {got!r}, want {want!r} {note}")


def samples(*spec, step_s: int = 60):
    """Newest-first samples from (ok, latency_ms) pairs given oldest-first."""
    out = []
    for i, (ok, ms) in enumerate(spec):
        out.append({"checked_at": T0 + timedelta(seconds=i * step_s), "ok": ok,
                    "latency_ms": ms, "status_code": 200 if ok else 500,
                    "error": None if ok else "boom"})
    return list(reversed(out))


def test_uptime_distinguishes_no_data_from_zero() -> None:
    s = samples((True, 10), (False, 10), (True, 10), (True, 10))
    check("uptime counts successes", h.uptime_pct(s, since=T0), 75.0)
    check("NO DATA is None, not 0%", h.uptime_pct([], since=T0), None,
          "an unprobed target has not had an outage")
    check("window excludes older samples",
          h.uptime_pct(s, since=T0 + timedelta(seconds=120)), 100.0)


def test_latency_ignores_failed_probes() -> None:
    s = samples((True, 100), (False, 9999), (True, 300))
    check("a timeout's duration is not a latency", h.latency_stats(s)["avg_ms"], 200)
    check("no successful probes -> None", h.latency_stats(samples((False, 5)))["avg_ms"], None)
    # Nearest-rank on a zero-indexed series: round(0.95 * (n-1)) = 94 -> the 95th value.
    p95 = h.latency_stats(samples(*[(True, v) for v in range(1, 101)]))["p95_ms"]
    check("p95 picks the 95th percentile", p95, 95)
    check("p95 is not just the max", p95 != 100, True)


def test_verdict_states() -> None:
    check("no samples is UNKNOWN, not down", h.verdict([]), "unknown",
          "never having looked differs from having looked and found it broken")
    check("all good is up", h.verdict(samples((True, 10), (True, 10))), "up")
    check("one failure is degraded, not down",
          h.verdict(samples((True, 10), (True, 10), (False, 10))), "degraded",
          "one hiccup called an outage trains people to ignore the status")
    check("three consecutive failures is down",
          h.verdict(samples((True, 10), (False, 1), (False, 1), (False, 1))), "down")
    check("slow but reachable is degraded",
          h.verdict(samples((True, 10), (True, 9000))), "degraded")
    check("recovery leaves down",
          h.verdict(samples((False, 1), (False, 1), (False, 1), (True, 10))), "degraded")


def test_status_since_reports_the_whole_run_not_the_last_probe() -> None:
    s = samples((False, 1), (False, 1), (False, 1), (True, 10), (True, 10), (True, 10))
    began = h.status_since(s)
    check("up-since is when the run of health STARTED", began, T0 + timedelta(seconds=180),
          "otherwise a week-long uptime reports as one probe interval")
    check("no samples -> None", h.status_since([]), None)


def test_incidents_open_on_down_and_close_on_recovery() -> None:
    s = samples((True, 10), (False, 1), (False, 1), (False, 1), (True, 10), (True, 10))
    inc = h.incidents(s)
    check("one incident recorded", len(inc), 1)
    check("opens when it became DOWN, not on the first blip",
          inc[0]["started_at"], T0 + timedelta(seconds=180))
    check("closes on recovery", inc[0]["ended_at"], T0 + timedelta(seconds=240))
    check("duration is real seconds", inc[0]["duration_seconds"], 60)

    check("a single failure is not an incident",
          h.incidents(samples((True, 10), (False, 1), (True, 10))), [])

    ongoing = h.incidents(samples((True, 10), (False, 1), (False, 1), (False, 1)))
    check("an unrecovered outage is ONGOING", ongoing[0]["ended_at"], None)
    check("ongoing has no duration", ongoing[0]["duration_seconds"], None)

    flap = h.incidents(samples((False, 1), (False, 1), (False, 1), (True, 10),
                               (False, 1), (False, 1), (False, 1)))
    check("flapping produces two incidents", len(flap), 2)


def test_probe_never_raises_and_unreachable_is_data() -> None:
    """A probe that throws is a failed probe, not a crashed monitor."""
    class Boom:
        kind = "ssh"

        def observe(self, target, settings):
            raise RuntimeError("host on fire")

    import boss_engine.services.deployment_connectors as dc
    original = dc._CONNECTORS.get("ssh")
    dc._CONNECTORS["ssh"] = Boom()
    try:
        got = h.probe({"target_kind": "ssh"}, object())
        check("a throwing observe records a failure", got["ok"], False)
        check("the error is captured", "host on fire" in (got["error"] or ""), True)
    finally:
        if original is not None:
            dc._CONNECTORS["ssh"] = original

    unknown = h.probe({"target_kind": "does-not-exist"}, object())
    check("an unknown connector is a failed probe, not an exception", unknown["ok"], False)


if __name__ == "__main__":
    for fn in (test_uptime_distinguishes_no_data_from_zero,
               test_latency_ignores_failed_probes,
               test_verdict_states,
               test_status_since_reports_the_whole_run_not_the_last_probe,
               test_incidents_open_on_down_and_close_on_recovery,
               test_probe_never_raises_and_unreachable_is_data):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all deployment-health checks passed")
