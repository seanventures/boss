"""ONE PIECE OF WORK, STEP BY STEP (screen 05b, the work drawer): `GET …/work/{id}/story`.

The Work row (work.py) carries the six stations as letters; the drawer tells how each went in a
few words, with the link that proves it:

  Plan           "PRD agreed" · PRD (the panel's meeting)
  You approved   who and when (the spec / plan decision)
  Build          "7 files changed" · Diff (the pull request's files)
  Review & test  "Tests pass · 2 review rounds · 1 finding deferred" · Findings (the build)
  Merge          "Only a person merges" / "Merged" · PR #3 ↗
  Staging        "Auto after merge" / "Live" · the deploy

plus the run's number in its project ("Run #4"), how long it took, the preview and the cost
split Plan · Build · Review (the build's recorded stage costs). Facts on record only: never a
model call, never a GitHub call. Times are ISO; the console words them in the viewer's zone.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import as_json, aware, href, iso, pr_number

#: A build's recorded stage costs (state.stage_costs keys) → the drawer's three cost parts.
_COST_PART = {"plan": "Plan", "design": "Plan", "refine": "Plan", "spec": "Plan",
              "implement": "Build", "write_tests": "Build", "check_tests": "Build",
              "repair": "Build", "build": "Build", "more_tests": "Build",
              "review_code": "Review", "security_check": "Review",
              "consistency_check": "Review", "adversarial": "Review", "review": "Review"}

#: How each station reads in the drawer (screen 05b's words).
NAMES = {"plan": "Plan", "design": "You approved", "build": "Build", "review": "Review & test",
         "pr": "Merge", "ship": "Staging"}


def _who(connection: Connection, actor: Any) -> Optional[str]:
    if not actor:
        return None
    from ..people_names import who

    return who(actor, connection)


def _builds(connection: Connection, project_id: str, item: Mapping[str, Any]) -> list[dict]:
    """The item's build attempts, oldest first (the run's own, or the standalone group)."""
    ids = [str(a.get("id")) for a in item.get("attempts") or [] if a.get("id")]
    if item.get("source") == "code_task" and str(item.get("id")) not in ids:
        ids.append(str(item["id"]))
    rows = connection.execute(text(
        "SELECT id, status, title, state, finalize_ctx, pull_request_url, pull_request_number, "
        "decision_id, created_at, updated_at FROM code_task_runs WHERE project_id = :p AND (id = ANY(:ids) OR "
        "finalize_ctx->>'workflow_run_id' = :r) ORDER BY created_at"),
        {"p": project_id, "ids": ids, "r": str(item.get("id"))}).mappings().all()
    return [dict(r) for r in rows]


def _steps(connection: Connection, run_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT DISTINCT ON (step_id) step_id, status, output, decision_id, started_at, "
        "completed_at FROM workflow_step_runs WHERE workflow_run_id = :r "
        "ORDER BY step_id, iteration DESC, created_at DESC"), {"r": run_id}).mappings().all()
    return {str(r["step_id"]): {**dict(r), "output": as_json(r["output"], {}) or {}} for r in rows}


def _decision(connection: Connection, decision_id: Any) -> Optional[dict[str, Any]]:
    if not decision_id:
        return None
    row = connection.execute(text(
        "SELECT id, status, approved_by, approved_at, updated_at FROM decisions WHERE id = :d"),
        {"d": decision_id}).mappings().first()
    return dict(row) if row else None


def _run_number(connection: Connection, project_id: str, item: Mapping[str, Any]) -> Optional[int]:
    if item.get("source") != "workflow_run":
        return None
    return connection.execute(text(
        "SELECT count(*) FROM workflow_runs WHERE project_id = :p AND created_at <= "
        "(SELECT created_at FROM workflow_runs WHERE id = :r)"),
        {"p": project_id, "r": item["id"]}).scalar()


def _cost_parts(builds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts = {"Plan": 0, "Build": 0, "Review": 0}
    for b in builds:
        costs = (as_json(b.get("state"), {}) or {}).get("stage_costs") or {}
        for key, micros in costs.items() if isinstance(costs, dict) else []:
            part = _COST_PART.get(str(key))
            if part is None:
                part = "Review" if "review" in str(key) or "check" in str(key) else "Build"
            try:
                parts[part] += int(micros or 0)
            except (TypeError, ValueError):
                continue
    return [{"label": k, "usd": round(v / 1_000_000, 2)} for k, v in parts.items() if v]


def story(connection: Connection, project_id: str, work_id: str) -> dict[str, Any]:
    """The work drawer's story for a workflow run, a build, or one attempt of either."""
    from . import work as work_service

    items = work_service.everything(connection, project_id)
    item = next((w for w in items if str(w["id"]) == work_id), None)
    if item is None:
        item = next((w for w in items for a in w.get("attempts") or []
                     if str(a.get("id")) == work_id), None)
    if item is None:
        raise LookupError(f"There is no work {work_id} in this project.")
    builds = _builds(connection, project_id, item)
    latest = builds[-1] if builds else {}
    state = as_json(latest.get("state"), {}) or {}
    fctx = as_json(latest.get("finalize_ctx"), {}) or {}
    steps = _steps(connection, str(item["id"])) if item.get("source") == "workflow_run" else {}
    outputs = [s["output"] for s in steps.values()]
    pr = (item.get("links") or {}).get("pr") or {}
    pr_url = pr.get("external_url") or latest.get("pull_request_url")
    number = pr.get("number") or latest.get("pull_request_number") or pr_number(pr_url)

    meeting = next((o for o in outputs if o.get("meeting_run_id")), {})
    spec_dec = _decision(connection, meeting.get("spec_decision_id")) or next(
        (_decision(connection, s["decision_id"]) for s in steps.values()
         if s.get("decision_id") and s["output"].get("capability") in ("spec_review",
                                                                        "architecture")), None)
    merge = fctx.get("merge") if isinstance(fctx.get("merge"), dict) else {}
    deploy = next((o for o in outputs if o.get("stack_deployment_id")
                   and o.get("deployment_status") == "healthy"), None) or next(
        (o for o in outputs if o.get("stack_deployment_id")), None)
    rounds = fctx.get("review_rounds") if isinstance(fctx.get("review_rounds"), list) else []
    deferred = state.get("deferred_findings") if isinstance(state.get("deferred_findings"),
                                                          list) else []
    files = state.get("files_changed")

    rows: dict[str, dict[str, Any]] = {}
    rows["plan"] = {"sub": "PRD agreed" if meeting.get("prd_markdown") else "Planned",
                    "link": ({"label": "PRD", "href": href(project_id, area="meetings",
                                                           meeting=meeting["meeting_run_id"])}
                             if meeting.get("meeting_run_id") else None)}
    approver = _who(connection, (spec_dec or {}).get("approved_by") or meeting.get("spec_approved_by"))
    rows["design"] = {"sub": approver or "", "at": iso((spec_dec or {}).get("approved_at")),
                      "person": True,
                      "link": ({"label": "Decision", "href": href(project_id, area="decisions",
                                                                  decision=spec_dec["id"])}
                               if spec_dec else None)}
    from ..local_repo_host import is_local_pr_url as _is_local_pr
    from ..repo_links import pr_link as _pr_link

    build_sub = (f"{files} file{'s' if files != 1 else ''} changed"
                 if isinstance(files, int) else "")
    rows["build"] = {"sub": build_sub,
                     # A local change set's diff is on BOSS's change-set view (BOSSAPP-919).
                     "link": ({"label": "Diff", "external_url": (
                         _pr_link(pr_url) if _is_local_pr(pr_url) else f"{pr_url}/files")}
                              if pr_url else None)}
    review_bits = []
    if state.get("tests_passed") is True:
        review_bits.append("tests pass")
    elif state.get("tests_passed") is False:
        review_bits.append("tests failing")
    if rounds:
        review_bits.append(f"{len(rounds)} review round{'s' if len(rounds) != 1 else ''}")
    if deferred:
        review_bits.append(f"{len(deferred)} finding{'s' if len(deferred) != 1 else ''} deferred")
    review_sub = " · ".join(review_bits)
    rows["review"] = {"sub": review_sub[:1].upper() + review_sub[1:],
                      "link": ({"label": "Findings", "href": (item.get("links") or {}).get("build")}
                               if (item.get("links") or {}).get("build") and (rounds or deferred)
                               else None)}
    # WHO APPROVED, WHEREVER A PERSON DID (05b-09): the merge (the build's own pull-request
    # decision) and a deploy a person approved read "<who> approved", with the moment, beside
    # the person's face — as the plan's approval does.
    merge_dec = _decision(connection, latest.get("decision_id"))
    merger = _who(connection, (merge_dec or {}).get("approved_by")) if merge.get("sha") else None
    rows["pr"] = {"sub": (f"{merger} approved · merged" if merger else "Merged")
                  if merge.get("sha") else "Only a person merges",
                  "at": iso(merge.get("at") or merge.get("merged_at")
                            or (merge_dec or {}).get("approved_at")), "person": True,
                  "link": ({"label": f"PR #{number}", "external_url": pr_url} if pr_url else None)}
    deploy_dec = next((d for d in (_decision(connection, st.get("decision_id"))
                                   for st in steps.values() if st.get("decision_id")
                                   and (st["output"].get("stack_deployment_id")
                                        or "deploy" in str(st.get("step_id") or "")))
                       if d and d.get("approved_by")), None)
    deployer = _who(connection, (deploy_dec or {}).get("approved_by"))
    live = ("Live" if (deploy or {}).get("deployment_status") == "healthy" else
            "Waiting to deploy" if deploy else "Auto after merge")
    rows["ship"] = {"sub": f"{deployer} approved · {live.lower()}" if deployer else live,
                    "at": iso((deploy_dec or {}).get("approved_at")) if deployer else None,
                    "person": bool(deployer),
                    "link": ({"label": "Deploy", "href": href(
                        project_id, area="infra", view="deploys",
                        deploy=deploy["stack_deployment_id"])} if deploy else None)}

    out_stages = []
    for st in item.get("stages") or []:
        if st.get("state") == "skipped":
            continue
        extra = rows.get(st["key"], {})
        out_stages.append({"key": st["key"], "name": NAMES.get(st["key"], st.get("name")),
                           "state": st["state"], "sub": extra.get("sub") or "",
                           "at": extra.get("at"), "person": bool(extra.get("person")),
                           "link": extra.get("link")})
    started = aware(builds[0]["created_at"]) if builds else None
    ended = aware(latest.get("updated_at")) if latest else None
    minutes = (round((ended - started).total_seconds() / 60) if started and ended
               and ended > started else None)
    return {"id": item["id"], "label": item.get("label"), "title": item.get("title"),
            "kind": item.get("kind"), "state": item.get("state"),
            "number": _run_number(connection, project_id, item), "minutes": minutes,
            "cost_usd": item.get("cost_usd"), "cost_parts": _cost_parts(builds),
            "pr": {"number": number, "external_url": pr_url} if pr_url else None,
            "preview": (item.get("links") or {}).get("preview"),
            "stages": out_stages}


__all__ = ["NAMES", "story"]
