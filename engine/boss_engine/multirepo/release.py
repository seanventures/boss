"""Deploy a change set together: ordered waves, one rollback (Infra › Deploys › the change).

Builds on "a new app's first deploy needs only a machine" (delivery_pipeline.deploy_build): each
component that ships to a machine is deployed by the same governed path a single app uses — its
own deployment target (created on first deploy, on the project's verified machine), a ChangeSet,
a health check — in the components' dependency order, one wave at a time.

ONE APPROVAL COVERS IT. The person approved "Approve and ship all"; each component's deploy
decision is approved under that approval (recorded as such, naming the person and the set), so
nobody is asked N more times — and nothing deploys that the set's approval did not cover.

ONE ROLLBACK. If a wave fails (a deploy that fails or rolls itself back, a failed health check),
every component already deployed in this release is put back to its previous release, in reverse
order, and the set is `rolled_back`. "Roll back all" does the same by hand after the release went
live. A component on its first deploy has no previous release: it is said so, never pretended.

Components that do not ship to a machine (a contracts package, a library) are released by the
merge: their wave is recorded as such.

The plan refuses to start (`ready: false`, with sentences) when a component that ships to a
machine has nowhere to go: no target and no verified machine in the project.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .common import MultiRepoError, event, iso, jdump, jload, sql

log = logging.getLogger(__name__)

ENVIRONMENT = "production"
OK = ("succeeded", "healthy")
BAD = ("failed", "blocked", "cancelled", "rolled_back", "rollback_pending", "unhealthy")


def _short(sha: Optional[str]) -> str:
    return (sha or "")[:7] or "—"


def _usable() -> str:
    from ..services.deployment_fleet import usable_hosts_sql
    return usable_hosts_sql()


def _verified_host(db: Any, project_id: str) -> Optional[dict[str, Any]]:
    row = db.execute(sql(f"SELECT id, name, address FROM deployment_hosts WHERE {_usable()} AND status = 'verified' "
                         "ORDER BY created_at LIMIT 1"), {"p": project_id}).mappings().first()
    return dict(row) if row else None


def _target(db: Any, project_id: str, ship: dict[str, Any], app_label: str) -> Optional[dict[str, Any]]:
    if ship.get("target_id"):
        row = db.execute(sql("SELECT * FROM deployment_targets WHERE id = :i AND project_id = :p"),
                         {"i": ship["target_id"], "p": project_id}).mappings().first()
        return dict(row) if row else None
    row = db.execute(sql("SELECT * FROM deployment_targets WHERE project_id = :p AND lower(app_name) = lower(:a) "
                         "AND environment = :e AND status = 'active' ORDER BY created_at DESC LIMIT 1"),
                     {"p": project_id, "a": app_label, "e": ENVIRONMENT}).mappings().first()
    return dict(row) if row else None


def _last_good(db: Any, target_id: str) -> Optional[dict[str, Any]]:
    row = db.execute(sql("SELECT id, commit_ref, finished_at FROM deployment_runs WHERE deployment_target_id = :t "
                         "AND status IN ('succeeded', 'healthy') ORDER BY finished_at DESC NULLS LAST LIMIT 1"),
                     {"t": target_id}).mappings().first()
    return dict(row) if row else None


def component_label(cs: dict[str, Any], components: list[str]) -> str:
    return f"{cs.get('app_name') or cs['app_key']} {' + '.join(components)}"


def plan(db: Any, cs_id: str) -> dict[str, Any]:
    """What deploying the set will do, wave by wave, and how each wave is undone."""
    from . import change_sets, components

    cs = change_sets._row(db, cs_id)  # noqa: SLF001
    app = components.get(db, cs["project_id"], cs["app_key"])
    comp_by = {c["name"]: c for c in app["components"]}
    lanes = [lane for lane in change_sets._lanes(db, cs_id) if lane["lane_state"] != "unchanged"]  # noqa: SLF001
    lanes.sort(key=lambda x: (x["wave"], x["full_name"]))
    rel = jload(cs.get("release"), {}) or {}
    steps_state = {s["lane_id"]: s for s in rel.get("steps") or []}
    host = _verified_host(db, cs["project_id"])
    problems: list[str] = []
    waves = []
    for i, lane in enumerate(lanes):
        comps = [comp_by[c] for c in lane["components"] if c in comp_by]
        ship = (comps[0]["ship_target"] if comps else {}) or {"kind": "host"}
        names = [c["name"] for c in comps] or list(lane["components"])
        label = component_label(cs, names)
        to_sha = lane.get("merged_sha") or lane.get("head_sha")
        st = steps_state.get(lane["id"]) or {}
        item: dict[str, Any] = {"order": i + 1, "lane_id": lane["id"], "components": names,
                                "repository": lane["full_name"], "ship": ship, "to": _short(to_sha),
                                "status": st.get("status") or "planned", "deployment_run_id": st.get("deployment_run_id"),
                                "error": st.get("error")}
        if ship.get("kind") in ("none", "package"):
            what = f"Publish {ship.get('package')}" if ship.get("kind") == "package" and ship.get("package") else \
                "Released by the merge"
            item.update(kind="merge", **{"from": "—"}, steps=[{"icon": "check", "text": f"{what} at {_short(to_sha)}"},
                                                               {"icon": "check", "text": "Additive: what depends on it "
                                                                                         "keeps working until it updates"}],
                        rollback="Revert the merge (a revert pull request)")
        else:
            target = _target(db, cs["project_id"], ship, label)
            previous = _last_good(db, target["id"]) if target else None
            where = (target.get("public_url") or ", ".join(target.get("domains") or []) or target.get("target_ref")) \
                if target else (f"a new {ENVIRONMENT} target on {host['name']}" if host else None)
            if not target and not host:
                problems.append(f"{' + '.join(names)} has nowhere to ship: add a machine in Infra › Hosts (it needs "
                                "only a machine), or choose its target in the app's Components.")
            item.update(kind="host", target_id=(target or {}).get("id"), host=(host or {}).get("name"),
                        **{"from": _short((previous or {}).get("commit_ref")) if previous else "not deployed"},
                        steps=[{"icon": "deploy", "text": f"Deploy {_short(to_sha)} to {where or '—'}"},
                               {"icon": "health", "text": "Health check must pass, else this wave rolls back"},
                               {"icon": "check", "text": "Then the next wave"}],
                        rollback=(f"Previous release {_short(previous['commit_ref'])}" if previous
                                  else "First deploy: nothing earlier to restore; the new release is stopped"))
        waves.append(item)
    rollback = [{"order": w["order"], "repository": w["repository"], "components": w["components"],
                 "text": (f"{w['components'][0].lower() if w['components'] else ''} → {w['rollback']}"),
                 "status": "ready" if w["status"] in ("planned", "live", "deploying") else w["status"]}
                for w in reversed(waves)]
    return {"change_set_id": cs_id, "state": cs["state"], "status": rel.get("status") or "planned",
            "environment": ENVIRONMENT, "host": (host or {}).get("name"), "ready": not problems,
            "problems": problems, "waves": waves, "rollback": rollback,
            "auto_rollback": "If a deploy fails or its health check fails, every component already deployed in "
                             "this release goes back to its previous release, newest first.",
            "approved_by": cs.get("approved_by"), "started_at": rel.get("started_at"),
            "finished_at": rel.get("finished_at")}


def _save(db: Any, cs_id: str, rel: dict[str, Any]) -> None:
    db.execute(sql("UPDATE repo_change_sets SET release = CAST(:r AS JSONB), updated_at = now() WHERE id = :i"),
               {"r": jdump(rel), "i": cs_id})


def after_merge(db: Any, settings: Any, cs_id: str, *, actor: str = "boss") -> dict[str, Any]:
    """Called once the whole set merged: start the release when the plan is ready, else wait
    (merged) for a person to give the components somewhere to ship."""
    from . import change_sets

    p = plan(db, cs_id)
    if all(w["kind"] == "merge" for w in p["waves"]):
        change_sets._set_state(db, cs_id, "live")  # noqa: SLF001
        _save(db, cs_id, {"status": "live", "steps": [{"lane_id": w["lane_id"], "status": "live"} for w in p["waves"]],
                          "finished_at": iso(datetime.now(timezone.utc))})
        _end(db, cs_id)
        return {"status": "live"}
    if not p["ready"]:
        _save(db, cs_id, {"status": "needs_target", "problems": p["problems"]})
        return {"status": "needs_target", "problems": p["problems"]}
    return start(db, settings, cs_id, actor=actor)


def start(db: Any, settings: Any, cs_id: str, *, actor: str = "boss") -> dict[str, Any]:
    from . import change_sets

    cs = change_sets._row(db, cs_id, lock=True)  # noqa: SLF001
    if cs["state"] not in ("merged",):
        raise MultiRepoError(409, "not_merged", f"This change is {cs['state']}; it deploys once every PR merged.")
    p = plan(db, cs_id)
    if not p["ready"]:
        raise MultiRepoError(409, "plan_not_ready", " ".join(p["problems"]))
    rel = {"status": "deploying", "started_at": iso(datetime.now(timezone.utc)), "by": actor,
           "steps": [{"lane_id": w["lane_id"], "order": w["order"], "kind": w["kind"], "status": "planned",
                      "target_id": w.get("target_id")} for w in p["waves"]]}
    _save(db, cs_id, rel)
    change_sets._set_state(db, cs_id, "deploying")  # noqa: SLF001
    event(db, cs["project_id"], "change_set.deploy_started", actor, "change_set", cs_id, {})
    return {"status": "deploying"}


def _approve_under_set(engine: Any, settings: Any, decision_id: Optional[str], cs: dict[str, Any]) -> bool:
    """A component's deploy decision is approved UNDER the set's approval (recorded as such)."""
    if not decision_id:
        return True
    from ..db import begin
    from ..services.decisions import SYSTEM_ACTOR, DecisionService

    try:
        with begin(engine) as c:
            status = c.execute(sql("SELECT status FROM decisions WHERE id = :d"), {"d": decision_id}).scalar()
            if status == "approved":
                return True
            DecisionService(settings).transition(
                c, decision_id, action="approve", actor=SYSTEM_ACTOR, system_policy=True,
                note=(f"Covered by {cs.get('approved_by') or 'the approver'}'s approval of change {cs['id']} "
                      "(approve and ship all)."))
        return True
    except Exception as error:  # noqa: BLE001 — then a person answers it in Approvals
        log.warning("change set %s: deploy decision %s not approved under the set: %s", cs["id"], decision_id, error)
        return False


def advance(engine: Any, settings: Any, cs_id: str) -> list[str]:
    """One step of a running release (deploying, or rolling back). Called every tick."""
    from ..db import begin
    from ..services import delivery_pipeline
    from . import change_sets

    with begin(engine) as db:
        cs = change_sets._row(db, cs_id)  # noqa: SLF001
        rel = jload(cs.get("release"), {}) or {}
        lanes = {lane["id"]: lane for lane in change_sets._lanes(db, cs_id)}  # noqa: SLF001
    if cs["state"] != "deploying":
        return []
    steps = rel.get("steps") or []
    done: list[str] = []
    if rel.get("status") == "rolling_back":
        return _advance_rollback(engine, settings, cs, rel)
    current = next((s for s in steps if s["status"] not in ("live", "skipped")), None)
    if current is None:
        with begin(engine) as db:
            rel.update(status="live", finished_at=iso(datetime.now(timezone.utc)))
            _save(db, cs_id, rel)
            change_sets._set_state(db, cs_id, "live")  # noqa: SLF001
            _end(db, cs_id)
            event(db, cs["project_id"], "change_set.live", "boss", "change_set", cs_id, {})
        return ["live"]
    lane = lanes[current["lane_id"]]
    if current["kind"] == "merge":
        current["status"] = "live"
        done.append(f"released {lane['full_name']} by the merge")
    elif current["status"] == "planned":
        with begin(engine) as db:
            comps = lane["components"] or []
            label = component_label(cs, comps)
            db.execute(sql("UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
                           "jsonb_build_object('app_name', CAST(:n AS text)) WHERE id = :r"),
                       {"n": label, "r": lane["code_task_run_id"]})
            host = _verified_host(db, cs["project_id"])
        out = delivery_pipeline.deploy_build(engine, settings, project_id=cs["project_id"],
                                             run_id=lane["code_task_run_id"], environment=ENVIRONMENT,
                                             target_id=current.get("target_id"), host_id=(host or {}).get("id"),
                                             actor="boss")
        if out.get("status") != "planned":
            current.update(status="failed", error=str(out.get("reason") or out.get("status"))[:400])
        else:
            current.update(status="deploying", deployment_run_id=out.get("deployment_run_id"),
                           target_id=out.get("target_id"), decision_id=out.get("decision_id"))
            if not _approve_under_set(engine, settings, out.get("decision_id"), cs):
                current["note"] = "Waiting for a person to approve this deploy in Approvals."
        done.append(f"deploying {lane['full_name']}")
    elif current["status"] == "deploying":
        with begin(engine) as db:
            status = db.execute(sql("SELECT status, failure_reason FROM deployment_runs WHERE id = :i"),
                                {"i": current.get("deployment_run_id")}).mappings().first()
        if status and status["status"] in OK:
            current["status"] = "live"
            done.append(f"{lane['full_name']} healthy")
        elif status and status["status"] in BAD:
            current.update(status="failed", error=str(status["failure_reason"] or status["status"])[:400])
    if current["status"] == "failed":
        rel["status"] = "rolling_back"
        rel["failed"] = {"lane_id": lane["id"], "repository": lane["full_name"], "error": current.get("error")}
        done.append(f"{lane['full_name']} failed: rolling back")
        with begin(engine) as db:
            event(db, cs["project_id"], "change_set.deploy_failed", "boss", "change_set", cs_id,
                  {"repository": lane["full_name"], "error": current.get("error")})
    with begin(engine) as db:
        rel["steps"] = steps
        _save(db, cs_id, rel)
    return done


def _advance_rollback(engine: Any, settings: Any, cs: dict[str, Any], rel: dict[str, Any]) -> list[str]:
    """Put every component this release deployed back, newest first, one at a time."""
    from ..db import begin
    from ..services import delivery_pipeline
    from . import change_sets

    steps = rel.get("steps") or []
    todo = [s for s in reversed(steps) if s.get("deployment_run_id") and s["status"] in ("live", "deploying", "failed")
            and not s.get("rollback")]
    busy = next((s for s in steps if (s.get("rollback") or {}).get("status") == "deploying"), None)
    done: list[str] = []
    if busy is not None:
        rb = busy["rollback"]
        with begin(engine) as db:
            st = db.execute(sql("SELECT status FROM deployment_runs WHERE id = :i"),
                            {"i": rb.get("deployment_run_id")}).scalar()
        if st in OK:
            rb["status"] = "done"
            busy["status"] = "rolled_back"
        elif st in BAD:
            rb.update(status="failed", error=f"the rollback deploy ended {st}")
            busy["status"] = "rollback_failed"
    elif todo:
        step = todo[0]
        with begin(engine) as db:
            previous = db.execute(sql("SELECT id, commit_ref FROM deployment_runs WHERE deployment_target_id = "
                                      "(SELECT deployment_target_id FROM deployment_runs WHERE id = :d) AND status IN "
                                      "('succeeded', 'healthy') AND id <> :d AND created_at < (SELECT created_at FROM "
                                      "deployment_runs WHERE id = :d) ORDER BY created_at DESC LIMIT 1"),
                                  {"d": step["deployment_run_id"]}).mappings().first()
        if previous is None:
            step["rollback"] = {"status": "nothing_earlier",
                                "note": "First deploy of this component: there is no earlier release to restore."}
            step["status"] = "rolled_back"
        else:
            out = delivery_pipeline.redeploy(engine, settings, previous["id"], actor="boss", execute=False)
            if out.get("status") == "planned":
                step["rollback"] = {"status": "deploying", "deployment_run_id": out.get("deployment_run_id"),
                                    "to": _short(previous["commit_ref"])}
                _approve_under_set(engine, settings, out.get("decision_id"), cs)
            else:
                step["rollback"] = {"status": "failed", "error": str(out.get("reason") or out.get("status"))[:300]}
                step["status"] = "rollback_failed"
        done.append("rolling back")
    else:
        failed = [s for s in steps if s["status"] == "rollback_failed"]
        rel.update(status="rolled_back" if not failed else "rollback_failed",
                   finished_at=iso(datetime.now(timezone.utc)))
        with begin(engine) as db:
            change_sets._set_state(db, cs["id"], "rolled_back",  # noqa: SLF001
                                   reason=("Rolled back: " + str((rel.get("failed") or {}).get("repository") or "a "
                                                                 "component") + " failed to deploy."
                                           + (" Some components could not be rolled back — see the plan." if failed else "")))
            _end(db, cs["id"])
            event(db, cs["project_id"], "change_set.rolled_back", "boss", "change_set", cs["id"],
                  {"failed": rel.get("failed"), "rollback_failed": [s["lane_id"] for s in failed]})
        done.append("rolled_back")
    with begin(engine) as db:
        rel["steps"] = steps
        _save(db, cs["id"], rel)
    return done


def roll_back_all(db: Any, cs_id: str, *, actor: str) -> dict[str, Any]:
    """"Roll back all": a live (or deploying) release goes back, every component, newest first."""
    from . import change_sets

    cs = change_sets._row(db, cs_id, lock=True)  # noqa: SLF001
    if cs["state"] not in ("live", "deploying"):
        raise MultiRepoError(409, "nothing_to_roll_back", f"This change is {cs['state']}; nothing of it is deployed.")
    rel = jload(cs.get("release"), {}) or {}
    rel.update(status="rolling_back", requested_by=actor, failed=rel.get("failed") or {"repository": None,
                                                                                         "error": "Roll back all"})
    _save(db, cs_id, rel)
    db.execute(sql("UPDATE repo_change_sets SET state = 'deploying', approved_by = COALESCE(approved_by, :a), "
                   "updated_at = now() WHERE id = :i"), {"a": actor, "i": cs_id})
    event(db, cs["project_id"], "change_set.rollback_requested", actor, "change_set", cs_id, {})
    return {"status": "rolling_back"}


def _end(db: Any, cs_id: str) -> None:
    from . import grants

    grants.end(db, cs_id)


def advance_all(engine: Any, settings: Any) -> dict[str, Any]:
    from ..db import begin

    try:
        with begin(engine) as db:
            ids = [str(r) for r in db.execute(sql("SELECT id FROM repo_change_sets WHERE mode = 'multi' AND "
                                                  "state = 'deploying' ORDER BY updated_at")).scalars().all()]
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Any] = {}
    for cs_id in ids:
        try:
            out[cs_id] = advance(engine, settings, cs_id)
        except Exception as error:  # noqa: BLE001
            log.warning("change set %s release did not advance: %s: %s", cs_id, type(error).__name__, error)
            out[cs_id] = {"error": type(error).__name__}
    return out


def component_status(db: Any, project_id: str, app_key: str) -> dict[str, dict[str, Any]]:
    """What is live per component (App › Components): the newest merged commit of the app's sets,
    where it ships, and how its last deploy went."""
    from . import components

    app = components.get(db, project_id, app_key)
    out: dict[str, dict[str, Any]] = {}
    rows = db.execute(sql(
        "SELECT p.components, p.merged_sha, p.merged_at, s.state, s.release FROM change_set_prs p "
        "JOIN repo_change_sets s ON s.id = p.change_set_id WHERE s.project_id = :p AND s.app_key = :a "
        "AND p.merged_sha IS NOT NULL ORDER BY p.merged_at DESC"), {"p": project_id, "a": app_key}).mappings().all()
    for c in app["components"]:
        ship = c.get("ship_target") or {}
        merged = next((r for r in rows if c["name"] in (r["components"] or []) and r["state"] in ("merged", "deploying",
                                                                                                 "live")), None)
        target = _target(db, project_id, ship, f"{app['app_name']} {c['name']}") if ship.get("kind", "host") == "host" \
            else None
        last = _last_good(db, target["id"]) if target else None
        ships_to = (ship.get("label") or ship.get("package") or (target or {}).get("public_url")
                    or ", ".join((target or {}).get("domains") or []) or None)
        if ship.get("kind") == "none":
            ships_to = ships_to or "with the merge"
        health = None
        if target:
            st = db.execute(sql("SELECT status FROM deployment_runs WHERE deployment_target_id = :t ORDER BY "
                                "created_at DESC LIMIT 1"), {"t": target["id"]}).scalar()
            health = "healthy" if st in OK else ("failing" if st in BAD else ("deploying" if st else None))
        elif merged:
            health = "published"
        out[c["name"]] = {"live": _short((last or {}).get("commit_ref") or (merged or {}).get("merged_sha"))
                          if (last or merged) else None,
                          "ships_to": ships_to, "health": health, "target_id": (target or {}).get("id")}
    return out
