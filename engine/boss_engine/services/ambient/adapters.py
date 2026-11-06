"""The signal adapters: BOSS's own stores, read into the one signal shape (BOSSAPP-903).

Each adapter is a PULL function `fn(connection, since, project_id=None) -> [observation]`
(project_id None = the whole install) registered with
`signals.adapter(name)`. It reads its store and returns record_signal keyword arguments; it
never writes, and it never decides what a problem means — that is triage's job (the
Chief-of-Staff, BOSSAPP-904). What it does decide is IDENTITY:

* the FINGERPRINT — what makes two observations the same problem (the same alert rule on the
  same app; the same step failing the same way in the same workflow; the same doctor finding);
* the SOURCE_REF — what makes one observation distinct from a re-read of the same row (an
  alert's id AND its fire count, so a re-fire is a new occurrence and a re-sweep is not);
* the `_cursor` — the value of the column it filtered `since` on, so a sweep that hit an
  adapter's row limit resumes after the last row it read instead of skipping the rest.

The stores and why each is a signal (design §3.3 gap 1, §5):

  app_alerts           alert.firing          firing app alerts, BOSS's own rules, pushed app
                                             signals (incident_signals) and external sources
  workflow_failures    workflow.step_failed  failed/deadlettered workflow steps
  build_failures       build.failed          failed code-task builds
  system_doctor        doctor.<kind>         open system_findings (install-wide)
  shepherd             shepherd.escalation   open shepherd escalations (a run that needs a call)
  stale_prs            pr.stale              a BOSS PR whose merge question has waited days
  ci_checks            ci.failed             a failed CI wait on a PR BOSS opened (ci_waits)
  dependency_advisories osv.vulnerable_dependency  an open OSV finding from the security scan
  cost_anomalies       cost.anomaly          an open cost anomaly (anomaly_detector)
  spend_spikes         cost.spend_spike      a day's spend far above the trailing mean

Adding a source (CI check_run, OSV, cost anomalies, KG drift ...) is one more function here, or
anywhere that imports `signals.adapter`, with a test against its real table.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..decision_actions import OPEN_STATUSES

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
        "COALESCE(a.fired_at, a.active_at) AS fired_at, t.app_name, "
        "COALESCE(a.last_eval_at, a.fired_at, a.active_at) AS seen_at "
        "FROM app_alerts a LEFT JOIN deployment_targets t ON t.id = a.target_id "
        "WHERE a.state = 'firing' AND COALESCE(a.last_eval_at, a.fired_at, a.active_at) > :s "
        f"{_SCOPE.format(t='a.')} ORDER BY seen_at LIMIT 500"),
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
            "_cursor": r["seen_at"],
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
            "dedupe_window_seconds": 6 * 3600, "_cursor": r["updated_at"],
        }


@adapter("build_failures")
def build_failures(connection: Connection, since: Any,
                   project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
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
            "dedupe_window_seconds": 6 * 3600, "_cursor": r["updated_at"],
        }


@adapter("system_doctor")
def system_doctor(connection: Connection, since: Any,
                  project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT id, project_id, kind, severity, title, first_seen, last_seen FROM system_findings "
        "WHERE status IN ('open', 'snoozed') AND last_seen > :s "
        # a finding with no project is install-wide: every project's sweep may see it
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p OR project_id IS NULL) "
        "ORDER BY last_seen LIMIT 200"), {"s": since, "p": project_id}).mappings().all()
    for r in rows:
        yield {
            "project_id": r["project_id"], "source": "doctor", "kind": f"doctor.{r['kind']}",
            "title": r["title"], "fingerprint_parts": ("doctor", r["kind"]),
            "subject_kind": "host", "subject_id": "install",
            "severity": {"critical": "critical", "warn": "medium", "info": "info"}.get(
                str(r["severity"]), "medium"),
            "evidence": [{"kind": "finding", "ref": r["id"]}],
            "source_ref": f"system_finding:{r['id']}", "observed_at": r["first_seen"],
            "_cursor": r["last_seen"],
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
            "_cursor": r["created_at"],
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
        "WHERE c.pull_request_url IS NOT NULL AND d.status = ANY(:open_statuses) "
        "AND d.created_at < now() - make_interval(days => :n) "
        f"{_SCOPE.format(t='c.')} ORDER BY d.created_at LIMIT 200"),
        {"n": STALE_PR_DAYS, "p": project_id,
         "open_statuses": sorted(OPEN_STATUSES)}).mappings().all()
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


# ── BOSSAPP-903, the remaining sources: CI, dependencies, cost ───────────────────────────────

def pr_ref(url: Any) -> Optional[str]:
    """`pr:owner/repo#41` from a GitHub pull-request URL (the board's subject_ref for a PR), so a
    signal, an item and a person's webhook all name the same PR the same way."""
    found = re.search(r"github\.com/([\w.\-]+/[\w.\-]+)/pull/(\d+)", str(url or ""))
    return f"pr:{found.group(1)}#{found.group(2)}" if found else None


@adapter("ci_checks")
def ci_checks(connection: Connection, since: Any,
              project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    """RED CI ON A PR BOSS OPENED (design §5 class 1: the highest-value, lowest-risk class). BOSS
    waits on the checks of every PR it opens (ci_gate → ci_waits); a wait that ended `failed` is
    the signal. Identity is the PR and WHICH checks failed, so a re-run that fails the same way
    is one problem and a different check failing is another. `caused_by_run_id` is the build that
    opened the PR: when an ambient item started that build, this signal is one causal level
    deeper (§6 rule 2/5: an ambient PR may wake CI for itself, and no further)."""
    rows = connection.execute(text(
        "SELECT id, project_id, repository, head_sha, pull_request_url, code_task_run_id, "
        "workflow_run_id, checks, failing_url, summary, "
        "COALESCE(finished_at, updated_at) AS seen_at FROM ci_waits "
        "WHERE status = 'failed' AND COALESCE(finished_at, updated_at) > :s "
        f"{_SCOPE.format(t='')} ORDER BY seen_at LIMIT 300"),
        {"s": since, "p": project_id}).mappings().all()
    for r in rows:
        failed = sorted({str(c.get("name") or "check") for c in (r["checks"] or [])
                         if isinstance(c, Mapping) and c.get("status") == "completed"
                         and str(c.get("conclusion") or "").lower() not in
                         ("success", "neutral", "skipped")}) or ["CI"]
        subject = pr_ref(r["pull_request_url"]) or f"sha:{r['repository']}@{r['head_sha'][:12]}"
        evidence: list[dict[str, Any]] = [{"kind": "ci_wait", "ref": r["id"],
                                           "note": str(r["summary"] or "")[:200]}]
        if r["failing_url"] and str(r["failing_url"]).startswith("http"):
            evidence.append({"kind": "url", "ref": r["failing_url"]})
        if r["code_task_run_id"]:
            evidence.append({"kind": "code_task", "ref": r["code_task_run_id"]})
        yield {
            "project_id": r["project_id"], "source": "ci", "kind": "ci.failed",
            "title": f"CI failed on BOSS PR: {', '.join(failed)[:120]}",
            "fingerprint_parts": ("ci", subject, ",".join(failed)),
            "subject_kind": "pull_request", "subject_id": subject,
            "severity": "high", "evidence": evidence,
            "source_ref": f"ci_wait:{r['id']}", "observed_at": r["seen_at"],
            "caused_by_run_id": r["code_task_run_id"] or r["workflow_run_id"],
            "dedupe_window_seconds": 6 * 3600, "_cursor": r["seen_at"],
        }


_OSV_SEVERITY = {"critical": "critical", "high": "high", "medium": "medium", "low": "low",
                 "info": "info"}


@adapter("dependency_advisories")
def dependency_advisories(connection: Connection, since: Any,
                          project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    """A KNOWN-VULNERABLE DEPENDENCY (design §5 class 3). The security scan runs OSV-Scanner over
    the app's lockfiles (security_scan, online through boss-egress); every open `osv` finding is a
    signal. One problem per package and advisory group — the scan already groups GHSA/CVE aliases
    under one primary id — so a nightly re-scan that finds it again adds nothing. Offline installs
    simply have no osv findings: this adapter reads, it never calls OSV itself."""
    rows = connection.execute(text(
        "SELECT f.id, f.rule_id, f.title, f.severity, f.location, f.evidence, s.project_id, "
        "s.app_key, COALESCE(s.ended_at, s.started_at) AS seen_at "
        "FROM security_findings f JOIN security_scans s ON s.id = f.scan_id "
        "WHERE f.tool = 'osv' AND f.status = 'open' AND s.status = 'done' "
        "AND COALESCE(s.ended_at, s.started_at) > :s "
        f"{_SCOPE.format(t='s.')} ORDER BY seen_at LIMIT 500"),
        {"s": since, "p": project_id}).mappings().all()
    for r in rows:
        loc = r["location"] or {}
        package = str(loc.get("package") or "a dependency")
        version = str(loc.get("version") or "")
        evidence: list[dict[str, Any]] = [{"kind": "security_finding", "ref": r["id"],
                                           "note": f"{package}@{version} {r['rule_id']}"[:200]}]
        advisory = (r["evidence"] or {}).get("advisory") if isinstance(r["evidence"], Mapping) \
            else None
        if advisory and str(advisory).startswith("http"):
            evidence.append({"kind": "url", "ref": advisory})
        yield {
            "project_id": r["project_id"], "source": "osv", "kind": "osv.vulnerable_dependency",
            "title": f"{package} {version} has {r['rule_id']}".strip()[:300],
            "fingerprint_parts": ("osv", package, r["rule_id"]),
            "subject_kind": "dependency", "subject_id": package, "app": r["app_key"],
            "severity": _OSV_SEVERITY.get(str(r["severity"]), "medium"),
            "evidence": evidence, "source_ref": f"security_finding:{r['id']}",
            "observed_at": r["seen_at"], "dedupe_window_seconds": 7 * 86400,
            "_cursor": r["seen_at"],
        }


@adapter("cost_anomalies")
def cost_anomalies(connection: Connection, since: Any,
                   project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    """A COST ANOMALY the detector already opened (anomaly_detector → cost_anomalies, design §5
    class 14). Identity is the provider and the anomaly type: the same runaway on the same
    provider is one problem however many cost events tripped it."""
    rows = connection.execute(text(
        "SELECT id, project_id, provider, anomaly_type, severity, summary, amount_usd_micros, "
        "threshold_usd_micros, created_at FROM cost_anomalies WHERE status = 'open' "
        f"AND created_at > :s {_SCOPE.format(t='')} ORDER BY created_at LIMIT 200"),
        {"s": since, "p": project_id}).mappings().all()
    for r in rows:
        sev = str(r["severity"] or "").lower()
        yield {
            "project_id": r["project_id"], "source": "cost", "kind": "cost.anomaly",
            "title": r["summary"] or f"{r['anomaly_type']} on {r['provider']}",
            "fingerprint_parts": ("cost", r["provider"], r["anomaly_type"]),
            "subject_kind": "provider", "subject_id": r["provider"],
            "severity": {"critical": "critical", "high": "high", "warning": "medium",
                         "medium": "medium", "low": "low", "info": "info"}.get(sev, "medium"),
            "evidence": [{"kind": "cost_anomaly", "ref": r["id"],
                          "note": f"${int(r['amount_usd_micros'] or 0) / 1e6:.2f} vs "
                                  f"${int(r['threshold_usd_micros'] or 0) / 1e6:.2f}"}],
            "source_ref": f"cost_anomaly:{r['id']}", "observed_at": r["created_at"],
            "dedupe_window_seconds": 86400, "_cursor": r["created_at"],
        }


#: A day's spend is a SPIKE when it is at least this many times the trailing daily mean …
SPEND_SPIKE_RATIO = 3.0
#: … and at least this much money (a $0.02 → $0.09 day is not a problem worth a card).
SPEND_SPIKE_FLOOR_USD_MICROS = 1_000_000
SPEND_BASELINE_DAYS = 7


@adapter("spend_spikes")
def spend_spikes(connection: Connection, since: Any,
                 project_id: Optional[str] = None) -> Iterable[dict[str, Any]]:
    """BOSS'S OWN SPEND JUMPED (design §5 class 14: "spend or retry spike"). State, not change:
    the last 24 hours of money on cost receipts against the trailing 7-day daily mean, per
    project. A subscription-covered call is $0 and never counts (included_spend). One signal per
    project per UTC day (the source_ref), so the sweep every minute does not re-raise it; the
    category that grew most is named so the triage can route it."""
    from ..included_spend import MONEY_SQL

    rows = connection.execute(text(
        "WITH recent AS (SELECT project_id, category, "
        f"sum({MONEY_SQL}) FILTER (WHERE created_at > now() - interval '1 day') AS day, "
        f"sum({MONEY_SQL}) FILTER (WHERE created_at <= now() - interval '1 day') AS before "
        "FROM cost_receipts WHERE created_at > now() - make_interval(days => :d + 1) "
        f"{_SCOPE.format(t='')} GROUP BY project_id, category) "
        "SELECT project_id, sum(COALESCE(day, 0)) AS day, sum(COALESCE(before, 0)) AS before, "
        "(array_agg(category ORDER BY COALESCE(day, 0) - COALESCE(before, 0) / :d DESC))[1] "
        "AS top_category FROM recent GROUP BY project_id"),
        {"d": SPEND_BASELINE_DAYS, "p": project_id}).mappings().all()
    from ._common import now

    today = now().date().isoformat()
    for r in rows:
        day, before = int(r["day"] or 0), int(r["before"] or 0)
        baseline = before / SPEND_BASELINE_DAYS
        if day < SPEND_SPIKE_FLOOR_USD_MICROS or day < SPEND_SPIKE_RATIO * max(baseline, 1):
            continue
        ratio = day / baseline if baseline else None
        words = f"{ratio:.0f}× the 7-day mean" if ratio else "with no spend the week before"
        yield {
            "project_id": r["project_id"], "source": "cost", "kind": "cost.spend_spike",
            "title": f"Spend ${day / 1e6:.2f} in 24h, {words}",
            "fingerprint_parts": ("spend_spike", r["project_id"]),
            "subject_kind": "budget", "subject_id": str(r["top_category"] or "spend"),
            "severity": "high" if (ratio or 10) >= 10 else "medium",
            "evidence": [{"kind": "query", "ref": (
                f"cost_receipts project={r['project_id']} last 24h ${day / 1e6:.2f}; "
                f"7-day mean ${baseline / 1e6:.2f}/day; top category {r['top_category']}")}],
            "source_ref": f"spend_spike:{r['project_id']}:{today}",
            "dedupe_window_seconds": 86400,
        }


def register() -> list[str]:
    """The built-in adapters register on import (the decorators above); calling this is how the
    sweep makes sure that import happened. Returns their names."""
    return ["app_alerts", "workflow_failures", "build_failures", "system_doctor", "shepherd",
            "stale_prs", "ci_checks", "dependency_advisories", "cost_anomalies", "spend_spikes"]
