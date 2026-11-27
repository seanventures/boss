"""THE TEST LEDGER: the functional test plan's result row for every fault case, from BOSS's records.

WHY. For every failure case (ENT-REC-*) the tester records the same four facts — fixed by itself,
time to fix, fixed by, message actionable — plus the run and workflow ids, and they were reading
each one off the console by hand: when the failure first showed, when the work moved on, whether
anyone clicked, which agent acted. BOSS already records all of it. `ledger` reads it back, one row
per LINEAGE (a build and its repairs, retries and follow-ups are one attempt at the case), so
`boss qa report` prints the ledger instead of the tester assembling it.

WHICH CASE. A lineage belongs to a case when BOSS's records tie it to one: a run of it names the
fault branch the fault kit pushed (`test/fault-rec-01`, scripts/install/qa_fault.py), a change of
that branch was reviewed by one of its runs (local_change_sets.run_id), or the work names the case
itself (the tester's New work titled "REC-01: add a recipe counter" — the kit says to). `case`
only FILTERS to that case's lineages; it never tags a lineage that does not name it.

WHAT EACH FIELD MEANS (read the same way the plan's "How to judge a failure case" does):
  failure_at     the first time a check failed: a repair round began, the app-start proof failed,
                 a gate failed, the draft change opened with checks failing, or the run failed
  green_at       the first pull request/change opened with its checks passing, or the run
                 completing, after the failure
  draft_at       the change opened marked checks failing (the plan's "if it cannot")
  fix_seconds    green_at − failure_at (None when it never went green)
  human_actions  decisions a PERSON answered for the lineage, and every act a person took on it
                 after it started (a Retry, a steer, a stop) — 0 is "Unattended: Y"
  acted          which of BOSS's agents acted: build repair, review repair, install fix, secret
                 repair, follow-up build, automatic retry, recovery (captain), self-fix
Free text (the title, the last error) passes the diagnostics report's own scrub
(share_scrub.scrub with this install's names); ids, times and counts are BOSS's own.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import text

#: The fault kit's branch, with its case (scripts/install/qa_fault.py BRANCH_PREFIX).
FAULT_BRANCH = re.compile(r"test/fault-(rec-\d{2})(?!\d)", re.IGNORECASE)
#: The case named in the work itself ("REC-01: …", "ENT-REC-01 …").
CASE_TAG = re.compile(r"(?<![\w-])(?:ENT-)?(REC-\d{2})(?!\d)", re.IGNORECASE)
CASE = re.compile(r"^(?:ENT-)?REC-\d{2}$", re.IGNORECASE)

#: Event → the agent or step of BOSS that acted.
ACTED_BY = {
    "code_task.inrun_convergence_round": "build repair",
    "code_task.review_repair_round": "review repair",
    "code_task.install_fixed": "install fix",
    "code_task.qa_static_fixed": "install fix",
    "code_task.secret_repair_started": "secret repair",
    "code_task.deferred_findings_task": "follow-up build",
    "code_task.step_recovered": "recovery (captain)",
    "recovery.decided": "recovery (captain)",
}
#: Events that say a check failed.
FAILED = {"code_task.inrun_convergence_round", "code_task.review_repair_round",
          "code_task.pr_opened_with_checks_failing", "code_task.waiting_for_secret",
          "code_task.step_escalating", "code_task.stage_budget_exhausted"}
DRAFT = "code_task.pr_opened_with_checks_failing"
#: A person starting the work is not an intervention; everything a person did after it is.
_START = re.compile(r"\.(started|created|queued|requested)$")


def normal_case(raw: Any) -> Optional[str]:
    value = str(raw or "").strip()
    if not value:
        return None
    if not CASE.match(value):
        raise ValueError("a case is written REC-01 (or ENT-REC-01)")
    return value.upper().removeprefix("ENT-")


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _json(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _case_of(run: dict) -> Optional[str]:
    blob = " ".join(json.dumps(_json(run.get(k)), default=str)
                    for k in ("spec", "finalize_ctx")) + " " + str(run.get("title") or "")
    m = FAULT_BRANCH.search(blob)
    if m:
        return m.group(1).upper()
    tagged = CASE_TAG.search(str(run.get("title") or "") + " "
                             + str(_json(run.get("spec")).get("instructions") or "")[:2000])
    return tagged.group(1).upper() if tagged else None


def _root(run: dict, by_id: dict[str, dict]) -> str:
    """The lineage's first run: root_run_id, else the parent chain, else the run itself."""
    seen: set[str] = set()
    current = run
    while True:
        fctx = _json(current.get("finalize_ctx"))
        if fctx.get("root_run_id") and str(fctx["root_run_id"]) in by_id:
            return str(fctx["root_run_id"])
        parent = str(fctx.get("parent_run_id") or fctx.get("deferred_from_run") or "")
        if not parent or parent not in by_id or parent in seen:
            return str(current["id"])
        seen.add(parent)
        current = by_id[parent]


def _people(connection: Any, project_id: str, actors: Iterable[Any]) -> set[str]:
    """Which of these actors are PEOPLE, positively, in one query: a signed-in principal (usr_…)
    or a human member of the project (a legacy install's `ceo`). BOSS's own actors — the
    pipeline, the shepherd, a seat, a role agent — are none of these, and an unknown name is not
    counted as a click."""
    names = {str(a).strip() for a in actors if str(a or "").strip()}
    out = {n for n in names if n.startswith(("usr_", "person:"))}
    rest = sorted(names - out)
    if rest:
        try:
            with connection.begin_nested():
                out |= {r[0] for r in connection.execute(text(
                    "SELECT member_id FROM project_members WHERE project_id = :p AND "
                    "member_id = ANY(:m) AND member_type = 'human'"),
                    {"p": project_id, "m": rest})}
        except Exception:  # noqa: BLE001 — no membership table, no person by name
            pass
    return out


def ledger(connection: Any, project_id: str, *, since: Optional[datetime] = None,
           case: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
    """{rows: [...], masked: {label: n}} — newest lineage first (see the module docstring)."""
    case = normal_case(case)
    runs = [dict(r) for r in connection.execute(text(
        "SELECT id, status, phase, title, spec, finalize_ctx, error, pull_request_url, "
        "decision_id, created_at, updated_at FROM code_task_runs WHERE project_id = :p "
        "AND (CAST(:since AS timestamptz) IS NULL OR created_at >= CAST(:since AS timestamptz)) "
        "ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "since": since, "n": int(limit) * 4}).mappings()]
    runs.reverse()                                    # oldest first inside a lineage
    by_id = {r["id"]: r for r in runs}
    lineages: dict[str, list[dict]] = {}
    for r in runs:
        lineages.setdefault(_root(r, by_id), []).append(r)
    # A pushed fault branch's change, reviewed by a run: that run's lineage is the case's.
    reviewed = {}
    try:
        with connection.begin_nested():
            for branch, run_id in connection.execute(text(
                    "SELECT branch, run_id FROM local_change_sets WHERE project_id = :p "
                    "AND branch LIKE 'test/fault-%' AND run_id = ANY(:ids)"),
                    {"p": project_id, "ids": list(by_id)}):
                m = FAULT_BRANCH.search(str(branch or ""))
                if m:
                    reviewed[str(run_id)] = m.group(1).upper()
    except Exception:  # noqa: BLE001 — no local changes table, no such link
        pass
    chosen: list[tuple[str, list[dict]]] = []
    for root, members in lineages.items():
        found = next((c for c in (reviewed.get(m["id"]) or _case_of(m) for m in members) if c),
                     None)
        if found and (case is None or found == case):
            chosen.append((found, members))
    chosen = sorted(chosen, key=lambda x: x[1][0]["created_at"], reverse=True)[:limit]
    from .share_scrub import install_names, scrub

    names = install_names(connection, project_id) if chosen else []
    masked: dict[str, int] = {}

    def clean(value: Any, limit_chars: int = 300) -> str:
        said, counts = scrub(" ".join(str(value or "").split())[:2000], names)
        for k, v in counts.items():
            masked[k] = masked.get(k, 0) + v
        return said[:limit_chars]

    facts = _facts(connection, project_id, [m for _, ms in chosen for m in ms])
    rows = [_row(case_id, members, facts, clean) for case_id, members in chosen]
    return {"project_id": project_id, "rows": rows, "masked": masked}


def _workflows(run: dict) -> set[str]:
    fctx = _json(run.get("finalize_ctx"))
    return {str(fctx.get("workflow_run_id") or (fctx.get("origin") or {}).get("workflow_run_id")
                or "")} - {""}


def _facts(connection: Any, project_id: str, runs: list[dict]) -> dict[str, Any]:
    """Everything the rows need, in one query per table for every lineage at once."""
    ids = [r["id"] for r in runs]
    workflows = sorted(set().union(*(_workflows(r) for r in runs))) if runs else []
    if not ids:
        return {"events": {}, "gates": {}, "decisions": [], "people": set()}
    events: dict[str, list[dict]] = {}
    for e in connection.execute(text(
            "SELECT event_type, actor, entity_id, data, created_at FROM events "
            "WHERE project_id = :p AND entity_id = ANY(:ids) ORDER BY created_at, id"),
            {"p": project_id, "ids": ids + workflows}).mappings():
        events.setdefault(str(e["entity_id"]), []).append(dict(e))
    gates = {str(r[0]): r[1] for r in connection.execute(text(
        "SELECT code_task_run_id, min(created_at) FROM quality_gates "
        "WHERE code_task_run_id = ANY(:ids) AND jsonb_typeof(gates_failed) = 'array' "
        "AND jsonb_array_length(gates_failed) > 0 GROUP BY code_task_run_id"), {"ids": ids})}
    decisions = [dict(d) for d in connection.execute(text(
        "SELECT id, status, approved_by, executed_by, answered_by, revision_requested_by, "
        "recommendation->>'run_id' AS run_id FROM decisions WHERE project_id = :p AND "
        "(id = ANY(:d) OR recommendation->>'run_id' = ANY(:ids))"),
        {"p": project_id, "d": [r["decision_id"] for r in runs if r.get("decision_id")],
         "ids": ids}).mappings()]
    actors = [e["actor"] for es in events.values() for e in es]
    actors += [d.get(k) for d in decisions
               for k in ("approved_by", "executed_by", "answered_by", "revision_requested_by")]
    return {"events": events, "gates": gates, "decisions": decisions,
            "people": _people(connection, project_id, actors)}


def _row(case: str, members: list[dict], facts: dict, clean) -> dict:
    ids = [m["id"] for m in members]
    workflows = sorted(set().union(*(_workflows(m) for m in members)))
    events = sorted([e for i in ids + workflows for e in facts["events"].get(i, [])
                     if i in ids or e["event_type"].startswith("recovery.")],
                    key=lambda e: e["created_at"])
    gate_times = [facts["gates"][i] for i in ids if i in facts["gates"]]
    decision_ids = {m["decision_id"] for m in members if m.get("decision_id")}
    decisions = [d for d in facts["decisions"]
                 if d["id"] in decision_ids or d.get("run_id") in ids]
    people = facts["people"]

    failed_at = [e["created_at"] for e in events if e["event_type"] in FAILED or (
        e["event_type"] == "code_task.app_serve_checked" and _json(e["data"]).get("ok") is False)]
    failed_at += gate_times
    failed_at += [m["updated_at"] for m in members if m["status"] in ("failed", "blocked")]
    failure = min(failed_at) if failed_at else None
    draft = next((e["created_at"] for e in events if e["event_type"] == DRAFT), None)
    green = next((e["created_at"] for e in events if e["event_type"] == "code_task.pr_opened"
                  and (failure is None or e["created_at"] >= failure)), None)
    if green is None:
        drafted = {e["entity_id"] for e in events if e["event_type"] == DRAFT}
        done = [m["updated_at"] for m in members if m["status"] == "completed"
                and m["id"] not in drafted]
        green = max(done) if done else None
    if green is not None and failure is not None and green < failure:
        green = None
    acted = []
    for e in events:
        who = ACTED_BY.get(e["event_type"])
        if e["event_type"].startswith("self_fix."):
            who = "self-fix"
        if who and who not in acted:
            acted.append(who)
    if any(_json(m["finalize_ctx"]).get("automatic_retry") for m in members) \
            and "automatic retry" not in acted:
        acted.append("automatic retry")
    root = members[0]
    person_acts = [e for e in events if str(e["actor"] or "") in people
                   and not (e["entity_id"] == root["id"] and _START.search(e["event_type"]))]
    person_decisions = [d for d in decisions if d["status"] not in ("pending", "open")
                        and any(str(d.get(k) or "") in people
                                for k in ("approved_by", "executed_by", "answered_by",
                                          "revision_requested_by"))]
    humans = len(person_acts) + len(person_decisions)
    last = members[-1]
    fix = (green - failure).total_seconds() if green and failure else None
    state = str(last["status"] or "")
    if draft and state == "completed":
        state = "draft change · checks failing"
    elif last.get("phase") and state not in ("completed", "failed"):
        state = f"{state} · {last['phase']}"
    return {
        "case": f"ENT-{case}",
        "runs": ids,
        "workflow_runs": workflows,
        "title": clean(root.get("title"), 120),
        "started_at": _iso(root["created_at"]),
        "failure_at": _iso(failure),
        "green_at": _iso(green),
        "draft_at": _iso(draft),
        "fix_seconds": round(fix) if fix is not None else None,
        "human_actions": humans,
        "unattended": humans == 0,
        "acted": acted,
        "final_state": state,
        "last_error": clean(last.get("error")) if last.get("error") else "",
        "pull_request": str(last.get("pull_request_url") or "") or None,
        "diagnostics": f"boss qa diagnostics {last['id']}",
    }


def as_markdown(rows: Iterable[dict]) -> str:
    """The plan's ledger columns, one row per case attempt."""
    head = ("| Case | Run(s) | Workflow | Failure seen | Green / draft | Time to fix | Human "
            "actions | Unattended | Fixed by | Final state | Diagnostics |")
    out = [head, "|" + "---|" * 11]

    def mins(s: Optional[int]) -> str:
        return "—" if s is None else (f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s")

    for r in rows:
        end = r["green_at"] or (f"draft {r['draft_at']}" if r["draft_at"] else "—")
        out.append("| " + " | ".join(str(x).replace("|", "/") for x in (
            r["case"], ", ".join(r["runs"]), ", ".join(r["workflow_runs"]) or "—",
            r["failure_at"] or "—", end, mins(r["fix_seconds"]), r["human_actions"],
            "Y" if r["unattended"] else "N", ", ".join(r["acted"]) or "—", r["final_state"],
            f"`{r['diagnostics']}`")) + " |")
    return "\n".join(out)
