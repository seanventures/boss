"""AI SRE overwatch observes only: honest empty, receipted dedupe, never remediates."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from boss_engine.services.ai_sre_overwatch import (
    OVERWATCH_EFFECT_KIND,
    safe_tick,
    tick,
)


class FakeEngine:
    def begin(self):
        return _FakeTx()


class _FakeTx:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeReceipts:
    def __init__(self):
        self.store = {}
        self.created = []
        self.succeeded = []

    def get_by_dedupe_key(self, connection, org_id, dedupe_key):
        return self.store.get((org_id, dedupe_key))

    def create_or_reuse(self, connection, org_id, workspace_id, project_id, dedupe_key,
                        effect_kind, effect_scope, payload, created_by_job_id=None,
                        data_boundary=None):
        key = (org_id, dedupe_key)
        existing = self.store.get(key)
        if existing:
            return existing, False
        receipt = {
            "id": f"rcpt_{len(self.store) + 1}",
            "dedupe_key": dedupe_key,
            "effect_kind": effect_kind,
            "status": "in_progress",
            "result": None,
        }
        self.store[key] = receipt
        self.created.append(receipt)
        return receipt, True

    def mark_succeeded(self, connection, receipt_id, provider_ref, result):
        self.succeeded.append((receipt_id, result))
        for receipt in self.store.values():
            if receipt["id"] == receipt_id:
                receipt["status"] = "succeeded"
                receipt["result"] = result
                receipt["provider_ref"] = provider_ref


class FakeReliability:
    def __init__(self, sources=None, incidents=None, policy=None):
        self.sources = list(sources or [])
        self.incidents = list(incidents or [])
        self.policy = policy or {
            "auto_remediate": True,
            "min_confidence": 0.1,
            "max_blast_radius": 99,
            "max_auto_severity": "critical",
        }
        self.scan_calls = []
        self.diagnose_calls = []
        self.remediate_calls = []

    def list_sources(self, connection, project_id, *, enabled_only=False):
        return list(self.sources)

    def scan(self, connection, project_id, source_spec, window, *, source_id=None,
             persist=False, **kwargs):
        self.scan_calls.append({
            "project_id": project_id,
            "source_spec": source_spec,
            "window": window,
            "source_id": source_id,
            "persist": persist,
        })
        inc = {
            "id": "inc_critical_1",
            "title": "error_surge: boom",
            "service": "billing",
            "severity": "critical",
            "status": "open",
            "confidence": None,
        }
        if persist:
            self.incidents = [inc]
            return {"signals": [{"kind": "error_surge", "key": "boom", "count": 12}],
                    "signal_count": 1, "incidents": [inc]}
        return {"signals": [], "signal_count": 0, "incidents": []}

    def list_incidents(self, connection, project_id, *, status=None, limit=50):
        rows = list(self.incidents)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows[:limit]

    def get_incident(self, connection, project_id, incident_id):
        for row in self.incidents:
            if row["id"] == incident_id:
                return {**row, "evidence": list(row.get("evidence") or []),
                        "remediations": list(row.get("remediations") or [])}
        return None

    def diagnose(self, connection, project_id, incident_id):
        self.diagnose_calls.append(incident_id)
        return {
            "incident_id": incident_id,
            "service": "billing",
            "hypothesis": {
                "basis": "context",
                "root_cause": "unknown-neighbor",
                "confidence": 0.4,
                "cause_key": "billing-db",
            },
            "hypotheses": [],
            "known_causes": [],
        }

    def remediate(self, connection, project_id, incident_id, **kwargs):
        self.remediate_calls.append(incident_id)
        raise AssertionError("overwatch must never auto-remediate")

    def get_policy(self, connection, project_id):
        return dict(self.policy)


class FakeOncall:
    def __init__(self):
        self.pages = []

    def page_reliability_incident(self, connection, project_id, incident_id, **kwargs):
        assert kwargs.get("force_live") is False
        page = {
            "page": {"id": f"ocpg_{incident_id}", "reliability_incident_id": incident_id},
            "simulated": True,
            "reused": False,
            "notify": {"simulated": True, "reason": "no_credentials"},
        }
        self.pages.append((incident_id, kwargs))
        return page


def _tick(reliability, receipts, oncall=None, **kwargs):
    now = kwargs.pop("now", datetime(2026, 8, 15, 18, 30, tzinfo=timezone.utc))
    return tick(
        FakeEngine(),
        SimpleNamespace(default_project_id="boss-dogfood"),
        project_id=kwargs.pop("project_id", "boss-dogfood"),
        actor=kwargs.pop("actor", "metatron"),
        now=now,
        reliability=reliability,
        oncall=oncall if oncall is not None else FakeOncall(),
        receipts=receipts,
        scope={"org_id": "org_test", "workspace_id": "ws_test"},
        **kwargs,
    )


def test_no_sources_returns_honest_empty():
    reliability = FakeReliability(sources=[])
    receipts = FakeReceipts()
    result = _tick(reliability, receipts)

    assert result["status"] == "empty"
    assert result["reason"] == "no_registered_sources"
    assert result["observe_only"] is True
    assert result["sources"] == []
    assert result["scanned"] == []
    assert result["incidents"] == []
    assert result["diagnosed"] == []
    assert result["paged"] == []
    assert result["remediated"] == []
    assert result["auto_applied_decisions"] == []
    assert result["merged_prs"] == []
    assert reliability.scan_calls == []
    assert reliability.diagnose_calls == []
    assert reliability.remediate_calls == []
    assert result["dedupe_key"] == "ai_sre:overwatch:boss-dogfood:202608151830"
    assert result["receipt_id"]
    assert result["reused"] is False


def test_persist_scan_and_receipt_dedupe():
    reliability = FakeReliability(sources=[{
        "id": "tsrc_mem",
        "kind": "memory",
        "name": "dogfood-logs",
        "config": {"logs": []},
        "enabled": True,
    }])
    receipts = FakeReceipts()
    oncall = FakeOncall()

    first = _tick(reliability, receipts, oncall=oncall)
    assert first["status"] == "observed"
    assert first["reused"] is False
    assert first["receipt_id"]
    assert first["dedupe_key"] == "ai_sre:overwatch:boss-dogfood:202608151830"
    assert first["observe_only"] is True
    assert reliability.scan_calls and reliability.scan_calls[0]["persist"] is True
    assert len(receipts.created) == 1
    assert receipts.created[0]["effect_kind"] == OVERWATCH_EFFECT_KIND
    assert first["remediated"] == []

    second = _tick(reliability, receipts, oncall=oncall)
    assert second["reused"] is True
    assert second["receipt_id"] == first["receipt_id"]
    assert second["dedupe_key"] == first["dedupe_key"]
    assert len(receipts.created) == 1
    assert len(reliability.scan_calls) == 1


def test_never_auto_remediates_even_when_policy_allows():
    reliability = FakeReliability(
        sources=[{
            "id": "tsrc_mem",
            "kind": "memory",
            "name": "dogfood-logs",
            "config": {},
            "enabled": True,
        }],
        policy={"auto_remediate": True, "min_confidence": 0.0,
                "max_blast_radius": 99, "max_auto_severity": "critical"},
    )
    receipts = FakeReceipts()
    oncall = FakeOncall()
    result = _tick(reliability, receipts, oncall=oncall)

    assert result["observe_only"] is True
    assert result["remediated"] == []
    assert result["auto_applied_decisions"] == []
    assert result["merged_prs"] == []
    assert reliability.remediate_calls == []
    assert reliability.diagnose_calls == ["inc_critical_1"]
    assert oncall.pages and oncall.pages[0][0] == "inc_critical_1"


class _BoomEngine:
    def begin(self):
        raise RuntimeError("postgres is down")


def test_safe_tick_never_raises():
    result = safe_tick(
        _BoomEngine(),
        SimpleNamespace(default_project_id="boss-dogfood"),
        project_id="boss-dogfood",
        actor="metatron",
    )
    assert result["status"] == "failed"
    assert result["observe_only"] is True
    assert result["remediated"] == []
    assert "RuntimeError" in (result.get("error") or "")
