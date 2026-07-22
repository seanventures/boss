"""RB-3/RB-4/RB-5 — the reliability pipeline: scan → signals → named incidents → diagnose.

The system-level surface of the AI SRE agent. Three moves, all here:

* **scan** (RB-3/4) — pull a window from a `TelemetrySource`, distill it with `telemetry_mining.mine()`
  (model-free), and — when persisting — write the signals and **cluster recurring ones into named
  incidents** by a stable `dedupe_key`, so the board shows issues, not noise.
* **incidents** (RB-4) — durable, deduped, each attributed to a `service` that is also a knowledge-graph node.
* **diagnose** (RB-5) — RCA over the **knowledge graph**: pull the failing service's `subgraph()` (context)
  and its `known_causes()` (memory). A recurring cause is recognized instantly and with high confidence; a
  novel one falls back to a context-based suspect. Deterministic today; the parallel-hypothesis LLM engine
  (RB-16) slots in behind the same call.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import telemetry_mining as tm
from .core import make_id, now_utc
from .knowledge_graph import KnowledgeGraphService
from .learning_loop import LearningLoopService
from .telemetry_source import InMemorySource, LogEvent, LogExportSource, TelemetrySource, TimeWindow

_CRITICAL_KINDS = ("error_surge", "new_template")

# incident feedback verdict → learning-example outcome + the incident status it drives
_VERDICT = {
    "real": ("confirmed", "triaged"),
    "false_alarm": ("dismissed", "dismissed"),
    "fix_good": ("fix_good", "resolved"),
    "fix_bad": ("fix_bad", "remediating"),
}


def build_source(spec: dict[str, Any]) -> TelemetrySource:
    """Construct a source from a request spec. Inline adapters today (memory/log_export); a persisted
    telemetry_source resolves its connector the same way once live connectors land (RB-9)."""
    kind = (spec or {}).get("kind", "memory")
    if kind == "memory":
        logs = [LogEvent(**e) if isinstance(e, dict) else e for e in spec.get("logs", [])]
        return InMemorySource(logs=logs)
    if kind == "log_export":
        return LogExportSource(spec["path"])
    raise ValueError(f"unsupported source kind {kind!r}")


def _dedupe_key(service: str, kind: str, signature: str) -> str:
    return hashlib.sha1(f"{service}|{kind}|{signature}".encode()).hexdigest()[:16]


def _severity_for(kind: str) -> str:
    return "critical" if kind in _CRITICAL_KINDS else "warning"


def _bug_frame(inc: dict[str, Any], hypothesis: dict[str, Any]) -> str:
    """Frame the incident as a BUG for the governed pipeline (same preamble as the Plane bug path):
    reproduce → root-cause → fix → regression test, with the RCA hypothesis as the starting lead."""
    return (
        "BUG REPORT — reproduce the issue, find the root cause, fix it, and add a regression test that "
        "FAILS before your fix and passes after.\n\n"
        f"Incident: {inc.get('title', '')}\n"
        f"Service: {inc.get('service', '') or 'unknown'}\n"
        f"Suspected root cause: {hypothesis.get('root_cause', 'unknown')} "
        f"(basis={hypothesis.get('basis', 'n/a')}, confidence={hypothesis.get('confidence', 'n/a')}).\n"
        f"Rationale: {hypothesis.get('rationale', '')}\n"
    )


def _service_by_template(events: list[LogEvent]) -> dict[str, str]:
    """Most-common service per template — so a clustered incident is attributed to a real service."""
    m: dict[str, Counter] = defaultdict(Counter)
    for e in events:
        m[tm.template_of(e.message)][e.service or ""] += 1
    return {t: c.most_common(1)[0][0] for t, c in m.items()}


class ReliabilityService:
    def __init__(self, settings=None):
        self.settings = settings
        self.kg = KnowledgeGraphService(settings)
        self.learning = LearningLoopService(settings)

    # --- scan (RB-3 read-only / RB-4 persist+cluster) ------------------------
    def scan(self, connection: Connection, project_id: str, source_spec: dict[str, Any],
             window: dict[str, float], *, source_id: Optional[str] = None,
             baseline: Optional[dict[str, float]] = None, query: str = "",
             persist: bool = False) -> dict[str, Any]:
        """Distill a window into the Insight report; when `persist`, cluster signals into incidents."""
        source = build_source(source_spec)
        win = TimeWindow(window["start"], window["end"])
        events = source.query_logs(query, win)
        metrics = source.query_metrics(query, win)
        report = tm.mine(events, baseline=baseline, metrics=metrics)
        report["incidents"] = []
        if not persist:
            return report                          # RB-3: read-only Insight, nothing written

        svc_by_tmpl = _service_by_template(events)
        touched: dict[str, dict] = {}
        for sig in report["signals"]:
            service = svc_by_tmpl.get(sig["key"], "") if sig["kind"] in ("new_template", "template_spike") else ""
            inc = self._upsert_incident(connection, project_id, sig, service)
            self._insert_signal(connection, project_id, source_id, sig, service, inc["id"])
            touched[inc["id"]] = inc
        report["incidents"] = list(touched.values())
        return report

    def _upsert_incident(self, connection: Connection, project_id: str, sig: dict[str, Any],
                         service: str) -> dict[str, Any]:
        key = _dedupe_key(service, sig["kind"], sig["key"])
        title = f"{sig['kind']}: {sig['key']}"[:200]
        severity = _severity_for(sig["kind"])
        count = int(sig.get("count") or 0)
        row = connection.execute(
            text(
                """
                INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity,
                                       status, signal_count, first_seen, last_seen)
                VALUES (:id, :p, :k, :title, :svc, :sev, 'open', :count, :now, :now)
                ON CONFLICT (project_id, dedupe_key) DO UPDATE SET
                    signal_count = incidents.signal_count + EXCLUDED.signal_count,
                    last_seen = EXCLUDED.last_seen,
                    -- an incident only escalates in severity, never silently downgrades
                    severity = CASE WHEN EXCLUDED.severity = 'critical' THEN 'critical'
                                    ELSE incidents.severity END
                RETURNING id, dedupe_key, title, service, severity, status, confidence, signal_count
                """
            ),
            {"id": make_id("inc"), "p": project_id, "k": key, "title": title, "svc": service,
             "sev": severity, "count": count, "now": now_utc()},
        ).mappings().one()
        if service:                                # keep the service on the graph
            self.kg.upsert_node(connection, project_id, "service", service, source="scan")
        return dict(row)

    def _insert_signal(self, connection: Connection, project_id: str, source_id: Optional[str],
                       sig: dict[str, Any], service: str, incident_id: str) -> None:
        import json
        connection.execute(
            text(
                """
                INSERT INTO anomaly_signals (id, project_id, source_id, incident_id, kind, signature,
                                             service, score, count, sample, ts)
                VALUES (:id, :p, :src, :inc, :kind, :sig, :svc, :score, :count, CAST(:sample AS JSONB), :now)
                """
            ),
            {"id": make_id("sig"), "p": project_id, "src": source_id, "inc": incident_id,
             "kind": sig["kind"], "sig": sig["key"], "svc": service, "score": sig.get("score"),
             "count": int(sig.get("count") or 0),
             "sample": json.dumps({"sample": sig.get("sample", ""), "meta": sig.get("meta", {})},
                                  default=str),
             "now": now_utc()},
        )

    # --- incidents (RB-4) ----------------------------------------------------
    def list_incidents(self, connection: Connection, project_id: str, *, status: Optional[str] = None,
                       limit: int = 50) -> list[dict[str, Any]]:
        clause = " AND status = :status" if status else ""
        params: dict[str, Any] = {"p": project_id, "limit": limit}
        if status:
            params["status"] = status
        rows = connection.execute(
            text(
                f"""
                SELECT id, dedupe_key, title, service, severity, status, confidence, signal_count,
                       first_seen, last_seen
                FROM incidents WHERE project_id = :p{clause}
                ORDER BY (severity = 'critical') DESC, last_seen DESC LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [dict(r) for r in rows]

    def get_incident(self, connection: Connection, project_id: str,
                     incident_id: str) -> Optional[dict[str, Any]]:
        inc = connection.execute(
            text("SELECT * FROM incidents WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": incident_id},
        ).mappings().first()
        if not inc:
            return None
        signals = connection.execute(
            text("SELECT kind, signature, service, score, count, sample, ts FROM anomaly_signals "
                 "WHERE incident_id = :id ORDER BY ts DESC LIMIT 100"),
            {"id": incident_id},
        ).mappings().all()
        evidence = connection.execute(
            text("SELECT kind, ref, payload, created_at FROM incident_evidence "
                 "WHERE incident_id = :id ORDER BY created_at"),
            {"id": incident_id},
        ).mappings().all()
        remediations = connection.execute(
            text("SELECT id, code_task_run_id, status, pull_request_url, created_at FROM remediations "
                 "WHERE incident_id = :id ORDER BY created_at DESC"),
            {"id": incident_id},
        ).mappings().all()
        out = dict(inc)
        out["signals"] = [dict(s) for s in signals]
        out["evidence"] = [dict(e) for e in evidence]
        out["remediations"] = [dict(r) for r in remediations]
        return out

    # --- diagnose (RB-5 — RCA over the knowledge graph) ----------------------
    def diagnose(self, connection: Connection, project_id: str,
                 incident_id: str) -> Optional[dict[str, Any]]:
        """Propose a root cause using the graph: context from `subgraph()`, memory from `known_causes()`.
        A recurring cause is recognized instantly (high confidence); a novel one falls back to a
        context-based suspect. The hypothesis + context are persisted as incident evidence."""
        inc = connection.execute(
            text("SELECT id, service, title FROM incidents WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": incident_id},
        ).mappings().first()
        if not inc:
            return None
        service = inc["service"] or ""
        context = self.kg.subgraph(connection, project_id, "service", service, depth=2) if service else \
            {"root": None, "nodes": [], "edges": []}
        known = self.kg.known_causes(connection, project_id, service) if service else []

        if known:
            top = known[0]
            hypothesis = {
                "root_cause": top["name"], "cause_kind": top["kind"], "cause_key": top["node_key"],
                "confidence": round(float(top["confidence"]), 3), "basis": "memory",
                "rationale": f"Recurring cause for service '{service}' — matched a prior confirmed root cause.",
            }
        else:
            suspects = [n for n in context["nodes"] if n["node_key"] != service]
            suspect = suspects[0] if suspects else None
            hypothesis = {
                "root_cause": (suspect["name"] if suspect else "unknown"),
                "cause_kind": (suspect["kind"] if suspect else "unknown"),
                "cause_key": (suspect["node_key"] if suspect else ""),
                "confidence": 0.4 if suspect else 0.1, "basis": "context",
                "rationale": ("No prior confirmed cause; nearest suspect from the dependency graph."
                              if suspect else "No graph context yet — connect topology / walkers (RB-15)."),
            }

        self._add_evidence(connection, incident_id, "context", ref=service,
                           payload={"nodes": context["nodes"], "edges": context["edges"],
                                    "known_causes": known})
        self._add_evidence(connection, incident_id, "rca_hypothesis", ref=hypothesis["cause_key"],
                           payload=hypothesis)
        connection.execute(
            text("UPDATE incidents SET status = 'diagnosing', confidence = :c, last_seen = :now "
                 "WHERE id = :id"),
            {"c": hypothesis["confidence"], "now": now_utc(), "id": incident_id},
        )
        return {"incident_id": incident_id, "service": service, "hypothesis": hypothesis,
                "context": context, "known_causes": known}

    # --- remediate (RB-7 — diagnosed incident → governed fix PR) -------------
    def remediate(self, connection: Connection, project_id: str, incident_id: str, *, jobs,
                  owner: Optional[str] = None, repo: Optional[str] = None,
                  base_branch: str = "main", actor: str = "system") -> Optional[dict[str, Any]]:
        """Turn a diagnosed incident into a **bug-framed governed code task** → gated PR. The fix runs
        through the same design→tests→implement→review→gate pipeline as any code task — this is the wedge
        (we ship the tested, governed fix, not just a plan)."""
        inc = connection.execute(
            text("SELECT id, title, service FROM incidents WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": incident_id},
        ).mappings().first()
        if not inc:
            return None
        hyp = connection.execute(
            text("SELECT payload FROM incident_evidence WHERE incident_id = :id AND kind = 'rca_hypothesis' "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"id": incident_id},
        ).mappings().first()
        hypothesis = (hyp["payload"] if hyp else None) or {}

        from .code_task_service import start_code_task
        run_id = start_code_task(
            connection, project_id=project_id, task_id=None,
            title=f"Fix: {inc['title']}"[:200],
            instructions=_bug_frame(dict(inc), hypothesis),
            acceptance_criteria=[
                "A regression test reproduces the incident and FAILS before the fix, passes after.",
                "The fix eliminates the incident's error signature at the root cause.",
                "No unrelated behavior changes.",
            ],
            test_commands=[],
            owner=owner or getattr(self.settings, "target_github_owner", "") or "",
            repo=repo or getattr(self.settings, "target_github_repo", "") or "",
            base_branch=base_branch,
            finalize_ctx={"source": "reliability", "incident_id": incident_id, "service": inc["service"]},
            jobs=jobs, risk_tier=3, open_pr=True,        # bug rigor + open the gated PR
        )
        rem_id = make_id("rem")
        connection.execute(
            text("INSERT INTO remediations (id, project_id, incident_id, code_task_run_id, status, "
                 "created_at, updated_at) VALUES (:id, :p, :inc, :run, 'queued', :now, :now)"),
            {"id": rem_id, "p": project_id, "inc": incident_id, "run": run_id, "now": now_utc()},
        )
        connection.execute(
            text("UPDATE incidents SET status = 'remediating', last_seen = :now WHERE id = :id"),
            {"now": now_utc(), "id": incident_id},
        )
        self._add_evidence(connection, incident_id, "remediation", ref=run_id,
                           payload={"code_task_run_id": run_id, "remediation_id": rem_id})
        return {"remediation_id": rem_id, "incident_id": incident_id,
                "code_task_run_id": run_id, "status": "queued"}

    # --- feedback → learn (RB-6/RB-8) ----------------------------------------
    def feedback(self, connection: Connection, project_id: str, incident_id: str, verdict: str, *,
                 cause: Optional[dict] = None, actor: str = "system") -> Optional[dict[str, Any]]:
        """Capture a human verdict on an incident into the LearningLoop, drive the incident status, and —
        on a confirmed cause — write it back to the knowledge graph so the next investigation recalls it.
        `verdict`: real | false_alarm | fix_good | fix_bad. Optional `cause` = {kind,key,name,confidence,evidence}."""
        if verdict not in _VERDICT:
            raise ValueError(f"unknown verdict {verdict!r}")
        inc = connection.execute(
            text("SELECT id, service FROM incidents WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": incident_id},
        ).mappings().first()
        if not inc:
            return None
        service = inc["service"] or ""
        signatures = [r[0] for r in connection.execute(
            text("SELECT DISTINCT signature FROM anomaly_signals WHERE incident_id = :id"),
            {"id": incident_id},
        ).all()]
        outcome, new_status = _VERDICT[verdict]

        example_id = self.learning.record(
            connection, project_id, agent="sre", kind="incident_feedback", label=verdict,
            outcome=outcome, ref=incident_id, source=actor,
            payload={"service": service, "signatures": signatures, "cause": cause or {}},
        )
        # a confirmed real cause is the learning write-back into the graph
        if verdict == "real" and cause and service:
            self.kg.record_root_cause(
                connection, project_id, service,
                cause.get("kind", "root_cause"), cause["key"], cause.get("name", cause["key"]),
                confidence=float(cause.get("confidence", 0.8)), evidence=cause.get("evidence"),
            )
        connection.execute(
            text("UPDATE incidents SET status = :s, last_seen = :now WHERE id = :id"),
            {"s": new_status, "now": now_utc(), "id": incident_id},
        )
        return {"incident_id": incident_id, "verdict": verdict, "outcome": outcome,
                "status": new_status, "learning_example_id": example_id,
                "tuning": self.learning.suggest_tuning(connection, project_id, "sre")}

    def _add_evidence(self, connection: Connection, incident_id: str, kind: str, *,
                      ref: str = "", payload: Optional[dict] = None) -> None:
        import json
        connection.execute(
            text("INSERT INTO incident_evidence (id, incident_id, kind, ref, payload, created_at) "
                 "VALUES (:id, :inc, :kind, :ref, CAST(:payload AS JSONB), :now)"),
            {"id": make_id("ev"), "inc": incident_id, "kind": kind, "ref": ref,
             # graph context carries datetime fields (node first_seen/last_seen) — default=str is safe for evidence
             "payload": json.dumps(payload or {}, default=str), "now": now_utc()},
        )
