"""Work tab (contract §3): every piece of work in a project as a row with a station strip.

A piece of work is a product workflow run (the one start door), a build started on its own (a
code task with no workflow run) or a roadmap item someone accepted that has not started yet.
Its stations — Plan · Decide · Build · Review · PR · Ship — are read from the run's steps in the
order its workflow version lists them; who did each step is command_center.step_agent, the
answer every other surface gives.

ONE ROW PER PIECE OF WORK. A follow-up ("Follow-up: checks failing — …") and a retry ("· retry 1")
are attempts of the build they came from, linked by finalize_ctx `root_run_id` (and, for a build
a workflow run started, its `workflow_run_id`). They are listed inside their root's row as
`attempts`, never as rows of their own: the Work tab once showed nineteen identical "Follow-up:
checks failing — Follow-up: checks…" rows, every one "Running" (2026-11-06).

RUNNING MEANS ACTIVE. A build left `repairing` at phase 'done' handed its work on; one silent past
the stall horizon is not working. Both read `stalled` (run_liveness.stalled_since), with the
time it stopped, and are counted apart from Running.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .new_work import issue_of
from .titles import short_title
from .common import (LIVE_RUN, age, agents, as_json, aware, face, href, iso, pr_number,
                     project_href, words)

#: The station strip (contract §3 `stages`), with the capabilities that belong to each.
STATIONS: tuple[tuple[str, str, frozenset], ...] = (
    ("plan", "P", frozenset({"research", "meeting"})),
    ("design", "D", frozenset({"spec_review", "architecture", "ux_review"})),
    ("build", "B", frozenset({"build", "test", "more_tests", "retry_build", "preview", "report",
                              "document", "security_fix", "dq_remediation_fix"})),
    ("review", "R", frozenset({"review", "security_review", "security_scan", "security_panel",
                               "security_decide", "security_rereview", "security_report", "plrr",
                               "cost_check"})),
    ("pr", "PR", frozenset({"wait_for_ci", "release_approval"})),
    ("ship", "S", frozenset({"deploy", "redeploy", "smoke_test", "monitoring_pack", "monitor",
                             "notify", "verify_recovery", "teardown_preview"})),
)
STATION_LABELS = {"plan": "Plan", "design": "Decide", "build": "Build", "review": "Review",
                  "pr": "PR", "ship": "Ship"}

_DONE_STEP = ("succeeded", "skipped")
_LIVE_STEP = ("running", "leased", "ready")
_FAILED_STEP = ("failed", "deadlettered")
_INTERNAL = "internal"


def _station_of(capability: str, step_kind: str) -> Optional[str]:
    for key, _letter, caps in STATIONS:
        if capability in caps:
            return key
    if step_kind == "decision":
        return "design"
    return None


def kind_of(workflow_key: Optional[str], given: Any = None) -> str:
    if str(given or "") in ("feature", "bug", "fix", "ticket", "chore", "incident"):
        return str(given)
    key = str(workflow_key or "").lower()
    if "incident" in key:
        return "incident"
    if "fix" in key or "bug" in key:
        return "bug"
    if any(w in key for w in ("chore", "upgrade", "dependency", "cleanup")):
        return "chore"
    return "feature"


def run_state(status: str, gate_open: bool) -> str:
    """A workflow run's status → the shared `work_state`."""
    if gate_open:
        return "needs_you"
    if status in ("queued", "validating"):
        return "planned"
    if status in LIVE_RUN:
        return "running"
    if status == "completed":
        return "done"
    if status == "cancelled":
        return "done"
    return "failed"


def stages(spec: Mapping[str, Any], steps: Mapping[str, Mapping[str, Any]], *,
           has_pr: bool, finished: bool) -> list[dict[str, str]]:
    """The six stations, each done | now | waiting | todo | failed | skipped."""
    grouped: dict[str, list[str]] = {}
    for spec_step in spec.get("steps") or []:
        if not isinstance(spec_step, dict):
            continue
        binding = spec_step.get("binding") if isinstance(spec_step.get("binding"), dict) else {}
        cap = str(binding.get("capability") or spec_step.get("capability") or "")
        station = _station_of(cap, str(spec_step.get("kind") or ""))
        if station:
            grouped.setdefault(station, []).append(str(spec_step.get("id") or ""))
    out = []
    for key, letter, _caps in STATIONS:
        ids = grouped.get(key) or []
        got = [steps.get(i) or {} for i in ids]
        statuses = [str(s.get("status") or "pending") for s in got]
        if key == "pr" and has_pr and not ids:
            state = "done"
        elif not ids:
            state = "skipped"
        elif any(s in _FAILED_STEP for s in statuses):
            state = "failed"
        elif any(s.get("gate_open") for s in got):
            state = "waiting"
        elif all(s in _DONE_STEP for s in statuses):
            state = "skipped" if all(s == "skipped" for s in statuses) else "done"
        elif any(s in _LIVE_STEP or s == "waiting" for s in statuses) or any(
                s in _DONE_STEP for s in statuses):
            state = "now"
        else:
            state = "todo"
        if key == "pr" and has_pr and state in ("todo", "skipped"):
            state = "done"
        if finished and state in ("now", "todo") and not any(s in _FAILED_STEP for s in statuses):
            state = "skipped" if state == "todo" else state
        out.append({"key": key, "label": letter, "name": STATION_LABELS[key], "state": state})
    return out


def _decided(strip: list[dict[str, str]], steps: Mapping[str, Mapping[str, Any]]
             ) -> list[dict[str, str]]:
    """THE D STATION IS THE PERSON'S DECISION ON THE PLAN (05-12/25, 05b-09). A workflow with no
    spec-review step still holds for its PRD when the panel's spec needs a person (the meeting
    step records `spec_decision_id` while it waits, `spec_approved_by` once approved): then D is
    that decision — waiting while it is open, done once a person approved it — and Plan is done
    (the panel finished). Where BOSS never waited on a person (an auto-approved PRD) D stays
    skipped: "You approve" only where BOSS really waits."""
    design = next((st for st in strip if st["key"] == "design"), None)
    if design is None or design["state"] != "skipped":
        return strip
    held = next((st for st in steps.values()
                 if isinstance(st.get("output"), dict) and st["output"].get("spec_decision_id")),
                None)
    if held is None:
        return strip
    status = str(held.get("status") or "")
    if held.get("gate_open") or status == "waiting":
        state, plan = "waiting", "done"
    elif status in _DONE_STEP or held["output"].get("spec_approved_by"):
        state, plan = "done", None
    elif status in _FAILED_STEP:
        state, plan = "failed", None
    else:
        return strip
    return [{**st, "state": state} if st["key"] == "design" else
            {**st, "state": plan} if st["key"] == "plan" and plan and st["state"] in ("waiting", "now")
            else st for st in strip]


def _with_merge(strip: list[dict[str, str]], merge_open: bool) -> list[dict[str, str]]:
    """A pull request waiting on a person's merge: the PR station waits on them."""
    if not merge_open:
        return strip
    return [{**s, "state": "waiting"} if s["key"] == "pr" else s for s in strip]


def _gate_word(spec: Mapping[str, Any], step_id: Optional[str]) -> str:
    for step in spec.get("steps") or []:
        if isinstance(step, dict) and str(step.get("id")) == str(step_id):
            binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
            cap = str(binding.get("capability") or step.get("capability") or "")
            if cap in ("deploy", "redeploy") or "deploy" in str(step_id):
                return "Approve deploy"
            if cap in ("release_approval",):
                return "Approve release"
            if cap in ("spec_review", "architecture", "ux_review") or step.get("kind") == "decision":
                return "Approve plan"
    return "Decide"


def _reviewed_in_build(strip: list[dict[str, str]], builds: list[Mapping[str, Any]]
                       ) -> list[dict[str, str]]:
    """A workflow with no review STEP still reviews: its build runs the review gates. The Review
    station is then the build's: done once a build passed them (it reached a pull request or
    completed), failed while the latest build is in a repair round — never "skipped"."""
    review = next((st for st in strip if st["key"] == "review"), None)
    if review is None or review["state"] != "skipped" or not builds:
        return strip
    latest = builds[-1]
    passed = any(b.get("pull_request_url") or str(b.get("status") or "") == "completed"
                 for b in builds)
    state = ("failed" if str(latest.get("status") or "") == "repairing" and not passed
             else "done" if passed else "now" if str(latest.get("phase") or "") == "validating"
             else "todo")
    return [{**st, "state": state} if st["key"] == "review" else st for st in strip]


def _versions(connection: Connection, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    rows = connection.execute(text(
        "SELECT id, spec FROM workflow_versions WHERE id = ANY(:ids)"), {"ids": wanted}).mappings()
    return {str(r["id"]): (as_json(r["spec"], {}) or {}) for r in rows}


def _steps(connection: Connection, run_ids: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """run → step_id → its newest iteration (status, output bits, whether its gate is open)."""
    from ..decision_actions import OPEN_STATUSES

    if not run_ids:
        return {}
    rows = connection.execute(text(
        "SELECT DISTINCT ON (s.workflow_run_id, s.step_id) s.workflow_run_id, s.step_id, "
        "s.step_kind, s.status, s.decision_id, s.output, s.started_at, s.completed_at, "
        "dd.status AS decision_status FROM workflow_step_runs s "
        "LEFT JOIN decisions dd ON dd.id = s.decision_id WHERE s.workflow_run_id = ANY(:ids) "
        "ORDER BY s.workflow_run_id, s.step_id, s.iteration DESC, s.created_at DESC"),
        {"ids": run_ids}).mappings().all()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        output = as_json(r["output"], {}) or {}
        out.setdefault(str(r["workflow_run_id"]), {})[str(r["step_id"])] = {
            "status": r["status"], "step_kind": r["step_kind"], "output": output,
            "decision_id": r["decision_id"],
            "gate_open": bool(r["decision_id"] and r["status"] == "waiting"
                              and r["decision_status"] in OPEN_STATUSES),
            "started_at": r["started_at"], "completed_at": r["completed_at"]}
    return out


#: One build attempt's facts (alias `c`), shared by the workflow-run and the standalone read so
#: both call a build Running, Stalled or Done by the same rule (run_liveness.stalled_since).
def _attempt_sql() -> str:
    from ..run_liveness import LAST_ACTIVITY_SQL, live_job_sql

    return (
        "SELECT c.id, c.status, c.phase, c.title, c.pull_request_url, c.pull_request_number, "
        "c.decision_id, dd.status AS decision_status, c.error, c.created_at, c.updated_at, "
        "c.finalize_ctx->>'workflow_run_id' AS wfr, c.finalize_ctx->>'root_run_id' AS root, "
        "c.finalize_ctx->>'parent_run_id' AS parent, "
        "c.finalize_ctx->>'deferred_from_run' AS deferred, "
        "c.finalize_ctx->>'attempt' AS attempt, "
        f"{LAST_ACTIVITY_SQL} AS last_activity, {live_job_sql('c')} AS live_job "
        "FROM code_task_runs c LEFT JOIN decisions dd ON dd.id = c.decision_id ")


def _attempt_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    from ..decision_actions import OPEN_STATUSES

    out = []
    for r in rows:
        row = dict(r)
        # The pull request waiting on a person's merge (the build's own decision).
        row["merge_open"] = bool(r["decision_id"] and r["decision_status"] in OPEN_STATUSES)
        out.append(row)
    return out


def _builds(connection: Connection, project_id: str, run_ids: list[str]
            ) -> dict[str, list[dict[str, Any]]]:
    if not run_ids:
        return {}
    rows = _attempt_rows(connection.execute(text(
        _attempt_sql() + "WHERE c.project_id = :p AND c.finalize_ctx->>'workflow_run_id' = ANY(:ids) "
        "ORDER BY c.created_at"), {"p": project_id, "ids": run_ids}).mappings().all())
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["wfr"]), []).append(row)
    return out


# ── attempts: a follow-up or a retry belongs to its root build ─────────────────────────────

#: "Follow-up: checks failing — " as a title lead (an em or en dash; a hyphen is ordinary text).
_FOLLOWUP_LEAD = re.compile(r"^\s*Follow-up:\s*([^—–]+?)\s*[—–]\s*", re.I)
_RETRY_TAIL = re.compile(r"\s*·\s*retry\s*\d+\s*$", re.I)


def clean_title(title: Any) -> str:
    """The work's own name: every stacked "Follow-up: … —" lead and "· retry N" tail off."""
    raw = str(title or "").strip()
    t = raw
    for _ in range(20):
        m = _FOLLOWUP_LEAD.match(t)
        if not m:
            break
        t = t[m.end():]
    for _ in range(5):
        t2 = _RETRY_TAIL.sub("", t)
        if t2 == t:
            break
        t = t2
    return t.strip() or raw


def _leads(title: Any) -> int:
    t, n = str(title or ""), 0
    while n < 20:
        m = _FOLLOWUP_LEAD.match(t)
        if not m:
            break
        t, n = t[m.end():], n + 1
    return n


def _reason(title: Any) -> str:
    m = _FOLLOWUP_LEAD.match(str(title or ""))
    said = (m.group(1).strip().lower() if m else "")
    if "review" in said:
        return "review findings"
    return said or "checks failing"


def _attempt_no(value: Any) -> int:
    try:
        return int(str(value or "1"))
    except ValueError:
        return 1


def label_attempts(members: list[dict[str, Any]], root_id: str) -> None:
    """Name each attempt of one piece of work, oldest first: "Build", "Retry 1",
    "Follow-up 2 · checks failing", "Follow-up 2 · retry 1". Sets `attempt_kind` and
    `attempt_label` on every member.

    The links are structural (finalize_ctx): a follow-up carries `deferred_from_run` (the run it
    fixes) and no `attempt`; a retry carries `parent_run_id` and `attempt` ≥ 2. Older follow-ups
    whose retries lost the follow-up mark are recognised by one more "Follow-up:" lead than the
    run they came from."""
    by_id = {str(m["id"]): m for m in members}
    base_of: dict[str, int] = {}
    retries: dict[int, int] = {}
    followups = 0
    for m in sorted(members, key=lambda x: str(iso(x.get("created_at")) or "")):
        mid = str(m["id"])
        parent = str(m.get("parent") or "")
        tried = _attempt_no(m.get("attempt")) >= 2
        up = by_id.get(parent)
        followup = not tried and (bool(m.get("deferred")) or (
            up is not None and _leads(m.get("title")) > _leads(up.get("title"))) or (
            up is None and mid != root_id and _leads(m.get("title")) > 0))
        if followup:
            followups += 1
            base = followups
            m["attempt_kind"] = "followup"
            m["attempt_label"] = f"Follow-up {base} · {_reason(m.get('title'))}"
        elif mid == root_id or not parent:
            base = 0
            m["attempt_kind"] = "build"
            m["attempt_label"] = "Build"
        else:
            base = base_of.get(parent, 0)
            retries[base] = retries.get(base, 0) + 1
            m["attempt_kind"] = "retry"
            m["attempt_label"] = (f"Retry {retries[base]}" if base == 0
                                  else f"Follow-up {base} · retry {retries[base]}")
        base_of[mid] = base


def attempt_state(r: Mapping[str, Any], now: datetime, horizon: float
                  ) -> tuple[str, Optional[datetime]]:
    """A build attempt's work_state, and since when it stalled (run_liveness.stalled_since)."""
    from ..run_liveness import stalled_since

    status = str(r.get("status") or "")
    if status in ("failed", "blocked"):
        state = "failed"
    elif status in ("completed", "cancelled"):
        state = "done"
    elif status == "queued":
        state = "planned"
    else:
        state = "running"
    if r.get("merge_open") and state != "failed":
        return "needs_you", None
    if state != "running":
        return state, None
    since = stalled_since(status=status, phase=r.get("phase"),
                          last_activity=aware(r.get("last_activity")) or aware(r.get("updated_at")),
                          live_job=bool(r.get("live_job")), now=now, horizon_seconds=horizon)
    return ("stalled", since) if since else ("running", None)


def _horizon() -> float:
    from ...config import get_settings
    from ..run_liveness import stall_horizon_seconds

    try:
        return float(stall_horizon_seconds(get_settings()))
    except Exception:  # noqa: BLE001 — the default horizon, never a failed tab
        return float(stall_horizon_seconds(None))


def _build_strip(r: Mapping[str, Any], state: str) -> list[dict[str, str]]:
    """The six stations of a build on its own (no workflow run): Plan and Decide skipped."""
    phases = ("queued", "provisioning", "coding", "validating", "opening_pr", "done")
    phase = str(r.get("phase") or "queued")
    at = phases.index(phase) if phase in phases else 0
    pr_url = r.get("pull_request_url")
    merge_open = bool(r.get("merge_open"))
    # A repair round: its checks failed at review and the work moved to the next attempt.
    repairing = str(r.get("status") or "") == "repairing" and phase == "done" and not pr_url

    def st(first: int, last: int) -> str:
        if state == "failed" and first <= at <= last:
            return "failed"
        if at > last or state == "done":
            return "done"
        if first <= at <= last:
            return "now"
        return "todo"

    review = "failed" if repairing else st(3, 3)
    pr = "waiting" if merge_open else "done" if pr_url else "todo" if repairing else st(4, 4)
    return [{"key": "plan", "label": "P", "name": "Plan", "state": "skipped"},
            {"key": "design", "label": "D", "name": "Decide", "state": "skipped"},
            {"key": "build", "label": "B", "name": "Build", "state": st(0, 2)},
            {"key": "review", "label": "R", "name": "Review", "state": review},
            {"key": "pr", "label": "PR", "name": "PR", "state": pr},
            {"key": "ship", "label": "S", "name": "Ship", "state": "skipped"}]


def _attempt_item(project_id: str, r: Mapping[str, Any], costs: Mapping[str, int],
                  latest_id: str) -> dict[str, Any]:
    pr_url = r.get("pull_request_url")
    state = str(r["_state"])
    # An older attempt left `repairing` handed its work to the next one: history, not stuck.
    if state == "stalled" and str(r["id"]) != latest_id:
        state = "handed_on"
    return {
        "id": str(r["id"]), "label": r.get("attempt_label") or "Build",
        "kind": r.get("attempt_kind") or "build", "title": r.get("title") or "",
        "state": state, "status": str(r.get("status") or ""),
        "created_at": iso(r.get("created_at")), "updated_at": iso(r.get("updated_at")),
        "stalled_since": iso(r.get("_since")) if state == "stalled" else None,
        "cost_usd": round(costs.get(str(r["id"]), 0) / 1_000_000, 2),
        "list_usd": list_usd(costs, [str(r["id"])]),
        "pr": ({"number": r.get("pull_request_number") or pr_number(pr_url),
                "external_url": pr_url} if pr_url else None),
        "build": href(project_id, area="code_tasks", view="apps", build=r["id"]),
        "href": project_href(project_id, tab="work", drawer=f"build:{r['id']}"),
    }


def _group_fields(project_id: str, members: list[dict[str, Any]], root_id: str,
                  costs: Mapping[str, int]) -> dict[str, Any]:
    """`attempts` (newest first), the counts and the latest attempt, for one piece of work."""
    label_attempts(members, root_id)
    ordered = sorted(members, key=lambda x: str(iso(x.get("created_at")) or ""), reverse=True)
    latest = ordered[0] if ordered else None
    attempts = [_attempt_item(project_id, m, costs, str(latest["id"]) if latest else "")
                for m in ordered]
    return {"attempts": attempts if len(attempts) > 1 else [],
            "attempt_count": len(attempts),
            "followups": sum(1 for a in attempts if a["kind"] == "followup"),
            "retries": sum(1 for a in attempts if a["kind"] == "retry"),
            "latest": latest}


class Costs(dict):
    """Money (micros) per build, and `included`: what a subscription's calls would have cost at
    list price, per build — never money, but never a bare $0.00 either (cost_words)."""

    def __init__(self, money: dict[str, int], included: dict[str, int]):
        super().__init__(money)
        self.included = included


def _cost(connection: Connection, build_ids: list[str]) -> Costs:
    """Money (micros) per build from llm_calls (a subscription's calls are $0), with the list
    price of the included calls beside it."""
    from ..included_spend import LLM_INCLUDED_SQL, LLM_MONEY_SQL

    if not build_ids:
        return Costs({}, {})
    rows = connection.execute(text(
        f"SELECT code_task_run_id AS b, sum({LLM_MONEY_SQL}) AS m, sum({LLM_INCLUDED_SQL}) AS i "
        "FROM llm_calls WHERE code_task_run_id = ANY(:ids) GROUP BY code_task_run_id"),
        {"ids": build_ids}).mappings().all()
    return Costs({str(r["b"]): int(r["m"] or 0) for r in rows},
                 {str(r["b"]): int(r["i"] or 0) for r in rows})


def list_usd(costs: Mapping[str, int], ids: Iterable[str]) -> Optional[float]:
    """The list price of the subscription calls of these builds, in dollars (None when none)."""
    included = getattr(costs, "included", {}) or {}
    micros = sum(int(included.get(str(i), 0)) for i in ids)
    return round(micros / 1_000_000, 2) if micros else None


def _deploys(connection: Connection, project_id: str, run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    rows = connection.execute(text(
        "SELECT DISTINCT ON (workflow_run_id) id, workflow_run_id, status, open_url "
        "FROM stack_deployments WHERE project_id = :p AND workflow_run_id = ANY(:ids) "
        "ORDER BY workflow_run_id, created_at DESC"), {"p": project_id, "ids": run_ids}).mappings()
    return {str(r["workflow_run_id"]): dict(r) for r in rows}


def _previews(connection: Connection, build_ids: list[str]) -> dict[str, str]:
    if not build_ids:
        return {}
    rows = connection.execute(text(
        "SELECT DISTINCT ON (run_id) id, run_id FROM app_previews WHERE run_id = ANY(:ids) "
        "AND status IN ('running', 'starting') ORDER BY run_id, created_at DESC"),
        {"ids": build_ids}).mappings().all()
    out = {}
    for r in rows:
        url = _preview_url(str(r["id"]))
        if url:
            out[str(r["run_id"])] = url
    return out


def _preview_url(preview_id: str) -> str:
    try:
        from ...config import get_settings
        from ..preview_service import public_preview_url

        return public_preview_url(preview_id, settings=get_settings())
    except Exception:  # noqa: BLE001 — a link is a help, never a failed tab
        return ""


def _standalone_groups(connection: Connection, project_id: str, limit: int
                       ) -> dict[str, dict[str, Any]]:
    """Builds started on their own (no workflow run of their own), grouped by their root build:
    root id → {"root": the root's row, "members": every attempt in the window, root included}.

    A follow-up or a retry carries its lineage's `root_run_id`, so the follow-up of a follow-up
    of a retry still names the build that started it all."""
    from ..code_task_service import _internal_run_prefixes  # noqa: SLF001 — the one definition

    params: dict[str, Any] = {"p": project_id, "n": max(limit * 5, 200)}
    clauses = []
    for i, prefix in enumerate(_internal_run_prefixes()):
        clauses.append(f"lower(coalesce(c.title, '')) NOT LIKE :pre{i}")
        params[f"pre{i}"] = f"{prefix}%"
    rows = _attempt_rows(connection.execute(text(
        _attempt_sql() + "WHERE c.project_id = :p "
        "AND COALESCE(c.finalize_ctx->>'workflow_run_id', '') = '' "
        + ("AND " + " AND ".join(clauses) + " " if clauses else "")
        + "ORDER BY c.created_at DESC LIMIT :n"), params).mappings().all())
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        groups.setdefault(str(r["root"] or r["id"]), {"root": None, "members": []})["members"].append(r)
    seen = {str(r["id"]) for r in rows}
    missing = [k for k in groups if k not in seen]
    found = {str(r["id"]): r for r in _attempt_rows(connection.execute(text(
        _attempt_sql() + "WHERE c.project_id = :p AND c.id = ANY(:ids)"),
        {"p": project_id, "ids": missing}).mappings().all())} if missing else {}
    for key, g in groups.items():
        root = next((m for m in g["members"] if str(m["id"]) == key), None) or found.get(key)
        g["root"] = root
        # A root outside the window is still the work's first attempt (unless a workflow run
        # owns it: then the run's own read lists it).
        if root is not None and str(root["id"]) not in {str(m["id"]) for m in g["members"]} \
                and not root.get("wfr"):
            g["members"].append(root)
    return groups


def _states(members: list[dict[str, Any]], now: datetime, horizon: float) -> None:
    for m in members:
        m["_state"], m["_since"] = attempt_state(m, now, horizon)


def _run_items(connection: Connection, project_id: str, limit: int,
               roster: Mapping[str, Mapping[str, Any]], now: datetime,
               standalone: dict[str, dict[str, Any]], horizon: float) -> list[dict[str, Any]]:
    from ..command_center import step_agent
    from ..run_liveness import HANDED_OFF_GRACE_SECONDS

    runs = [dict(r) for r in connection.execute(text(
        "SELECT r.id, r.status, r.created_at, r.updated_at, r.completed_at, r.workflow_version_id, "
        "r.input->>'title' AS title, r.input->>'directive' AS directive, "
        "r.input->>'work_kind' AS work_kind, r.input->>'source_ref' AS source_ref, "
        "r.input->>'source_url' AS source_url, r.input->>'repository' AS repository, "
        "r.error, r.blocked_reason, d.name AS workflow, "
        "d.workflow_key, (SELECT count(*) FROM workflow_runs n WHERE n.project_id = r.project_id "
        "AND n.created_at <= r.created_at) AS run_number "
        "FROM workflow_runs r JOIN workflow_definitions d ON "
        "d.id = r.workflow_definition_id WHERE r.project_id = :p AND d.namespace = 'product' "
        "AND COALESCE(r.mode, 'normal') <> 'test' ORDER BY r.created_at DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()]
    if not runs:
        return []
    ids = [str(r["id"]) for r in runs]
    specs = _versions(connection, (r["workflow_version_id"] for r in runs))
    steps = _steps(connection, ids)
    builds = _builds(connection, project_id, ids)
    # A follow-up started before follow-ups carried their workflow run: its ROOT build names the
    # run, so the follow-up joins that run's row instead of standing alone.
    attached: dict[str, list[dict[str, Any]]] = {}
    for key in [k for k, g in standalone.items() if (g["root"] or {}).get("wfr") in set(ids)]:
        g = standalone.pop(key)
        attached.setdefault(str(g["root"]["wfr"]), []).extend(
            m for m in g["members"] if str(m["id"]) != key)
    all_builds = [str(b["id"]) for bs in builds.values() for b in bs] + [
        str(m["id"]) for ms in attached.values() for m in ms]
    costs = _cost(connection, all_builds)
    deploys = _deploys(connection, project_id, ids)
    previews = _previews(connection, all_builds)
    out = []
    for r in runs:
        rid = str(r["id"])
        spec = specs.get(str(r["workflow_version_id"]), {})
        run_steps = steps.get(rid, {})
        own = builds.get(rid, [])
        members = own + [m for m in attached.get(rid, []) if str(m["id"]) not in
                         {str(b["id"]) for b in own}]
        members.sort(key=lambda m: str(iso(m.get("created_at")) or ""))
        _states(members, now, horizon)
        group = _group_fields(project_id, members, str(members[0]["id"]) if members else "", costs)
        latest = group.pop("latest")
        pr_url = next((s["output"].get("pull_request_url") for s in run_steps.values()
                       if s["output"].get("pull_request_url")), None) or next(
            (b["pull_request_url"] for b in reversed(members) if b["pull_request_url"]), None)
        gate_id = next((sid for sid, st in run_steps.items() if st["gate_open"]), None)
        gate = run_steps.get(gate_id) if gate_id else None
        merge = next((b for b in reversed(members) if b.get("merge_open")), None)
        status = str(r["status"])
        state = run_state(status, gate is not None or merge is not None)
        since = None
        if latest is not None and latest["_state"] == "stalled" and state == "running":
            # The run waits on a build that stopped moving, and nothing else in the run moved.
            moved = [t for t in [aware(r["updated_at"])] + [
                aware(st.get("completed_at")) or aware(st.get("started_at"))
                for st in run_steps.values()] if t]
            last = max(moved) if moved else None
            if last is None or (now - last).total_seconds() >= HANDED_OFF_GRACE_SECONDS:
                state, since = "stalled", latest["_since"]
        elif latest is not None and state in ("done", "failed") and latest not in own \
                and latest["_state"] in ("running", "stalled", "planned"):
            # A follow-up after the run ended is where this work is now.
            state, since = latest["_state"], latest["_since"]
        finished = state in ("done", "failed")
        faces: list[dict[str, Any]] = []
        seen: set[str] = set()
        spec_steps = {str(s.get("id")): s for s in spec.get("steps") or [] if isinstance(s, dict)}
        for sid, st in run_steps.items():
            who = step_agent(spec_steps.get(sid, {}), {**st, "output": st["output"]}, {})
            f = face(roster, who.get("role_key"))
            if f and f["role_key"] not in seen:
                seen.add(f["role_key"])
                faces.append(f)
        build = latest
        deploy = deploys.get(rid)
        cost = sum(costs.get(str(b["id"]), 0) for b in members)
        number = pr_number(pr_url) if pr_url else None
        title = r["title"] or r["workflow"]
        out.append({
            "id": rid, "source": "workflow_run", "source_label": "Workflow",
            # "Run #43": the run's number in its project (the drawer's story counts the same way).
            "run_number": int(r["run_number"] or 0) or None,
            # A short NAME (titles.short_title), never the brief's first words.
            "label": short_title(title, r["directive"]), "title": title,
            "kind": kind_of(r["workflow_key"], r["work_kind"]), "state": state, "status": status,
            "age": age(r["created_at"], now), "created_at": iso(r["created_at"]),
            "updated_at": iso(r["updated_at"]),
            "last_attempt_at": iso((latest or {}).get("created_at")) or iso(r["created_at"]),
            "stalled_since": iso(since),
            "stages": _reviewed_in_build(
                _with_merge(_decided(stages(spec, run_steps, has_pr=bool(pr_url) and merge is None,
                                            finished=finished), run_steps), merge is not None),
                members),
            # The one action a person takes here, named for what it does (never "Merge" on a
            # deploy approval): Merge · Approve deploy · Approve plan · Decide.
            "action_label": ("Merge" if merge is not None and gate is None
                             else _gate_word(spec, gate_id) if gate is not None else None),
            "agents": faces[:4], "cost_usd": round(cost / 1_000_000, 2),
            # On a subscription the money is $0; the work still has a list price (cost_words).
            "list_usd": list_usd(costs, [str(b["id"]) for b in members]),
            "links": {
                "workflow_run": href(project_id, area="delivery_workflows", wfr=rid),
                "build": href(project_id, area="code_tasks", view="apps", build=build["id"])
                if build else None,
                # THE BUILD'S OWN LIVE LOG: its page with the Logs drawer open on the build tab,
                # which reads that run's activity only (logs=build; code-tasks-ui readFactoryUrl).
                "log": href(project_id, area="code_tasks", build=build["id"], logs="build")
                if build else None,
                "pr": {"number": number, "external_url": pr_url} if pr_url else None,
                "deploy": href(project_id, area="infra", view="deploys", deploy=deploy["id"])
                if deploy else None,
                "preview": next((previews[str(b["id"])] for b in reversed(members)
                                 if str(b["id"]) in previews), None),
                "decision": href(project_id, area="decisions",
                                 decision=(gate or {}).get("decision_id") or merge["decision_id"])
                if (gate or merge) else None,
                # The issue it was started from (a pasted Ticket link, `issue:12`): the #12 chip.
                "issue": issue_of(r["source_ref"], r["source_url"], r["repository"]),
            },
            "decision_ids": [d for d in ((gate or {}).get("decision_id"),
                                         (merge or {}).get("decision_id")) if d],
            "detail": (r["directive"] or r["error"] or r["blocked_reason"] or r["workflow"]),
            "workflow": r["workflow"],
            "build_id": str(build["id"]) if build else None,
            "href": project_href(project_id, tab="work", drawer=f"run:{rid}"),
            **group,
        })
    return out


def _build_items(connection: Connection, project_id: str,
                 groups: dict[str, dict[str, Any]], roster: Mapping[str, Mapping[str, Any]],
                 now: datetime, horizon: float) -> list[dict[str, Any]]:
    """Builds started on their own: ONE row per root build, its follow-ups and retries inside."""
    ids = [str(m["id"]) for g in groups.values() for m in g["members"]]
    costs = _cost(connection, ids)
    previews = _previews(connection, ids)
    builder = face(roster, "engineering_captain")
    out = []
    for key, g in groups.items():
        members = g["members"]
        if not members:
            continue
        _states(members, now, horizon)
        group = _group_fields(project_id, members, key, costs)
        latest = group.pop("latest")
        root = g["root"] or min(members, key=lambda m: str(iso(m.get("created_at")) or ""))
        merge = next((m for m in sorted(members, key=lambda m: str(iso(m["created_at"])),
                                        reverse=True) if m.get("merge_open")), None)
        state = latest["_state"]
        if merge is not None and state != "failed":
            state = "needs_you"
        with_pr = next((m for m in sorted(members, key=lambda m: str(iso(m["created_at"])),
                                          reverse=True) if m.get("pull_request_url")), None)
        pr_url = with_pr["pull_request_url"] if with_pr else None
        strip = _build_strip({**latest, "merge_open": bool(merge)}, latest["_state"])
        raw_title = root.get("title") or "Build"
        title = clean_title(raw_title)
        out.append({
            "id": key, "source": "code_task", "source_label": "Build",
            "label": short_title(title), "title": title,
            # A build that is itself a fix of another's failing checks is a Fix, not a Feature.
            "kind": "fix" if _leads(raw_title) else kind_of(None), "state": state,
            "status": str(latest.get("status") or ""),
            "age": age(latest["created_at"], now), "created_at": iso(root["created_at"]),
            "updated_at": iso(latest["updated_at"]),
            "last_attempt_at": iso(latest["created_at"]),
            "stalled_since": iso(latest["_since"]) if state == "stalled" else None,
            "stages": strip,
            "agents": [builder] if builder else [],
            "cost_usd": round(sum(costs.get(str(m["id"]), 0) for m in members) / 1_000_000, 2),
            "list_usd": list_usd(costs, [str(m["id"]) for m in members]),
            "links": {"workflow_run": href(project_id, area="delivery_workflows", wfr=root["wfr"])
                      if root.get("wfr") else None,
                      "build": href(project_id, area="code_tasks", view="apps", build=latest["id"]),
                      "log": href(project_id, area="code_tasks", build=latest["id"], logs="build"),
                      "pr": ({"number": with_pr["pull_request_number"] or pr_number(pr_url),
                              "external_url": pr_url} if pr_url else None),
                      "deploy": None, "preview": previews.get(str(latest["id"])),
                      "decision": href(project_id, area="decisions", decision=merge["decision_id"])
                      if merge is not None else None},
            "decision_ids": [merge["decision_id"]] if merge is not None else [],
            "action_label": "Merge" if merge is not None else None,
            "detail": latest.get("error") or title,
            "build_id": str(latest["id"]),
            "href": project_href(project_id, tab="work", drawer=f"build:{key}"),
            **group,
        })
    return out


#: An accepted item's kind of work, from the roadmap recommendation's kind.
_REC_WORK_KIND = {"hardening": "bug", "design_fix": "bug", "refactor": "chore", "cost": "chore",
                  "process": "chore", "test_gap": "chore"}
#: The tile and tag a planned row shows for where it came from (05-19..05-22).
ORIGIN_TAGS = {"ci": "CI", "bug": "Bug", "ticket": "Ticket", "roadmap": "from Roadmap"}


def _planned_items(connection: Connection, project_id: str, now: datetime,
                   moving: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """Roadmap items a person accepted (§6) that no run has picked up yet: planned work, IN
    MARSHAL'S RANK (plan._planned: Next by rank, then Later) — the order the Work tab's Planned
    group, its drag-to-re-rank and the Roadmap's lanes share. The first is `next_up`, with the
    face of the agent who will plan it. An item whose work is already moving shows once, as that
    work (plan.lanes' rule)."""
    from . import plan as plan_service
    from .plan import _norm  # noqa: SLF001 — the one "same piece of work" rule

    taken = {_norm(w.get("title") or w.get("label")) for w in moving
             if w.get("state") in plan_service.NOW_STATES}
    from ..workflow_catalogue import default_agent_for

    # Who plans it once it starts: the panel's convener (Juno), as every stock workflow names.
    planner = face(agents(connection, project_id), default_agent_for("meeting"))
    out = []
    for p in plan_service._planned(connection, project_id):  # noqa: SLF001
        if _norm(p["title"]) in taken or _norm(p["label"]) in taken:
            continue
        work_kind = str(p.get("work_kind") or "")
        kind = ("bug" if work_kind == "bug" or p.get("origin") in ("bug", "ci") else
                _REC_WORK_KIND.get(str(p.get("rec_kind") or ""), work_kind or "feature"))
        if kind not in ("feature", "bug", "ticket", "chore", "fix", "incident"):
            kind = "feature"
        origin = str(p.get("origin") or "roadmap")
        if origin not in ORIGIN_TAGS:
            origin = "roadmap"
        issue = p.get("issue")
        out.append({
            "id": p["id"], "source": "roadmap", "source_label": "Roadmap",
            "origin": origin, "origin_label": ORIGIN_TAGS[origin],
            "label": p["label"], "title": p["title"], "kind": kind, "state": "planned",
            "status": "accepted", "lane": p["lane"], "rank": p.get("rank"),
            "next_up": False, "where": p.get("where"),
            "age": age(p.get("decided_at"), now), "created_at": iso(p.get("decided_at")),
            "updated_at": iso(p.get("decided_at")),
            "stages": [{"key": k, "label": letter, "name": STATION_LABELS[k], "state": "todo"}
                       for k, letter, _ in STATIONS],
            "agents": [], "cost_usd": 0.0,
            "links": {"workflow_run": None, "build": None, "pr": None, "deploy": None,
                      "preview": None, "decision": None,
                      "issue": issue},
            "decision_ids": [], "action_label": None, "attempts": [], "attempt_count": 0,
            "followups": 0, "retries": 0,
            "stalled_since": None, "last_attempt_at": iso(p.get("decided_at")),
            "detail": p.get("why") or p["title"],
            "start": p["start"], "move": p["move"],
            "href": p["href"],
        })
    if out:
        # NEXT UP: the top of Marshal's rank, and who plans it once it starts.
        out[0]["next_up"] = True
        out[0]["agents"] = [planner] if planner else []
    return out


#: The onboarding agent's run as a piece of work (05-27): its state in the shared enum.
_ONBOARDING_STATE = {"running": "running", "needs_you": "needs_you", "ready": "done",
                     "failed": "failed"}
_ONBOARDING_FLOW = (("discover", "Discover"), ("ready", "Ready"), ("backlog", "Backlog"))


def onboarding_cost(connection: Connection, project_id: str, started: Any,
                    finished: Any) -> float:
    """What the onboarding agent's model calls cost (llm_calls recorded with stage
    `onboarding` while it ran), in dollars."""
    from ..included_spend import LLM_MONEY_SQL

    micros = connection.execute(text(
        f"SELECT COALESCE(sum({LLM_MONEY_SQL}), 0) FROM llm_calls WHERE project_id = :p "
        "AND stage = 'onboarding' AND code_task_run_id IS NULL AND created_at >= :s "
        "AND (CAST(:e AS timestamptz) IS NULL OR created_at <= CAST(:e AS timestamptz) "
        "+ interval '5 minutes')"),
        {"p": project_id, "s": started, "e": finished}).scalar()
    return round(int(micros or 0) / 1_000_000, 2)


def _onboarding_item(connection: Connection, project_id: str,
                     roster: Mapping[str, Mapping[str, Any]], now: datetime,
                     onboarding: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    """ONBOARDING IS A PIECE OF WORK (screen 05 "Onboarding · Setup"): while it runs, while it
    has items only a person can do, and once it is done — with its own mini flow Discover →
    Ready → Backlog instead of the build stations, Helm (who onboards) and Marshal (who ranks
    the backlog), its cost and a link to its record."""
    from . import onboarding as onboarding_service

    ob = onboarding if onboarding is not None else onboarding_service.view(connection, project_id)
    if not ob or ob.get("state") in (None, "not_started") or not ob.get("run_id"):
        return None
    state = _ONBOARDING_STATE.get(str(ob["state"]), "running")
    waiting = list(ob.get("only_you") or [])
    if state == "needs_you" and not waiting:
        state = "done"
    stages = {s["key"]: s for s in ob.get("stages") or []}
    flow = [{"key": k, "label": label, "state": (stages.get(k) or {}).get("state") or "todo"}
            for k, label in _ONBOARDING_FLOW]
    if state == "done":
        flow = [{**f, "state": "done" if f["state"] in ("done", "now", "skipped") else f["state"]}
                for f in flow]
    started, finished = ob.get("started_at"), ob.get("finished_at")
    faces = [f for f in (face(roster, "engineering_captain"), face(roster, "chief_of_staff")) if f]
    opened = project_href(project_id, tab="onboarding")
    return {
        "id": str(ob["run_id"]), "source": "onboarding", "source_label": "Setup",
        "label": "Onboarding", "title": "Onboarding", "kind": "setup", "state": state,
        "status": str(ob["state"]), "age": age(started, now), "created_at": started,
        "updated_at": finished or started, "last_attempt_at": finished or started,
        "stalled_since": None, "flow": flow,
        "stages": [{"key": k, "label": letter, "name": STATION_LABELS[k], "state": "skipped"}
                   for k, letter, _ in STATIONS],
        "agents": faces, "cost_usd": onboarding_cost(connection, project_id, started, finished),
        "links": {"workflow_run": None, "build": None, "pr": None, "deploy": None,
                  "preview": None, "decision": None, "onboarding": opened},
        "decision_ids": [], "action_label": "Do it" if state == "needs_you" else None,
        "waiting": [{"key": i.get("key"), "label": i.get("label"),
                     "action": i.get("action")} for i in waiting],
        "attempts": [], "attempt_count": 1, "followups": 0, "retries": 0,
        "detail": ob.get("now") or ob.get("error") or "Helm onboarded the project.",
        "href": opened,
    }


def _with_why(connection: Connection, found: list[dict[str, Any]]) -> None:
    """Each unfinished row's `why` (stuck_work.wait_reason: what its build waits for, in words)
    and `diagnosis` (what the Shepherd found). A row whose build waits on a person is Needs you;
    one whose build cannot move without a fix is Stalled -- never "Running" over a build that
    says "Queued" (the laptop's CVE rows, 2026-11-07)."""
    from .. import stuck_work

    for w in found:
        # A workflow run no build carries yet (a gate, a quiet step, a failure with its words).
        if not w.get("build_id") and w.get("source") == "workflow_run" and w.get("state") != "done":
            try:
                with connection.begin_nested():
                    why = stuck_work.workflow_wait_reason(connection, str(w["id"]))
            except Exception:  # noqa: BLE001 — a row with no reason still renders
                why = None
            w["why"] = why
            if why and w.get("state") == "running" and why["who"] == "stuck":
                w["state"] = "stalled"
    open_rows = [w for w in found if w.get("build_id") and w.get("state") not in ("done",)]
    if not open_rows:
        return
    ids = [str(w["build_id"]) for w in open_rows]
    rows = {str(r["id"]): dict(r) for r in connection.execute(text(
        "SELECT * FROM code_task_runs WHERE id = ANY(:ids)"), {"ids": ids}).mappings().all()}
    reasons = stuck_work.wait_reasons(connection, list(rows.values()))
    for w in open_rows:
        run = rows.get(str(w["build_id"]))
        if run is None:
            continue
        why = reasons.get(str(w["build_id"]))
        w["why"] = why
        w["diagnosis"] = stuck_work.public_diagnosis(run.get("finalize_ctx"))
        if why and w.get("state") in ("running", "planned"):
            if why["who"] == "you":
                w["state"] = "needs_you"
            elif why["who"] == "stuck":
                w["state"] = "stalled"
                w["stalled_since"] = w.get("stalled_since") or why.get("since")


def everything(connection: Connection, project_id: str, *, limit: int = 60,
               now: Optional[datetime] = None,
               onboarding: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """Every WorkItem — one per piece of work, its attempts inside — newest activity first."""
    now = now or datetime.now(timezone.utc)
    roster = agents(connection, project_id)
    horizon = _horizon()
    standalone = _standalone_groups(connection, project_id, limit)
    found = (_run_items(connection, project_id, limit, roster, now, standalone, horizon)
             + _build_items(connection, project_id, standalone, roster, now, horizon))
    _with_why(connection, found)
    onboarding_row = _onboarding_item(connection, project_id, roster, now, onboarding)
    if onboarding_row is not None:
        found.append(onboarding_row)
    found.sort(key=lambda w: w.get("last_attempt_at") or w.get("created_at") or "", reverse=True)
    # Planned work keeps Marshal's rank (never re-sorted by date): the order a drag saves.
    planned = _planned_items(connection, project_id, now, moving=found)
    return found[:limit] + planned


def matching(found: list[dict[str, Any]], *, state: Optional[str] = None,
             q: Optional[str] = None) -> list[dict[str, Any]]:
    """Filtered by `state` (one or more work_states, comma-separated) and `q` (words or a PR
    number)."""
    if state:
        wanted = {s.strip() for s in str(state).split(",") if s.strip()}
        found = [w for w in found if w["state"] in wanted]
    if q and re.fullmatch(r"\s*#\d+\s*", str(q)):
        # "#3" is a number: the pull request's, or the issue's it came from — never "12" in words.
        number = str(q).strip().lstrip("#")
        return [w for w in found
                if number == str((w["links"].get("pr") or {}).get("number") or "")
                or number == str((w["links"].get("issue") or {}).get("number") or "")]
    if q:
        needle = str(q).strip().lower().lstrip("#")
        found = [w for w in found if needle in (w["title"] or "").lower()
                 or any(needle in str(a.get("title") or "").lower() for a in w.get("attempts") or [])
                 or needle in str(w.get("detail") or "").lower()
                 or needle == str((w["links"].get("pr") or {}).get("number") or "")]
    return found


def items(connection: Connection, project_id: str, *, state: Optional[str] = None,
          q: Optional[str] = None, limit: int = 60, now: Optional[datetime] = None
          ) -> list[dict[str, Any]]:
    return matching(everything(connection, project_id, limit=limit, now=now), state=state, q=q)


def counts(found: list[dict[str, Any]]) -> dict[str, int]:
    out = {s: 0 for s in ("running", "stalled", "needs_you", "planned", "done", "failed")}
    for w in found:
        out[w["state"]] = out.get(w["state"], 0) + 1
    return out


def view(connection: Connection, project_id: str, *, state: Optional[str] = None,
         q: Optional[str] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    found = everything(connection, project_id, now=now)
    return {"items": matching(found, state=state, q=q), "counts": counts(found), "state": state or None, "q": q or None,
            "new_work": {"href": f"/v1/projects/{project_id}/work",
                         "kinds": ["feature", "bug", "ticket"]}}


__all__ = ["STATIONS", "attempt_state", "clean_title", "counts", "everything", "items",
           "kind_of", "label_attempts", "matching", "run_state", "stages", "view"]
