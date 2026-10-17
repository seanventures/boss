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
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import telemetry_mining as tm
from .core import make_id, now_utc
from .change_correlation import ChangeCorrelationService
from .investigation import InvestigationEngine
from .knowledge_graph import KnowledgeGraphService
from .learning_loop import LearningLoopService
from .telemetry_source import (
    Alert,
    InMemorySource,
    LogEvent,
    LogExportSource,
    MetricSeries,
    Span,
    TelemetrySource,
    TimeWindow,
)

_CRITICAL_KINDS = ("error_surge", "new_template")
_TUNED_MIN_COUNT = 9        # a raise-listed signature needs 3x the default floor to fire again
_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}

# incident feedback verdict → learning-example outcome + the incident status it drives
_VERDICT = {
    "real": ("confirmed", "triaged"),
    "false_alarm": ("dismissed", "dismissed"),
    "fix_good": ("fix_good", "resolved"),
    "fix_bad": ("fix_bad", "remediating"),
}


def build_source(spec: dict[str, Any]) -> TelemetrySource:
    """Construct a source from a request spec. All connectors are read-only; auth is indirect
    (`bearer_env` = env-var NAME, resolved at call time — creds never in the spec)."""
    kind = (spec or {}).get("kind", "memory")
    if kind == "memory":
        logs = [LogEvent(**e) if isinstance(e, dict) else e for e in spec.get("logs", [])]
        metrics_raw = spec.get("metrics") or []
        metrics = []
        for m in metrics_raw:
            if isinstance(m, MetricSeries):
                metrics.append(m)
            elif isinstance(m, dict):
                from .telemetry_source import MetricPoint
                points = [MetricPoint(**p) if isinstance(p, dict) else p
                          for p in m.get("points", [])]
                metrics.append(MetricSeries(name=m.get("name", ""), points=points,
                                            labels=dict(m.get("labels") or {})))
        traces = [Span(**t) if isinstance(t, dict) else t for t in (spec.get("traces") or [])]
        alerts = [Alert(**a) if isinstance(a, dict) else a for a in (spec.get("alerts") or [])]
        return InMemorySource(logs=logs, metrics=metrics, traces=traces, alerts=alerts)
    if kind == "log_export":
        return LogExportSource(spec["path"])
    if kind == "prometheus":                      # RB-9 native connectors
        from .telemetry_connectors import PrometheusSource
        return PrometheusSource(spec["base_url"], bearer_env=spec.get("bearer_env", ""),
                                step_seconds=int(spec.get("step_seconds", 60)))
    if kind == "loki":
        from .telemetry_connectors import LokiSource
        return LokiSource(spec["base_url"], bearer_env=spec.get("bearer_env", ""))
    if kind == "grafana":
        from .telemetry_connectors import GrafanaSource
        return GrafanaSource(spec["base_url"], prometheus_uid=spec.get("prometheus_uid", ""),
                             loki_uid=spec.get("loki_uid", ""),
                             bearer_env=spec.get("bearer_env", ""),
                             step_seconds=int(spec.get("step_seconds", 60)))
    if kind == "splunk":
        from .telemetry_connectors import SplunkSource
        return SplunkSource(spec["base_url"], bearer_env=spec.get("bearer_env", ""),
                            index=spec.get("index", ""))
    if kind == "datadog":
        from .telemetry_connectors import DatadogSource
        return DatadogSource(spec.get("base_url", "https://api.datadoghq.com"),
                             api_key_env=spec.get("api_key_env", "DD_API_KEY"),
                             app_key_env=spec.get("app_key_env", "DD_APP_KEY"))
    if kind == "cloudwatch":
        from .telemetry_connectors import CloudWatchSource
        return CloudWatchSource(spec["log_group"], region=spec.get("region", "us-east-1"))
    if kind == "pagerduty":
        from .telemetry_connectors import PagerDutySource
        return PagerDutySource(spec.get("base_url", "https://api.pagerduty.com"),
                               token_env=spec.get("token_env", "PAGERDUTY_TOKEN"))
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
        self.changes = ChangeCorrelationService(settings)
        self.engine = InvestigationEngine()

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

        # RB-12 auto-tuning: signatures repeatedly dismissed as false alarms need a higher bar
        raised = {t["signature"]
                  for t in self.learning.suggest_tuning(connection, project_id, "sre")["raise_threshold"]}
        svc_by_tmpl = _service_by_template(events)
        touched: dict[str, dict] = {}
        suppressed: list[dict[str, Any]] = []
        for sig in report["signals"]:
            if sig["key"] in raised and int(sig.get("count") or 0) < _TUNED_MIN_COUNT:
                suppressed.append({"kind": sig["kind"], "key": sig["key"],
                                   "count": sig.get("count"), "reason": "tuned_threshold"})
                continue
            service = svc_by_tmpl.get(sig["key"], "") if sig["kind"] in ("new_template", "template_spike") else ""
            inc = self._upsert_incident(connection, project_id, sig, service)
            self._insert_signal(connection, project_id, source_id, sig, service, inc["id"])
            touched[inc["id"]] = inc
        report["incidents"] = list(touched.values())
        report["suppressed"] = suppressed          # transparency: tuned-out, not silently dropped
        return report

    def _upsert_incident(self, connection: Connection, project_id: str, sig: dict[str, Any],
                         service: str, *, severity: Optional[str] = None) -> dict[str, Any]:
        key = _dedupe_key(service, sig["kind"], sig["key"])
        title = f"{sig['kind']}: {sig['key']}"[:200]
        severity = severity or _severity_for(sig["kind"])
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

    def investigation(self, connection: Connection, project_id: str,
                      incident_id: str) -> Optional[dict[str, Any]]:
        """RB-20 — the consolidated investigation bundle for agent consumption (the MCP surface):
        the incident + signals + evidence + remediations + the graph neighborhood + what the
        autonomy policy would do right now. One call, full context."""
        inc = self.get_incident(connection, project_id, incident_id)
        if not inc:
            return None
        service = inc.get("service") or ""
        return {
            "incident": {k: inc[k] for k in ("id", "title", "service", "severity", "status",
                                             "confidence", "signal_count", "first_seen",
                                             "last_seen")},
            "signals": inc["signals"], "evidence": inc["evidence"],
            "remediations": inc["remediations"],
            "graph": (self.kg.subgraph(connection, project_id, "service", service, depth=2)
                      if service else {"root": None, "nodes": [], "edges": []}),
            "decision": self.decide_action(connection, project_id, incident_id),
        }

    # --- diagnose (RB-5 — RCA over the knowledge graph) ----------------------
    def diagnose(self, connection: Connection, project_id: str,
                 incident_id: str) -> Optional[dict[str, Any]]:
        """Autonomous RCA (RB-16): the investigation engine enumerates candidates from memory
        (`known_causes`), change-correlation, and the graph `subgraph()`, scores each by confidence
        (corroborated against the incident's own signals), and ranks them. The winner + full ranking +
        context are persisted as incident evidence."""
        inc = connection.execute(
            text("SELECT id, service, title, first_seen FROM incidents WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": incident_id},
        ).mappings().first()
        if not inc:
            return None
        service = inc["service"] or ""
        context = self.kg.subgraph(connection, project_id, "service", service, depth=2) if service else \
            {"root": None, "nodes": [], "edges": []}
        known = self.kg.known_causes(connection, project_id, service) if service else []
        signals = [
            {"signature": r["signature"], "service": r["service"],
             "sample": (r["sample"] or {}).get("sample", "")}
            for r in connection.execute(
                text("SELECT signature, service, sample FROM anomaly_signals WHERE incident_id = :id"),
                {"id": incident_id}).mappings().all()
        ]
        # RB-17: recent changes near onset are candidate triggers (the causal signal)
        changes = self.changes.correlate(connection, project_id, inc["first_seen"])

        result = self.engine.investigate(service=service, signals=signals,
                                         known_causes=known, subgraph=context, changes=changes)
        hypothesis = result["winner"]
        hypotheses = result["hypotheses"]

        # RB-12: confirmed prior examples ride along as few-shot context (for the LLM stage)
        few_shot = self.learning.suggest_tuning(connection, project_id, "sre")["few_shot_candidates"]
        self._add_evidence(connection, incident_id, "context", ref=service,
                           payload={"nodes": context["nodes"], "edges": context["edges"],
                                    "known_causes": known, "changes": changes,
                                    "hypotheses": hypotheses, "few_shot": few_shot})
        self._add_evidence(connection, incident_id, "rca_hypothesis", ref=hypothesis["cause_key"],
                           payload=hypothesis)
        connection.execute(
            text("UPDATE incidents SET status = 'diagnosing', confidence = :c, last_seen = :now "
                 "WHERE id = :id"),
            {"c": hypothesis["confidence"], "now": now_utc(), "id": incident_id},
        )
        return {"incident_id": incident_id, "service": service, "hypothesis": hypothesis,
                "hypotheses": hypotheses, "changes": changes,
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

        # AN APP'S INCIDENT IS FIXED AS THAT APP'S NEXT CHANGE. The incident an app's own monitor
        # raised knows the build lineage that owns the app (incident_response.deploy_context);
        # the fix continues it — the app's branch, its repository, its identity — instead of a
        # fresh task against the deployment's default repository under a NEW app identity
        # derived from the incident's title, which would have built "Fix: …" as another app.
        ctx = connection.execute(
            text("SELECT finalize_ctx FROM incidents WHERE id = :id"), {"id": incident_id},
        ).scalar() or {}
        if isinstance(ctx, str):
            ctx = json.loads(ctx)
        lineage = str(((ctx.get("deploy") or {}).get("extends_run_id")) or "").strip()
        if lineage:
            from .code_task_service import start_followup_task

            started = start_followup_task(
                connection, parent_run_id=lineage, jobs=jobs, actor=actor,
                title=f"Fix: {inc['title']}"[:150],
                instructions=_bug_frame(dict(inc), hypothesis),
                acceptance_criteria=[
                    "A regression test reproduces the incident and FAILS before the fix, passes "
                    "after.",
                    "The fix eliminates the incident's error signature at the root cause.",
                    "No unrelated behavior changes.",
                ],
                finalize_extra={"source": "reliability", "incident_id": incident_id})
            run_id = started["run_id"]
            return self._record_remediation(connection, project_id, incident_id, run_id)
        # A telemetry incident (no app behind it) keeps the old route: the given repository,
        # else the deployment's default one.
        owner = owner or getattr(self.settings, "target_github_owner", "") or ""
        repo = repo or getattr(self.settings, "target_github_repo", "") or ""

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
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            finalize_ctx={"source": "reliability", "incident_id": incident_id, "service": inc["service"]},
            jobs=jobs, risk_tier=3, open_pr=True,        # bug rigor + open the gated PR
        )
        return self._record_remediation(connection, project_id, incident_id, run_id)

    def _record_remediation(self, connection: Connection, project_id: str, incident_id: str,
                            run_id: str) -> dict[str, Any]:
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

    # --- autonomy policy & tiers (RB-19) --------------------------------------
    def get_policy(self, connection: Connection, project_id: str) -> dict[str, Any]:
        row = connection.execute(
            text("SELECT project_id, auto_remediate, min_confidence, max_blast_radius, "
                 "max_auto_severity, updated_at FROM reliability_policies WHERE project_id = :p"),
            {"p": project_id},
        ).mappings().first()
        if row:
            return dict(row)
        return {"project_id": project_id, "auto_remediate": False, "min_confidence": 0.8,
                "max_blast_radius": 5, "max_auto_severity": "critical", "updated_at": None}

    def set_policy(self, connection: Connection, project_id: str, *, auto_remediate: bool,
                   min_confidence: float, max_blast_radius: int,
                   max_auto_severity: str = "critical") -> dict[str, Any]:
        connection.execute(
            text(
                """
                INSERT INTO reliability_policies
                    (project_id, auto_remediate, min_confidence, max_blast_radius,
                     max_auto_severity, updated_at)
                VALUES (:p, :ar, :mc, :mbr, :mas, :now)
                ON CONFLICT (project_id) DO UPDATE SET
                    auto_remediate = EXCLUDED.auto_remediate,
                    min_confidence = EXCLUDED.min_confidence,
                    max_blast_radius = EXCLUDED.max_blast_radius,
                    max_auto_severity = EXCLUDED.max_auto_severity,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {"p": project_id, "ar": auto_remediate, "mc": min_confidence, "mbr": max_blast_radius,
             "mas": max_auto_severity, "now": now_utc()},
        )
        return self.get_policy(connection, project_id)

    def decide_action(self, connection: Connection, project_id: str, incident_id: str) -> dict[str, Any]:
        """The governed autonomy spectrum: pick the tier by confidence × blast radius × severity.
        Tier-0 auto_remediate (auto-launch the still-gated fix PR) · Tier-1 gated_pr (operator
        launches) · Tier-2 handoff (notify only). Every path stays receipted + fail-closed."""
        inc = connection.execute(
            text("SELECT id, service, severity, confidence FROM incidents "
                 "WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": incident_id},
        ).mappings().first()
        if not inc:
            return {"tier": 2, "action": "handoff", "reason": "incident not found"}
        policy = self.get_policy(connection, project_id)
        blast = len(self.kg.subgraph(connection, project_id, "service", inc["service"] or "",
                                     depth=1)["nodes"]) if inc["service"] else 0
        conf = float(inc["confidence"] or 0.0)
        if not policy["auto_remediate"]:
            tier, action, reason = 1, "gated_pr", "auto_remediate disabled by policy"
        elif conf < float(policy["min_confidence"]):
            tier, action, reason = 1, "gated_pr", \
                f"confidence {conf} below policy floor {policy['min_confidence']}"
        elif blast > int(policy["max_blast_radius"]):
            tier, action, reason = 2, "handoff", \
                f"blast radius {blast} exceeds cap {policy['max_blast_radius']} — human judgment"
        elif _SEV_RANK.get(inc["severity"], 1) > _SEV_RANK.get(policy["max_auto_severity"], 2):
            tier, action, reason = 2, "handoff", "severity above the autonomous cap"
        else:
            tier, action, reason = 0, "auto_remediate", \
                f"confidence {conf} ≥ {policy['min_confidence']}, blast {blast} within bounds"
        return {"tier": tier, "action": action, "reason": reason,
                "confidence": conf, "blast_radius": blast, "policy": policy}

    # --- telemetry sources (RB-9/RB-10 — registered connections) --------------
    def register_source(self, connection: Connection, project_id: str, kind: str, name: str, *,
                        config: Optional[dict] = None, secret_ref: str = "",
                        enabled: bool = True) -> dict[str, Any]:
        """Register/enable a telemetry source. `config` is connector config (base_url, uids,
        bearer_env…) — never credentials; auth stays indirect via env-var names / SecretRefs."""
        row = connection.execute(
            text(
                """
                INSERT INTO telemetry_sources (id, project_id, kind, name, config, secret_ref, enabled)
                VALUES (:id, :p, :kind, :name, CAST(:config AS JSONB), :sr, :enabled)
                ON CONFLICT (project_id, kind, name) DO UPDATE SET
                    config = EXCLUDED.config, secret_ref = EXCLUDED.secret_ref,
                    enabled = EXCLUDED.enabled
                RETURNING id, project_id, kind, name, config, enabled, created_at
                """
            ),
            {"id": make_id("tsrc"), "p": project_id, "kind": kind, "name": name,
             "config": json.dumps(config or {}, default=str), "sr": secret_ref, "enabled": enabled},
        ).mappings().one()
        return dict(row)

    def list_sources(self, connection: Connection, project_id: str, *,
                     enabled_only: bool = False) -> list[dict[str, Any]]:
        clause = " AND enabled" if enabled_only else ""
        rows = connection.execute(
            text(f"SELECT id, kind, name, config, enabled, created_at FROM telemetry_sources "
                 f"WHERE project_id = :p{clause} ORDER BY created_at"),
            {"p": project_id},
        ).mappings().all()
        return [dict(r) for r in rows]

    def get_source(self, connection: Connection, project_id: str,
                   source_id: str) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, kind, name, config, enabled, created_at, secret_ref
                FROM telemetry_sources
                WHERE project_id = :p AND id = :id
                """
            ),
            {"p": project_id, "id": source_id},
        ).mappings().first()
        return dict(row) if row else None

    def query_telemetry(
        self,
        connection: Connection,
        project_id: str,
        *,
        kind: str,
        query: str = "",
        window: dict[str, float],
        source_id: Optional[str] = None,
        source_spec: Optional[dict[str, Any]] = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Direct telemetry read (metrics|logs|traces|alerts) through a connector.

        Distinct from ``scan`` (which mines signals): this is the raw connector query
        surface. Fail-closed on missing creds/config — never returns silent fake data.
        """
        from .telemetry_connectors import (
            ConnectorNotReady,
            ensure_connector_ready,
            run_source_query,
        )

        spec: dict[str, Any]
        resolved_source_id = source_id
        if source_id:
            row = self.get_source(connection, project_id, source_id)
            if not row:
                raise LookupError(f"telemetry source {source_id!r} not found")
            if not row.get("enabled", True):
                raise ConnectorNotReady(
                    f"telemetry source {source_id!r} is disabled",
                    status_code=422,
                )
            config = row.get("config") or {}
            if isinstance(config, str):
                config = json.loads(config)
            spec = {"kind": row["kind"], **dict(config)}
        elif source_spec:
            spec = dict(source_spec)
        else:
            raise ConnectorNotReady(
                "source_id or source is required",
                status_code=422,
            )

        ensure_connector_ready(spec)
        try:
            source = build_source(spec)
        except (ValueError, KeyError, TypeError) as error:
            raise ConnectorNotReady(str(error), status_code=422) from error

        win = TimeWindow(float(window["start"]), float(window["end"]))
        if win.end < win.start:
            raise ConnectorNotReady("window.end must be >= window.start", status_code=422)

        items = run_source_query(source, kind, query, win, limit=limit)
        return {
            "project_id": project_id,
            "kind": kind,
            "query": query,
            "source_id": resolved_source_id,
            "source_kind": getattr(source, "kind", spec.get("kind")),
            "window": {"start": win.start, "end": win.end},
            "count": len(items),
            "items": items,
        }

    # --- alert intake (RB-10 — the incident-response trigger) -----------------
    def verify_alert_signature(self, raw_body: bytes, signature: str) -> bool:
        """HMAC-sha256 over the raw body, hex; fail-closed when the secret is unset."""
        import hmac as _hmac
        from hashlib import sha256
        from hmac import compare_digest
        secret = (getattr(self.settings, "reliability_alert_secret", None) or "").strip()
        if not secret or not signature:
            return False
        digest = _hmac.new(secret.encode(), raw_body, sha256).hexdigest()
        candidate = signature.strip()
        if candidate.startswith("sha256="):
            candidate = candidate[len("sha256="):]
        return compare_digest(candidate, digest)

    @staticmethod
    def _parse_alert(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Normalize the common alert shapes into {title, service, severity, description}:
        Alertmanager/Grafana ({alerts:[{labels,annotations}…]}), PagerDuty ({event:{data:…}}),
        and a generic {title|alertname, service, severity, description}."""
        if isinstance(payload.get("alerts"), list) and payload["alerts"]:
            a = payload["alerts"][0]
            labels = a.get("labels") or {}
            ann = a.get("annotations") or {}
            return {"title": labels.get("alertname", "alert"),
                    "service": labels.get("service", labels.get("job", "")),
                    "severity": labels.get("severity", "warning"),
                    "description": ann.get("summary", ann.get("description", ""))}
        if isinstance(payload.get("event"), dict):                    # PagerDuty v3 webhook
            data = payload["event"].get("data") or {}
            svc = (data.get("service") or {})
            return {"title": data.get("title", "alert"),
                    "service": svc.get("summary", svc.get("name", "")),
                    "severity": {"P1": "critical", "P2": "critical"}.get(
                        (data.get("priority") or {}).get("summary", ""), "warning"),
                    "description": data.get("title", "")}
        title = payload.get("title") or payload.get("alertname")
        if title:
            return {"title": title, "service": payload.get("service", ""),
                    "severity": payload.get("severity", "warning"),
                    "description": payload.get("description", "")}
        return None

    def ingest_alert(self, connection: Connection, project_id: str, payload: dict[str, Any], *,
                     scan_window_minutes: int = 30, jobs=None) -> Optional[dict[str, Any]]:
        """Alert → incident → scoped scan of every enabled source → diagnose → **autonomy decision**
        (RB-19: with `jobs` and a Tier-0 policy verdict, the gated remediation PR auto-launches).
        The event-driven entry to the loop: an alert firing anywhere becomes an investigated
        incident here."""
        alert = self._parse_alert(payload)
        if not alert:
            return None
        severity = alert["severity"] if alert["severity"] in ("info", "warning", "critical") else "warning"
        sig = {"kind": "alert", "key": alert["title"], "count": 1, "score": None,
               "sample": alert["description"] or alert["title"]}
        inc = self._upsert_incident(connection, project_id, sig, alert["service"], severity=severity)
        self._insert_signal(connection, project_id, None, sig, alert["service"], inc["id"])

        now = now_utc().timestamp()
        window = {"start": now - scan_window_minutes * 60, "end": now}
        scans = []
        for src in self.list_sources(connection, project_id, enabled_only=True):
            try:
                rep = self.scan(connection, project_id, {"kind": src["kind"], **(src["config"] or {})},
                                window, source_id=src["id"], persist=True)
                scans.append({"source": src["name"], "signals": rep["signal_count"],
                              "incidents": [i["id"] for i in rep["incidents"]]})
            except Exception as exc:  # a broken source must not kill alert intake
                scans.append({"source": src["name"], "error": str(exc)[:200]})

        diagnosis = self.diagnose(connection, project_id, inc["id"])
        decision = self.decide_action(connection, project_id, inc["id"])
        remediation = None
        if decision["action"] == "auto_remediate" and jobs is not None:      # Tier-0, still gated
            remediation = self.remediate(connection, project_id, inc["id"], jobs=jobs)
        autonomy = {k: decision[k] for k in ("tier", "action", "reason")}
        # RB-20: investigation card to the oncall channel (always best-effort)
        from .oncall_notify import notify_incident
        notified = notify_incident(self.settings, inc["id"], alert,
                                   hypothesis=(diagnosis or {}).get("hypothesis"),
                                   autonomy=autonomy, remediation=remediation)
        return {"incident_id": inc["id"], "alert": alert, "scans": scans,
                "hypothesis": (diagnosis or {}).get("hypothesis"),
                "autonomy": autonomy, "remediation": remediation, "notified": notified,
                "status": "remediating" if remediation else "diagnosing"}

    # --- verify (RB-18 — production verification loop) ------------------------
    def verify(self, connection: Connection, project_id: str, incident_id: str,
               source_spec: dict[str, Any], window: dict[str, float], *,
               stability_minutes: float = 30.0, query: str = "",
               actor: str = "system") -> Optional[dict[str, Any]]:
        """Close the loop autonomously: re-watch the SAME signals that raised the incident.
        * signature still present  → **recurred**: reopen the incident (+ learning example, evidence).
        * signal clear             → start/continue a no-recurrence **stability window**; once it has
          elapsed with every check clear → **recovered**: auto-resolve. Until then → **stabilizing**.
        Verification bookkeeping lives in `incidents.finalize_ctx.verification` so repeated checks
        (manual or scheduled) converge on one durable state machine."""
        inc = connection.execute(
            text("SELECT id, service, status, finalize_ctx FROM incidents "
                 "WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": incident_id},
        ).mappings().first()
        if not inc:
            return None
        sigs = connection.execute(
            text("SELECT DISTINCT kind, signature FROM anomaly_signals WHERE incident_id = :id"),
            {"id": incident_id},
        ).all()

        source = build_source(source_spec)
        win = TimeWindow(window["start"], window["end"])
        events = source.query_logs(query, win)
        counts = Counter(tm.template_of(e.message) for e in events)

        recurred: list[str] = []
        for kind, signature in sigs:
            if kind in ("new_template", "template_spike"):
                if counts.get(signature, 0) >= 3:
                    recurred.append(signature)
            elif kind == "error_surge":
                if tm.error_surge_signal(events, baseline_error_rate=None) is not None:
                    recurred.append(signature)
            elif kind == "metric_spike":
                if any(s.key == signature
                       for s in tm.metric_signals(source.query_metrics(query, win))):
                    recurred.append(signature)

        ctx = dict(inc["finalize_ctx"] or {})
        ver = dict(ctx.get("verification") or {})
        now = now_utc()
        checks = int(ver.get("checks", 0)) + 1

        if recurred:
            # the fix did not hold — reopen and reset the stability window
            ver = {"state": "recurred", "checks": checks, "last_check": now.isoformat(),
                   "recurred_signatures": recurred}
            new_status = "open"
        else:
            first_clear = (datetime.fromisoformat(ver["first_clear"])
                           if ver.get("first_clear") else now)
            stable_at = first_clear + timedelta(minutes=stability_minutes)
            state = "recovered" if now >= stable_at else "stabilizing"
            ver = {"state": state, "checks": checks, "last_check": now.isoformat(),
                   "first_clear": first_clear.isoformat(), "stable_at": stable_at.isoformat()}
            new_status = "resolved" if state == "recovered" else inc["status"]

        ctx["verification"] = ver
        connection.execute(
            text("UPDATE incidents SET status = :s, finalize_ctx = CAST(:ctx AS JSONB), "
                 "last_seen = :now WHERE id = :id"),
            {"s": new_status, "ctx": json.dumps(ctx, default=str), "now": now, "id": incident_id},
        )
        self._add_evidence(connection, incident_id, "verification", ref=ver["state"], payload=ver)
        if ver["state"] in ("recurred", "recovered"):     # terminal outcomes feed the learning loop
            self.learning.record(
                connection, project_id, agent="sre", kind="verification", label=ver["state"],
                outcome=("recurred" if ver["state"] == "recurred" else "verified_recovered"),
                ref=incident_id, source=actor,
                payload={"service": inc["service"], "recurred_signatures": recurred, "checks": checks},
            )
        return {"incident_id": incident_id, "state": ver["state"], "status": new_status,
                "checks": checks, "recurred_signatures": recurred,
                "first_clear": ver.get("first_clear"), "stable_at": ver.get("stable_at")}

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
