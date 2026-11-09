"""ONE ANSWER to "where does this project's app run, and is it there yet?" (BOSSAPP-930/932).

Every surface that names a project's deployment targets reads them here: the Deployments tab,
the project header and its tiles, the Health environments, the agents' `list_deployments`, and
the Deploy step when it explains a target it could not use. Before this, each read its own
`deployment_targets` query, and they disagreed on the laptop's Hello World (2026-11-08):

  * the header said "hello world rest api · 2.29.2.202:8007" and the card linked :8007 while
    the only deploy there had FAILED — an address was shown as live because a target row held
    it, not because anything answered there;
  * the card's status said "Unknown" (health with no probe) instead of "Last deploy failed";
  * a second card, "boss-sandbox Staging :8006", was onboarding's "Use for staging" slot, made
    before any build named the app, and never deployed — beside the app's real target;
  * the Deploy step said "No such deployment target in this project" about a target it had
    just made in a transaction the planner could not see yet.

THE RULES, decided once:
  * A target's URL is LIVE only after a deploy to it succeeded (deployment_health.newest_deploys,
    the one DEPLOYED rule). Before that it is an `address`, never a link.
  * A target's `state` is what its newest attempt says, in words: Not deployed yet · Setting up
    (first deploy waiting or running) · Deploying · 2/7 · Live · Last deploy failed.
  * ONE STAGING TARGET PER APP. A repository-named slot that never shipped (onboarding's
    "Use for staging", made before any build named the app) is SUPERSEDED once the app has a
    target of its own in that environment: it is hidden from every read here, retired by the
    next deploy, and ADOPTED (renamed, given the app's manifest) by the app's first deploy
    instead of a second target being made.
  * While a stack deploy runs, its steps read as seven phases (prepare → upload → environment
    → switch → install → health → smoke) with status, elapsed time and each phase's log tail.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: The seven phases a stack deploy shows while it runs, in order, with their labels.
PHASES: tuple[tuple[str, str], ...] = (
    ("prepare", "Prepare"), ("upload", "Upload"), ("environment", "Environment"),
    ("switch", "Switch"), ("install", "Install"), ("health", "Health"), ("smoke", "Smoke"))
PHASE_KEYS = tuple(k for k, _ in PHASES)

#: Target states (`state`) and their words when nothing more specific applies.
NOT_DEPLOYED, SETTING_UP, DEPLOYING, WAITING, LIVE, FAILED = (
    "not_deployed", "setting_up", "deploying", "waiting", "live", "failed")

_RUNNING = {"running", "applying", "deploying"}
_WAITING = {"planned", "awaiting_approval", "pending_approval", "pending", "queued"}
_FAILED = {"failed", "unhealthy", "error", "blocked"}


# ── phases ───────────────────────────────────────────────────────────────────────────────────

def phase_of(step_name: Any) -> str:
    """Which of the seven phases a recorded stack step belongs to. The retire steps are part of
    switching releases; every manifest command (install / update / migrate) is install;
    `finalize` is the smoke check that the app answers where it says it does."""
    n = str(step_name or "").strip().lower()
    for key in ("prepare", "upload", "environment", "switch", "health"):
        if n.startswith(key):
            return key
    if n.startswith("retire"):
        return "switch"
    if n.startswith(("smoke", "finalize", "verify")):
        return "smoke"
    return "install"


def _ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        got = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def _log_by_step(log_tail: str) -> dict[str, str]:
    """The runner writes `\\n$ [<step name>]\\n` before each step's output (stack_deploy
    _StackRun._start), so the one redacted tail splits back into each step's own lines."""
    out: dict[str, str] = {}
    parts = re.split(r"\n?\$ \[([^\]\n]+)\]\n", str(log_tail or ""))
    # parts = [before, name1, body1, name2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        name, body = parts[i], parts[i + 1]
        out[name] = (out.get(name, "") + body)
    return out


def progress(steps: Iterable[Mapping[str, Any]], status: Any, *, log_tail: str = "",
             now: Optional[datetime] = None, log_chars: int = 2000) -> dict[str, Any]:
    """The seven phases of one stack deploy, for the live card and the header chip.

    → {phases: [{key, label, status, started_at, ended_at, elapsed_s, log}], index, total,
       current, failed_phase}. A phase is `failed` if any of its steps failed, `running` while
    one runs, `done` when all finished, `skipped` when the plan has none of it (no health
    check declared), else `pending`. `index` is the phase being worked on, 1-based (the one
    that failed, for a failed deploy; `total` once it is done)."""
    now = now or datetime.now(timezone.utc)
    steps = [dict(s) for s in steps or [] if isinstance(s, Mapping)]
    if str(status or "") not in _RUNNING:
        # A SETTLED DEPLOY'S CLOCK STOPS: a step left "running" by a crashed job is timed to
        # the last moment anything was recorded, never to now — so its answer (and its ETag)
        # stays the same on every read.
        seen = [t for s in steps for t in (_ts(s.get("started_at")), _ts(s.get("ended_at"))) if t]
        now = max(seen) if seen else now
    logs = _log_by_step(log_tail)
    grouped: dict[str, list[dict[str, Any]]] = {k: [] for k in PHASE_KEYS}
    for s in steps:
        grouped[phase_of(s.get("name"))].append(s)
    phases = []
    for key, label in PHASES:
        mine = grouped[key]
        states = {str(s.get("status") or "pending") for s in mine}
        if not mine:
            state = "skipped"
        elif "failed" in states:
            state = "failed"
        elif "running" in states:
            state = "running"
        elif states <= {"succeeded", "skipped", "done"}:
            state = "done"
        elif states & {"succeeded", "done"}:
            state = "running"      # some steps finished, the next has not started yet
        else:
            state = "pending"
        started = [t for t in (_ts(s.get("started_at")) for s in mine) if t]
        ended = [t for t in (_ts(s.get("ended_at")) for s in mine) if t]
        start = min(started) if started else None
        end = max(ended) if ended and state in ("done", "failed") else None
        elapsed = int(((end or now) - start).total_seconds()) if start else None
        log = "".join(logs.get(str(s.get("name")), "") for s in mine).strip("\n")
        phases.append({"key": key, "label": label, "status": state,
                       "started_at": start.isoformat() if start else None,
                       "ended_at": end.isoformat() if end else None,
                       "elapsed_s": max(0, elapsed) if elapsed is not None else None,
                       "log": log[-log_chars:] if log else ""})
    present = [p for p in phases if p["status"] != "skipped"]
    total = len(present)
    failed = next((i for i, p in enumerate(present, 1) if p["status"] == "failed"), None)
    running = next((i for i, p in enumerate(present, 1) if p["status"] == "running"), None)
    done = sum(1 for p in present if p["status"] == "done")
    if failed:
        index = failed
    elif running:
        index = running
    elif str(status or "") == "healthy" or (total and done == total):
        index = total
    else:
        index = min(total, done + 1) if str(status or "") in _RUNNING else done
    current = present[index - 1]["key"] if index and index <= total else None
    return {"phases": phases, "index": index, "total": total, "current": current,
            "failed_phase": present[failed - 1]["key"] if failed else None}


# ── targets ──────────────────────────────────────────────────────────────────────────────────

def _repo_name(repo: Any) -> str:
    return str(repo or "").strip().removesuffix(".git").rsplit("/", 1)[-1].lower()


def is_repo_slot(target: Mapping[str, Any]) -> bool:
    """A target named after its REPOSITORY rather than an app: onboarding's "Use for staging"
    (onboarding._stack_name), made before any build said which app the project ships."""
    from .naming import identity_key

    repo = _repo_name(target.get("repo"))
    return bool(repo) and identity_key(str(target.get("app_name") or "")) == identity_key(repo)


def _ever_deployed(connection: Connection, target_ids: list[str]) -> set[str]:
    """Targets anything was ever planned onto (any stack deployment or deployment run)."""
    if not target_ids:
        return set()
    return {str(r[0]) for r in connection.execute(text(
        "SELECT target_id FROM stack_deployments WHERE target_id = ANY(:ids) "
        "UNION SELECT deployment_target_id FROM deployment_runs "
        "WHERE deployment_target_id = ANY(:ids)"), {"ids": list(target_ids)}).all()}


def superseded(connection: Connection, rows: list[Mapping[str, Any]]) -> set[str]:
    """Ids of never-deployed repository slots another active target of the same repository and
    environment has taken over — ONE STAGING TARGET PER APP, whatever made the second."""
    slots = [r for r in rows if is_repo_slot(r)]
    if not slots:
        return set()
    used = _ever_deployed(connection, [str(r["id"]) for r in slots])
    out = set()
    for slot in slots:
        if str(slot["id"]) in used:
            continue
        env = str(slot.get("environment") or "").lower()
        repo = _repo_name(slot.get("repo"))
        if any(str(r["id"]) != str(slot["id"]) and str(r.get("environment") or "").lower() == env
               and _repo_name(r.get("repo")) in (repo, "") for r in rows):
            out.add(str(slot["id"]))
    return out


def project_targets(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """THE project's active targets, every column, superseded slots left out: the one listing
    the Deployments tab, the header, Health and the agents read."""
    rows = [dict(r) for r in connection.execute(text(
        "SELECT * FROM deployment_targets WHERE project_id = :p AND status = 'active' "
        "ORDER BY created_at"), {"p": project_id}).mappings().all()]
    gone = superseded(connection, rows)
    return [r for r in rows if str(r["id"]) not in gone]


def adoptable_slot(connection: Connection, project_id: str, environment: str,
                   repository: str) -> Optional[dict[str, Any]]:
    """The never-deployed repository slot an app's FIRST deploy takes over instead of making a
    second target beside it — only while it is the only active target of that repository in
    that environment (a slot another app already took is not free)."""
    env = str(environment or "staging").lower()
    repo = _repo_name(repository)
    rows = [dict(r) for r in connection.execute(text(
        "SELECT * FROM deployment_targets WHERE project_id = :p AND status = 'active' "
        "AND lower(environment) = :e ORDER BY created_at"),
        {"p": project_id, "e": env}).mappings().all()]
    same = [r for r in rows if _repo_name(r.get("repo")) in (repo, "")]
    if len(same) != 1 or not is_repo_slot(same[0]) or not same[0].get("host_id"):
        return None
    if _ever_deployed(connection, [str(same[0]["id"])]):
        return None
    return same[0]


def adopt_slot(connection: Connection, slot: Mapping[str, Any], *, app_name: str,
               manifest_path: Optional[str], actor: str) -> dict[str, Any]:
    """Give the slot to the app: its name and its manifest. Nothing was ever deployed there, so
    nothing it carried is lost; its port and host stay."""
    from .core import emit_event

    connection.execute(text(
        "UPDATE deployment_targets SET app_name = :a, manifest_path = COALESCE(:m, manifest_path), "
        "updated_at = now() WHERE id = :i"),
        {"a": app_name, "m": manifest_path, "i": slot["id"]})
    emit_event(connection, str(slot["project_id"]), "deployment.target_adopted", actor,
               "deployment_target", str(slot["id"]),
               {"from_name": slot.get("app_name"), "app_name": app_name,
                "manifest_path": manifest_path, "environment": slot.get("environment"),
                "why": "the app's first deploy takes the project's unused staging slot rather "
                       "than making a second staging target"})
    row = connection.execute(text("SELECT * FROM deployment_targets WHERE id = :i"),
                             {"i": slot["id"]}).mappings().first()
    return dict(row) if row else dict(slot)


def retire_superseded(connection: Connection, project_id: str, *, actor: str) -> list[str]:
    """Retire every superseded slot of the project (the read already hides them); returns ids."""
    from .core import emit_event

    rows = [dict(r) for r in connection.execute(text(
        "SELECT * FROM deployment_targets WHERE project_id = :p AND status = 'active'"),
        {"p": project_id}).mappings().all()]
    gone = sorted(superseded(connection, rows))
    for tid in gone:
        connection.execute(text(
            "UPDATE deployment_targets SET status = 'retired', updated_at = now() "
            "WHERE id = :i AND status = 'active'"), {"i": tid})
        emit_event(connection, project_id, "deployment.target_retired", actor,
                   "deployment_target", tid,
                   {"why": "a never-deployed repository slot: the app has its own target in "
                           "this environment (one staging target per app)"})
    return gone


def why_not_found(connection: Connection, target_id: Any, project_id: str) -> str:
    """The deploy's lookup, said: which target it looked for and why it is not usable here."""
    tid = str(target_id or "").strip()
    if not tid:
        return "The deploy named no target."
    row = connection.execute(text(
        "SELECT id, project_id, app_name, environment, status FROM deployment_targets "
        "WHERE id = :i"), {"i": tid}).mappings().first()
    if not row:
        return (f"It looked for target {tid}, and no target with that id exists (it may have "
                "been made in a step that had not finished saving it).")
    name = f"{row['app_name']!r} ({tid}, {row['environment']})"
    if row["project_id"] != project_id:
        return f"It looked for target {name}, which belongs to project {row['project_id']}."
    if row["status"] != "active":
        return f"It looked for target {name}, which is {row['status']}."
    return f"It looked for target {name}."


# ── truth ────────────────────────────────────────────────────────────────────────────────────

def _attempts(connection: Connection, target_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Per target, its newest deploy attempt, whichever way it ships (a stack deployment or a
    one-container deployment run). A request a later one overtook (superseded) is not one."""
    if not target_ids:
        return {}
    rows = connection.execute(text(
        "SELECT DISTINCT ON (t) * FROM ("
        "  SELECT target_id AS t, id, 'stack' AS kind, status, commit_sha AS commit, steps, "
        "         log_tail, failure_reason, error::text AS error, open_url, created_at, "
        "         apply_started_at, finished_at, workflow_run_id "
        "  FROM stack_deployments WHERE target_id = ANY(:ids) AND status <> 'superseded' "
        "  UNION ALL "
        "  SELECT deployment_target_id AS t, id, 'registry' AS kind, status, commit_ref AS commit, "
        "         steps, '' AS log_tail, failure_reason, NULL AS error, NULL AS open_url, "
        "         created_at, started_at AS apply_started_at, finished_at, NULL AS workflow_run_id "
        "  FROM deployment_runs WHERE deployment_target_id = ANY(:ids)"
        ") a ORDER BY t, created_at DESC"), {"ids": list(target_ids)}).mappings().all()
    return {str(r["t"]): dict(r) for r in rows}


def _reason(attempt: Mapping[str, Any]) -> Optional[str]:
    import json

    said = " ".join(str(attempt.get("failure_reason") or "").split())
    if not said and attempt.get("error"):
        try:
            err = json.loads(str(attempt["error"]))
        except ValueError:
            err = {}
        said = " ".join(str((err or {}).get("headline") or "").split())
    return said or None


def truth(connection: Connection, targets: list[Mapping[str, Any]], *,
          now: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """Per target id: {state, word, live, live_url, address, attempt, reason, progress}.

    `live_url` is set ONLY when a deploy to the target succeeded; `address` is where it will
    answer, for "Setting up" words without a link."""
    from .deployment_health import newest_deploys
    from .project_experience.common import web_url

    ids = [str(t["id"]) for t in targets]
    live = newest_deploys(connection, ids)
    attempts = _attempts(connection, ids)
    stack_live = {str(r["t"]): r["open_url"] for r in connection.execute(text(
        "SELECT DISTINCT ON (target_id) target_id AS t, open_url FROM stack_deployments "
        "WHERE target_id = ANY(:ids) AND status = 'healthy' "
        "ORDER BY target_id, COALESCE(finished_at, created_at) DESC"),
        {"ids": ids}).mappings()} if ids else {}
    out: dict[str, dict[str, Any]] = {}
    for t in targets:
        tid = str(t["id"])
        domains = t.get("domains") if isinstance(t.get("domains"), list) else []
        address = web_url(t.get("public_url") or (domains[0] if domains else None))
        was_live = tid in live
        a = attempts.get(tid)
        status = str((a or {}).get("status") or "")
        newer = bool(a) and (not was_live or (_ts(a["created_at"]) or now)
                             > (_ts(live[tid].get("finished_at")) or datetime.min.replace(
                                 tzinfo=timezone.utc)))
        prog = None
        if a and a["kind"] == "stack" and (status in _RUNNING or status in _FAILED):
            prog = progress(a.get("steps") or [], status, log_tail=a.get("log_tail") or "",
                            now=now)
        if a and newer and status in _RUNNING:
            state = DEPLOYING
            step = f" · {prog['index']}/{prog['total']}" if prog and prog["total"] else ""
            word = f"Deploying{step}" if was_live else f"Setting up{step}"
        elif a and newer and status in _WAITING:
            state = WAITING
            word = "Update waiting for approval" if was_live else "Setting up · waiting for approval"
        elif a and newer and status in _FAILED:
            state = FAILED
            word = "Last deploy failed"
        elif was_live:
            state, word = LIVE, "Live"
        else:
            state, word = NOT_DEPLOYED, "Not deployed yet"
        live_url = web_url(stack_live.get(tid) or address) if was_live else None
        out[tid] = {
            "state": state, "word": word, "live": was_live, "live_url": live_url,
            "address": address,
            "reason": _reason(a) if state == FAILED and a else None,
            "attempt": ({"id": a["id"], "kind": a["kind"], "status": status,
                         "commit": (str(a.get("commit") or "")[:7] or None),
                         "workflow_run_id": a.get("workflow_run_id"),
                         "at": (_ts(a.get("finished_at") or a.get("apply_started_at")
                                    or a.get("created_at")) or now or datetime.now(timezone.utc)
                                ).isoformat()} if a else None),
            "progress": prog,
        }
    return out


def project_deploy(connection: Connection, project_id: str, *,
                   now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """The header chip's one deploy: the newest attempt that is running, else one that failed
    and has not been followed by a success. None when nothing is moving or broken."""
    targets = project_targets(connection, project_id)
    told = truth(connection, targets, now=now)
    named = {str(t["id"]): t for t in targets}
    for want in (DEPLOYING, WAITING, FAILED):
        hits = [(tid, v) for tid, v in told.items() if v["state"] == want]
        if hits:
            tid, v = max(hits, key=lambda kv: (kv[1]["attempt"] or {}).get("at") or "")
            t = named[tid]
            return {"target_id": tid, "app": t.get("app_name"),
                    "environment": t.get("environment"), **v}
    return None


def targets_view(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """What the agents read (list_deployments): each target with its truth, in plain fields."""
    targets = project_targets(connection, project_id)
    told = truth(connection, targets)
    return [{"target_id": str(t["id"]), "app_name": t.get("app_name"),
             "environment": t.get("environment"), "repo": t.get("repo"),
             "target_kind": t.get("target_kind"), "manifest_path": t.get("manifest_path"),
             "host_id": t.get("host_id"), **{k: v for k, v in told[str(t["id"])].items()
                                             if k != "progress"}}
            for t in targets]


__all__ = ["PHASES", "adopt_slot", "adoptable_slot", "is_repo_slot", "phase_of", "progress",
           "project_deploy", "project_targets", "retire_superseded", "superseded", "targets_view",
           "truth", "why_not_found"]
