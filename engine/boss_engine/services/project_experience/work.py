"""Work tab (contract §3): every piece of work in a project as a row with a station strip.

A piece of work is a product workflow run (the one start door), a build started on its own (a
code task with no workflow run) or a roadmap item someone accepted that has not started yet.
Its stations — Plan · Decide · Build · Review · PR · Ship — are read from the run's steps in the
order its workflow version lists them; who did each step is command_center.step_agent, the
answer every other surface gives.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import (LIVE_RUN, age, agents, as_json, face, href, iso, pr_number, project_href,
                     words)

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
    if str(given or "") in ("feature", "bug", "ticket", "chore", "incident"):
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


def _with_merge(strip: list[dict[str, str]], merge_open: bool) -> list[dict[str, str]]:
    """A pull request waiting on a person's merge: the PR station waits on them."""
    if not merge_open:
        return strip
    return [{**s, "state": "waiting"} if s["key"] == "pr" else s for s in strip]


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


def _builds(connection: Connection, project_id: str, run_ids: list[str]
            ) -> dict[str, list[dict[str, Any]]]:
    if not run_ids:
        return {}
    from ..decision_actions import OPEN_STATUSES

    rows = connection.execute(text(
        "SELECT c.id, c.status, c.pull_request_url, c.pull_request_number, c.created_at, "
        "c.decision_id, dd.status AS decision_status, "
        "c.finalize_ctx->>'workflow_run_id' AS wfr FROM code_task_runs c "
        "LEFT JOIN decisions dd ON dd.id = c.decision_id WHERE c.project_id = :p "
        "AND c.finalize_ctx->>'workflow_run_id' = ANY(:ids) ORDER BY c.created_at"),
        {"p": project_id, "ids": run_ids}).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        row = dict(r)
        # The pull request waiting on a person's merge (the build's own decision).
        row["merge_open"] = bool(r["decision_id"] and r["decision_status"] in OPEN_STATUSES)
        out.setdefault(str(r["wfr"]), []).append(row)
    return out


def _cost(connection: Connection, build_ids: list[str]) -> dict[str, int]:
    """Money (micros) per build from llm_calls (a subscription's calls are $0)."""
    from ..included_spend import LLM_MONEY_SQL

    if not build_ids:
        return {}
    rows = connection.execute(text(
        f"SELECT code_task_run_id AS b, sum({LLM_MONEY_SQL}) AS m FROM llm_calls "
        "WHERE code_task_run_id = ANY(:ids) GROUP BY code_task_run_id"),
        {"ids": build_ids}).mappings().all()
    return {str(r["b"]): int(r["m"] or 0) for r in rows}


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


def _run_items(connection: Connection, project_id: str, limit: int,
               roster: Mapping[str, Mapping[str, Any]], now: datetime) -> list[dict[str, Any]]:
    from ..command_center import step_agent

    runs = [dict(r) for r in connection.execute(text(
        "SELECT r.id, r.status, r.created_at, r.updated_at, r.completed_at, r.workflow_version_id, "
        "r.input->>'title' AS title, r.input->>'directive' AS directive, "
        "r.input->>'work_kind' AS work_kind, r.error, r.blocked_reason, d.name AS workflow, "
        "d.workflow_key FROM workflow_runs r JOIN workflow_definitions d ON "
        "d.id = r.workflow_definition_id WHERE r.project_id = :p AND d.namespace = 'product' "
        "AND COALESCE(r.mode, 'normal') <> 'test' ORDER BY r.created_at DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()]
    if not runs:
        return []
    ids = [str(r["id"]) for r in runs]
    specs = _versions(connection, (r["workflow_version_id"] for r in runs))
    steps = _steps(connection, ids)
    builds = _builds(connection, project_id, ids)
    all_builds = [str(b["id"]) for bs in builds.values() for b in bs]
    costs = _cost(connection, all_builds)
    deploys = _deploys(connection, project_id, ids)
    previews = _previews(connection, all_builds)
    out = []
    for r in runs:
        rid = str(r["id"])
        spec = specs.get(str(r["workflow_version_id"]), {})
        run_steps = steps.get(rid, {})
        run_builds = builds.get(rid, [])
        pr_url = next((s["output"].get("pull_request_url") for s in run_steps.values()
                       if s["output"].get("pull_request_url")), None) or next(
            (b["pull_request_url"] for b in reversed(run_builds) if b["pull_request_url"]), None)
        gate = next((s for s in run_steps.values() if s["gate_open"]), None)
        merge = next((b for b in reversed(run_builds) if b.get("merge_open")), None)
        status = str(r["status"])
        state = run_state(status, gate is not None or merge is not None)
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
        build = run_builds[-1] if run_builds else None
        deploy = deploys.get(rid)
        cost = sum(costs.get(str(b["id"]), 0) for b in run_builds)
        number = pr_number(pr_url) if pr_url else None
        title = r["title"] or r["workflow"]
        out.append({
            "id": rid, "source": "workflow_run", "label": words(title, 6), "title": title,
            "kind": kind_of(r["workflow_key"], r["work_kind"]), "state": state, "status": status,
            "age": age(r["created_at"], now), "created_at": iso(r["created_at"]),
            "updated_at": iso(r["updated_at"]),
            "stages": _with_merge(stages(spec, run_steps, has_pr=bool(pr_url) and merge is None,
                                         finished=finished), merge is not None),
            "agents": faces[:4], "cost_usd": round(cost / 1_000_000, 2),
            "links": {
                "workflow_run": href(project_id, area="delivery_workflows", wfr=rid),
                "build": href(project_id, area="code_tasks", view="apps", build=build["id"])
                if build else None,
                "pr": {"number": number, "external_url": pr_url} if pr_url else None,
                "deploy": href(project_id, area="infra", view="deploys", deploy=deploy["id"])
                if deploy else None,
                "preview": next((previews[str(b["id"])] for b in reversed(run_builds)
                                 if str(b["id"]) in previews), None),
                "decision": href(project_id, area="decisions",
                                 decision=(gate or {}).get("decision_id") or merge["decision_id"])
                if (gate or merge) else None,
            },
            "decision_ids": [d for d in ((gate or {}).get("decision_id"),
                                         (merge or {}).get("decision_id")) if d],
            "detail": (r["directive"] or r["error"] or r["blocked_reason"] or r["workflow"]),
            "workflow": r["workflow"],
            "href": project_href(project_id, tab="work", drawer=f"run:{rid}"),
        })
    return out


def _build_items(connection: Connection, project_id: str, limit: int,
                 roster: Mapping[str, Mapping[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Builds started on their own (no workflow run): a code task is work too."""
    from ..code_task_service import _internal_run_prefixes  # noqa: SLF001 — the one definition

    params: dict[str, Any] = {"p": project_id, "n": limit}
    clauses = []
    for i, prefix in enumerate(_internal_run_prefixes()):
        clauses.append(f"lower(coalesce(title, '')) NOT LIKE :pre{i}")
        params[f"pre{i}"] = f"{prefix}%"
    from ..decision_actions import OPEN_STATUSES

    rows = [dict(r) for r in connection.execute(text(
        "SELECT c.id, c.status, c.phase, c.title, c.pull_request_url, c.pull_request_number, "
        "c.decision_id, dd.status AS decision_status, c.error, c.created_at, c.updated_at "
        "FROM code_task_runs c LEFT JOIN decisions dd ON dd.id = c.decision_id "
        "WHERE c.project_id = :p AND COALESCE(c.finalize_ctx->>'workflow_run_id', '') = '' "
        + ("AND " + " AND ".join(clauses).replace("coalesce(title", "coalesce(c.title") + " "
           if clauses else "")
        + "ORDER BY c.created_at DESC LIMIT :n"), params).mappings().all()]
    ids = [str(r["id"]) for r in rows]
    costs = _cost(connection, ids)
    previews = _previews(connection, ids)
    builder = face(roster, "engineering_captain")
    out = []
    phases = ("queued", "provisioning", "coding", "validating", "opening_pr", "done")
    for r in rows:
        status, phase = str(r["status"] or ""), str(r["phase"] or "queued")
        if status in ("failed", "blocked"):
            state = "failed"
        elif status in ("completed", "cancelled"):
            state = "done"
        elif status == "queued":
            state = "planned"
        else:
            state = "running"
        pr_url = r["pull_request_url"]
        merge_open = bool(r["decision_id"] and r["decision_status"] in OPEN_STATUSES)
        if merge_open and state not in ("failed",):
            state = "needs_you"
        at = phases.index(phase) if phase in phases else 0

        def st(key: str, first: int, last: int) -> str:
            if state == "failed" and first <= at <= last:
                return "failed"
            if at > last or state == "done":
                return "done"
            if first <= at <= last:
                return "now"
            return "todo"

        strip = [{"key": "plan", "label": "P", "name": "Plan", "state": "skipped"},
                 {"key": "design", "label": "D", "name": "Decide", "state": "skipped"},
                 {"key": "build", "label": "B", "name": "Build", "state": st("build", 0, 2)},
                 {"key": "review", "label": "R", "name": "Review", "state": st("review", 3, 3)},
                 {"key": "pr", "label": "PR", "name": "PR",
                  "state": "waiting" if merge_open else "done" if pr_url else st("pr", 4, 4)},
                 {"key": "ship", "label": "S", "name": "Ship", "state": "skipped"}]
        title = r["title"] or "Build"
        out.append({
            "id": str(r["id"]), "source": "code_task", "label": words(title, 6), "title": title,
            "kind": kind_of(None), "state": state, "status": status,
            "age": age(r["created_at"], now), "created_at": iso(r["created_at"]),
            "updated_at": iso(r["updated_at"]), "stages": strip,
            "agents": [builder] if builder else [],
            "cost_usd": round(costs.get(str(r["id"]), 0) / 1_000_000, 2),
            "links": {"workflow_run": None,
                      "build": href(project_id, area="code_tasks", view="apps", build=r["id"]),
                      "pr": ({"number": r["pull_request_number"] or pr_number(pr_url),
                              "external_url": pr_url} if pr_url else None),
                      "deploy": None, "preview": previews.get(str(r["id"])),
                      "decision": href(project_id, area="decisions", decision=r["decision_id"])
                      if merge_open else None},
            "decision_ids": [r["decision_id"]] if merge_open else [],
            "detail": r["error"] or title,
            "href": project_href(project_id, tab="work", drawer=f"build:{r['id']}"),
        })
    return out


def _planned_items(connection: Connection, project_id: str, now: datetime
                   ) -> list[dict[str, Any]]:
    """Roadmap items a person accepted (§6) that no run has picked up yet: planned work."""
    rows = connection.execute(text(
        "SELECT work_item_id, title, why, kind, decided_at, accepted_as FROM ambient_recommendations "
        "WHERE project_id = :p AND status = 'accepted' AND COALESCE(artifact->>'workflow_run_id', "
        "'') = '' ORDER BY decided_at DESC NULLS LAST LIMIT 20"), {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        kind = "bug" if r["kind"] in ("hardening", "design_fix") else (
            "chore" if r["kind"] in ("refactor", "cost", "process", "test_gap") else "feature")
        out.append({
            "id": str(r["work_item_id"]), "source": "roadmap", "label": words(r["title"], 6),
            "title": r["title"], "kind": kind, "state": "planned", "status": "accepted",
            "age": age(r["decided_at"], now), "created_at": iso(r["decided_at"]),
            "updated_at": iso(r["decided_at"]),
            "stages": [{"key": k, "label": letter, "name": STATION_LABELS[k], "state": "todo"}
                       for k, letter, _ in STATIONS],
            "agents": [], "cost_usd": 0.0,
            "links": {"workflow_run": None, "build": None, "pr": None, "deploy": None,
                      "preview": None, "decision": None},
            "decision_ids": [],
            "detail": r["why"] or r["title"],
            "href": project_href(project_id, tab="roadmap", drawer=f"rec:{r['work_item_id']}"),
        })
    return out


def everything(connection: Connection, project_id: str, *, limit: int = 60,
               now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every WorkItem, newest first."""
    now = now or datetime.now(timezone.utc)
    roster = agents(connection, project_id)
    found = (_run_items(connection, project_id, limit, roster, now)
             + _build_items(connection, project_id, limit, roster, now)
             + _planned_items(connection, project_id, now))
    found.sort(key=lambda w: w.get("created_at") or "", reverse=True)
    return found[:limit]


def matching(found: list[dict[str, Any]], *, state: Optional[str] = None,
             q: Optional[str] = None) -> list[dict[str, Any]]:
    """Filtered by `state` (one or more work_states, comma-separated) and `q` (words or a PR
    number)."""
    if state:
        wanted = {s.strip() for s in str(state).split(",") if s.strip()}
        found = [w for w in found if w["state"] in wanted]
    if q:
        needle = str(q).strip().lower().lstrip("#")
        found = [w for w in found if needle in (w["title"] or "").lower()
                 or needle in str(w.get("detail") or "").lower()
                 or needle == str((w["links"].get("pr") or {}).get("number") or "")]
    return found


def items(connection: Connection, project_id: str, *, state: Optional[str] = None,
          q: Optional[str] = None, limit: int = 60, now: Optional[datetime] = None
          ) -> list[dict[str, Any]]:
    return matching(everything(connection, project_id, limit=limit, now=now), state=state, q=q)


def counts(found: list[dict[str, Any]]) -> dict[str, int]:
    out = {s: 0 for s in ("running", "needs_you", "planned", "done", "failed")}
    for w in found:
        out[w["state"]] = out.get(w["state"], 0) + 1
    return out


def view(connection: Connection, project_id: str, *, state: Optional[str] = None,
         q: Optional[str] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    found = everything(connection, project_id, now=now)
    return {"items": matching(found, state=state, q=q), "counts": counts(found), "state": state or None, "q": q or None,
            "new_work": {"href": f"/v1/projects/{project_id}/work",
                         "kinds": ["feature", "bug", "ticket"]}}


__all__ = ["STATIONS", "counts", "everything", "items", "matching", "kind_of", "run_state", "stages", "view"]
