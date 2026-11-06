"""The first five signal adapters: BOSS's own stores, read into the one signal shape (BOSSAPP-903).

Each adapter is a PULL function `fn(connection, since, project_id=None) -> [observation]`
(project_id None = the whole install) registered with
`signals.adapter(name)`. It reads its store and returns record_signal keyword arguments; it
never writes, and it never decides what a problem means — that is triage's job (the
Chief-of-Staff, BOSSAPP-904). What it does decide is IDENTITY:

* the FINGERPRINT — what makes two observations the same problem (the same alert rule on the
  same app; the same step failing the same way in the same workflow; the same doctor finding);
* the SOURCE_REF — what makes one observation distinct from a re-read of the same row (an
  alert's id AND its fire count, so a re-fire is a new occurrence and a re-sweep is not).

The stores and why each is a signal (design §3.3 gap 1, §5):

  app_alerts           alert.firing          firing app alerts, BOSS's own rules, pushed app
                                             signals (incident_signals) and external sources
  workflow_failures    workflow.step_failed  failed/deadlettered workflow steps
                       build.failed          failed code-task builds
  system_doctor        doctor.<kind>         open system_findings (install-wide)
  shepherd             shepherd.escalation   open shepherd escalations (a run that needs a call)
  stale_prs            pr.stale              a BOSS PR whose merge question has waited days

Adding a source (CI check_run, OSV, cost anomalies, KG drift ...) is one more function here, or
anywhere that imports `signals.adapter`, with a test against its real table.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .signals import adapter

#: Every adapter reads one project when the sweep names one, else the whole install.
_SCOPE = "AND (CAST(:p AS TEXT) IS NULL OR {t}project_id = :p)"
#: A merge question unanswered this long is a stalled handoff (design §5 class 13).
STALE_PR_DAYS = 3
_NOISE = re.compile(r"(0x[0-9a-f]+|[0-9a-f-]{36}|[0-9a-f]{8,}|\d+)", re.I)
_ID = re.compile(r"\b[a-z]{2,6}_[0-9a-zA-Z]{6,}\b")


def error_class(message: Any) -> str:
    """The stable shape of an error: its first line with ids, hex and numbers folded away, so the
    same failure on two runs fingerprints the same and a different failure does not."""
    first = str(message or "").strip().splitlines()[0] if str(message or "").strip() else ""
    folded = _NOISE.sub("#", _ID.sub("<id>", first.lower()))
    return re.sub(r"\s+", " ", folded)[:160] or "unknown error"


def _severity_from(labels: Mapping[str, Any], urgency: str) -> str:
    said = str((labels or {}).get("severity") or "").lower()
    return {"critical": "critical", "page": "critical", "sev1": "critical", "p1": "critical",
            "high": "high", "error": "high", "sev2": "high", "p2": "high", "sev3": "medium",
            "warning": "medium", "warn": "medium", "info": "info", "low": "low"}.get(
        said, "high" if urgency == "high" else "medium")


@adapter("app_alerts")
def app_alerts(connection: Connection, since: Any,
               project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT a.id, a.project_id, a.fingerprint, a.alertname, a.labels, a.annotations, "
        "a.urgency, a.fire_count, a.value, a.threshold, a.unit, a.source_kind, a.subject_id, "
        "COALESCE(a.fired_at, a.active_at) AS fired_at, t.app_name "
        "FROM app_alerts a LEFT JOIN deployment_targets t ON t.id = a.target_id "
        "WHERE a.state = 'firing' AND COALESCE(a.last_eval_at, a.fired_at, a.active_at) > :s "
        f"{_SCOPE.format(t='a.')} ORDER BY a.active_at LIMIT 500"),
        {"s": since, "p": project_id}).mappings().all()
    for r in rows:
        labels = r["labels"] or {}
        app = r["app_name"] or labels.get("app") or labels.get("service") or r["subject_id"]
        summary = (r["annotations"] or {}).get("summary")
        yield {
            "project_id": r["project_id"], "source": "alert", "kind": "alert.firing",
            "title": summary or f"{r['alertname']} firing on {app or 'an app'}",
            "fingerprint_parts": ("alert", r["fingerprint"]),
            "subject_kind": "app", "subject_id": r["subject_id"], "app": app,
            "severity": _severity_from(labels, str(r["urgency"] or "")),
            "evidence": [{"kind": "alert", "ref": r["id"],
                          "note": f"{r['source_kind']} · value {r['value']} vs {r['threshold']}"
                                  f"{r['unit'] or ''}"}],
            "source_ref": f"app_alert:{r['id']}#{int(r['fire_count'] or 1)}",
            "observed_at": r["fired_at"], "dedupe_window_seconds": 3600,
        }


@adapter("workflow_failures")
def workflow_failures(connection: Connection, since: Any,
                      project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    steps = connection.execute(text(
        "SELECT s.id, s.project_id, s.workflow_run_id, s.step_id, s.status, s.last_error, "
        "s.updated_at, r.workflow_definition_id, d.name AS workflow_name "
        "FROM workflow_step_runs s JOIN workflow_runs r ON r.id = s.workflow_run_id "
        "JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
        "WHERE s.status IN ('failed', 'deadlettered') AND s.updated_at > :s "
        f"{_SCOPE.format(t='s.')} ORDER BY s.updated_at LIMIT 500"),
        {"s": since, "p": project_id}).mappings().all()
    for r in steps:
        klass = error_class(r["last_error"])
        yield {
            "project_id": r["project_id"], "source": "workflow", "kind": "workflow.step_failed",
            "title": f"Step {r['step_id']} {r['status']} in {r['workflow_name']}",
            "fingerprint_parts": (r["workflow_definition_id"], r["step_id"], klass),
            "subject_kind": "workflow_run", "subject_id": r["workflow_run_id"],
            "severity": "high" if r["status"] == "deadlettered" else "medium",
            "evidence": [{"kind": "step_run", "ref": r["id"], "note": klass},
                         {"kind": "workflow_run", "ref": r["workflow_run_id"]}],
            "source_ref": f"step_run:{r['id']}:{r['status']}", "observed_at": r["updated_at"],
            "dedupe_window_seconds": 6 * 3600,
        }
    builds = connection.execute(text(
        "SELECT id, project_id, title, error, updated_at FROM code_task_runs "
        f"WHERE status = 'failed' AND updated_at > :s {_SCOPE.format(t='')} "
        "ORDER BY updated_at LIMIT 500"), {"s": since, "p": project_id}).mappings().all()
    for r in builds:
        klass = error_class(r["error"])
        yield {
            "project_id": r["project_id"], "source": "build", "kind": "build.failed",
            "title": f"Build failed: {klass[:120]}",
            "fingerprint_parts": ("build", klass),
            "subject_kind": "code_task", "subject_id": r["id"], "severity": "medium",
            "evidence": [{"kind": "code_task", "ref": r["id"], "note": r["title"]}],
            "source_ref": f"code_task:{r['id']}:failed", "observed_at": r["updated_at"],
            "dedupe_window_seconds": 6 * 3600,
        }


@adapter("system_doctor")
def system_doctor(connection: Connection, since: Any,
                  project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT id, project_id, kind, severity, title, first_seen, last_seen FROM system_findings "
        "WHERE status IN ('open', 'snoozed') AND last_seen > :s "
        # a finding with no project is install-wide: every project's sweep may see it
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p OR project_id IS NULL) "
        "ORDER BY first_seen LIMIT 200"), {"s": since, "p": project_id}).mappings().all()
    for r in rows:
        yield {
            "project_id": r["project_id"], "source": "doctor", "kind": f"doctor.{r['kind']}",
            "title": r["title"], "fingerprint_parts": ("doctor", r["kind"]),
            "subject_kind": "host", "subject_id": "install",
            "severity": {"critical": "critical", "warn": "medium", "info": "info"}.get(
                str(r["severity"]), "medium"),
            "evidence": [{"kind": "finding", "ref": r["id"]}],
            "source_ref": f"system_finding:{r['id']}", "observed_at": r["first_seen"],
        }


@adapter("shepherd")
def shepherd(connection: Connection, since: Any,
             project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT id, project_id, task_ref, question, created_at FROM shepherd_escalations "
        f"WHERE status = 'open' AND created_at > :s {_SCOPE.format(t='')} "
        "ORDER BY created_at LIMIT 200"), {"s": since, "p": project_id}).mappings().all()
    for r in rows:
        yield {
            "project_id": r["project_id"], "source": "shepherd", "kind": "shepherd.escalation",
            "title": r["question"], "fingerprint_parts": ("shepherd", r["task_ref"]),
            "subject_kind": "task", "subject_id": r["task_ref"], "severity": "high",
            "evidence": [{"kind": "escalation", "ref": r["id"]}],
            "source_ref": f"shepherd_escalation:{r['id']}", "observed_at": r["created_at"],
        }


@adapter("stale_prs")
def stale_prs(connection: Connection, since: Any,
              project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    """State, not change: every BOSS PR whose merge question is still open after STALE_PR_DAYS.
    `since` is not used — a PR goes stale by NOT changing — so the source_ref (task + decision)
    is what keeps a re-sweep a no-op."""
    rows = connection.execute(text(
        "SELECT c.id, c.project_id, c.pull_request_url, c.pull_request_number, c.title, "
        "d.id AS decision_id, d.created_at AS asked_at, "
        "floor(EXTRACT(EPOCH FROM (now() - d.created_at)) / 86400)::int AS days "
        "FROM code_task_runs c JOIN decisions d ON d.id = c.decision_id "
        "WHERE c.pull_request_url IS NOT NULL AND d.status IN ('pending', 'delegated') "
        "AND d.created_at < now() - make_interval(days => :n) "
        f"{_SCOPE.format(t='c.')} ORDER BY d.created_at LIMIT 200"),
        {"n": STALE_PR_DAYS, "p": project_id}).mappings().all()
    for r in rows:
        number = f"#{r['pull_request_number']}" if r["pull_request_number"] else "PR"
        yield {
            "project_id": r["project_id"], "source": "pr", "kind": "pr.stale",
            "title": f"PR {number} waiting {r['days']}d for a merge decision",
            "fingerprint_parts": ("pr", r["pull_request_url"]),
            "subject_kind": "pull_request", "subject_id": r["pull_request_url"],
            "severity": "low",
            "evidence": [{"kind": "code_task", "ref": r["id"], "note": r["title"]},
                         {"kind": "decision", "ref": r["decision_id"]},
                         {"kind": "url", "ref": r["pull_request_url"]}],
            "source_ref": f"stale_pr:{r['id']}:{r['decision_id']}", "observed_at": r["asked_at"],
        }


def register() -> list[str]:
    """The built-in adapters register on import (the decorators above); calling this is how the
    sweep makes sure that import happened. Returns their names."""
    return ["app_alerts", "workflow_failures", "system_doctor", "shepherd", "stale_prs"]
