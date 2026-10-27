"""One change across N repositories: a change set.

    planned ─► building ─► in_review ─► approved ─► merging ─► merged ─► deploying ─► live
                  │  ▲          │  ▲                    │                     │
                  ▼  │          ▼  │ (a push re-asks)   ▼                     ▼
               blocked      blocked                 failed (reverted)    rolled_back

PLAN: the planner (a model on the project's route) writes what changes in each component; a
component with nothing to change gets no PR (`unchanged`). The plan is recorded on the set.

BUILD: each touched repository is a LANE: an ordinary governed build (its own sandbox, its own
run token naming that one repository, its own gates and adversarial review, its own PR) started
with `finalize_ctx.change_set_id`. Lanes run in WAVES of the components' dependency graph: a lane
starts when every lane it depends on has opened its PR with its gates green, and its brief names
the branch and head SHA of each dependency to build against. Independent lanes run together.
Every lane's build skips its own merge decision: the set has one.

ONE REPO FAILING BLOCKS THE SET (build, gates, review, cross-repo check): nothing asks for
approval and no further lane starts. A retry of the failed build (Factory's Retry) re-points the
lane at the new attempt and the set moves again.

REVIEW: when every lane is ready, BOSS links the PRs (each description lists the set) and runs the
cross-repo check (cross_check.py). Only then does it ask for ONE approval, pinned to the exact
head SHA of every PR (the decision's content hash). Any push to any PR of the set — before or after
the approval — re-runs the check and asks again.

MERGE: in dependency order, each with a required-SHA merge; heads are re-read first. If one merge
fails, BOSS reverts the merges that already landed (revert PRs, merged under the same approval)
and the set is `failed`: never half-merged.

A SINGLE-REPO RUN is a one-PR set (mode `single`) recorded beside its own pipeline, so Factory's
"Recent changes" reads one table; its approval and merge stay exactly what they were.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..services.core import make_id
from .common import MultiRepoError, event, iso, jdump, jload, norm_repo, org_of_project, sql

log = logging.getLogger(__name__)

OPEN = ("planned", "building", "blocked", "in_review", "approved", "merging", "deploying")
DONE = ("merged", "live", "rolled_back", "failed", "rejected", "cancelled")
ACTION = "approve_change_set"

#: The lane's five steps, and which build phases belong to each (code_task_runs.phase).
LANE_STEPS = (("sandbox", "Sandbox"), ("code", "Code"), ("tests", "Tests"), ("review", "Review"), ("pr", "PR"))
_PHASE_STEP = {
    "queued": 0, "provisioning": 0,
    "design": 1, "write_tests": 1, "implement": 1, "coding": 1, "iac_verify": 1, "plan": 1,
    "awaiting_plan_approval": 1,
    "run_tests": 2, "check_tests": 2, "test_arbitration": 2, "serve_check": 2, "validating": 2,
    "fixture_repair": 2,
    "review_code": 3, "review_panel": 3, "consistency_check": 3, "awaiting_gate_override": 3,
    "opening_pr": 4, "waiting_for_ci": 4,
}

PlanModel = Callable[[Any, Any, str, str, str], str]


# ── reading ────────────────────────────────────────────────────────────────────────────────────

def _row(db: Any, cs_id: str, *, lock: bool = False) -> dict[str, Any]:
    row = db.execute(sql("SELECT * FROM repo_change_sets WHERE id = :i" + (" FOR UPDATE" if lock else "")),
                     {"i": cs_id}).mappings().first()
    if row is None:
        raise MultiRepoError(404, "unknown_change_set", "No such change.")
    return dict(row)


def _lanes(db: Any, cs_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(sql(
        "SELECT * FROM change_set_prs WHERE change_set_id = :c ORDER BY wave, full_name"), {"c": cs_id}).mappings().all()]


def member_change_set(db: Any, code_task_run_id: Optional[str]) -> Optional[str]:
    """The MULTI-repo set a build belongs to (its lane), or None."""
    if not code_task_run_id:
        return None
    try:
        with db.begin_nested():
            row = db.execute(sql(
                "SELECT s.id FROM change_set_prs p JOIN repo_change_sets s ON s.id = p.change_set_id "
                "WHERE p.code_task_run_id = :r AND s.mode = 'multi' LIMIT 1"), {"r": code_task_run_id}).scalar()
            if row is None:
                fctx = db.execute(sql("SELECT finalize_ctx->>'change_set_id' FROM code_task_runs WHERE id = :r"),
                                  {"r": code_task_run_id}).scalar()
                if fctx:
                    row = db.execute(sql("SELECT id FROM repo_change_sets WHERE id = :c AND mode = 'multi'"),
                                     {"c": fctx}).scalar()
    except Exception:  # noqa: BLE001 — before change sets exist
        return None
    return str(row) if row else None


def for_run(db: Any, code_task_run_id: str) -> Optional[str]:
    """Any set (single or multi) a build belongs to."""
    try:
        with db.begin_nested():
            row = db.execute(sql("SELECT change_set_id FROM change_set_prs WHERE code_task_run_id = :r "
                                 "ORDER BY updated_at DESC LIMIT 1"), {"r": code_task_run_id}).scalar()
    except Exception:  # noqa: BLE001
        return None
    return str(row) if row else None


def _short(full_name: str) -> str:
    return full_name.split("/")[-1]


def recent_for_app(db: Any, project_id: str, app_key: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = db.execute(sql("SELECT id, title, state, mode, created_at, updated_at FROM repo_change_sets "
                          "WHERE project_id = :p AND app_key = :a ORDER BY created_at DESC LIMIT :n"),
                      {"p": project_id, "a": app_key, "n": limit}).mappings().all()
    out = []
    for r in rows:
        lanes = [lane for lane in _lanes(db, r["id"]) if lane["lane_state"] != "unchanged"]
        out.append({"id": r["id"], "title": r["title"], "state": r["state"], "mode": r["mode"],
                    "repos": [_short(lane["full_name"]) for lane in lanes],
                    "prs": sum(1 for lane in lanes if lane.get("pr_number")),
                    "run_id": next((lane["code_task_run_id"] for lane in lanes if lane.get("code_task_run_id")), None),
                    "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"])})
    return out


def _lane_steps(lane: dict[str, Any], run: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    state = lane["lane_state"]
    if state in ("ready", "merged", "reverted") or (run and run.get("status") == "completed"):
        reached, current = 5, None
    elif state in ("waiting", "unchanged") or run is None:
        reached, current = 0, None
    else:
        idx = _PHASE_STEP.get(str(run.get("phase") or "queued"), 1)
        reached, current = idx, idx
    out = []
    for i, (key, label) in enumerate(LANE_STEPS):
        if i < reached:
            s = "done"
        elif current is not None and i == current:
            s = "failed" if state == "failed" else "active"
        else:
            s = "waiting"
        out.append({"key": key, "label": label, "state": s})
    return out


def _run_rows(db: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    return {r["id"]: dict(r) for r in db.execute(sql(
        "SELECT id, status, phase, error, title, finalize_ctx, usage, created_at, updated_at, "
        "pull_request_number, pull_request_url FROM code_task_runs WHERE id = ANY(:i)"), {"i": ids}).mappings().all()}


def _findings(run: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    fctx = jload((run or {}).get("finalize_ctx"), {}) or {}
    out = []
    for c in (fctx.get("review_comments") or [])[:12]:
        if isinstance(c, dict):
            out.append({"severity": c.get("severity"), "body": str(c.get("body") or "")[:400],
                        "path": c.get("path"), "line": c.get("line"), "author": c.get("author"),
                        "status": c.get("status"), "stage": c.get("stage")})
    return out


def view(db: Any, cs_id: str) -> dict[str, Any]:
    """Everything the cockpit, the linked-PRs view, the approval card and the deploy plan read."""
    from . import grants

    cs = _row(db, cs_id)
    lanes = _lanes(db, cs_id)
    runs = _run_rows(db, [lane["code_task_run_id"] for lane in lanes if lane.get("code_task_run_id")])
    plan = jload(cs.get("plan"), {}) or {}
    by_name = {c["name"]: c for c in (plan.get("components") or []) if isinstance(c, dict)}
    deps = _repo_deps(db, cs)
    lane_views = []
    cost = 0
    for i, lane in enumerate(lanes):
        run = runs.get(lane.get("code_task_run_id") or "")
        cost += int(((jload((run or {}).get("usage"), {}) or {}).get("cost_usd_micros")) or 0)
        waiting_for = [_short(d) for d in deps.get(lane["full_name"], [])
                       if next((x for x in lanes if x["full_name"] == d and x["lane_state"] not in ("ready", "unchanged",
                                                                                                  "merged")), None)]
        lane_views.append({
            "id": lane["id"], "order": i + 1, "full_name": lane["full_name"], "components": list(lane["components"] or []),
            "wave": lane["wave"], "lane_state": lane["lane_state"], "steps": _lane_steps(lane, run),
            "code_task_run_id": lane.get("code_task_run_id"), "phase": (run or {}).get("phase"),
            "pr_number": lane.get("pr_number"), "pr_url": lane.get("pr_url"), "branch": lane.get("branch"),
            "base_branch": lane.get("base_branch"), "head_sha": lane.get("head_sha"),
            "additions": lane.get("additions"), "deletions": lane.get("deletions"),
            "changed_files": lane.get("changed_files"), "ci_state": lane.get("ci_state"),
            "review_state": lane.get("review_state"), "findings": _findings(run),
            "waiting_for": waiting_for, "error": lane.get("error") or (run or {}).get("error"),
            "merged_sha": lane.get("merged_sha"), "revert_pr_number": lane.get("revert_pr_number"),
            "plan": [by_name[c] for c in (lane["components"] or []) if c in by_name],
        })
    check = latest_check(db, cs_id)
    decision = None
    if cs.get("decision_id"):
        d = db.execute(sql("SELECT id, status, approved_by, approved_at, created_at FROM decisions WHERE id = :i"),
                       {"i": cs["decision_id"]}).mappings().first()
        decision = {k: (iso(v) if k.endswith("_at") else v) for k, v in dict(d).items()} if d else None
    touched = [lv for lv in lane_views if lv["lane_state"] != "unchanged"]
    built = sum(1 for lv in touched if lv["lane_state"] in ("ready", "merged"))
    started = cs.get("created_at")
    minutes = int(((datetime.now(timezone.utc) - started).total_seconds()) // 60) if started else 0
    return {
        "id": cs["id"], "project_id": cs["project_id"], "app_key": cs["app_key"],
        "app_name": cs.get("app_name") or cs["app_key"], "title": cs["title"], "mode": cs["mode"],
        "state": cs["state"], "blocked_reason": cs.get("blocked_reason"), "error": cs.get("error"),
        "stage": _stage(cs, touched, check, built),
        "lanes": lane_views, "plan": plan, "cross_check": check,
        "grant": grants.get_for_change_set(db, cs_id), "decision": decision,
        "approved_shas": jload(cs.get("approved_shas"), None), "approved_by": cs.get("approved_by"),
        "approved_at": iso(cs.get("approved_at")), "release": jload(cs.get("release"), {}) or {},
        "cost_usd": round(cost / 1_000_000, 2), "minutes": minutes,
        "workflow_run_id": cs.get("workflow_run_id"),
        "created_at": iso(cs.get("created_at")), "updated_at": iso(cs.get("updated_at")),
    }


def _stage(cs: dict[str, Any], touched: list[dict[str, Any]], check: Optional[dict[str, Any]], built: int) -> dict[str, Any]:
    order = ["plan", "build", "review", "approve", "deploy"]
    labels = {"plan": "Plan", "build": "Build", "review": "Review set", "approve": "You approve",
              "deploy": "Deploy together"}
    state = cs["state"]
    current = {"planned": "plan", "building": "build", "blocked": None, "in_review": "approve",
               "approved": "deploy", "merging": "deploy", "merged": "deploy", "deploying": "deploy",
               "live": None, "rolled_back": "deploy", "failed": None, "rejected": "approve",
               "cancelled": None}.get(state, "build")
    if state == "blocked":
        current = "review" if built == len(touched) and touched else "build"
    if state in ("live",):
        done_upto = 5
    elif state == "failed":
        done_upto = order.index("deploy") if cs.get("approved_at") else order.index("build")
        current = order[done_upto]
    else:
        done_upto = order.index(current) if current in order else 0
    steps = []
    for i, key in enumerate(order):
        if i < done_upto:
            s = "done"
        elif key == current:
            s = "failed" if state in ("blocked", "failed", "rolled_back", "rejected") else "active"
        else:
            s = "waiting"
        label = labels[key]
        if key == "build" and touched:
            label = f"Build · {built} of {len(touched)}"
        steps.append({"key": key, "label": label, "state": s})
    return {"current": current, "steps": steps}


def latest_check(db: Any, cs_id: str) -> Optional[dict[str, Any]]:
    row = db.execute(sql("SELECT * FROM cross_repo_checks WHERE change_set_id = :c ORDER BY at DESC LIMIT 1"),
                     {"c": cs_id}).mappings().first()
    if row is None:
        return None
    results = jload(row["results"], []) or []
    return {"id": row["id"], "verdict": row["verdict"], "results": results, "head_shas": jload(row["head_shas"], {}),
            "passed": sum(1 for r in results if r.get("pass")), "total": len(results), "at": iso(row["at"])}


# ── starting ───────────────────────────────────────────────────────────────────────────────────

def _repo_deps(db: Any, cs: dict[str, Any]) -> dict[str, list[str]]:
    """Repository-level dependencies from the components (a repo depends on the repos of the
    components its components depend on)."""
    from . import components

    app = components.get(db, cs["project_id"], cs["app_key"])
    repo_of = {c["name"]: c["repository"] for c in app["components"]}
    out: dict[str, list[str]] = {}
    for c in app["components"]:
        for d in c["depends_on"]:
            dep = repo_of.get(d)
            if dep and norm_repo(dep) != norm_repo(c["repository"]):
                out.setdefault(c["repository"], [])
                if dep not in out[c["repository"]]:
                    out[c["repository"]].append(dep)
    return out


def _repo_waves(app: dict[str, Any]) -> dict[str, int]:
    """Each repository's wave: the earliest wave of any of its components (a monorepo builds once)."""
    wave_of: dict[str, int] = {}
    for i, layer in enumerate(app["waves"]):
        for name in layer:
            comp = next(c for c in app["components"] if c["name"] == name)
            wave_of.setdefault(comp["repository"], i + 1)
    return wave_of


def start(db: Any, settings: Any, jobs: Any, *, project_id: str, app_key: str, title: str, instructions: str,
          acceptance_criteria: Optional[list[str]] = None, test_commands: Optional[list[str]] = None,
          actor: str = "operator", workflow_run_id: Optional[str] = None, task_id: Optional[str] = None,
          finalize_extra: Optional[dict[str, Any]] = None, remember: bool = False) -> dict[str, Any]:
    """Start one change across the app's repositories. Asks for consent (409 consent_required with
    the `multirepo` details) when no grant of the person's covers EXACTLY the app's repositories."""
    from . import components, grants, registry

    app = components.get(db, project_id, app_key)
    if len(app["components"]) < 2:
        raise MultiRepoError(409, "not_multi", "This app is built from one repository; start an ordinary build.")
    org_id = org_of_project(db, project_id) or ""
    repos = grants.required_repos(db, project_id, app_key)
    refused = registry.mint_allowed(db, org_id, [r["full_name"] for r in repos])
    if refused:
        raise MultiRepoError(409, "not_allowed", f"{', '.join(refused)} {'is' if len(refused) == 1 else 'are'} not "
                                                 "allowed in Repo and models any more.")
    inactive = [c["repository"] for c in app["components"] + app["deploy_config"] if not c["repository_active"]]
    if inactive:
        raise MultiRepoError(409, "repository_removed", f"{', '.join(inactive)} was removed from this project.")
    running = components._running_change_sets(db, project_id, app_key)  # noqa: SLF001
    if running:
        raise MultiRepoError(409, "change_running", "A change is already being built on this app. Wait for it, or "
                                                    "stop it, before starting another.", change_set_id=running[0])
    cs_id = make_id("chs")
    acting = _authority(db, project_id=project_id, cs_id=cs_id, app_key=app_key, title=title,
                        workflow_run_id=workflow_run_id, repos=[r["full_name"] for r in repos])
    db.execute(sql(
        "INSERT INTO repo_change_sets (id, project_id, app_key, app_name, title, directive, mode, state, acting, "
        "workflow_run_id, task_id, options, created_by) VALUES (:id, :p, :a, :an, :t, :d, 'multi', 'planned', "
        "CAST(:acting AS JSONB), :wf, :task, CAST(:opt AS JSONB), :by)"),
        {"id": cs_id, "p": project_id, "a": app_key, "an": app["app_name"], "t": title[:300], "d": instructions,
         "acting": jdump(acting.to_json()) if acting is not None else None, "wf": workflow_run_id, "task": task_id,
         "opt": jdump({"acceptance_criteria": acceptance_criteria or [], "test_commands": test_commands or [],
                       "finalize_extra": finalize_extra or {}}), "by": actor})
    wave_of = _repo_waves(app)
    by_repo: dict[str, dict[str, Any]] = {}
    for c in app["components"]:
        lane = by_repo.setdefault(c["repository"], {"binding_id": c["binding_id"], "components": [],
                                                    "base": c["base_branch"]})
        lane["components"].append(c["name"])
    for full, lane in by_repo.items():
        db.execute(sql("INSERT INTO change_set_prs (id, change_set_id, binding_id, full_name, components, wave, "
                       "lane_state, base_branch) VALUES (:id, :c, :b, :f, :comps, :w, 'waiting', :base)"),
                   {"id": make_id("csp"), "c": cs_id, "b": lane["binding_id"], "f": full, "comps": lane["components"],
                    "w": wave_of.get(full, 1), "base": lane["base"]})
    grants.record(db, project_id=project_id, app_key=app_key, change_set_id=cs_id,
                  identity_grant_id=getattr(acting, "grant_id", None),
                  consented_by=getattr(acting, "principal_id", None) or actor, remember=remember)
    event(db, project_id, "change_set.started", actor, "change_set", cs_id,
          {"app_key": app_key, "repositories": sorted(by_repo), "workflow_run_id": workflow_run_id})
    return view(db, cs_id)


def _authority(db: Any, *, project_id: str, cs_id: str, app_key: str, title: str,
               workflow_run_id: Optional[str], repos: list[str]) -> Any:
    """Whose authority the set's builds run under. Inside a workflow run: the run's recorded grant
    (whose consent named the app's repositories). Otherwise the person starting it, under a grant
    that covers every repository of the app — or the consent sheet, with the multi-repo details."""
    from ..identity import consent
    from ..identity.lineage import Acting
    from . import grants

    if workflow_run_id:
        return consent.acting_for_workflow_run(db, workflow_run_id)
    try:
        acting = consent.require_authority(
            org_id=None, project_id=project_id, role_keys=[consent.DEFAULT_BUILDER],
            permissions=consent.CODE_TASK_PERMISSIONS, repos=repos, task=title, run_id=f"run_cs:{cs_id}", db=db)
    except consent.ConsentRequired as asked:
        asked.body["multirepo"] = grants.consent_details(db, project_id, app_key, run_label=title)
        raise
    if acting is None:
        return None
    return Acting(principal_id=acting.principal_id, org_id=acting.org_id, grant_id=acting.grant_id,
                  run_id=f"run_cs:{cs_id}")


def acting_for_lane(db: Any, cs_id: str) -> Any:
    """The authority a lane's build is started under: the set's, as `run_cs:<set>` (its builds
    are `run_cs:<set>/ct:<build>`, children of the set the grant was claimed for)."""
    from ..identity.lineage import Acting

    cs = _row(db, cs_id)
    if cs.get("workflow_run_id"):
        # A set a workflow's build step started: its lanes run under the workflow run's recorded
        # grant (whose consent named exactly the app's repositories) as run_wf:<run>/ct:<build>.
        from ..identity import consent

        return consent.acting_for_workflow_run(db, cs["workflow_run_id"])
    data = jload(cs.get("acting"), None)
    if not data:
        return None
    try:
        return Acting.from_json(data)
    except Exception:  # noqa: BLE001
        return None


# ── planning ───────────────────────────────────────────────────────────────────────────────────

def _default_model(db: Any, settings: Any, project_id: str, ref: str, prompt: str) -> str:
    from ..services import model_routing
    from ..services.model_router.router import RouteRequest

    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    purpose = RouteRequest(kind="panel", step="panel_facilitator", project_id=project_id, unit_id=ref,
                           role="architect", text=prompt[:6000], ref=ref)
    with model_routing.routing_purpose(purpose):
        adapter = model_routing.project_chat_adapter(db, settings, project_id)
    _ref, text_out, _usage = adapter.create_text_response(
        prompt, system_prompt="You plan and review changes that span several repositories. Answer with JSON only.")
    return str(text_out or "")


MODEL: PlanModel = _default_model


def _json_in(text_out: str) -> Any:
    text_out = str(text_out or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text_out, re.S)
    if fence:
        text_out = fence.group(1)
    start_at = min([i for i in (text_out.find("{"), text_out.find("[")) if i >= 0] or [0])
    try:
        return json.loads(text_out[start_at:])
    except ValueError:
        return None


def _plan_prompt(cs: dict[str, Any], app: dict[str, Any]) -> str:
    comps = "\n".join(f"- {c['name']}: {c['repository']}{'' if c['path'] == '/' else c['path']} "
                      f"(needs: {', '.join(c['depends_on']) or 'nothing'})" for c in app["components"])
    return (
        "BOSS MULTI-REPO PLAN\n"
        f"App: {app['app_name']}\nChange: {cs['title']}\n\nWhat was asked:\n{cs.get('directive') or cs['title']}\n\n"
        f"Components, in build order:\n{comps}\n\n"
        "For EACH component say what must change in it for this request, and the interface it adds or "
        "changes for the components that depend on it. A component that needs no change: unchanged true.\n"
        'Answer JSON: {"summary": "...", "components": [{"name": "<component>", "change": "<what changes>", '
        '"interface": "<what dependents can now use, or empty>", "unchanged": false}]}')


def make_plan(db: Any, settings: Any, cs: dict[str, Any]) -> dict[str, Any]:
    from . import components

    app = components.get(db, cs["project_id"], cs["app_key"])
    names = [c["name"] for c in app["components"]]
    try:
        raw = MODEL(db, settings, cs["project_id"], f"change_set:{cs['id']}", _plan_prompt(cs, app))
        parsed = _json_in(raw)
    except Exception as error:  # noqa: BLE001 — the plan falls back to "the whole ask, per component"
        log.warning("change set %s: planner failed: %s", cs["id"], type(error).__name__)
        parsed = None
    planned: dict[str, dict[str, Any]] = {}
    by = "model"
    if isinstance(parsed, dict) and isinstance(parsed.get("components"), list):
        for item in parsed["components"]:
            if isinstance(item, dict) and item.get("name") in names:
                planned[item["name"]] = {"name": item["name"], "change": str(item.get("change") or "")[:2000],
                                         "interface": str(item.get("interface") or "")[:1000],
                                         "unchanged": bool(item.get("unchanged"))}
    if not planned:
        by = "fallback"
    out = []
    for name in names:
        out.append(planned.get(name) or {
            "name": name, "unchanged": False, "interface": "",
            "change": f"The part of “{cs['title']}” that belongs in {name}."})
    if all(c["unchanged"] for c in out):
        for c in out:                           # a plan that changes nothing is not a plan: build all
            c["unchanged"] = False
        by = "fallback"
    return {"summary": (parsed or {}).get("summary") if isinstance(parsed, dict) else None,
            "components": out, "by": by, "at": iso(datetime.now(timezone.utc))}


# ── lanes ──────────────────────────────────────────────────────────────────────────────────────

def _lane_brief(db: Any, cs: dict[str, Any], lane: dict[str, Any], lanes: list[dict[str, Any]],
                plan: dict[str, Any], deps: dict[str, list[str]]) -> str:
    comps = {c["name"]: c for c in plan.get("components") or []}
    mine = [comps[c] for c in lane["components"] if c in comps]
    others = [x for x in lanes if x["full_name"] != lane["full_name"] and x["lane_state"] != "unchanged"]
    lines = [cs.get("directive") or cs["title"], "",
             f"THIS IS ONE PART OF A CHANGE ACROSS {len(others) + 1} REPOSITORIES (“{cs['title']}”). "
             f"You work ONLY in {lane['full_name']}; the other repositories are changed by other builds."]
    for c in mine:
        lines.append(f"- Component {c['name']}: {c['change']}")
        if c.get("interface"):
            lines.append(f"  Interface it must provide to the components that depend on it: {c['interface']}")
    dep_lanes = [x for x in lanes if x["full_name"] in deps.get(lane["full_name"], [])]
    if dep_lanes:
        lines.append("")
        lines.append("BUILD AGAINST THESE CHANGES (already built, gates green, not merged yet):")
        for d in dep_lanes:
            dc = [comps[c] for c in d["components"] if c in comps]
            desc = "; ".join(f"{c['name']}: {c.get('interface') or c['change']}" for c in dc)
            if d.get("pr_number"):
                lines.append(f"- {d['full_name']} PR #{d['pr_number']} on branch {d.get('branch')} at "
                             f"{(d.get('head_sha') or '')[:12]} — {desc}. Pin to that build "
                             f"(pre-release r{cs['id'][-6:]}) rather than the released version.")
            else:
                lines.append(f"- {d['full_name']}: unchanged in this change.")
    return "\n".join(lines)


def _start_lane(db: Any, settings: Any, jobs: Any, cs: dict[str, Any], lane: dict[str, Any],
                lanes: list[dict[str, Any]], plan: dict[str, Any], deps: dict[str, list[str]], actor: str) -> str:
    from ..identity import lineage
    from ..services import code_task_service

    owner, _, repo = lane["full_name"].partition("/")
    opts = jload(cs.get("options"), {}) or {}
    fctx = {**(opts.get("finalize_extra") or {}), "change_set_id": cs["id"], "change_set_components": list(lane["components"]),
            "app_id": f"app:{cs['project_id']}:{cs['app_key']}", "app_name": cs.get("app_name") or cs["app_key"]}
    if cs.get("workflow_run_id"):
        fctx["workflow_run_id"] = cs["workflow_run_id"]
    acting = acting_for_lane(db, cs["id"])
    with (lineage.acting(acting) if acting is not None else lineage.nullcontext()):
        run_id = code_task_service.start_code_task(
            db, project_id=cs["project_id"], task_id=cs.get("task_id"), title=cs["title"],
            instructions=_lane_brief(db, cs, lane, lanes, plan, deps),
            acceptance_criteria=list(opts.get("acceptance_criteria") or []),
            test_commands=list(opts.get("test_commands") or []), owner=owner, repo=repo,
            base_branch=lane.get("base_branch") or None, finalize_ctx=fctx, jobs=jobs,
            allow_duplicate=True)
    db.execute(sql("UPDATE change_set_prs SET code_task_run_id = :r, lane_state = 'building', error = NULL, "
                   "updated_at = now() WHERE id = :i"), {"r": run_id, "i": lane["id"]})
    event(db, cs["project_id"], "change_set.lane_started", actor, "change_set", cs["id"],
          {"repository": lane["full_name"], "code_task_run_id": run_id})
    return run_id


def _latest_attempt(db: Any, run_id: str) -> str:
    """A failed lane retried from Factory continues as the newest attempt of that build."""
    seen = {run_id}
    current = run_id
    for _ in range(10):
        nxt = db.execute(sql("SELECT id FROM code_task_runs WHERE finalize_ctx->>'parent_run_id' = :r "
                             "ORDER BY created_at DESC LIMIT 1"), {"r": current}).scalar()
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        current = str(nxt)
    return current


def _refresh_lane(db: Any, settings: Any, cs: dict[str, Any], lane: dict[str, Any]) -> dict[str, Any]:
    """Read the lane's build and say what the lane is now."""
    from . import github

    rid = lane.get("code_task_run_id")
    if not rid or lane["lane_state"] in ("unchanged", "merged", "reverted"):
        return lane
    newest = _latest_attempt(db, rid)
    if newest != rid:
        db.execute(sql("UPDATE change_set_prs SET code_task_run_id = :r, lane_state = 'building', error = NULL, "
                       "updated_at = now() WHERE id = :i"), {"r": newest, "i": lane["id"]})
        lane = {**lane, "code_task_run_id": newest, "lane_state": "building", "error": None}
        rid = newest
    run = db.execute(sql("SELECT status, phase, error, pull_request_number, pull_request_url FROM code_task_runs "
                         "WHERE id = :r"), {"r": rid}).mappings().first()
    if run is None:
        return lane
    if run["status"] in ("failed", "blocked", "cancelled") and lane["lane_state"] != "failed":
        why = str(run["error"] or run["status"])[:500]
        db.execute(sql("UPDATE change_set_prs SET lane_state = 'failed', error = :e, updated_at = now() WHERE id = :i"),
                   {"e": why, "i": lane["id"]})
        return {**lane, "lane_state": "failed", "error": why}
    if run["status"] == "completed" and run["pull_request_number"] and lane["lane_state"] != "ready":
        patch: dict[str, Any] = {"n": int(run["pull_request_number"]), "u": run["pull_request_url"], "i": lane["id"],
                                 "sha": None, "br": None, "add": None, "del": None, "files": None}
        try:
            pr = github.Repo(db, settings, cs["project_id"], cs["id"], lane["full_name"]).pull(run["pull_request_number"])
            patch.update(sha=(pr.get("head") or {}).get("sha"), br=(pr.get("head") or {}).get("ref"),
                         add=pr.get("additions"), **{"del": pr.get("deletions")}, files=pr.get("changed_files"))
        except MultiRepoError as error:
            log.warning("change set %s: could not read %s: %s", cs["id"], lane["full_name"], error.code)
        if not patch["sha"]:
            return lane                                    # not ready until GitHub says which commit
        db.execute(sql("UPDATE change_set_prs SET lane_state = 'ready', pr_number = :n, pr_url = :u, head_sha = :sha, "
                       "branch = :br, additions = :add, deletions = :del, changed_files = :files, review_state = 'pass', "
                       "ci_state = COALESCE(ci_state, 'pass'), updated_at = now() WHERE id = :i"), patch)
        return {**lane, "lane_state": "ready", "pr_number": patch["n"], "head_sha": patch["sha"], "branch": patch["br"]}
    return lane


# ── advancing ──────────────────────────────────────────────────────────────────────────────────

def _set_state(db: Any, cs_id: str, state: str, *, reason: Optional[str] = None, error: Optional[str] = None) -> None:
    db.execute(sql("UPDATE repo_change_sets SET state = :s, blocked_reason = :r, error = COALESCE(:e, error), "
                   "updated_at = now() WHERE id = :i"), {"s": state, "r": reason, "e": error, "i": cs_id})


def advance(db: Any, settings: Any, jobs: Any, cs_id: str, *, actor: str = "boss") -> list[str]:
    """Move one set as far as it can go now. Idempotent; called every runner tick."""
    cs = _row(db, cs_id, lock=True)
    if cs["mode"] != "multi" or cs["state"] in DONE or cs["state"] in ("merging", "deploying"):
        return []
    done: list[str] = []
    if cs["state"] == "planned":
        plan = make_plan(db, settings, cs)
        unchanged = {c["name"] for c in plan["components"] if c["unchanged"]}
        for lane in _lanes(db, cs_id):
            if set(lane["components"] or []) <= unchanged:
                db.execute(sql("UPDATE change_set_prs SET lane_state = 'unchanged', updated_at = now() WHERE id = :i"),
                           {"i": lane["id"]})
        db.execute(sql("UPDATE repo_change_sets SET plan = CAST(:p AS JSONB), state = 'building', updated_at = now() "
                       "WHERE id = :i"), {"p": jdump(plan), "i": cs_id})
        done.append("planned")
        cs = _row(db, cs_id)
    plan = jload(cs.get("plan"), {}) or {}
    lanes = [_refresh_lane(db, settings, cs, lane) for lane in _lanes(db, cs_id)]
    failed = [lane for lane in lanes if lane["lane_state"] == "failed"]
    if failed:
        reason = "; ".join(f"{lane['full_name']}: {str(lane.get('error') or 'failed')[:160]}" for lane in failed)
        if cs["state"] != "blocked" or cs.get("blocked_reason") != reason:
            _set_state(db, cs_id, "blocked", reason=f"One repository failed, so the whole set waits. {reason}")
            _withdraw_decision(db, cs, "a lane failed")
            done.append("blocked")
        return done
    if cs["state"] == "blocked" and (cs.get("blocked_reason") or "").startswith("One repository failed"):
        _set_state(db, cs_id, "building")
        cs = _row(db, cs_id)
        done.append("unblocked")
    deps = _repo_deps(db, cs)
    by_name = {lane["full_name"]: lane for lane in lanes}
    for lane in lanes:
        if lane["lane_state"] != "waiting":
            continue
        needs = [by_name[d] for d in deps.get(lane["full_name"], []) if d in by_name]
        if all(n["lane_state"] in ("ready", "unchanged") for n in needs):
            _start_lane(db, settings, jobs, cs, lane, lanes, plan, deps, actor)
            done.append(f"started {lane['full_name']}")
    lanes = _lanes(db, cs_id)
    touched = [lane for lane in lanes if lane["lane_state"] != "unchanged"]
    if touched and all(lane["lane_state"] == "ready" for lane in touched):
        moved = _watch_heads(db, settings, cs, touched)
        if moved == ["merged_outside"]:
            return done + moved
        if moved and cs["state"] in ("in_review", "approved"):
            # A PUSH RE-ASKS: the open (or given) approval was of other commits.
            _withdraw_decision(db, cs, "new commits on " + ", ".join(moved))
            _set_state(db, cs_id, "building")
            done.append("pushed")
        cs = _row(db, cs_id)
        touched = [lane for lane in _lanes(db, cs_id) if lane["lane_state"] != "unchanged"]
        if cs["state"] in ("building", "blocked"):
            last = latest_check(db, cs_id)
            if last and last["head_shas"] == _heads(touched) and last["verdict"] != "pass" and not moved:
                return done                       # checked these exact commits already: waits for a push
            done.extend(_review_set(db, settings, cs, touched))
    return done


def _set_body(cs: dict[str, Any], lane: dict[str, Any], touched: list[dict[str, Any]], original: str) -> str:
    marker = "<!-- boss:change-set -->"
    head = original.split(marker)[0].rstrip()
    rows = "\n".join(f"- {'**this PR**' if x['full_name'] == lane['full_name'] else x['full_name'] + '#' + str(x['pr_number'])}"
                     f" — {', '.join(x['components'] or [])}" for x in touched)
    return (f"{head}\n\n{marker}\n### Part of a change across {len(touched)} repositories\n"
            f"**{cs['title']}** (BOSS change {cs['id']}). These pull requests are reviewed, approved, merged and "
            f"deployed together, in this order:\n{rows}\n\nDo not merge this pull request on its own.")


def _review_set(db: Any, settings: Any, cs: dict[str, Any], touched: list[dict[str, Any]]) -> list[str]:
    """Every lane is ready: link the PRs, check they fit together, ask once."""
    from . import cross_check, github

    done = []
    for lane in touched:
        try:
            repo = github.Repo(db, settings, cs["project_id"], cs["id"], lane["full_name"])
            pr = repo.pull(lane["pr_number"])
            body = str(pr.get("body") or "")
            wanted = _set_body(cs, lane, touched, body)
            if body.strip() != wanted.strip():
                repo.set_body(lane["pr_number"], wanted)
        except MultiRepoError as error:
            log.warning("change set %s: could not link %s: %s", cs["id"], lane["full_name"], error.code)
    done.append("linked")
    check = cross_check.run(db, settings, cs, touched)
    done.append(f"checked {check['verdict']}")
    if check["verdict"] != "pass":
        failing = [r["name"] for r in check["results"] if not r.get("pass")]
        _set_state(db, cs["id"], "blocked",
                   reason="Cross-repo check failed: " + "; ".join(failing)[:400] + ". The whole set waits.")
        _withdraw_decision(db, cs, "the cross-repo check failed")
        return done + ["blocked"]
    _ask(db, cs, touched)
    return done + ["asked"]


def content_hash(heads: dict[str, str]) -> str:
    return hashlib.sha256(jdump(sorted(heads.items())).encode()).hexdigest()


def _heads(touched: list[dict[str, Any]]) -> dict[str, str]:
    return {lane["full_name"]: str(lane["head_sha"]) for lane in touched}


def _ask(db: Any, cs: dict[str, Any], touched: list[dict[str, Any]]) -> str:
    """ONE decision for the whole set, pinned to the exact head SHAs (the content hash)."""
    from ..services.core import create_blocking_decision

    heads = _heads(touched)
    n = len(touched)
    prs = [{"repository": lane["full_name"], "pr_number": lane["pr_number"], "pr_url": lane["pr_url"],
            "head_sha": lane["head_sha"], "additions": lane.get("additions"), "deletions": lane.get("deletions"),
            "components": list(lane["components"] or [])} for lane in touched]
    recommendation = {
        "action": "approve_change_set", "change_set_id": cs["id"], "app_key": cs["app_key"],
        "app_name": cs.get("app_name") or cs["app_key"], "title": cs["title"], "pull_requests": prs,
        "head_shas": heads,
        "summary": (f"One decision for all {n} pull requests of “{cs['title']}”. They merge together and deploy "
                    "together, or not at all."),
        "action_preview": {"kind": "fields", "content_hash": content_hash(heads),
                           "fields": [{"label": p["repository"], "value": f"#{p['pr_number']} @ {p['head_sha'][:12]}"}
                                      for p in prs]},
    }
    decision_id = create_blocking_decision(
        db, cs["project_id"], cs.get("task_id"), f"Ship a change · {cs.get('app_name') or cs['app_key']}: {cs['title']}",
        recommendation, {}, "change_set_approval_required", risk_tier=2)
    db.execute(sql("UPDATE repo_change_sets SET state = 'in_review', decision_id = :d, approved_shas = NULL, "
                   "approved_by = NULL, approved_at = NULL, blocked_reason = NULL, updated_at = now() WHERE id = :i"),
               {"d": decision_id, "i": cs["id"]})
    event(db, cs["project_id"], "change_set.approval_asked", "boss", "change_set", cs["id"],
          {"decision_id": decision_id, "head_shas": heads})
    return decision_id


def _withdraw_decision(db: Any, cs: dict[str, Any], why: str) -> None:
    """The open question is about content that no longer exists: withdraw it, never leave it
    answerable (an approval of old SHAs would merge something nobody reviewed)."""
    if not cs.get("decision_id"):
        return
    db.execute(sql("UPDATE decisions SET status = 'expired', resolution_note = :n, updated_at = now() "
                   "WHERE id = :d AND status IN ('pending', 'delegated', 'revision_requested', 'approved')"),
               {"n": f"Withdrawn: {why}. BOSS asks again when the set is ready.", "d": cs["decision_id"]})
    db.execute(sql("UPDATE repo_change_sets SET approved_shas = NULL, approved_by = NULL, approved_at = NULL, "
                   "updated_at = now() WHERE id = :i"), {"i": cs["id"]})


def _watch_heads(db: Any, settings: Any, cs: dict[str, Any], touched: list[dict[str, Any]]) -> list[str]:
    """Read every PR's head. Returns the repositories whose head moved (recorded), or
    ["merged_outside"] when a PR of the set was merged on GitHub by hand (the set is flagged)."""
    from . import github

    moved = []
    for lane in touched:
        try:
            pr = github.Repo(db, settings, cs["project_id"], cs["id"], lane["full_name"]).pull(lane["pr_number"])
        except MultiRepoError:
            continue
        if pr.get("merged") and not lane.get("merged_sha"):
            _set_state(db, cs["id"], "blocked",
                       reason=f"{lane['full_name']}#{lane['pr_number']} was merged outside BOSS. Decide whether to "
                              "finish the set by hand or revert it.")
            _withdraw_decision(db, cs, "a pull request was merged outside BOSS")
            return ["merged_outside"]
        sha = (pr.get("head") or {}).get("sha")
        if sha and sha != lane["head_sha"]:
            db.execute(sql("UPDATE change_set_prs SET head_sha = :s, additions = :a, deletions = :d, "
                           "changed_files = :f, updated_at = now() WHERE id = :i"),
                       {"s": sha, "a": pr.get("additions"), "d": pr.get("deletions"), "f": pr.get("changed_files"),
                        "i": lane["id"]})
            moved.append(lane["full_name"])
    if moved:
        event(db, cs["project_id"], "change_set.pushed", "boss", "change_set", cs["id"], {"repositories": moved})
    return moved


def advance_all(engine: Any, settings: Any, jobs: Any, project_id: Optional[str] = None) -> dict[str, Any]:
    """Every open multi-repo set, each in its own transaction (one set that cannot move never stops
    the others). Called from the workflow runner's tick (POST /v1/delivery-workflows/run-capabilities)."""
    from ..db import begin

    try:
        with begin(engine) as db:
            ids = [str(r) for r in db.execute(sql(
                "SELECT id FROM repo_change_sets WHERE mode = 'multi' AND state IN ('planned', 'building', 'blocked', "
                "'in_review', 'approved')" + (" AND project_id = :p" if project_id else "") + " ORDER BY updated_at"),
                {"p": project_id} if project_id else {}).scalars().all()]
    except Exception:  # noqa: BLE001 — before the tables exist
        return {}
    out: dict[str, Any] = {}
    for cs_id in ids:
        try:
            with begin(engine) as db:
                out[cs_id] = advance(db, settings, jobs, cs_id)
        except Exception as error:  # noqa: BLE001
            log.warning("change set %s did not advance: %s: %s", cs_id, type(error).__name__, error)
            out[cs_id] = {"error": type(error).__name__}
    return out


# ── the one decision ───────────────────────────────────────────────────────────────────────────

def on_decision(db: Any, decision_row: dict[str, Any], *, status: str, actor: str, jobs: Any = None,
                note: Optional[str] = None) -> None:
    """DecisionService's follow-on for `approve_change_set`: approve -> pin the SHAs and queue the
    merge; revise (Send back) -> each PR gets a fix build from the note; reject -> the set stops."""
    rec = decision_row.get("recommendation") or {}
    if isinstance(rec, str):
        rec = jload(rec, {}) or {}
    cs_id = str(rec.get("change_set_id") or "")
    if not cs_id:
        return
    cs = _row(db, cs_id, lock=True)
    if cs.get("decision_id") != decision_row["id"]:
        raise MultiRepoError(409, "stale_decision", "This approval is for an older version of the change. Open the "
                                                    "current one.")
    if status == "approved":
        lanes = [lane for lane in _lanes(db, cs_id) if lane["lane_state"] != "unchanged"]
        heads = _heads(lanes)
        if heads != (rec.get("head_shas") or {}):
            raise MultiRepoError(409, "stale_heads", "New commits arrived after this was asked. BOSS asks again.")
        db.execute(sql("UPDATE repo_change_sets SET state = 'approved', approved_shas = CAST(:h AS JSONB), "
                       "approved_by = :a, approved_at = now(), updated_at = now() WHERE id = :i"),
                   {"h": jdump(heads), "a": actor, "i": cs_id})
        if jobs is not None:
            jobs.create_job(db, project_id=cs["project_id"], task_id=cs.get("task_id"), kind=MERGE_JOB, priority=20,
                            payload={"project_id": cs["project_id"], "change_set_id": cs_id,
                                     "decision_id": decision_row["id"], "dedupe_key": f"change-set-merge-{cs_id}"})
        event(db, cs["project_id"], "change_set.approved", actor, "change_set", cs_id, {"head_shas": heads})
    elif status == "revision_requested":
        _send_back(db, cs, note or "", actor, jobs)
    elif status == "rejected":
        _set_state(db, cs_id, "rejected", reason="Rejected. Nothing was merged or deployed.")
        from . import grants

        grants.end(db, cs_id)
        event(db, cs["project_id"], "change_set.rejected", actor, "change_set", cs_id, {})


MERGE_JOB = "change_set.merge"


def _send_back(db: Any, cs: dict[str, Any], note: str, actor: str, jobs: Any) -> None:
    """"Send back": every PR of the set gets a fix build ON ITS OWN BRANCH (a follow-up of the
    lane's build: same branch, same open PR) from the note; the set is reviewed and asked again
    when they are done."""
    from ..services import code_task_service

    if not note.strip():
        raise MultiRepoError(422, "note_required", "Say what to change.")
    for lane in [x for x in _lanes(db, cs["id"]) if x["lane_state"] == "ready"]:
        started = code_task_service.start_followup_task(
            db, parent_run_id=lane["code_task_run_id"], jobs=jobs, actor=actor, title=cs["title"],
            instructions=(f"FIX THE OPEN PULL REQUEST #{lane['pr_number']} of “{cs['title']}”, part of a change "
                          f"across several repositories (you work only in {lane['full_name']}). "
                          f"The approver sent the set back:\n\n{note}"),
            workflow_run_id=cs.get("workflow_run_id"),
            finalize_extra={"change_set_id": cs["id"], "change_set_components": list(lane["components"] or [])})
        db.execute(sql("UPDATE change_set_prs SET code_task_run_id = :r, lane_state = 'building', updated_at = now() "
                       "WHERE id = :i"), {"r": started["run_id"], "i": lane["id"]})
    _set_state(db, cs["id"], "building", reason=None)
    event(db, cs["project_id"], "change_set.sent_back", actor, "change_set", cs["id"], {"note": note[:500]})


def halt(db: Any, cs_id: str, reason: str) -> None:
    """Stop the set (the identity cascade, or Stop): every running lane is asked to stop."""
    cs = _row(db, cs_id, lock=True)
    if cs["state"] in DONE:
        return
    for lane in _lanes(db, cs_id):
        if lane.get("code_task_run_id") and lane["lane_state"] == "building":
            try:
                db.execute(sql("INSERT INTO code_task_stop_requests (run_id, project_id, actor, reason, phase) "
                               "VALUES (:r, :p, 'boss', :why, 'change_set') ON CONFLICT DO NOTHING"),
                           {"r": lane["code_task_run_id"], "p": cs["project_id"], "why": reason[:300]})
            except Exception:  # noqa: BLE001
                pass
    _withdraw_decision(db, cs, "the change was stopped")
    _set_state(db, cs_id, "cancelled", reason=reason[:400])
    from . import grants

    grants.end(db, cs_id)


# ── single-repo runs: a one-PR set, recorded beside the run's own pipeline ─────────────────────

def record_single(db: Any, *, run_id: str, project_id: str, full_name: str, pr_number: int, pr_url: str,
                  branch: Optional[str], head_sha: Optional[str], decision_id: Optional[str], title: str) -> Optional[str]:
    """Observer only: nothing about the run's approval or merge changes."""
    try:
        with db.begin_nested():
            existing = for_run(db, run_id)
            if existing:
                return existing
            run = db.execute(sql("SELECT finalize_ctx, title FROM code_task_runs WHERE id = :r"), {"r": run_id}).mappings().first()
            from ..services.agent_assignment import app_key as key_of
            from ..services.factory_view import app_name

            name = app_name({"title": (run or {}).get("title") or title, "finalize_ctx": jload((run or {}).get("finalize_ctx"), {})})
            cs_id = make_id("chs")
            db.execute(sql("INSERT INTO repo_change_sets (id, project_id, app_key, app_name, title, mode, state, decision_id) "
                           "VALUES (:i, :p, :a, :an, :t, 'single', 'in_review', :d)"),
                       {"i": cs_id, "p": project_id, "a": key_of(name), "an": name, "t": title[:300], "d": decision_id})
            db.execute(sql("INSERT INTO change_set_prs (id, change_set_id, full_name, lane_state, code_task_run_id, "
                           "pr_number, pr_url, branch, head_sha) VALUES (:i, :c, :f, 'ready', :r, :n, :u, :b, :s)"),
                       {"i": make_id("csp"), "c": cs_id, "f": full_name, "r": run_id, "n": pr_number, "u": pr_url,
                        "b": branch, "s": head_sha})
            return cs_id
    except Exception as error:  # noqa: BLE001 — the record never breaks the run it records
        log.warning("could not record a one-PR change for %s: %s", run_id, type(error).__name__)
        return None


def mark_single(db: Any, run_id: str, state: str) -> None:
    try:
        with db.begin_nested():
            db.execute(sql("UPDATE repo_change_sets SET state = :s, updated_at = now() WHERE mode = 'single' AND id IN "
                           "(SELECT change_set_id FROM change_set_prs WHERE code_task_run_id = :r)"),
                       {"s": state, "r": run_id})
    except Exception:  # noqa: BLE001
        pass
