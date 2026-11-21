"""Availability, SLO, incidents and alerts count only samples the target's CURRENT probe took.

THE INCIDENT (2026-11-21). After the probe fix (#261) both Northgate gateway targets answered 200,
yet the header read "Degraded — SLO breached: 0.42% up, target 99.5%": the 24 h window still held
the samples the OLD probe wrote (GET / → 404, "no connector for target_kind"). That was a probe
bug, not downtime.

Now each sample records the probe that took it (probe_signature), the target records the probe
it is measured with now and since when, and every reader reads `counted_health_samples`.
"""
from __future__ import annotations

import pathlib
import re
from datetime import timedelta

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project, sample, target
from boss_engine.services import deployment_health as dh
from boss_engine.services.project_experience.health import target_states

HEALTHZ = "GET http://192.0.2.202:8080/healthz expect 200"
READYZ = "GET http://192.0.2.202:8080/readyz expect 200"


def _ok(sig, ok=True):
    return {"ok": ok, "status_code": 200 if ok else 503, "latency_ms": 12,
            "error": None if ok else "expected 200, got 503", "probe_signature": sig}


@pytest.fixture(scope="module")
def seeded():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        now = c.execute(text("SELECT now()")).scalar()
        ids = {}
        for name in ("wrongprobe", "genuine", "changed"):
            tid = target(c, pid, f"{pid}_{name}", name)
            ids[name] = tid
            # 24 h of what the OLD probe wrote: failures, no signature.
            for i in range(1, 200):
                sample(c, pid, tid, now - timedelta(minutes=7 * i), False, None,
                       "expected 2xx-3xx, got 404")
    return eng, pid, ids


def _t(c, tid):
    return dict(c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                          {"t": tid}).mappings().first())


def test_before_any_current_probe_the_old_history_still_counts(seeded):
    eng, _pid, ids = seeded
    with eng.connect() as c:
        st = target_states(c, [ids["wrongprobe"]])[ids["wrongprobe"]]
    assert st["health"] == "down" and st["measured_since"] is None


def test_the_wrong_probe_s_history_is_excluded_once_the_current_probe_answers(seeded):
    eng, _pid, ids = seeded
    tid = ids["wrongprobe"]
    with eng.begin() as c:
        now = c.execute(text("SELECT now()")).scalar()
        dh.record_sample(c, _t(c, tid), _ok(HEALTHZ), now)
    with eng.connect() as c:
        st = target_states(c, [tid])[tid]
        view = {t["target_id"]: t for t in dh.health_view(c, _pid_of(c, tid))["targets"]}[tid]
        counted = c.execute(text("SELECT count(*) FROM counted_health_samples "
                                 "WHERE deployment_target_id = :t"), {"t": tid}).scalar()
    assert counted == 1
    assert st["health"] == "healthy", st
    assert st["uptime_pct"] == 100.0
    assert "SLO breached" not in (st["health_reason"] or "")
    assert st["measured_since"] and st["measured_words"].startswith("Measured since ")
    assert view["status"] == "up" and view["uptime_24h"] == 100.0
    assert view["measured_with"] == HEALTHZ and view["incidents"] == []


def test_genuine_failures_of_the_same_probe_still_count(seeded):
    eng, _pid, ids = seeded
    tid = ids["genuine"]
    with eng.begin() as c:
        now = c.execute(text("SELECT now()")).scalar()
        dh.record_sample(c, _t(c, tid), _ok(HEALTHZ), now - timedelta(minutes=8))
        for i in range(3):
            dh.record_sample(c, _t(c, tid), _ok(HEALTHZ, ok=False), now - timedelta(minutes=2 - i))
    with eng.connect() as c:
        st = target_states(c, [tid])[tid]
    assert st["health"] == "down"
    assert st["uptime_pct"] == 25.0


def test_a_changed_probe_starts_a_new_record(seeded):
    eng, _pid, ids = seeded
    tid = ids["changed"]
    with eng.begin() as c:
        now = c.execute(text("SELECT now()")).scalar()
        for i in range(4):
            dh.record_sample(c, _t(c, tid), _ok(HEALTHZ, ok=False), now - timedelta(minutes=20 - i))
        first_since = _t(c, tid)["probe_signature_since"]
        dh.record_sample(c, _t(c, tid), _ok(READYZ), now)
    with eng.connect() as c:
        st = target_states(c, [tid])[tid]
        t = _t(c, tid)
    assert t["probe_signature"] == READYZ and t["probe_signature_since"] > first_since
    assert st["health"] == "healthy" and st["uptime_pct"] == 100.0


def test_a_throwing_probe_counts_against_the_current_probe(monkeypatch):
    def boom(self, target, settings):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(dh.StackHealthProbe, "observe", boom)
    out = dh.probe({"target_kind": "stack", "probe_signature": HEALTHZ}, None)
    assert out["ok"] is False and out["probe_signature"] == HEALTHZ


def _pid_of(c, tid):
    return c.execute(text("SELECT project_id FROM deployment_targets WHERE id = :t"),
                     {"t": tid}).scalar()


class TestTheSignature:
    def test_it_names_what_was_asked_in_a_stable_order(self):
        a = dh.probe_signature([{"url": "http://h/readyz", "expected": 200},
                                {"socket": "h:22"}, {"skipped": "x", "check": {}}])
        b = dh.probe_signature([{"socket": "h:22"}, {"url": "http://h/readyz", "expected": 200}])
        assert a == b == "GET http://h/readyz expect 200 | TCP h:22"

    def test_nothing_asked_is_no_signature(self):
        assert dh.probe_signature([{"skipped": "x"}]) is None


def test_every_reader_of_health_samples_reads_the_counted_view():
    """Only writers, the due-check and retention may touch the raw table; anything that turns
    samples into availability, SLO, incidents, alerts or a verdict reads the counted view."""
    root = pathlib.Path(dh.__file__).resolve().parents[1]
    allowed = {"services/deployment_health.py", "services/telemetry_retention.py"}
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        for m in re.finditer(r"FROM\s+deployment_health_samples", path.read_text(errors="ignore")):
            offenders.append(rel)
    assert not offenders, offenders
