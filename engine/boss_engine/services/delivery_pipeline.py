"""The missing link: a merged PR becomes a deployment.

Design: docs/product/CICD_DEPLOYMENT_PLAN.md

BOSS already has the whole delivery apparatus — `resource_plane.plan_deployment_run`,
`deployment_rollout_engine`, `promotion_orchestrator`, `telemetry_connectors`, and nine
tables behind them. Every one of those tables was EMPTY, and nothing in the code-task or
merge path referenced any of it. The parts existed and had never been assembled.

This module is only the seam. It deliberately builds no new deployment machinery: it observes
a merge, resolves the target, and calls `plan_deployment_run`. Writing a fourth way to insert
a `deployment_runs` row is exactly the "two copies of one decision" defect that produced the
preview-resolver, test-oracle and failure-taxonomy bugs in this codebase already.

Boundaries, each carried from something already paid for:

  * STAGING IS AUTOMATIC, PRODUCTION IS A HUMAN GATE. An agent promoting its own build to
    production is what a governance product exists to prevent — the same reasoning that makes
    a Tier-2 merge approval require the `ceo` role.
  * A deployment_run row exists BEFORE anything is attempted, so a crash leaves a record. The
    code-task pipeline learned this by losing whole runs to state written only at the end.
  * Best-effort at the seam: a delivery failure must never retroactively un-merge a PR or
    fail a completed code task. It is recorded and surfaced, not raised.
"""
from __future__ import annotations

import fnmatch
import json
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from .core import create_blocking_decision, emit_event

# Environments a merge may deploy to WITHOUT a human. Everything else needs a promotion
# decision. Deliberately a set rather than a flag: adding "production" here should be a
# visible, reviewable change, not a config toggle.
AUTO_DEPLOY_ENVIRONMENTS = frozenset({"staging", "dev", "preview"})


def fanout_entry_status(outcome: Optional[dict[str, Any]]) -> str:
    """Report the actual result of a merge-triggered deployment."""
    status = str((outcome or {}).get("status") or "")
    if status in {"succeeded", "executed", "healthy"}:
        return "executed"
    if status in {"failed", "blocked", "not_found", "rolled_back"}:
        return status
    return "execute_failed"


def _globs_of(target: dict) -> list[str]:
    """`path_globs` as a list, whatever the driver handed back (JSONB or TEXT)."""
    raw = target.get("path_globs") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [str(g) for g in raw if str(g).strip()]


def _path_matches(path: str, globs: list[str]) -> bool:
    """Does a changed repo path fall inside any of this app's globs?

    `fnmatch` treats `*` as crossing `/`, so "apps/web/**" and "apps/web/*" both match
    "apps/web/src/main.tsx". That is the forgiving direction on purpose: a glob that matches
    too much causes an extra deploy of unchanged code, while one that matches too little
    causes an app to silently never ship — which is the exact defect (G1) this function exists
    to remove.
    """
    p = (path or "").lstrip("./")
    for g in globs:
        g = g.strip().lstrip("./")
        if fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.rstrip("/") + "/*"):
            return True
    return False


def targets_for(connection, project_id: str, repo: str, environment: str, *,
                changed_paths: Optional[list[str]] = None) -> list[dict]:
    """EVERY target this merge should deploy — a list, deliberately.

    This used to be `target_for`, returning one row via `ORDER BY created_at DESC LIMIT 1`.
    The TABLE is keyed `(project_id, app_name, environment)`, so a repo holding two apps has
    two targets — and the singular resolver deployed whichever was registered most recently
    while the other silently never shipped. Because the no-target path is (correctly) quiet,
    nothing reported it. Returning a list is the fix; the callers fan out.

    `path_globs` scopes a target to part of the repo. Empty globs mean the whole repo, so
    every existing single-app target keeps behaving exactly as it did.
    """
    # SELECT * rather than a column list. The list version omitted `required_resources` in
    # execute_deployment and silently stranded the target's declared port; the same list here
    # would have omitted `auto_deploy` and made this function permanently answer "not
    # automatic". A projection that must be extended per new consumer will eventually not be.
    rows = connection.execute(
        sql("SELECT * FROM deployment_targets "
            "WHERE project_id = :p AND repo = :r AND environment = :e "
            "  AND status = 'active' "
            "ORDER BY created_at DESC"),
        {"p": project_id, "r": repo, "e": environment},
    ).mappings().all()

    selected: list[dict] = []
    for row in rows:
        target = dict(row)
        globs = _globs_of(target)
        if not globs:
            selected.append(target)          # whole-repo target: always in scope
        elif changed_paths is None:
            # We could not determine what changed. Deploy it anyway rather than skip it:
            # an unnecessary deploy of unchanged code is cheap and visible, an app that
            # silently stops shipping is G1 all over again. The caller records that globs
            # went unevaluated so this is never a mystery.
            target["_globs_unevaluated"] = True
            selected.append(target)
        elif any(_path_matches(p, globs) for p in changed_paths):
            selected.append(target)
    return selected


def target_for(connection, project_id: str, repo: str, environment: str) -> Optional[dict]:
    """Back-compat shim: the first target, or None. Prefer `targets_for`.

    Kept so any caller outside this module keeps working, but it carries the singular-resolver
    hazard by construction — anything that deploys should use `targets_for`.
    """
    found = targets_for(connection, project_id, repo, environment)
    return found[0] if found else None


def pick_deploy_target(candidates: list[dict], app_name: str) -> Optional[dict]:
    """Return the unique target with this app's identity, never a repo-only match."""
    from .naming import identity_key

    want = identity_key(app_name)
    if not want:
        return None
    named = [t for t in candidates if identity_key(str(t.get("app_name") or "")) == want]
    return named[0] if len(named) == 1 else None


def deploy_build(engine, settings: Settings, *, project_id: str, run_id: str,
                 environment: str = "staging", target_id: Optional[str] = None,
                 actor: str = "operator") -> dict[str, Any]:
    """Deploy a finished build to a target, on request. The first deployment, not a re-ship.

    BOSS could create hosts and targets and could REDEPLOY, and had no way to perform a
    deployment that had never happened. So "deploy Living Colors to live" had no executable
    action behind it at all: redeploy needs an existing deployment_run to re-ship, and for an
    app that has never shipped there is none. The agent kept improvising markup for a button
    that could not exist, which is a design answer, not a prompting problem.

    Deliberately narrow:
      * the build must have SUCCEEDED — deploying a failed build is never what was meant;
      * the target must already exist and be active, so this cannot invent infrastructure;
      * it reuses plan_deployment_run and execute_deployment, so budget reservation, health
        checks and rollback behave exactly as they do on the merge path.
    """
    from ..db import begin

    with begin(engine) as c:
        run = c.execute(
            # finalize_ctx WAS NEVER SELECTED, so the app-name lookup below silently fell
            # through to `title` on every single run — and a title is the operator's whole
            # directive ("Build a change calendar. Who it is for: a team that keeps deploying…"),
            # not a name. The matching that was supposed to prevent a wrong deployment was
            # therefore comparing target names against a paragraph.
            sql("SELECT id, project_id, status, title, spec, finalize_ctx FROM code_task_runs "
                "WHERE id = :i AND project_id = :p"),
            {"i": run_id, "p": project_id}).mappings().first()
        if not run:
            return {"status": "failed", "reason": f"no such run in this project: {run_id}"}
        if str(run["status"]) not in ("completed", "succeeded", "passed"):
            # The exact substitution this whole area keeps trying to make.
            return {"status": "failed",
                    "reason": f"run {run_id} is '{run['status']}', not a successful build. "
                              "Deploying is not a way to finish a build — rebuild first, "
                              "which costs money and is a separate decision."}

        if target_id:
            target = c.execute(
                sql("SELECT * FROM deployment_targets WHERE id = :i AND project_id = :p"),
                {"i": target_id, "p": project_id}).mappings().first()
        else:
            # NEVER GUESS WHICH APP. ONE REPO HOLDS MANY OF THEM.
            #
            # This matched on repo + environment and took ORDER BY created_at DESC LIMIT 1.
            # Every app in this project lives in the SAME repo on the SAME environment, so that
            # silently resolved to whichever target was created last — and a deploy of "Living
            # Colors" shipped its build to Invoice-Chaser's port and reported success. A wrong
            # deployment that says "succeeded" is the worst outcome available here.
            #
            # Match on the app as well, and when it is still ambiguous REFUSE and list the
            # candidates. An operator picking from four names is fine; a coin flip that
            # overwrites a running app is not.
            spec = run["spec"] or {}
            repo = str(spec.get("repo") or "")
            candidates = [dict(r) for r in c.execute(
                sql("SELECT * FROM deployment_targets "
                    "WHERE project_id = :p AND repo = :r AND environment = :e "
                    "  AND status = 'active' "
                    "ORDER BY created_at DESC"),
                {"p": project_id, "r": repo, "e": environment}).mappings().all()]

            from .naming import identity_key, project_name

            fctx = run.get("finalize_ctx") or {}
            app_name = str(fctx.get("app_name") or "").strip()
            if not app_name:
                # `project_name` is what everything else in BOSS uses to turn a directive into
                # the app's display name — the same function the console titles cards with.
                display, _blurb, _label = project_name(str(run.get("title") or ""))
                app_name = display if display and display != "Untitled app" else ""

            wanted = identity_key(app_name) if app_name else ""

            def _matches(t: dict) -> bool:
                return bool(wanted) and identity_key(str(t.get("app_name") or "")) == wanted

            named = [t for t in candidates if _matches(t)]
            target = pick_deploy_target(candidates, app_name)
            if target is None and named:
                return {
                    "status": "failed",
                    "reason": (
                        f"{len(named)} active '{environment}' targets are registered for "
                        f"'{app_name}'. Pass target_id — deploying to the wrong one of an app's "
                        "own targets is still a wrong deployment."),
                    "candidates": [
                        {"target_id": t["id"], "app_name": t.get("app_name"),
                         "domains": t.get("domains")} for t in named
                    ],
                }
            elif target is None and candidates and wanted:
                # NOT AMBIGUOUS. MISSING.
                #
                # This branch used to say "N targets share repo 'boss-sandbox', pass target_id"
                # and list the other apps as CANDIDATES. Every app in this project is built into
                # the one sandbox repo, so that fired whenever an app had no target of its own —
                # which is the normal state of an app that has never shipped. The message named
                # someone else's infrastructure as the answer, and BOSS spent two turns trying
                # to work out which of Meeting cost meter or Vendor-Status-Watcher to deploy a
                # change calendar onto. There was no right answer in that list.
                #
                # "Pass target_id" was also advice that could not be followed: no target for
                # this app existed to pass, and until create_deployment_target there was no tool
                # to make one, so the only route left was hand-written SQL. That is exactly what
                # BOSS did.
                return {
                    "status": "failed",
                    "reason": (
                        f"'{app_name}' has no '{environment}' deployment target of its own, so "
                        f"there is nowhere to ship it. The {len(candidates)} '{environment}' "
                        f"targets on repo '{repo}' belong to other apps ("
                        + ", ".join(sorted({str(t.get("app_name") or "?") for t in candidates}))
                        + ") — deploying onto one of those would overwrite a running app. "
                        "Create a target for this app first: create_deployment_target(app_name="
                        f"'{app_name}', environment='{environment}'), which picks a free port on "
                        "the host and registers it, then deploy again."),
                    "missing": "deployment_target",
                    "app_name": app_name,
                    "fix": {"tool": "create_deployment_target",
                            "args": {"app_name": app_name, "environment": environment,
                                     "run_id": run_id}},
                    "other_apps_targets": [
                        {"target_id": t["id"], "app_name": t.get("app_name"),
                         "domains": t.get("domains")} for t in candidates
                    ],
                }
            elif target is None and candidates:
                return {
                    "status": "failed",
                    "reason": (
                        f"{len(candidates)} active '{environment}' targets share repo "
                        f"'{repo}', and this run does not name an app to match them against, so "
                        "none of them can be chosen safely. Pass target_id — deploying the wrong "
                        "app is worse than not deploying."),
                    "candidates": [
                        {"target_id": t["id"], "app_name": t.get("app_name"),
                         "domains": t.get("domains")} for t in candidates
                    ],
                }

        if not target:
            existing = [
                f"{r['environment']} ({r['status']})" for r in c.execute(
                    sql("SELECT environment, status FROM deployment_targets "
                        "WHERE project_id = :p AND repo = :r ORDER BY environment"),
                    {"p": project_id,
                     "r": str((run["spec"] or {}).get("repo") or "")}).mappings().all()
            ]
            return {"status": "failed",
                    "reason": f"no active '{environment}' target for this app."
                              + (f" It has: {', '.join(existing)}." if existing else
                                 " It has no deployment targets at all."),
                    "environments_available": existing}

        if target.get("paused"):
            return {"status": "failed",
                    "reason": f"target {target['id']} is paused; un-pause it first"}

        from .resource_plane import ResourcePlaneService

        planned = ResourcePlaneService(settings).plan_deployment_run(
            c,
            project_id=project_id,
            deployment_target_id=target["id"],
            commit_ref=None,
            release_packet={"source": "operator_deploy", "code_task_run_id": run_id,
                            "requested_by": actor},
            expected_cost_usd_micros=0,
            reservation_id=None,
            actor=actor,
        )
        deployment_run_id = planned.get("deployment_run_id") or planned.get("id")
        if not deployment_run_id:
            return {"status": "failed", "reason": f"could not plan a deployment: {planned}"}
        if planned.get("status") in {"failed", "blocked"}:
            return {"status": "blocked", "deployment_run_id": deployment_run_id,
                    "reason": planned.get("failure_reason") or "Deployment policy blocked this run"}
        c.execute(sql("UPDATE deployment_runs SET trigger = 'operator' WHERE id = :i"),
                  {"i": deployment_run_id})
        emit_event(c, project_id, "delivery.requested", actor, "code_task_run", run_id,
                   {"deployment_run_id": deployment_run_id, "target": target["id"],
                    "environment": target["environment"]})

    result = execute_deployment(engine, settings, deployment_run_id, actor=actor)
    return {"status": str(result.get("status") or "unknown"),
            "deployment_run_id": deployment_run_id,
            "target_id": target["id"],
            "app_name": target.get("app_name"),
            "environment": target["environment"],
            "detail": result}


def on_pull_request_merged(engine, settings: Settings, *, project_id: str, run_id: str,
                           repo: str, commit_ref: Optional[str], decision_id: Optional[str],
                           actor: str = "delivery") -> dict[str, Any]:
    """Called when a merge decision completes. Starts a staging deployment if one is configured.

    Returns a dict describing what happened, always — `skipped` is a first-class outcome and
    the common one. Never raises: this runs after a PR has already merged, and an exception
    here would turn a successful delivery into a failure report for work that shipped.
    """
    from ..db import begin

    result: dict[str, Any] = {"status": "skipped", "reason": "no active staging target",
                              "deployments": []}
    planned_runs: list[dict[str, Any]] = []
    try:
        with begin(engine) as c:
            # What did this task actually change? Reuses the preview's lineage resolver rather
            # than deriving a second file list — `state.impl_files` is per-ATTEMPT and on a
            # repair holds only the touched-up files, which is precisely how the preview once
            # served the wrong app. Best-effort: if it cannot be determined, `targets_for`
            # falls open and deploys every target rather than silently skipping scoped ones.
            changed_paths: Optional[list[str]] = None
            try:
                from .preview_service import lineage_impl_files

                run_row = c.execute(
                    sql("SELECT * FROM code_task_runs WHERE id = :i"), {"i": run_id}
                ).mappings().first()
                if run_row:
                    found = lineage_impl_files(engine, dict(run_row))
                    changed_paths = [str(f) for f in found] or None
            except Exception:  # noqa: BLE001 — path scoping is an optimisation, not a gate
                changed_paths = None

            targets = targets_for(c, project_id, repo, "staging",
                                  changed_paths=changed_paths)
            if not targets:
                # Silent by design. Recorded at debug level via the event so the absence is
                # visible when someone asks "why did nothing deploy?", without alarming a
                # project that never wanted delivery.
                emit_event(c, project_id, "delivery.skipped", actor, "code_task_run", run_id,
                           {"why": "no active staging deployment_target for this repo",
                            "repo": repo})
                return result

            for target in targets:
                if target.get("_globs_unevaluated"):
                    emit_event(c, project_id, "delivery.globs_unevaluated", actor,
                               "code_task_run", run_id,
                               {"why": "changed paths unknown; deploying this scoped target "
                                       "anyway rather than skipping it",
                                "target": target["id"], "app_name": target.get("app_name")})

                if not target.get("auto_deploy"):
                    # A target that has not opted in is planned-only, exactly as before.
                    # Silent rather than alarming: most repos never asked to deploy on merge.
                    emit_event(c, project_id, "delivery.not_auto", actor, "code_task_run",
                               run_id, {"why": "auto_deploy is off for this target",
                                        "target": target["id"]})
                    planned_runs.append({"status": "skipped", "reason": "auto_deploy is off",
                                         "target_id": target["id"],
                                         "app_name": target.get("app_name")})
                    continue

                if target.get("paused"):
                    # Paused is louder than not-configured: someone turned this OFF
                    # deliberately and will want to know a merge went by while it was.
                    emit_event(c, project_id, "delivery.paused", actor, "code_task_run",
                               run_id, {"why": "auto-deploy is paused", "target": target["id"],
                                        "commit_ref": commit_ref})
                    planned_runs.append({"status": "paused", "target_id": target["id"],
                                         "app_name": target.get("app_name")})
                    continue

                if target["environment"] not in AUTO_DEPLOY_ENVIRONMENTS:
                    emit_event(c, project_id, "delivery.gated", actor, "code_task_run", run_id,
                               {"why": f"{target['environment']} requires a promotion decision",
                                "target": target["id"]})
                    planned_runs.append({"status": "gated",
                                         "environment": target["environment"],
                                         "target_id": target["id"],
                                         "app_name": target.get("app_name")})
                    continue

                from .resource_plane import ResourcePlaneService

                plane = ResourcePlaneService(settings)
                planned = plane.plan_deployment_run(
                    c,
                    project_id=project_id,
                    deployment_target_id=target["id"],
                    commit_ref=commit_ref,
                    release_packet={
                        "source": "code_task_merge",
                        "code_task_run_id": run_id,
                        "decision_id": decision_id,
                        "repo": repo,
                        "app_name": target.get("app_name"),
                    },
                    # The merge is already paid for; the deployment is not an LLM cost.
                    expected_cost_usd_micros=0,
                    reservation_id=None,
                    actor=actor,
                )
                deployment_run_id = planned.get("deployment_run_id") or planned.get("id")
                if planned.get("status") in {"failed", "blocked"}:
                    planned_runs.append({
                        "status": "gated", "environment": target["environment"],
                        "target_id": target["id"], "app_name": target.get("app_name"),
                        "deployment_run_id": deployment_run_id,
                        "reason": planned.get("failure_reason") or "Deployment policy blocked this run",
                    })
                    continue
                emit_event(c, project_id, "delivery.deployment_planned", actor,
                           "code_task_run", run_id,
                           {"deployment_run_id": deployment_run_id,
                            "environment": target["environment"], "target": target["id"],
                            "app_name": target.get("app_name"), "commit_ref": commit_ref})
                c.execute(sql("UPDATE deployment_runs SET trigger = 'merge' WHERE id = :i"),
                          {"i": deployment_run_id})
                planned_runs.append({"status": "planned",
                                     "environment": target["environment"],
                                     "target_id": target["id"],
                                     "app_name": target.get("app_name"),
                                     "deployment_run_id": deployment_run_id})

    except Exception as exc:  # noqa: BLE001
        # A delivery failure must never un-merge a PR or fail a completed code task.
        try:
            with begin(engine) as c:
                emit_event(c, project_id, "delivery.failed", actor, "code_task_run", run_id,
                           {"error": f"{type(exc).__name__}: {exc}"[:300], "repo": repo})
        except Exception:  # noqa: BLE001
            pass
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300],
                "deployments": planned_runs}

    # Execute OUTSIDE the transaction above, and outside the merge's own transaction: a deploy
    # takes minutes, does network I/O, and must not hold a database lock or be rolled back by
    # something unrelated finishing badly. Until this existed, a merge planned a deployment and
    # nothing ever ran it — the row simply sat there, which is why every deployment so far was
    # started by hand. That is the whole of the missing "continuous".
    #
    # Sequential, not concurrent: two apps on ONE host would otherwise race for ports and for
    # the same docker daemon. Fan-out is about not dropping an app, not about speed.
    for entry in planned_runs:
        if entry.get("status") != "planned" or not entry.get("deployment_run_id"):
            continue
        try:
            outcome = execute_deployment(engine, settings, entry["deployment_run_id"],
                                         actor=actor)
            entry["status"] = fanout_entry_status(outcome)
            entry["deployment"] = outcome
            if entry["status"] != "executed":
                entry["error"] = (
                    (outcome or {}).get("error")
                    or (outcome or {}).get("detail")
                    or (outcome or {}).get("reason")
                    or f"deployment returned {(outcome or {}).get('status')!r}"
                )
        except Exception as exc:  # noqa: BLE001
            # Still never raises: the PR has merged, and a delivery failure must not be
            # reported as a failure of work that already shipped. One app failing must also
            # not stop the next one from deploying.
            entry["status"] = "execute_failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]

    statuses = {e.get("status") for e in planned_runs}
    if not planned_runs:
        overall = "skipped"
    elif len(statuses) == 1:
        overall = statuses.pop()
    else:
        overall = "partial"
    result = {"status": overall, "deployments": planned_runs}
    # Back-compat: callers written against the single-target era read these top-level keys.
    if len(planned_runs) == 1:
        result = {**planned_runs[0], **result, "status": planned_runs[0].get("status", overall)}
    return result


def redeploy(engine, settings: Settings, deployment_run_id: str, *,
             commit_ref: Optional[str] = None, actor: str = "operator",
             execute: bool = True, expected_cost_usd_micros: int = 0,
             reservation_id: Optional[str] = None) -> dict[str, Any]:
    """Plan the same target again, as a NEW deployment_run.

    Deliberately not a re-execution of the existing row. An operator asking "what is running and
    since when" is asking about a history, and rewriting one row in place destroys exactly that:
    the previous deployment's start, finish and health verdict would be overwritten by its
    replacement. A redeploy is a new event, so it gets a new record.

    `commit_ref` defaults to whatever the prior run shipped, which makes the common case — "put
    that back" / "run it again" — a button with no arguments.
    """
    from ..db import begin

    with begin(engine) as c:
        prior = c.execute(sql("SELECT * FROM deployment_runs WHERE id = :i"),
                          {"i": deployment_run_id}).mappings().first()
        if not prior:
            return {"status": "not_found", "deployment_run_id": deployment_run_id}

        from .resource_plane import ResourcePlaneService

        packet = dict(prior["release_packet"] or {})
        # Keep the lineage explicit: a redeploy that cannot say what it came from is just an
        # unexplained second row.
        packet.update({"source": "redeploy", "redeploy_of": deployment_run_id})
        planned = ResourcePlaneService(settings).plan_deployment_run(
            c,
            project_id=prior["project_id"],
            deployment_target_id=prior["deployment_target_id"],
            commit_ref=commit_ref or prior["commit_ref"],
            release_packet=packet,
            expected_cost_usd_micros=expected_cost_usd_micros,
            reservation_id=reservation_id,
            actor=actor,
        )
        new_id = planned.get("deployment_run_id") or planned.get("id")
        if planned.get("status") in {"failed", "blocked"}:
            return {"status": "blocked", "deployment_run_id": new_id,
                    "reason": planned.get("failure_reason") or "Deployment policy blocked this run",
                    "redeploy_of": deployment_run_id}
        # Say WHY this run exists. Without it the row reads as a manual deploy nobody remembers
        # asking for, and "why did this deploy?" — the question `trigger` was added to answer —
        # goes back to being unanswerable.
        c.execute(sql("UPDATE deployment_runs SET trigger='redeploy' WHERE id=:i"),
                  {"i": new_id})
        emit_event(c, prior["project_id"], "delivery.redeploy_requested", actor,
                   "deployment_run", new_id,
                   {"redeploy_of": deployment_run_id,
                    "commit_ref": commit_ref or prior["commit_ref"]})

    # The API uses `execute=False` so approval and Resource Plane apply can
    # complete the new ChangeSet. Direct connector execution below remains
    # guarded for legacy callers.
    if not execute:
        return {"status": "planned", "deployment_run_id": new_id,
                "redeploy_of": deployment_run_id,
                "changeset_id": planned.get("changeset_id")}
    return {"deployment_run_id": new_id,
            **execute_deployment(engine, settings, new_id, actor=actor)}


# How long any single deployment may run before it is treated as hung. Generous on purpose:
# a real build + transfer of a Node app to a small VM takes minutes, and killing a slow-but-
# healthy deploy is a worse failure than a stale row. This bounds the case that actually
# happened — a step that spins forever because its worker died or an SSH never returned.
DEPLOYMENT_MAX_RUNTIME_SECONDS = 45 * 60


def fail_hung_deployments(engine, *, max_seconds: int = DEPLOYMENT_MAX_RUNTIME_SECONDS
                          ) -> dict[str, Any]:
    """Close out deployments that have been running implausibly long.

    `fail_orphaned_deployments` only runs at API startup, so a deployment that hung while the
    API stayed up was never reconciled: BugBox sat in `preflight` for minutes with the console
    faithfully polling a step that would never finish. Startup-only cleanup assumes the thing
    that goes wrong is a restart; the thing that actually goes wrong is a hang.

    Safe to call on a schedule — it only touches rows that are BOTH still open and older than
    the cap, so a healthy long deploy is never interrupted.
    """
    from ..db import begin

    with begin(engine) as c:
        rows = c.execute(
            sql("UPDATE deployment_runs SET status='failed', finished_at=now(), "
                "failure_reason = COALESCE(failure_reason, "
                "  'this deployment ran longer than the maximum and was treated as hung. "
                "The host may hold a partial release — redeploy to be sure.') "
                "WHERE status IN ('created','running','in_progress') "
                "  AND COALESCE(started_at, created_at) < now() - make_interval(secs => :s) "
                "RETURNING id"),
            {"s": int(max_seconds)}).mappings().all()
    return {"failed": [r["id"] for r in rows]}


def fail_orphaned_deployments(engine) -> dict[str, Any]:
    """Close out deployments whose executing process no longer exists.

    Called at API startup. A deployment executes in a background thread, so a restart abandons
    it — and the row keeps saying `running`, which the console polls forever and an operator
    reads as "still going". The work really did stop; saying so is the only accurate option.

    Deliberately narrow: only rows already marked started, and only at startup, when nothing of
    this process can be mid-deployment. A periodic sweep with a timeout would eventually kill a
    slow but healthy deploy, which is a worse failure than a stale row.
    """
    from ..db import begin

    with begin(engine) as c:
        rows = c.execute(
            sql("UPDATE deployment_runs SET status='failed', finished_at=now(), "
                "failure_reason = COALESCE(failure_reason, "
                "  'the API restarted while this deployment was running, so it was abandoned "
                "mid-flight. The host may hold a partial release — redeploy to be sure.'), "
                "updated_at=now() "
                # `created` too. A deployment is inserted as 'created' and only becomes
                # 'running' once execution starts writing steps — so a worker that died in
                # between left a row this sweep did not match, and it stayed 'created' with a
                # step spinning forever. At startup NOTHING of this process is mid-deployment,
                # so every non-terminal row is abandoned by definition.
                "WHERE status IN ('created','running','planning','in_progress') "
                "RETURNING id")).mappings().all()
    return {"abandoned": len(rows), "ids": [r["id"] for r in rows][:10]}


def _target_view(row) -> dict[str, Any]:
    """A target as the console needs it: the row, plus the resources unpacked.

    `required_resources` is a list of {kind, name} — correct for the connector, hostile to a
    UI that wants to know the owner of the repo it is about to clone. Unpacking here means
    every reader gets the same answer; leaving it packed is how a branch picker came to ask
    GitHub for `//boss-sandbox/refs` and quietly render no branches at all.
    """
    target = dict(row)
    resources = {str(r.get("kind")): r.get("name")
                 for r in (target.get("required_resources") or []) if isinstance(r, dict)}
    target["repo_owner"] = resources.get("repo_owner")
    target["app_entry"] = resources.get("app_entry")
    target["app_port"] = resources.get("app_port")
    return target


def pipeline_view(connection, project_id: str, *, limit: int = 25) -> dict[str, Any]:
    """One query pass for the console: code task -> PR -> deployment -> rollout -> health.

    Assembled here rather than in the UI so the stages cannot disagree with each other — the
    same reason the shepherd console has a single overview endpoint.
    """
    runs = connection.execute(
        sql("""
            SELECT r.id AS run_id, r.title, r.status AS run_status,
                   r.pull_request_url, r.pull_request_number, r.updated_at,
                   d.id AS deployment_run_id, d.status AS deploy_status,
                   d.environment, d.app_name, d.target_kind, d.commit_ref,
                   d.started_at, d.finished_at, d.failure_reason,
                   d.health_check_results,
                   -- WHICH target this stage belongs to. Without it the console cannot match a
                   -- stage to the card it is drawing, so every card fell back to "never
                   -- deployed" — including one whose app was answering 200 on the host.
                   d.deployment_target_id AS target_id,
                   -- Where the thing actually IS. Without it the console can say a deployment
                   -- succeeded but not where to click, which is the state the operator hit.
                   t.domains AS target_domains
            FROM code_task_runs r
            LEFT JOIN deployment_runs d
                   ON (d.release_packet->>'code_task_run_id') = r.id
            LEFT JOIN deployment_targets t ON t.id = d.deployment_target_id
            WHERE r.project_id = :p AND r.pull_request_url IS NOT NULL
            ORDER BY r.updated_at DESC LIMIT :l
        """), {"p": project_id, "l": limit}).mappings().all()

    # SELECT * on purpose. A hand-listed projection here is how the console lost `domains`,
    # `auto_deploy` and the repo owner one field at a time: every new column had to be
    # remembered in a second place, and forgetting was silent. `target_for` was fixed this way
    # and this query was not — a rule applied on one path is not a rule.
    targets = connection.execute(
        sql("SELECT * FROM deployment_targets WHERE project_id = :p ORDER BY environment"),
        {"p": project_id}).mappings().all()

    # A TARGET KNOWS ITS OWN DEPLOYMENT HISTORY.
    #
    # "Never deployed" was derived from the stages above, which start at a code task with a PR.
    # A deployment made any other way — an operator pressing Deploy, trigger='manual' — creates
    # a deployment_run and no stage, so the card reported "never deployed" about an app that
    # was serving 200 on the host. The badge was answering a different question than the one it
    # appeared to answer.
    last = connection.execute(
        sql("""
            SELECT DISTINCT ON (deployment_target_id)
                   deployment_target_id AS target_id, id AS deployment_run_id,
                   status, finished_at, started_at, commit_ref, trigger,
                   health_check_results
            FROM deployment_runs
            WHERE project_id = :p
            ORDER BY deployment_target_id, COALESCE(finished_at, started_at, created_at) DESC
        """), {"p": project_id}).mappings().all()
    latest = {r["target_id"]: dict(r) for r in last}

    return {
        "project_id": project_id,
        "stages": [dict(r) for r in runs],
        "targets": [{**_target_view(t), "last_deployment": latest.get(t["id"])}
                    for t in targets],
        # Stated plainly so the console can say "nothing is configured" rather than
        # rendering an empty table that looks like a failure.
        "configured": bool(targets),
    }


# --------------------------------------------------------------------------- #
# Execution — BOSS places the artifact, observes it, and rolls back on its own
# --------------------------------------------------------------------------- #
def block_ungoverned_connector_execution(
    connection, run: dict, settings: Settings, *, actor: str
) -> dict[str, Any]:
    """Deny direct connector effects until they use the Resource Plane receipt path.

    A ChangeSet approval alone is not an effect envelope. The SSH connector is
    currently called outside `apply_changeset`, so it cannot produce the
    ResourceReceipt and budget settlement that method owns. This guard runs
    before even connector preflight, which can contact a host.
    """
    run = dict(run)
    run_id = run["id"]
    project_id = run["project_id"]
    if run["status"] in {"failed", "blocked", "succeeded", "rolled_back"}:
        return {"status": "blocked", "deployment_run_id": run_id,
                "reason": f"Deployment run is already {run['status']}."}

    changeset_id = run.get("changeset_id")
    reason_code = "deployment_missing_changeset"
    reason = "Deployment run has no ChangeSet; direct connector execution is blocked."
    changeset = None
    if changeset_id:
        changeset = connection.execute(
            sql("SELECT id, project_id, status, rollback_plan, reservation_id, "
                "expected_cost_usd_micros, expected_receipts FROM changesets "
                "WHERE id = :id AND project_id = :project_id"),
            {"id": changeset_id, "project_id": project_id},
        ).mappings().first()
        if not changeset:
            reason_code = "deployment_changeset_scope_mismatch"
            reason = "Deployment ChangeSet does not belong to this project."
        elif changeset["status"] != "approved":
            reason_code = "deployment_changeset_not_approved"
            reason = "Deployment ChangeSet must be approved before execution."
        elif not run.get("rollback_plan") or not changeset["rollback_plan"]:
            reason_code = "deployment_missing_rollback_plan"
            reason = "Deployment run and ChangeSet both require a rollback plan."
        elif int(changeset["expected_cost_usd_micros"] or 0) > 0:
            from .budget import BudgetGovernor

            try:
                reservation = (
                    BudgetGovernor(settings).assert_active_reservation(
                        connection, project_id, changeset["reservation_id"]
                    )
                    if changeset["reservation_id"] else None
                )
            except ValueError:
                # A rejected reservation is a policy denial, not an unhandled
                # connector error. Preserve the audited blocked attempt below.
                reservation = None
            if (
                not reservation
                or int(reservation["amount_usd_micros"])
                < int(changeset["expected_cost_usd_micros"])
            ):
                reason_code = "deployment_missing_budget_reservation"
                reason = "Deployment ChangeSet requires an active, sufficient BudgetReservation."
        if reason_code == "deployment_missing_changeset":
            if not changeset["expected_receipts"] or not run.get("release_receipt_ids"):
                reason_code = "deployment_missing_expected_receipts"
                reason = "Deployment ChangeSet must declare expected ResourceReceipts."
            else:
                # Expected receipt keys are plans, not durable receipts. The
                # connector must be wrapped by apply_changeset or an equivalent
                # audited adapter before it can touch a provider.
                reason_code = "deployment_connector_receipt_boundary_missing"
                reason = (
                    "Direct connector execution has no ResourceReceipt boundary; "
                    "apply the approved ChangeSet through ProviderOps."
                )

    # A historical run with no ChangeSet can never be repaired by retrying it;
    # leave a durable refusal and decision. A linked ChangeSet may still be
    # approved/applied through ProviderOps, so keep its planned run intact.
    if not changeset_id:
        decision_id = create_blocking_decision(
            connection,
            project_id,
            None,
            "Direct deployment connector lacks governance",
            {"action": "plan_governed_changeset", "deployment_run_id": run_id},
            {"deployment_run_id": run_id, "reason": reason_code},
            reason_code,
            risk_tier=3 if run.get("environment") == "production" else 2,
        )
        connection.execute(
            sql("UPDATE deployment_runs SET status = 'blocked', decision_id = :decision_id, "
                "failure_reason = :reason, finished_at = now(), updated_at = now() "
                "WHERE id = :id AND project_id = :project_id"),
            {"id": run_id, "project_id": project_id,
             "decision_id": decision_id, "reason": reason},
        )
    emit_event(
        connection,
        project_id,
        "deployment_run.execute_blocked",
        actor,
        "deployment_run",
        run_id,
        {"reason": reason_code, "changeset_id": changeset_id},
    )
    return {"status": "blocked", "deployment_run_id": run_id,
            "reason": reason, "reason_code": reason_code}


def execute_deployment(engine, settings: Settings, deployment_run_id: str, *,
                       actor: str = "delivery") -> dict[str, Any]:
    """Run a planned deployment through its connector: plan -> apply -> observe.

    The whole point of the connector contract is that this function does not know or care
    which destination it is talking to. Adding Cloudflare or Kubernetes later changes a
    registry entry, not this code — branching per destination here is how the preview
    resolver ended up serving the wrong app from two divergent copies of one decision.

    observe() decides the outcome, never apply()'s exit status: a process that started is not
    a service that works, and a canary that trusts an exit code will promote a broken build.
    """
    from ..db import begin
    from .deployment_connectors import connector_for, preflight
    from .deployment_steps import StepRecorder

    # Two plain reads instead of a join with a hand-listed projection. The projection version
    # silently dropped `required_resources`, so the connector never saw the target's declared
    # app_port and fell back to 8080 — a port already taken on that host. The deployment failed
    # with "address already in use" for a port nobody had configured, which is the least
    # debuggable shape a bug can take.
    #
    # A column list that must be extended every time a consumer reads a new field is a standing
    # invitation to that failure (the same defect as `_row_to_profile` dropping new profile
    # columns). Passing the row through means adding a column can never again strand a feature.
    with begin(engine) as c:
        row = c.execute(sql("SELECT * FROM deployment_runs WHERE id = :i"),
                        {"i": deployment_run_id}).mappings().first()
        if not row:
            return {"status": "not_found", "deployment_run_id": deployment_run_id}
        if row["status"] in {"failed", "blocked"}:
            # Planning may have created an auditable refusal rather than a runnable job.
            # The merge, manual, and redeploy callers all converge here; none may build or
            # touch a host after a policy denial.
            return {"status": "blocked", "deployment_run_id": deployment_run_id,
                    "reason": row.get("failure_reason") or "Deployment policy blocked this run"}
        trow = c.execute(sql("SELECT * FROM deployment_targets WHERE id = :t"),
                         {"t": row["deployment_target_id"]}).mappings().first()
        if not trow:
            return {"status": "not_found", "reason": "deployment_target missing",
                    "deployment_run_id": deployment_run_id}
        target = dict(trow)
        # The connector path below does not yet participate in ChangeSet apply,
        # ResourceReceipt creation, or budget settlement. Keep this guard in the
        # service as well as HTTP, so merge/redeploy/background calls cannot
        # bypass it. Unsupported target kinds have no connector and fail preflight.
        if connector_for(target) is not None or row.get("changeset_id"):
            return block_ungoverned_connector_execution(c, row, settings, actor=actor)
        project_id = row["project_id"]
        run_id = (row["release_packet"] or {}).get("code_task_run_id") or ""

    # A run without a code_task_run_id is no longer an error: the connector builds from the
    # target's own repo at the requested ref. That is what makes a freshly onboarded app
    # deployable at all — requiring a prior code task made the wizard a dead end.
    if not run_id and not (row.get("commit_ref") or target.get("repo")):
        with begin(engine) as c:
            c.execute(sql("UPDATE deployment_runs SET status='blocked', failure_reason=:r, "
                          "updated_at=now() WHERE id=:i"),
                      {"r": "no code task run, repo or ref: nothing to build from",
                       "i": deployment_run_id})
        return {"status": "blocked", "deployment_run_id": deployment_run_id,
                "problems": ["this deployment names neither a code task run nor a repo/ref, "
                             "so there is nothing to build"]}

    # Progress is recorded as it happens, so the console has something to render and a crash
    # leaves evidence of how far it got.
    rec = StepRecorder(engine, deployment_run_id)
    with rec.step("resolve") as h:
        h.log(f"target={target.get('app_name')} env={target.get('environment')}\n"
              f"repo={target.get('repo')} ref={row.get('commit_ref') or '(default branch)'}\n"
              f"host={target.get('target_ref')}")

    # Cheap gate first: never build or transfer anything for a target that cannot accept it.
    # The connection lets preflight RESOLVE the target's bound credential rather than assume
    # a global one — otherwise a per-target secret is checked at deploy time, which is the
    # expensive place to discover it is missing.
    with rec.step("preflight") as h:
        with begin(engine) as c:
            pre = preflight(target, settings, c)
        h.log("ready" if pre["ready"] else "\n".join(pre["problems"]))
    if not pre["ready"]:
        rec.finish()
        with begin(engine) as c:
            c.execute(sql("UPDATE deployment_runs SET status='blocked', failure_reason=:r, "
                          "updated_at=now() WHERE id=:i"),
                      {"r": "; ".join(pre["problems"])[:400], "i": deployment_run_id})
            emit_event(c, project_id, "delivery.blocked", actor, "deployment_run",
                       deployment_run_id, {"problems": pre["problems"]})
        return {"status": "blocked", "problems": pre["problems"]}

    conn = connector_for(target)
    try:
        with rec.step("build", label="Plan and build") as h:
            plan = conn.plan(engine, settings, target, run_id, ref=row.get("commit_ref"))
            h.log(f"strategy={plan.get('strategy', '')}\n"
                  f"branch={plan.get('branch', '')}")
        with begin(engine) as c:
            c.execute(sql("UPDATE deployment_runs SET status='running', started_at=now(), "
                          "release_packet = COALESCE(release_packet,'{}'::jsonb) "
                          "  || jsonb_build_object('plan', CAST(:p AS JSONB)), "
                          "updated_at=now() WHERE id=:i"),
                      {"p": json.dumps(plan, default=str), "i": deployment_run_id})
            emit_event(c, project_id, "delivery.planned", actor, "deployment_run",
                       deployment_run_id, {"connector": conn.kind,
                                           "domains": target.get("domains")})

        # The deployment's own commit_ref is what we promised to ship, so it is what gets
        # built — not whatever the code task's branch happens to point at now.
        with rec.step("transfer", label="Build, transfer and release") as h:
            applied = conn.apply(engine, settings, target, run_id, plan,
                                 ref=row.get("commit_ref"))
            h.log(json.dumps({k: v for k, v in (applied or {}).items()
                              if k not in ("plan",)}, default=str)[:2000])
            applied_status = str((applied or {}).get("status") or "")
            # A rollback can leave the previous release healthy. It must never make this
            # deployment look successful when its requested commit was not left live.
            if applied_status != "deployed":
                status = "blocked" if applied_status == "blocked" else "failed"
                reason = (
                    "; ".join((applied or {}).get("problems") or [])[:400]
                    or (applied or {}).get("detail")
                    or (applied or {}).get("error")
                    or f"apply status={applied_status or 'unknown'}"
                )[:400]
                h.failed(reason)
                total_ms = rec.finish()
                with begin(engine) as c:
                    c.execute(sql("UPDATE deployment_runs SET status=:s, finished_at=now(), "
                                  "duration_ms=:d, failure_reason=:r, updated_at=now() WHERE id=:i"),
                              {"s": status, "d": total_ms, "r": reason, "i": deployment_run_id})
                    emit_event(c, project_id, f"delivery.{status}", actor, "deployment_run",
                               deployment_run_id,
                               {"applied": applied_status, "domains": target.get("domains")})
                return {"status": status, "applied": applied, "healthy": False}

        with rec.step("observe", label="Health check") as h:
            # THE TARGET APPLY() WROTE, NOT THE ONE READ BEFORE IT.
            #
            # `apply()` allocates the real port — `_ensure_port` picks one on the machine and
            # persists it — but it does that in its own copy, so `target` here is still the row
            # as it was BEFORE the deployment. The health check therefore probed the port the
            # target was created with rather than the one the container is published on: a
            # first deployment of a new target went out fine, answered fine, and was recorded
            # as unhealthy against a port with nothing on it. Measured on Change Calendar —
            # container published 3013, health check probed 3000, "Connection refused".
            #
            # Found by an investigation, with the line numbers, while fixing something else.
            with begin(engine) as c:
                _fresh = c.execute(
                    sql("SELECT * FROM deployment_targets WHERE id = :i"),
                    {"i": target["id"]}).mappings().first()
            if _fresh:
                target = dict(_fresh)
            health = conn.observe(target, settings)
            h.log(json.dumps(health, default=str)[:2000])
            if not health.get("healthy"):
                checks = health.get("checks") or [{}]
                first = checks[0]
                h.failed(first.get("error")
                         or f"{first.get('url', 'target')} returned {first.get('status')}, "
                            f"expected {first.get('expected')}")
        # The verdict after a successful placement is health, not the apply result alone.
        ok = bool(health.get("healthy"))
        status = "succeeded" if ok else "failed"
        reason = None if ok else json.dumps(health)[:400]

        # Total wall-clock, written in the SAME statement as the status. The recorder's own
        # flush happens moments earlier, and a separate write for the duration lost the race —
        # which is why "how long did it take" kept coming back empty.
        total_ms = rec.finish()
        with begin(engine) as c:
            # Record the SHA that was actually checked out. Storing the REQUESTED ref is how
            # this field came to name a build that was never running.
            c.execute(sql("UPDATE deployment_runs SET status=:s, finished_at=now(), "
                          "health_check_results=CAST(:h AS JSONB), failure_reason=:r, "
                          "commit_ref=COALESCE(:sha, commit_ref), duration_ms=:d, "
                          "updated_at=now() WHERE id=:i"),
                      {"s": status, "h": json.dumps(health, default=str),
                       "r": reason, "i": deployment_run_id, "d": total_ms,
                       "sha": (applied or {}).get("deployed_sha")})
            emit_event(c, project_id, f"delivery.{status}", actor, "deployment_run",
                       deployment_run_id,
                       {"healthy": ok, "applied": applied.get("status"),
                        "domains": target.get("domains")})
        return {"status": status, "healthy": ok, "applied": applied, "health": health}

    except Exception as exc:  # noqa: BLE001
        total_ms = rec.finish()
        with begin(engine) as c:
            c.execute(sql("UPDATE deployment_runs SET status='failed', finished_at=now(), "
                          "failure_reason=:r, duration_ms=:d, updated_at=now() WHERE id=:i"),
                      {"r": f"{type(exc).__name__}: {exc}"[:400], "i": deployment_run_id,
                       "d": total_ms})
            emit_event(c, project_id, "delivery.failed", actor, "deployment_run",
                       deployment_run_id, {"error": f"{type(exc).__name__}: {exc}"[:300]})
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
