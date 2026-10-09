"""The `security_review` workflow's steps, as the capability runner calls them (spec §4.2).

    scan -> panel -> decide -> fix -> rescan -> rereview -> fix_2 -> rescan_2 -> rereview_2 -> report

The runner owns WHEN a step runs, waits and finishes; this module owns WHAT each security step
does, from what the run has produced so far:

* `prepare` turns a tool-backed step's inputs into its tool's arguments (`run_security_scan`,
  `request_meeting`, `create_code_task`), or answers `not_applicable` with the reason when the
  step has nothing to do (a scan that found nothing needs no panel; a fix nobody chose is not
  built; round 2 does not run when round 1 passed);
* `has_choices` is asked before the decide gate opens: it writes the recommendations the person
  chooses from (the Panel's, plus a derived one for every finding the Panel left uncovered), and
  a review with none raises no gate;
* `perform` runs the two steps with no tool: the re-review verdict and the report.

THE LOOP IS BOUNDED AND VISIBLE. "Changes asked" in round 1 runs round 2 (`fix_2`, `rescan_2`,
`rereview_2`) on what the re-scan still found; round 2 is the last — a re-review that does not
pass there fails its step with the reasons, and the operator decides (retry, or stop). Step ids
are named literally in the catalogue's inputs (`"after": "fix"`), so the chain reads in one place.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from ..security_scan import report as scan_report
from ..security_scan import review, scope, service
from .core import emit_event

#: Capabilities whose work is a tool call this module prepares.
TOOL_KEYS = frozenset({"security_scan", "security_panel", "security_fix"})
#: Capabilities with no tool: performed here.
PERFORMED_KEYS = frozenset({"security_rereview", "security_report"})
#: The Panel's seats, chair first (spec §4.2 step 2: security reviewer chairs, an engineer and an
#: architect sit). Meeting role keys; one the project has not enabled is left out by the meeting.
PANEL_SEATS = ("security", "engineer", "architect")


class StepRefused(RuntimeError):
    """A step that cannot run and will not by retrying: the runner settles it with this reason."""


def _steps(context: dict[str, Any]) -> dict[str, Any]:
    return context.get("steps") or {}


def _out(context: dict[str, Any], step_id: Optional[str]) -> dict[str, Any]:
    value = _steps(context).get(str(step_id or "")) if step_id else None
    return value if isinstance(value, dict) else {}


def _ran(output: dict[str, Any]) -> bool:
    return bool(output) and str(output.get("status") or "") != "not_applicable"


def not_applicable(key: str, note: str, **extra: Any) -> dict[str, Any]:
    return {"capability": key, "tool": None, "status": "not_applicable", "note": note, **extra}


def app_key_of(context: dict[str, Any], inputs: dict[str, Any]) -> str:
    return str(inputs.get("app_key") or (context.get("input") or {}).get("app_key") or "").strip()


def first_scan_id(context: dict[str, Any]) -> str:
    return str(_out(context, "scan").get("scan_id") or "")


# ---- tool-backed steps -------------------------------------------------------------------------

def prepare(connection: Any, project_id: str, key: str, context: dict[str, Any],
            inputs: dict[str, Any]) -> dict[str, Any]:
    """{"args": {...}} for the step's tool, or a `not_applicable` result."""
    run_id = str(context.get("run_id") or "")
    app_key = app_key_of(context, inputs)
    if key == "security_scan":
        after = str(inputs.get("after") or "")
        if after and not str(_out(context, after).get("code_task_run_id") or ""):
            return not_applicable(key, "No fix was built in this round, so there is nothing to re-scan.")
        args: dict[str, Any] = {"app_key": app_key, "workflow_run_id": run_id}
        for name in ("tools", "zap_mode"):
            if inputs.get(name):
                args[name] = inputs[name]
        if after:
            # THE SAME TOOLS AS THE FIRST SCAN, so before and after compare like for like (S4.6).
            first = service.get_scan(connection, first_scan_id(context)) if first_scan_id(context) else None
            if first:
                target = first.get("target") or {}
                args["tools"] = list(target.get("tools") or args.get("tools") or [])
                zap = target.get("zap") or {}
                if zap.get("mode"):
                    args["zap_mode"] = zap["mode"]
        return {"args": args}

    if key == "security_panel":
        scan_id = first_scan_id(context)
        scan = service.get_scan(connection, scan_id) if scan_id else None
        if scan is None:
            raise StepRefused("the scan step produced no scan to review")
        findings = service.list_findings(connection, scan_id)
        if not findings:
            return not_applicable(key, "The scan found nothing, so there is nothing for a panel to review.")
        digest = review.findings_digest(findings, app_key=app_key, commit=str(scan.get("commit_sha") or ""))
        args = {"directive": review.panel_directive(digest, app_key=app_key,
                                                    focus=str(inputs.get("focus") or "")),
                "seats": list(PANEL_SEATS), "purpose": "security_review"}
        objective = str(context.get("objective") or "").strip()
        if objective:
            args["objective"] = objective
            args["workflow_name"] = str(context.get("workflow_name") or "").strip()
        return {"args": args}

    if key == "security_fix":
        round_ = int(inputs.get("round") or 1)
        chosen = review.chosen(connection, run_id)
        remaining = new_blocking = None
        if round_ > 1:
            previous = _out(context, str(inputs.get("after_review") or ""))
            if not _ran(previous) or previous.get("passed"):
                return not_applicable(key, f"Round {round_ - 1} passed its re-review, so there is nothing left to fix.")
            remaining = list(previous.get("failing") or [])
            new_blocking = list(previous.get("new_blocking") or [])
        if not chosen:
            return not_applicable(key, "Nobody chose a fix, so nothing is built.")
        scan_id = first_scan_id(context)
        by_fp = {f["fingerprint"]: f for f in service.list_findings(connection, scan_id)} if scan_id else {}
        brief, criteria = review.fix_brief(chosen, by_fp, app_key=app_key, remaining=remaining,
                                           new_blocking=new_blocking)
        try:
            app = scope.resolve_app(connection, project_id, app_key, None)
        except scope.ScopeRefused as exc:
            raise StepRefused(f"cannot build a fix for {app_key}: {exc}") from exc
        return {"args": {
            "title": f"Security fixes: {app_key}" + (f" (round {round_})" if round_ > 1 else ""),
            "instructions": brief, "acceptance_criteria": criteria,
            # THE APP'S NEXT BUILD, on its own branch: the fix is a change to this app.
            "extends_run_id": app.run_id, "workflow_run_id": run_id,
        }}
    raise StepRefused(f"not a security review step: {key}")


# ---- the decide gate ---------------------------------------------------------------------------

def ensure_recommendations(connection: Any, project_id: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    """The list the person chooses from, written once per run: the Panel's recommendations read
    from its document, then one derived per finding it left uncovered."""
    run_id = str(context.get("run_id") or "")
    existing = review.list_recommendations(connection, run_id)
    if existing:
        return existing
    scan_id = first_scan_id(context)
    if not scan_id:
        return []
    findings = service.list_findings(connection, scan_id)
    if not findings:
        return []
    panel = _out(context, "panel")
    markdown = str(panel.get("prd_markdown") or "")
    if not markdown and panel.get("meeting_run_id"):
        state = connection.execute(text("SELECT state FROM meeting_runs WHERE id = :i"),
                                   {"i": panel["meeting_run_id"]}).scalar()
        if isinstance(state, str):
            state = json.loads(state or "{}")
        markdown = str((state or {}).get("prd_markdown") or "")
    parsed = review.parse_panel_recommendations(markdown, [f["fingerprint"] for f in findings])
    recs = review.merge_recommendations(parsed, findings)
    meeting_id = None
    if panel.get("meeting_run_id"):
        meeting_id = connection.execute(text("SELECT meeting_id FROM meeting_runs WHERE id = :i"),
                                        {"i": panel["meeting_run_id"]}).scalar()
    stored = review.store_recommendations(connection, project_id=project_id, run_id=run_id,
                                          scan_id=scan_id, meeting_id=meeting_id, recs=recs)
    emit_event(connection, project_id, "security_review.recommendations_ready", "boss", "workflow_run",
               run_id, {"count": len(stored), "from_panel": len(parsed),
                        "derived": sum(1 for r in stored if r["source"] == "derived")})
    return stored


def has_choices(connection: Any, project_id: str, context: dict[str, Any]) -> bool:
    return bool(ensure_recommendations(connection, project_id, context))


# ---- the steps with no tool --------------------------------------------------------------------

def targeted_fingerprints(connection: Any, run_id: str) -> list[str]:
    return [fp for r in review.chosen(connection, run_id) for fp in r["fingerprints"]]


def verdict_between(connection: Any, project_id: str, app_key: str, run_id: str,
                    before: str, after: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rep = scan_report.build_report(connection, project_id, app_key, before=before, after=after)
    verdict = review.rereview_verdict(rep, targeted_fingerprints(connection, run_id),
                                      review.justified_fingerprints(connection, after))
    return rep, verdict


def perform(connection: Any, settings: Any, project_id: str, key: str, context: dict[str, Any],
            inputs: dict[str, Any]) -> dict[str, Any]:
    run_id = str(context.get("run_id") or "")
    app_key = app_key_of(context, inputs)
    if key == "security_rereview":
        after_scan = str(_out(context, str(inputs.get("after") or "")).get("scan_id") or "")
        if not after_scan:
            return not_applicable(key, "No re-scan ran in this round, so there is nothing to re-review.")
        before_scan = first_scan_id(context)
        try:
            _rep, verdict = verdict_between(connection, project_id, app_key, run_id, before_scan, after_scan)
        except scan_report.ReportError as exc:
            raise StepRefused(f"the re-review could not compare the scans: {exc}") from exc
        final = bool(inputs.get("final"))
        round_ = int(inputs.get("round") or 1)
        emit_event(connection, project_id, "security_review.rereviewed", "boss", "workflow_run", run_id,
                   {"round": round_, "passed": verdict["passed"], "summary": verdict["summary"],
                    "before_scan_id": before_scan, "after_scan_id": after_scan})
        if not verdict["passed"] and final:
            raise StepRefused(
                f"the re-review did not pass after round {round_}: " + "; ".join(verdict["reasons"])
                + ". Retry this step after another fix, or stop the run.")
        return {"capability": key, "tool": None, "status": "succeeded" if verdict["passed"] else "changes_asked",
                "passed": verdict["passed"], "round": round_, "before_scan_id": before_scan,
                "after_scan_id": after_scan, "summary": verdict["summary"], "reasons": verdict["reasons"],
                "failing": verdict["failing"], "new_blocking": verdict["new_blocking"],
                "verdict": {k: verdict[k] for k in ("passed", "fixed", "removed", "justified", "rules_differ")},
                "note": ("Passed: every targeted finding is gone or justified, and no new high or critical "
                         "finding appeared." if verdict["passed"] else
                         "Changes asked: " + "; ".join(verdict["reasons"]) + ". Round 2 fixes what is left.")}
    if key == "security_report":
        return write_report(connection, settings, project_id, app_key, context)
    raise StepRefused(f"not a security review step: {key}")


def write_report(connection: Any, settings: Any, project_id: str, app_key: str,
                 context: dict[str, Any]) -> dict[str, Any]:
    """The before/after, as a document the Factory shows and anyone can download (spec §4.6)."""
    from .core import create_artifact

    run_id = str(context.get("run_id") or "")
    before = first_scan_id(context)
    last = next((_out(context, s) for s in ("rereview_2", "rereview") if _ran(_out(context, s))), {})
    after = str(last.get("after_scan_id") or "")
    content: dict[str, Any] = {"kind": "security_report", "app_key": app_key, "workflow_run_id": run_id,
                               "before_scan_id": before or None, "after_scan_id": after or None,
                               "recommendations": review.list_recommendations(connection, run_id)}
    if before and after:
        rep, verdict = verdict_between(connection, project_id, app_key, run_id, before, after)
        content.update({"report": rep, "verdict": verdict, "passed": verdict["passed"],
                        "summary": review.plain_summary(rep, verdict),
                        "markdown": scan_report.to_markdown(rep)})
    else:
        scan = service.get_scan(connection, before) if before else None
        total = int(((scan or {}).get("counts") or {}).get("total") or 0)
        content.update({"report": None, "verdict": None, "passed": None, "scan": scan,
                        "summary": (f"Scanned: {total} findings. No fix was chosen, so there is no after."
                                    if total else "Scanned: nothing found.")})
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    artifact_id = create_artifact(connection, settings, project_id, None, "security_report",
                                  f"Security report: {app_key}", content)
    return {"capability": "security_report", "tool": None, "status": "recorded", "artifact_id": artifact_id,
            "passed": content["passed"], "summary": content["summary"],
            "before_scan_id": before or None, "after_scan_id": after or None}


# ---- what the console reads --------------------------------------------------------------------

_STEP_ORDER = ("scan", "panel", "decide", "fix", "rescan", "rereview", "fix_2", "rescan_2", "rereview_2",
               "report")


def review_state(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    """One review run as the console draws it: its steps, scans, recommendations, the gate a
    person answers, and the verdict so far."""
    run = connection.execute(text(
        "SELECT r.id, r.project_id, r.status, r.input, r.created_at, r.updated_at, d.workflow_key "
        "FROM workflow_runs r JOIN workflow_versions v ON v.id = r.workflow_version_id "
        "JOIN workflow_definitions d ON d.id = v.workflow_definition_id WHERE r.id = :r"),
        {"r": run_id}).mappings().first()
    if run is None or run["workflow_key"] != "security_review":
        return None
    rows = connection.execute(text(
        "SELECT DISTINCT ON (step_id) step_id, status, output, last_error, decision_id FROM workflow_step_runs "
        "WHERE workflow_run_id = :r ORDER BY step_id, created_at DESC, iteration DESC"), {"r": run_id}).mappings().all()
    steps = {}
    for row in rows:
        out = row["output"] if isinstance(row["output"], dict) else {}
        steps[row["step_id"]] = {"id": row["step_id"], "status": row["status"],
                                 "note": out.get("note") or out.get("summary"), "error": row["last_error"],
                                 "decision_id": row["decision_id"],
                                 **{k: out[k] for k in ("scan_id", "meeting_run_id", "code_task_run_id",
                                                        "pull_request_url", "passed", "artifact_id",
                                                        "after_scan_id") if out.get(k) is not None}}
    run_input = run["input"] if isinstance(run["input"], dict) else {}
    decide = steps.get("decide") or {}
    return {
        "run_id": run["id"], "project_id": run["project_id"], "status": run["status"],
        "app_key": run_input.get("app_key"),
        "created_at": run["created_at"].isoformat() if run["created_at"] else None,
        "updated_at": run["updated_at"].isoformat() if run["updated_at"] else None,
        "steps": [steps[s] for s in _STEP_ORDER if s in steps],
        "recommendations": review.list_recommendations(connection, run_id),
        "awaiting_choice": decide.get("status") == "waiting" and bool(decide.get("decision_id")),
        "decision_id": decide.get("decision_id") if decide.get("status") == "waiting" else None,
        "report_artifact_id": (steps.get("report") or {}).get("artifact_id"),
    }


def list_reviews(connection: Any, project_id: str, app_key: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
    ids = connection.execute(text(
        "SELECT r.id FROM workflow_runs r JOIN workflow_versions v ON v.id = r.workflow_version_id "
        "JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
        "WHERE r.project_id = :p AND d.workflow_key = 'security_review' "
        "AND (CAST(:a AS TEXT) IS NULL OR r.input->>'app_key' = :a) ORDER BY r.created_at DESC LIMIT :n"),
        {"p": project_id, "a": app_key, "n": max(1, min(int(limit), 100))}).scalars().all()
    return [s for s in (review_state(connection, i) for i in ids) if s]
