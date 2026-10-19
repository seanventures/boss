"""WHO DID WHAT in an incident's response, in the order it happened (BOSSAPP-598, D2 operations).

The incident card used to show the response as a row of coloured dots: Investigate ● Build ●
CI ● … A reader could see THAT each step ran, never WHO ran it, what that agent is for, or what
it found. "An engineer took and addressed it" was all an operator could say about the response.

This module answers it from what BOSS already recorded, never from a guess:

  * triage        — the alert's triage record (app_alerts.triage): the on-call agent's own words,
                    its outcome and confidence, or which part of BOSS decided when no agent did;
  * every step    — the run's step rows in declared order, joined to the acts ledger
                    (agent_acts: which agent, named how) and to the decision a person answered;
  * investigate   — the runbook the firing rule names, the evidence the investigation read
                    (the app's metrics, its logs, the release diff) and the root cause it drew;
  * build         — the crew inside the build (each stage's agent from the same ledger), what
                    each reviewer found (the build's recorded verdicts), and the regression test:
                    red on the faulty release (`red_first`, governed build) and green with the fix;
  * approve       — the person who approved, by name;
  * deploy / CI   — BOSS's own machinery, run on the person's approval (the deploy keeps running
                    as the run's actor by design: its stack plan records who asked), said so.

Skills are the agent's real loadout (role_skills), so the card can say what the agent holds and
link to it. Every field is optional on the wire: a step that has not run yet has no agent words.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from ..ports import sql

#: What each step of an incident's response is FOR, said once, in the console's words.
JOBS: dict[str, str] = {
    "triage": "Decide what the alert is and what BOSS does with it",
    "investigate_incident": ("Find the root cause in the app's metrics, logs and the serving "
                             "release"),
    "build": "Write the fix with a regression test, and have it reviewed",
    "wait_for_ci": "Wait for the pull request's own CI",
    "report": "Account for the run: who did what and what it cost",
    "release_approval": "Approve the release",
    "deploy": "Ship the fix",
    "verify_recovery": "Prove recovery on BOSS's own monitor, not the fixer's word",
    "incident_report": "Write the incident report",
}

#: The build's stages, in the order they run, with what each one is for.
STAGE_JOBS: dict[str, str] = {
    "design": "Plans the change",
    "review_plan": "Reviews the plan",
    "write_tests": "Writes the regression test first",
    "implement": "Writes the fix",
    "review_code": "Tries to break the fix",
    "security_check": "Checks it is safe",
    "consistency_check": "Checks it matches the spec and the diff",
    "check_tests": "Runs the whole suite",
}


def _json(v: Any) -> Any:
    if isinstance(v, (dict, list)) or v is None:
        return v
    try:
        import json

        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _iso(v: Any) -> Optional[str]:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v else None


def first_sentence(text: Any, limit: int = 220) -> str:
    """The first sentence of an agent's summary, without markdown, at most `limit` chars."""
    s = str(text or "")
    # Drop what is not a sentence: a REVIEWED/SUMMARY line, a bold or # heading on its own line.
    s = re.sub(r"(?im)^\s*(REVIEWED|SUMMARY|VERDICT)\s*:.*$", "", s)
    s = re.sub(r"(?m)^\s*(\*\*[^*\n]+\*\*|#+ [^\n]+)\s*$", "", s)
    s = re.sub(r"\*\*|`|^#+ ", "", s, flags=re.M)
    s = " ".join(s.split())
    m = re.search(r"(.+?[.!?])(\s|$)", s)
    out = (m.group(1) if m else s).strip()
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _roles(connection, project_id: str) -> dict[str, str]:
    return {r["role_key"]: r["display_name"] for r in connection.execute(sql(
        "SELECT role_key, display_name FROM roles WHERE project_id = :p"),
        {"p": project_id}).mappings().all()}


def _skills(connection, project_id: str, role_key: str) -> list[str]:
    return [str(s) for s in connection.execute(sql(
        "SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r "
        "ORDER BY created_at, skill_id"), {"p": project_id, "r": role_key}).scalars().all()]


def _person(connection, principal_id: Optional[str]) -> str:
    from .incident_response import _person as person

    return person(connection, principal_id)


def _agent(roles: dict[str, str], role_key: Optional[str], how: str = "named") -> dict[str, Any]:
    return {"kind": "agent", "role_key": role_key, "role": roles.get(role_key or "") or role_key,
            "how": how}


def red_green(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The regression test's two runs, as the build recorded them: red on the faulty release
    (before the fix existed) and green with the fix. None when the build wrote no test."""
    files = [str(f) for f in (state.get("test_files") or [])]
    red = state.get("red_first") if isinstance(state.get("red_first"), dict) else None
    if not files and not red:
        return None
    return {
        "files": files,
        "red": ({"ran": bool(red.get("ran")), "failed": bool(red.get("failed")),
                 "exit_code": red.get("exit_code"), "failing": list(red.get("failing") or [])[:5],
                 "word": red.get("word") or ""} if red else None),
        "green": bool(state.get("tests_passed")) if state.get("tests_passed") is not None else None,
        "suite": first_sentence(state.get("qa_summary"), 160) if state.get("qa_summary") else "",
    }


def _stage_finding(stage: str, state: dict[str, Any]) -> tuple[str, Optional[str]]:
    """(what the stage's agent found, its verdict) from the build's recorded state."""
    if stage == "write_tests":
        files = [str(f).rsplit("/", 1)[-1] for f in state.get("test_files") or []]
        rg = red_green(state) or {}
        red = rg.get("red") or {}
        word = f"Wrote {', '.join(files)}" if files else "Wrote the tests"
        if red.get("ran"):
            word += ("; it fails on the faulty release" if red.get("failed")
                     else "; it did NOT fail on the faulty release")
        return word, ("red" if red.get("failed") else None)
    if stage == "implement":
        files = state.get("impl_files") or []
        names = [str(f.get("path") if isinstance(f, dict) else f).rsplit("/", 1)[-1]
                 for f in files][:4]
        return (f"Changed {', '.join(names)}" if names else "Wrote the fix"), None
    if stage == "review_code":
        return (first_sentence(state.get("review_summary")),
                str(state.get("adversarial_verdict") or "") or None)
    if stage == "security_check":
        return first_sentence(state.get("security_summary")), (
            str(state.get("security_verdict") or "") or None)
    if stage == "consistency_check":
        return first_sentence(state.get("consistency_summary")), (
            str(state.get("consistency_verdict") or "") or None)
    if stage == "check_tests":
        return (first_sentence(state.get("qa_summary"), 160),
                "passed" if state.get("tests_passed") else (
                    "failed" if state.get("tests_passed") is False else None))
    return "", None


def _triage(connection, roles: dict[str, str], incident_id: str,
            since: Optional[datetime]) -> list[dict[str, Any]]:
    out = []
    rows = connection.execute(sql(
        "SELECT id, alertname, labels, annotations, triage, triage_state, fired_at, active_at, "
        "       created_at, state, COALESCE(subject_id, target_id) AS target_id "
        "FROM app_alerts WHERE incident_id = :i "
        # An alert goes pending before it fires (its For), so it may predate the occurrence.
        "  AND (CAST(:s AS timestamptz) IS NULL OR COALESCE(fired_at, created_at) >= "
        "       CAST(:s AS timestamptz) - interval '15 minutes') "
        "ORDER BY COALESCE(fired_at, active_at, created_at)"),
        {"i": incident_id, "s": since}).mappings().all()
    for a in rows:
        t = _json(a["triage"]) or {}
        if a["triage_state"] != "done" or not t:
            continue
        answered = t.get("answered") if isinstance(t.get("answered"), dict) else None
        # Answered: the agent that ASKED is who triaged; the person's answer is what was done.
        by = ((t.get("asked") or {}).get("by") if answered else None) or t.get("by") or {}
        if by.get("role_key"):
            who = _agent(roles, str(by["role_key"]), "on_call")
            who["model"] = by.get("model")
        else:
            who = {"kind": "boss", "role_key": None,
                   "role": by.get("word") or "BOSS", "how": str(by.get("backend") or "")}
        labels = _json(a["labels"]) or {}
        word = {"opened": "Opened an incident", "joined": "Kept it in this incident",
                "split": "Opened its own incident", "noise": "Called it noise",
                "asked": "Asked you", "page": "Paged now"}.get(
            str(t.get("outcome") or ""), str(t.get("word") or "Triaged"))
        asking = t.get("outcome") == "asked" and a["state"] == "firing"
        words = ((t.get("asked") or {}).get("reason") if answered else t.get("reason")) or None
        if answered:
            options = {o.get("action"): o.get("label") for o in
                       ((t.get("asked") or {}).get("options") or t.get("options") or [])}
            word = (f"Asked; {answered.get('name') or 'a person'} answered: "
                    f"{options.get(answered.get('action')) or word}")
        out.append({
            "step_id": f"triage:{a['id']}", "kind": "triage", "label": "Triage",
            "job": JOBS["triage"], "status": "waiting" if asking else "succeeded",
            "started_at": _iso(a["fired_at"] or a["active_at"]), "ended_at": t.get("at"),
            "who": who, "did": word, "words": str(words) if words else None,
            "detail": {"alert": a["alertname"], "alert_id": a["id"],
                       "target_id": a["target_id"], "severity": labels.get("severity"),
                       "confidence": ((t.get("asked") or {}).get("confidence") if answered
                                      else t.get("confidence")),
                       # THE QUESTION AND ITS BUTTONS, while the agent waits for a person.
                       "question": t.get("question") if asking else None,
                       "options": (t.get("options") or []) if asking else [],
                       "answered": answered},
        })
    return out


def _investigation(connection, incident_id: str, since: Optional[datetime]) -> dict[str, Any]:
    """The runbook the firing rule names and the evidence the investigation read."""
    from .app_alerts_view import _runbook

    ev = {e["kind"]: _json(e["payload"]) or {} for e in connection.execute(sql(
        "SELECT kind, payload FROM incident_evidence WHERE incident_id = :i "
        "AND kind IN ('logs', 'release_diff', 'rca_hypothesis', 'alert') "
        "AND (CAST(:s AS timestamptz) IS NULL OR created_at >= CAST(:s AS timestamptz)) "
        "ORDER BY created_at, seq"), {"i": incident_id, "s": since}).mappings().all()}
    cited: list[dict[str, str]] = []
    alert = ev.get("alert") or {}
    routes = sorted([r for r in alert.get("routes") or [] if (r.get("errors") or 0) > 0],
                    key=lambda r: -(r.get("error_rate") or 0))[:2]
    if routes:
        cited.append({"kind": "metrics", "word": "; ".join(
            f"{r['route']}: {round(float(r.get('error_rate') or 0) * 100, 1)}% of "
            f"{int(r.get('requests') or 0)} requests failing" for r in routes)})
    logs = ev.get("logs") or {}
    if logs:
        sig = (logs.get("signatures") or [{}])[0] or {}
        where = sig.get("location") or ""
        what = (f"{sig.get('count')}× {sig.get('exc_type') or 'error'}"
                + (f" at {str(where).rsplit('/app/', 1)[-1]}" if where else "")) if sig else ""
        cited.append({"kind": "logs", "word": "; ".join(x for x in (logs.get("word"), what) if x)})
    diff = ev.get("release_diff") or {}
    if diff:
        files = [str(f.get("filename") or "").rsplit("/", 1)[-1]
                 for f in diff.get("files") or []][:4]
        cited.append({"kind": "release", "word": f"{diff.get('word')}"
                      + (f": {', '.join(files)}" if files else "")})
    hyp = ev.get("rca_hypothesis") or {}
    rule_runbook = connection.execute(sql(
        "SELECT r.annotations->>'runbook_url' FROM app_alerts a JOIN app_alert_rules r "
        "ON r.id = a.rule_id WHERE a.incident_id = :i AND r.annotations ? 'runbook_url' "
        "ORDER BY a.created_at LIMIT 1"), {"i": incident_id}).scalar()
    from .app_alerts import BUILTIN_RUNBOOK

    book = _runbook(rule_runbook or BUILTIN_RUNBOOK + "app-serving", {})
    return {"runbook": {"title": book["title"], "url": book["url"], "kind": book["kind"],
                        "markdown": book.get("markdown")},
            "evidence": cited,
            "root_cause": hyp.get("root_cause"), "basis": hyp.get("basis"),
            "confidence": hyp.get("confidence")}


def _ran_as_agent(output: dict[str, Any]) -> Optional[tuple[str, str]]:
    """(role_key, how) from the step's own `ran_as`, for a step the acts ledger has not synced
    yet; None when the step ran as the person who started the run."""
    ran = output.get("ran_as") if isinstance(output.get("ran_as"), dict) else {}
    agent = str(ran.get("agent") or "").strip().lower()
    how = str(ran.get("how") or "named")
    if not agent or how in ("run_actor", "workflow_runner", "legacy_workflow_runner") \
            or agent.startswith("usr_") or agent == "human":
        return None
    return agent, how


def _incident_ctx(connection, incident_id: str) -> dict[str, Any]:
    return _json(connection.execute(sql("SELECT finalize_ctx FROM incidents WHERE id = :i"),
                                    {"i": incident_id}).scalar()) or {}


def responders(connection, project_id: str, incident_id: str, run_id: Optional[str], *,
               since: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every part of the response, in order: who did it, what that agent is for, what it did."""
    from .workflow_capabilities import CAPABILITIES
    from .workflow_step_order import in_declared_order

    roles = _roles(connection, project_id)
    out = _triage(connection, roles, incident_id, since)
    if not run_id:
        return out
    run = connection.execute(sql("SELECT actor FROM workflow_runs WHERE id = :r"),
                             {"r": run_id}).mappings().first()
    actor_name = _person(connection, run["actor"]) if run else "the person who started it"
    steps = in_declared_order(connection, run_id, connection.execute(sql(
        "SELECT s.step_id, s.status, s.output, s.binding_snapshot, s.started_at, s.completed_at, "
        "       s.updated_at, d.approved_by, d.approved_at, d.status AS decision_status "
        "FROM workflow_step_runs s LEFT JOIN decisions d ON d.id = s.decision_id "
        "WHERE s.workflow_run_id = :r ORDER BY s.created_at, s.iteration"),
        {"r": run_id}).mappings().all())
    acts = connection.execute(sql(
        "SELECT step_id, kind, role_key, how, label, status, code_task_run_id, started_at, "
        "       ended_at FROM agent_acts WHERE workflow_run_id = :r "
        "AND kind IN ('step', 'stage') ORDER BY started_at"), {"r": run_id}).mappings().all()
    by_step = {a["step_id"]: a for a in acts if a["kind"] == "step"}
    stages = [a for a in acts if a["kind"] == "stage"]
    seen: set[str] = set()
    for s in steps:
        if s["step_id"] in seen:
            continue
        seen.add(s["step_id"])
        out_ = _json(s["output"]) or {}
        binding = _json(s["binding_snapshot"]) or {}
        cap_key = str(binding.get("capability") or s["step_id"])
        cap = CAPABILITIES.get(cap_key)
        act = by_step.get(s["step_id"])
        entry: dict[str, Any] = {
            "step_id": s["step_id"], "kind": cap_key,
            "label": cap.label if cap else s["step_id"],
            "job": JOBS.get(cap_key) or (first_sentence(cap.summary, 140) if cap else ""),
            "status": s["status"], "started_at": _iso(s["started_at"]),
            "ended_at": _iso(s["completed_at"] or (s["updated_at"] if s["status"] in (
                "succeeded", "failed", "skipped") else None)),
            "who": None, "did": "", "words": None, "detail": {},
        }
        if cap_key == "release_approval":
            if s["approved_by"]:
                entry["who"] = {"kind": "person", "role_key": None,
                                "name": _person(connection, s["approved_by"]),
                                "role": "Approver", "how": "approved"}
                entry["did"] = ("Approved the release" if s["decision_status"] != "rejected"
                                else "Rejected the release")
                entry["ended_at"] = _iso(s["approved_at"]) or entry["ended_at"]
            else:
                approver = (binding.get("approval") or {}).get("approver_role")
                entry["who"] = {"kind": "person", "role_key": None, "name": None,
                                "role": roles.get(approver or "") or "A person",
                                "how": "waiting"}
                entry["did"] = "Waiting for a person" if s["status"] == "waiting" else ""
        elif (act and act["role_key"]) or _ran_as_agent(out_):
            role_key, how = ((act["role_key"], act["how"]) if act and act["role_key"]
                             else _ran_as_agent(out_))
            entry["who"] = _agent(roles, role_key, how)
            entry["who"]["skills"] = _skills(connection, project_id, role_key)
        else:
            entry["who"] = {"kind": "boss", "role_key": None, "role": "BOSS",
                            "how": "run_actor", "on_behalf_of": actor_name}
        note = out_.get("note") or out_.get("recovery_summary") or out_.get("ci_summary") or ""
        if cap_key == "release_approval":
            pass
        elif cap_key == "investigate_incident":
            inv = _investigation(connection, incident_id, since)
            entry["detail"] = inv
            entry["did"] = (f"Root cause, {round(float(inv['confidence']) * 100)}% sure"
                            if inv.get("confidence") is not None else first_sentence(note))
            entry["words"] = inv.get("root_cause")
        elif cap_key == "build":
            crun = out_.get("code_task_run_id") or out_.get("run_id")
            row = connection.execute(sql(
                "SELECT state, pull_request_url FROM code_task_runs WHERE id = :i"),
                {"i": crun}).mappings().first() if crun else None
            state = (_json(row["state"]) or {}) if row else {}
            crew, crew_seen = [], set()
            for st in stages:
                key = str(st["step_id"])
                if key in crew_seen or key not in STAGE_JOBS:
                    continue
                crew_seen.add(key)
                finding, verdict = _stage_finding(key, state)
                crew.append({"stage": key, "label": str(st["label"]).split(" · ")[0],
                             "job": STAGE_JOBS[key], "status": st["status"],
                             "who": _agent(roles, st["role_key"], st["how"]),
                             "finding": finding or None, "verdict": verdict})
            order = list(STAGE_JOBS)
            crew.sort(key=lambda c: order.index(c["stage"]))
            entry["detail"] = {"pull_request_url": (row or {}).get("pull_request_url")
                               or out_.get("pull_request_url"),
                               "crew": crew, "tests": red_green(state)}
            entry["did"] = ("Opened the fix's pull request" if entry["detail"]["pull_request_url"]
                            else first_sentence(note) or "")
        elif cap_key == "deploy":
            commit = str(out_.get("commit") or "")[:7]
            entry["did"] = (f"Deployed {commit}" if commit else first_sentence(note)) + (
                f" · {out_.get('status')}" if out_.get("status") and commit else "")
            if entry["who"] and entry["who"]["kind"] == "boss":
                entry["who"]["role"] = "BOSS's deploy"
        elif cap_key == "verify_recovery":
            # The step's own output keeps what it said when it started waiting; the verdict is
            # the incident's verification, written when the monitor judged it.
            ver = (_incident_ctx(connection, incident_id).get("verification") or {})
            entry["did"] = first_sentence(
                (ver.get("summary") if s["status"] == "succeeded" else None)
                or out_.get("recovery_summary") or note, 260)
            entry["detail"] = {"state": ver.get("state"), "judged_by": "BOSS's own monitor"}
        elif cap_key == "incident_report":
            entry["did"] = first_sentence(note) or "Wrote the report"
        else:
            entry["did"] = first_sentence(note)
        out.append(entry)
    return out


def who_did_what_markdown(rows: list[dict[str, Any]], *, callsign=None) -> list[str]:
    """The report's "Who did what" section: one line per step, agent and role named."""
    def name(w: dict) -> str:
        role = w.get("role") or "BOSS"
        sign = callsign(w) if callsign else None
        return f"{sign} ({role})" if sign and sign != role else role
    lines = ["## Who did what", ""]
    for r in rows:
        w = r.get("who") or {}
        if w.get("kind") == "person":
            who = f"{w.get('name') or 'A person'} (person)"
        elif w.get("kind") == "agent":
            who = name(w)
        else:
            who = f"{w.get('role') or 'BOSS'}" + (
                f", for {w['on_behalf_of']}" if w.get("on_behalf_of") else "")
        did = r.get("did") or r.get("status") or ""
        lines.append(f"- **{r.get('label')}** — {who}: {did}")
        for c in (r.get("detail") or {}).get("crew") or []:
            lines.append(f"  - {c['label']} — {name(c['who'])}: "
                         f"{c.get('finding') or c.get('status')}")
    return lines + [""]
