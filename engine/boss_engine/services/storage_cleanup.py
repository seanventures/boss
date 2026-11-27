"""THE TIERED CLEANUP EXECUTOR — deterministic Layer 1 (LLD §4.3; design decisions 2 and 3).

    tier = reversibility × label × approved rule

    1  rebuildable, BOSS-labelled                        automatic     build cache, old images,
                                                                        dead pg temp, orphan volumes,
                                                                        logs, scratch DBs, Docker trim
    2  irreversible, BOSS-labelled, past an APPROVED rule automatic     backups beyond keep-N,
                                                                        telemetry partitions
    3  irreversible with no rule yet, or anything unlabelled   ONE Decision card (kind
       `storage_delete`) that carries the rule proposal: "ask once, automatic after"

Rules of the executor: dry run first (the exact item list and bytes; the real call must carry the
dry run's `plan_hash` or it is refused); a receipt per real step (`storage:{incident}:{tool}:
{plan_hash}`); stop when enough (free ≥ target ends the plan); labelled only — the host helper
filters by `boss.install_id` itself, and the API-side tools refuse any database not named
`boss_scratch_*`; no shell strings — typed verbs and typed arguments only. The executor never
waits on the Infra Agent: tiers 1–2 run from the rules whether or not the model is up.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy import text

from . import host_jobs, storage_rules
from .core import make_id

_log = logging.getLogger("boss.storage_cleanup")

#: Re-ask a "Not now" after this long, or on a further fall (the next escalation re-plans).
ASK_AGAIN_AFTER = timedelta(hours=1)
#: THE CARD'S FORM (typed_forms): what the console may send in the approve body's `answer`.
#: `make_rule` (checkbox, defaults to the card's pre-tick) and `args` (an object: "Keep 20
#: instead"). Without a form, typed_forms.record_answer refuses every answer.
ASK_FORM: dict = {"fields": [
    {"key": "make_rule", "type": "checkbox", "label": "Make this a permanent rule", "required": False,
     "help": "Approved once; next time this step is tier 2 and automatic."},
    {"key": "args", "type": "object", "label": "Other arguments", "required": False,
     "help": "e.g. {\"keep_newest\": 20}: planned again with these, re-asked if still tier 3."},
]}
#: A dry run older than this is re-planned before a real run (the disk moved on).
DRY_RUN_FRESH_FOR = timedelta(minutes=30)
IDLE_IN_TRANSACTION_MINUTES = 10
SCRATCH_PREFIX = "boss_scratch_"
TERMINAL = frozenset({"done", "failed", "skipped", "refused"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def install_id(connection: Any = None) -> str:
    """BOSS_INSTALL_ID, else the id the resource ledger recorded (resource_ledger.install_id), so
    the labels the executor asks for are the ones BOSS wrote. Empty only when neither exists —
    and then the helper refuses every job (its own id must match, and "" never does)."""
    env = (os.environ.get("BOSS_INSTALL_ID") or "").strip()
    if env or connection is None:
        return env
    try:
        from .resource_ledger import install_id as _ledger_id

        return str(_ledger_id(connection) or "").strip()
    except Exception:  # noqa: BLE001 — no ledger: no id
        return ""


# ── the tool catalogue ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolDef:
    name: str
    label: str                      # ≤ 4 words
    where: str                      # host | api
    reversibility: str              # safe | irreversible
    verb: Optional[str] = None      # host_jobs verb
    rule_kind: Optional[str] = None # the retention rule it reads
    needs_rule: bool = False        # irreversible: automatic only once a rule is approved
    applies: Optional[Callable[[Mapping[str, Any], Mapping[str, Mapping[str, Any]]], bool]] = None
    args_of: Optional[Callable[[Mapping[str, Any], Mapping[str, Mapping[str, Any]]], dict]] = None


def _alloc_unused(s: Mapping[str, Any]) -> float:
    a, u = s.get("docker_alloc_gb"), s.get("docker_used_gb")
    return max(0.0, _f(a) - _f(u)) if a is not None and u is not None else 0.0


def _volumes_reclaimable(s: Mapping[str, Any]) -> float:
    """What the orphan-volume job could really free: the helper's measured figure (ours, of a
    rebuildable kind, dangling, old enough). `volumes_gb` is every volume on the disk, data ones
    included, so it is never the reason to plan this step (BOSSAPP-1193). A volume that needs a
    relabel is not a reason either — no job can remove it; the Storage card names it."""
    return _f((s.get("by_type") or {}).get("volumes_reclaimable"))


def _cache_over_cap(s: Mapping[str, Any], r: Mapping[str, Any]) -> float:
    from .storage_watch import cache_freeable_gb, cache_owned

    # A shared Docker's cache is not BOSS's to clear: the helper refuses it, so it is no step;
    # the Storage card's budget row tells the person, with the command.
    if cache_owned(s) is False:
        return 0.0
    return _f(cache_freeable_gb(s)) - _f(r.get("build_cache", {}).get("cap_gb", 10))


TOOLS: tuple[ToolDef, ...] = (
    ToolDef("storage.prune_build_cache", "Prune build cache", "host", "safe", "prune_build_cache",
            # Only what Docker can free, past the cap: a cache held by installed images is not a
            # step that could free anything (ENT-SETUP-01).
            "build_cache", applies=lambda s, r: _cache_over_cap(s, r) > 0.5,
            args_of=lambda s, r: {"keep_storage_gb": int(r.get("build_cache", {}).get("cap_gb", 10))}),
    ToolDef("storage.remove_images", "Remove old images", "host", "safe", "remove_images", "images",
            applies=lambda s, r: _f(s.get("images_gb")) > 1.0,
            args_of=lambda s, r: {"keep_versions": int(r.get("images", {}).get("keep_versions", 2))}),
    ToolDef("storage.remove_pg_temp", "Remove dead temp", "api", "safe", None, None,
            applies=lambda s, r: _f(s.get("pg_temp_gb")) > 0.1,
            args_of=lambda s, r: {"idle_minutes": IDLE_IN_TRANSACTION_MINUTES}),
    ToolDef("storage.remove_orphan_volumes", "Remove orphan volumes", "host", "safe",
            "remove_orphan_volumes", None,
            applies=lambda s, r: _volumes_reclaimable(s) > 0.1,
            args_of=lambda s, r: {"older_than_hours": 24}),
    ToolDef("storage.cap_logs", "Cap container logs", "host", "safe", "cap_logs", "logs",
            applies=lambda s, r: _f(s.get("logs_gb")) > 0.1,
            args_of=lambda s, r: {"max_size_mb": int(r.get("logs", {}).get("max_size_mb", 50))}),
    ToolDef("storage.drop_scratch_dbs", "Drop scratch DBs", "api", "safe", None, "scratch_db",
            applies=lambda s, r: True,
            args_of=lambda s, r: {"ttl_hours": int(r.get("scratch_db", {}).get("ttl_hours", 24))}),
    ToolDef("storage.docker_trim", "Trim Docker disk", "host", "safe", "docker_trim", "docker_trim",
            applies=lambda s, r: _alloc_unused(s) > 1.0 and bool(r.get("docker_trim", {"enabled": True}).get("enabled", True)),
            args_of=lambda s, r: {}),
    ToolDef("storage.remove_backups", "Remove old backups", "host", "irreversible", "remove_backups",
            "backups", needs_rule=True, applies=lambda s, r: _f(s.get("backups_gb")) > 0.5,
            args_of=lambda s, r: dict(r.get("backups") or storage_rules.PROPOSED_BACKUP_RULE)),
    ToolDef("storage.drop_partitions", "Drop old telemetry", "api", "irreversible", None, "telemetry",
            needs_rule=True, applies=lambda s, r: True,
            args_of=lambda s, r: {"keep_days": int(r.get("telemetry", {}).get("keep_days", 30))}),
)
TOOL_BY_NAME = {t.name: t for t in TOOLS}


def tier_of(tool: ToolDef, *, labelled: bool = True, rule_approved: bool = False) -> int:
    """reversibility × label × rule. Anything unlabelled is tier 3 whatever else is true."""
    if not labelled:
        return 3
    if tool.reversibility == "safe":
        return 1
    return 2 if rule_approved else 3


def plan_hash(tool: str, host_id: str, args: Mapping[str, Any], items: Sequence[str]) -> str:
    """Binds a real run to the dry run's exact item list (the decision `content_hash` idea)."""
    clean = {k: v for k, v in dict(args).items() if k not in ("dry_run", "plan_hash")}
    raw = json.dumps({"tool": tool, "host_id": host_id, "args": clean, "items": sorted(str(i) for i in items)},
                     sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_plan(connection: Any, incident: Mapping[str, Any], sample: Mapping[str, Any], *,
               max_tier: int = 3, keep: Optional[Sequence[Mapping[str, Any]]] = None) -> list[dict[str, Any]]:
    """The ordered steps for this reading: tier 1, then 2, then at most one tier-3 ask. A step
    that has nothing to do is left out. `keep` carries states of an earlier plan over (a re-plan
    on escalation never forgets what already ran)."""
    host_id = str(incident["host_id"])
    rules = {r["kind"]: r for r in storage_rules.list_rules(connection, host_id)}
    rule_args = {k: dict(v.get("rule") or {}) for k, v in rules.items() if v.get("enabled")}
    earlier = {s["tool"]: s for s in (keep or []) if isinstance(s, Mapping)}
    steps: list[dict[str, Any]] = []
    for tool in TOOLS:
        if tool.applies is not None and not tool.applies(sample, rule_args):
            continue
        approved = bool(tool.rule_kind and rules.get(tool.rule_kind, {}).get("enabled")
                        and rules[tool.rule_kind].get("source") in ("operator", "learned", "default")
                        and (tool.rule_kind != "backups" or rules[tool.rule_kind].get("source") != "default"))
        tier = tier_of(tool, labelled=True, rule_approved=approved)
        if tier > max_tier:
            continue
        args = dict(tool.args_of(sample, rule_args) if tool.args_of else {})
        if tool.where == "host":
            args["install_id"] = install_id(connection)
        prev = earlier.get(tool.name)
        if prev and prev.get("state") in TERMINAL:
            steps.append(dict(prev))
            continue
        steps.append({"tool": tool.name, "tier": tier, "where": tool.where, "label": tool.label,
                      "state": "planned", "items": None, "gb": None, "freed_gb": 0.0,
                      "plan_hash": None, "decision_id": None, "receipt_id": None, "action_id": None,
                      "reason": None, "args": args, "reversibility": tool.reversibility,
                      "rule_kind": tool.rule_kind, "started_at": None, "ended_at": None,
                      "asked_at": None, "ask_again_at": None, "detail": None, "host_job_id": None})
    steps.sort(key=lambda s: (int(s["tier"]), TOOLS.index(TOOL_BY_NAME[s["tool"]])))
    # ONE tier-3 ask at a time: the biggest irreversible item first, the rest wait.
    asks = [s for s in steps if s["tier"] == 3 and s["state"] not in TERMINAL]
    for extra in asks[1:]:
        extra["state"] = "skipped"
        extra["reason"] = "one question at a time; asked after the first is answered"
    return steps


def request_safe_cleanup(connection: Any, incident_id: str, sample: Mapping[str, Any]) -> None:
    """Start a fresh tier-one dry run on explicit request. A second click joins live work."""
    incident, previous = _plan(connection, incident_id)
    if not incident or incident["status"] == "resolved":
        return
    if any(step["state"] not in TERMINAL for step in previous):
        return
    # Preserve higher-tier decisions and refusals. Receipts for earlier attempts remain in
    # storage_actions; each fresh host operation obtains its own inventory and plan hash.
    retained = [s for s in previous if s["tier"] != 1 or s["state"] == "refused"]
    protected = {s["tool"] for s in retained}
    fresh = build_plan(connection, incident, sample, max_tier=1)
    _save_plan(connection, incident_id, [s for s in fresh if s["tool"] not in protected] + retained)


def dry_run_matches(step: Mapping[str, Any], items: Sequence[str], host_id: str) -> bool:
    return bool(step.get("plan_hash")) and plan_hash(str(step["tool"]), host_id, step.get("args") or {},
                                                       items) == step["plan_hash"]


# ── persistence helpers ─────────────────────────────────────────────────────────────────────

def _plan(connection: Any, incident_id: str, *, lock: bool = True) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    row = connection.execute(text(
        "SELECT * FROM storage_incidents WHERE id = :id" + (" FOR UPDATE" if lock else "")),
        {"id": incident_id}).mappings().first()
    if row is None:
        return None, []
    return dict(row), list(_json(row.get("plan"), []) or [])


def _save_plan(connection: Any, incident_id: str, plan: list[dict[str, Any]]) -> None:
    connection.execute(text(
        "UPDATE storage_incidents SET plan = CAST(:p AS JSONB), updated_at = now() WHERE id = :id"),
        {"p": json.dumps(plan, default=str), "id": incident_id})
    from . import storage_incidents

    storage_incidents.refresh_flow(connection, incident_id)


def _action(connection: Any, inc: Mapping[str, Any], step: Mapping[str, Any], *, dry_run: bool,
            status: str, actor: str, plan_hash_: Optional[str] = None, decision_id: Optional[str] = None,
            items: Optional[int] = None, bytes_: Optional[int] = None,
            result: Optional[dict] = None) -> str:
    action_id = make_id("sact")
    connection.execute(text("""
        INSERT INTO storage_actions (id, incident_id, host_id, tool, tier, where_run, args, plan_hash,
               dry_run, items, bytes, status, decision_id, actor, started_at, result)
        VALUES (:id, :i, :h, :t, :tier, :w, CAST(:a AS JSONB), :ph, :d, :n, :b, :s, :dec, :actor,
                now(), CAST(:r AS JSONB))"""),
        {"id": action_id, "i": inc["id"], "h": inc["host_id"], "t": step["tool"], "tier": int(step["tier"]),
         "w": step["where"], "a": json.dumps(step.get("args") or {}, default=str),
         "ph": plan_hash_ or step.get("plan_hash") or "pending", "d": bool(dry_run), "n": items, "b": bytes_,
         "s": status, "dec": decision_id, "actor": actor, "r": json.dumps(result or {}, default=str)})
    return action_id


def _finish_action(connection: Any, action_id: Optional[str], *, status: str, items: Optional[int] = None,
                   bytes_: Optional[int] = None, result: Optional[dict] = None,
                   receipt_id: Optional[str] = None, plan_hash_: Optional[str] = None) -> None:
    if not action_id:
        return
    connection.execute(text("""
        UPDATE storage_actions SET status = :s, items = COALESCE(:n, items), bytes = COALESCE(:b, bytes),
               result = CAST(:r AS JSONB), receipt_id = COALESCE(:rid, receipt_id),
               plan_hash = COALESCE(:ph, plan_hash), ended_at = now() WHERE id = :id"""),
        {"id": action_id, "s": status, "n": items, "b": bytes_, "r": json.dumps(result or {}, default=str),
         "rid": receipt_id, "ph": plan_hash_})


def _scope(connection: Any, project_id: Optional[str]) -> Optional[tuple[str, str, str]]:
    row = connection.execute(text(
        "SELECT p.id, p.workspace_id, w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE (CAST(:p AS TEXT) IS NULL OR p.id = :p) ORDER BY p.created_at LIMIT 1"),
        {"p": project_id}).first()
    return (str(row[0]), str(row[1]), str(row[2])) if row else None


def _receipt(connection: Any, inc: Mapping[str, Any], step: Mapping[str, Any], items: Sequence[str]) -> Optional[str]:
    from .receipts import ReceiptService

    scope = _scope(connection, inc.get("project_id"))
    if scope is None:
        return None
    project_id, workspace_id, org_id = scope
    receipt, _ = ReceiptService().create_or_reuse(
        connection, org_id, workspace_id, project_id,
        f"storage:{inc['id']}:{step['tool']}:{step['plan_hash']}", "storage.cleanup",
        {"host_id": inc["host_id"], "tool": step["tool"], "tier": step["tier"]},
        {"args": step.get("args") or {}, "items": list(items)[:500], "plan_hash": step["plan_hash"]})
    return str(receipt["id"])


def _receipt_done(connection: Any, receipt_id: Optional[str], ok: bool, result: dict) -> None:
    if not receipt_id:
        return
    from .receipts import ReceiptService

    svc = ReceiptService()
    if ok:
        svc.mark_succeeded(connection, receipt_id, None, result)
    else:
        svc.mark_failed(connection, receipt_id, {"message": str(result.get("error") or "failed")[:500]})


def _emit(connection: Any, inc: Mapping[str, Any], event_type: str, data: dict[str, Any]) -> None:
    try:
        from . import storage_incidents
        from .core import emit_event

        pid = inc.get("project_id") or storage_incidents.default_project(connection)
        if pid:
            emit_event(connection, str(pid), event_type, "agent:infra", "storage_incident", str(inc["id"]),
                       {"incident_id": inc["id"], "host_id": inc["host_id"], **data})
    except Exception:  # noqa: BLE001
        _log.debug("event %s not emitted", event_type, exc_info=True)


# ── stop when enough ────────────────────────────────────────────────────────────────────────

def target_reached(connection: Any, inc: Mapping[str, Any]) -> bool:
    from .storage_watch import free_pct_of, host_disk_sample

    s = host_disk_sample(connection, str(inc["host_id"]), fresh_only=True)
    fp = free_pct_of(s or {})
    return fp is not None and fp >= _f(inc.get("target_pct") or 25)


def skip_remaining(connection: Any, incident_id: str, *, reason: str) -> int:
    inc, plan = _plan(connection, incident_id)
    if inc is None:
        return 0
    n = 0
    for s in plan:
        # A real run already handed to the helper ('running') finishes and is receipted; only
        # what has not started — or a pending dry run / question — is skipped.
        if s.get("state") in ("planned", "ask", "queued"):
            s["state"], s["reason"], s["ended_at"] = "skipped", reason, _iso(_now())
            n += 1
    if n:
        _save_plan(connection, incident_id, plan)
    return n


# ── the state machine ───────────────────────────────────────────────────────────────────────

def advance_all(engine: Any) -> list[dict[str, Any]]:
    with engine.begin() as c:
        ids = [str(r[0]) for r in c.execute(text(
            "SELECT id FROM storage_incidents WHERE status <> 'resolved' ORDER BY opened_at")).all()]
    out = []
    for incident_id in ids:
        try:
            out.append(advance(engine, incident_id))
        except Exception as exc:  # noqa: BLE001 — one incident never stops another
            _log.warning("storage cleanup: %s not advanced", incident_id, exc_info=True)
            out.append({"incident_id": incident_id, "error": f"{type(exc).__name__}: {exc}"[:200]})
    return out


def advance(engine: Any, incident_id: str, *, actor: str = "agent:infra") -> dict[str, Any]:
    """Move the plan one notch: dry-run the next step, hand a host step to the helper or run an
    API step here, read a helper's result, raise the tier-3 card, stop at the target."""
    with engine.begin() as c:
        inc, plan = _plan(c, incident_id)
        if inc is None or inc["status"] == "resolved":
            return {"incident_id": incident_id, "state": "resolved"}
        if target_reached(c, inc):
            skipped = skip_remaining(c, incident_id, reason="target reached")
            return {"incident_id": incident_id, "state": "target_reached", "skipped": skipped}
        for step in plan:
            state = step.get("state")
            if state in TERMINAL:
                if state == "refused" and step.get("ask_again_at") and \
                        datetime.fromisoformat(step["ask_again_at"]) <= _now():
                    step.update({"state": "planned", "reason": None, "ask_again_at": None, "plan_hash": None})
                    _save_plan(c, incident_id, plan)
                    return {"incident_id": incident_id, "state": "re_asking", "tool": step["tool"]}
                continue
            if state == "ask":
                return {"incident_id": incident_id, "state": "asking", "tool": step["tool"],
                        "decision_id": step.get("decision_id")}
            if state == "planned":
                return _start(c, engine, inc, plan, step, actor=actor)
            if state in ("queued", "running"):
                return _poll(c, engine, inc, plan, step, actor=actor)
        from .storage_watch import host_disk_sample

        sample = host_disk_sample(c, str(inc["host_id"]), fresh_only=True)
        if sample:
            extended = extend_exhausted_plan(c, inc, sample, plan)
            if len(extended) > len(plan):
                _save_plan(c, incident_id, extended)
                return {"incident_id": incident_id, "state": "plan_extended"}
        return {"incident_id": incident_id, "state": "plan_exhausted"}


def extend_exhausted_plan(connection: Any, incident: Mapping[str, Any], sample: Mapping[str, Any],
                          plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if any(step.get("state") not in TERMINAL for step in plan):
        return plan
    known = {step["tool"] for step in plan}
    candidates = build_plan(connection, incident, sample,
                            max_tier=3 if incident.get("severity") == "critical" else 1)
    return [*plan, *(step for step in candidates if step["tool"] not in known)]


def _start(c: Any, engine: Any, inc: dict, plan: list, step: dict, *, actor: str) -> dict[str, Any]:
    tool = TOOL_BY_NAME[step["tool"]]
    step["started_at"] = step.get("started_at") or _iso(_now())
    if tool.where == "host":
        args = {**(step.get("args") or {}), "dry_run": True}
        if not args.get("install_id"):
            step.update({"state": "refused", "reason": "no install id: the host gate would refuse it",
                         "ended_at": _iso(_now())})
            _save_plan(c, inc["id"], plan)
            return {"incident_id": inc["id"], "state": "refused", "tool": step["tool"]}
        action_id = _action(c, inc, step, dry_run=True, status="queued", actor=actor)
        job = host_jobs.enqueue(c, host_id=str(inc["host_id"]), verb=str(tool.verb), args=args, action_id=action_id)
        step.update({"state": "queued", "dry": True, "host_job_id": job["id"], "action_id": action_id})
        _save_plan(c, inc["id"], plan)
        return {"incident_id": inc["id"], "state": "dry_run_queued", "tool": step["tool"], "job_id": job["id"]}
    # API-side: dry run now, then decide.
    items, size = run_api_tool(engine, c, step["tool"], step.get("args") or {}, dry_run=True)
    ph = plan_hash(step["tool"], str(inc["host_id"]), step.get("args") or {}, items)
    action_id = _action(c, inc, step, dry_run=True, status="done", actor=actor, plan_hash_=ph,
                        items=len(items), bytes_=size, result={"items": items[:500]})
    step.update({"items": len(items), "gb": round(size / 1e9, 3), "plan_hash": ph, "action_id": action_id,
                 "dry_run_at": _iso(_now())})
    if not items:
        step.update({"state": "skipped", "reason": "nothing to do", "ended_at": _iso(_now())})
        _save_plan(c, inc["id"], plan)
        return {"incident_id": inc["id"], "state": "nothing_to_do", "tool": step["tool"]}
    if int(step["tier"]) == 3:
        return _ask(c, inc, plan, step, items)
    return _execute_api(c, engine, inc, plan, step, items, actor=actor)


def _poll(c: Any, engine: Any, inc: dict, plan: list, step: dict, *, actor: str) -> dict[str, Any]:
    job = host_jobs.get(c, str(step.get("host_job_id") or ""))
    if job is None:
        step.update({"state": "failed", "reason": "its host job vanished", "ended_at": _iso(_now())})
        _save_plan(c, inc["id"], plan)
        return {"incident_id": inc["id"], "state": "failed", "tool": step["tool"]}
    if job["status"] in ("queued", "leased"):
        if datetime.fromisoformat(job["expires_at"]) <= _now():
            step.update({"state": "failed", "reason": "the host helper did not pick it up in time",
                         "ended_at": _iso(_now())})
            _finish_action(c, step.get("action_id"), status="failed", result={"error": "expired"})
            _save_plan(c, inc["id"], plan)
            return {"incident_id": inc["id"], "state": "helper_unreachable", "tool": step["tool"]}
        return {"incident_id": inc["id"], "state": "waiting_for_helper", "tool": step["tool"]}
    result = job.get("result") or {}
    items = [str(i) for i in (result.get("items") or [])]
    size = int(result.get("bytes") or 0)
    if job["status"] in ("failed", "expired") or not result.get("ok"):
        step.update({"state": "failed", "reason": str(result.get("error") or job["status"])[:200],
                     "ended_at": _iso(_now())})
        _finish_action(c, step.get("action_id"), status="failed", result=result)
        _receipt_done(c, step.get("receipt_id"), False, result)
        _save_plan(c, inc["id"], plan)
        _emit(c, inc, "storage.action.failed", {"tool": step["tool"], "error": step["reason"]})
        return {"incident_id": inc["id"], "state": "failed", "tool": step["tool"]}
    if step.get("dry"):
        ph = plan_hash(step["tool"], str(inc["host_id"]), step.get("args") or {}, items)
        # A BOSS volume made before its labels existed: the helper names it instead of passing
        # over it in silence; it is never an item (BOSSAPP-1193).
        relabel = [str(v) for v in (result.get("needs_relabel") or [])][:20]
        note = f"; unlabelled BOSS volume — needs relabel: {', '.join(relabel)}" if relabel else ""
        step.update({"items": len(items), "gb": round(size / 1e9, 3), "plan_hash": ph, "dry": False,
                     "dry_run_at": _iso(_now()), "detail": f"{len(items)} items, {size / 1e9:.1f} GB{note}"})
        if relabel:
            step["needs_relabel"] = relabel
        _finish_action(c, step.get("action_id"), status="done", items=len(items), bytes_=size,
                       result={"items": items[:500], **({"needs_relabel": relabel} if relabel else {})},
                       plan_hash_=ph)
        if not items:
            step.update({"state": "skipped", "ended_at": _iso(_now()),
                         "reason": f"nothing to do{note}"[:200]})
            _save_plan(c, inc["id"], plan)
            return {"incident_id": inc["id"], "state": "nothing_to_do", "tool": step["tool"]}
        if int(step["tier"]) == 3:
            return _ask(c, inc, plan, step, items)
        return _execute_host(c, inc, plan, step, items, actor=actor)
    # The real run came back: it must be the run we asked for (the helper echoes plan_hash).
    if str(result.get("plan_hash") or "") != str(step.get("plan_hash") or ""):
        step.update({"state": "failed", "reason": "the helper's result names another plan_hash",
                     "ended_at": _iso(_now())})
        _finish_action(c, step.get("action_id"), status="failed", result=result)
        _receipt_done(c, step.get("receipt_id"), False, {"error": "plan_hash mismatch"})
        _save_plan(c, inc["id"], plan)
        return {"incident_id": inc["id"], "state": "failed", "tool": step["tool"]}
    freed = size / 1e9
    step.update({"state": "done", "freed_gb": round(freed, 3), "ended_at": _iso(_now()),
                 "detail": f"{len(items)} items, {freed:.1f} GB freed"})
    _finish_action(c, step.get("action_id"), status="done", items=len(items), bytes_=size,
                   result={"items": items[:500], "freed_bytes": size})
    _receipt_done(c, step.get("receipt_id"), True, {"items": len(items), "freed_bytes": size})
    from . import storage_incidents

    storage_incidents.add_freed(c, inc["id"], freed)
    if inc.get("status") == "asking":
        storage_incidents.set_status(c, inc["id"], "critical")
    _save_plan(c, inc["id"], plan)
    _emit(c, inc, "storage.action.done", {"tool": step["tool"], "freed_gb": round(freed, 3), "items": len(items)})
    return {"incident_id": inc["id"], "state": "done", "tool": step["tool"], "freed_gb": round(freed, 3)}


def _execute_host(c: Any, inc: dict, plan: list, step: dict, items: Sequence[str], *, actor: str) -> dict[str, Any]:
    """The real host run: bound to the dry run's plan_hash, receipted first."""
    if not step.get("plan_hash") or not dry_run_matches(step, items, str(inc["host_id"])):
        step.update({"state": "refused", "reason": "no matching dry run: a real run needs the dry run's plan_hash",
                     "ended_at": _iso(_now())})
        _save_plan(c, inc["id"], plan)
        return {"incident_id": inc["id"], "state": "refused", "tool": step["tool"]}
    tool = TOOL_BY_NAME[step["tool"]]
    args = {**(step.get("args") or {}), "dry_run": False, "plan_hash": step["plan_hash"]}
    step["receipt_id"] = _receipt(c, inc, step, items)
    action_id = _action(c, inc, step, dry_run=False, status="running", actor=actor,
                        decision_id=step.get("decision_id"), items=len(items))
    c.execute(text("UPDATE storage_actions SET receipt_id = :r WHERE id = :id"),
              {"r": step["receipt_id"], "id": action_id})
    job = host_jobs.enqueue(c, host_id=str(inc["host_id"]), verb=str(tool.verb), args=args, action_id=action_id)
    step.update({"state": "running", "dry": False, "host_job_id": job["id"], "action_id": action_id})
    _save_plan(c, inc["id"], plan)
    return {"incident_id": inc["id"], "state": "running", "tool": step["tool"], "job_id": job["id"]}


def _execute_api(c: Any, engine: Any, inc: dict, plan: list, step: dict, items: Sequence[str], *,
                 actor: str, plan_hash_given: Optional[str] = None) -> dict[str, Any]:
    if plan_hash_given is not None and plan_hash_given != step.get("plan_hash"):
        step.update({"state": "refused", "reason": "plan_hash does not match the dry run", "ended_at": _iso(_now())})
        _save_plan(c, inc["id"], plan)
        return {"incident_id": inc["id"], "state": "refused", "tool": step["tool"]}
    step["receipt_id"] = _receipt(c, inc, step, items)
    action_id = _action(c, inc, step, dry_run=False, status="running", actor=actor,
                        decision_id=step.get("decision_id"), items=len(items))
    c.execute(text("UPDATE storage_actions SET receipt_id = :r WHERE id = :id"),
              {"r": step["receipt_id"], "id": action_id})
    try:
        done_items, size = run_api_tool(engine, c, step["tool"], step.get("args") or {}, dry_run=False,
                                        expected=items)
        ok, error = True, None
    except Exception as exc:  # noqa: BLE001 — recorded, never raised past the plan
        done_items, size, ok, error = [], 0, False, f"{type(exc).__name__}: {exc}"[:300]
    freed = size / 1e9
    step.update({"state": "done" if ok else "failed", "freed_gb": round(freed, 3), "reason": error,
                 "ended_at": _iso(_now()), "action_id": action_id,
                 "detail": f"{len(done_items)} items, {freed:.1f} GB freed" if ok else error})
    _finish_action(c, action_id, status="done" if ok else "failed", items=len(done_items), bytes_=size,
                   result={"items": list(done_items)[:500], "freed_bytes": size, "error": error},
                   receipt_id=step["receipt_id"])
    _receipt_done(c, step["receipt_id"], ok, {"items": len(done_items), "freed_bytes": size, "error": error})
    if ok:
        from . import storage_incidents

        storage_incidents.add_freed(c, inc["id"], freed)
    _save_plan(c, inc["id"], plan)
    _emit(c, inc, "storage.action.done" if ok else "storage.action.failed",
          {"tool": step["tool"], "freed_gb": round(freed, 3), "items": len(done_items), "error": error})
    return {"incident_id": inc["id"], "state": step["state"], "tool": step["tool"], "freed_gb": round(freed, 3)}


# ── tier 3: one card ────────────────────────────────────────────────────────────────────────

#: What this cleanup will not touch (BOSSAPP-1192). Named on the card, beside never_automatic,
#: so a person sees what was left for them. The policy chips stay the shorter words.
LEFT_ALONE = ("unlabelled items", "data volumes", "non-scratch databases")
_LEFT_ALONE_LINE = "Left alone: unlabelled items, data volumes, and non-scratch databases."


def _ask(c: Any, inc: dict, plan: list, step: dict, items: Sequence[str]) -> dict[str, Any]:
    """ONE decision card carrying the rule proposal (docs/infra/INFRA_AGENT_API.md §7)."""
    from . import storage_incidents
    from .core import create_blocking_decision

    tool = TOOL_BY_NAME[step["tool"]]
    rule_kind = tool.rule_kind or "backups"
    proposed = dict((step.get("args") or {}))
    proposed.pop("install_id", None)
    cause_key = str(inc.get("cause_key") or "")
    preticked = storage_incidents.times_seen(c, cause_key) >= storage_incidents.LEARN_AFTER if cause_key else False
    gb = _f(step.get("gb"))
    done_gb = sum(_f(s.get("freed_gb")) for s in plan if s.get("state") == "done")
    free_now = _f(inc.get("free_pct_now"))
    keeps = int(proposed.get("keep_newest") or 0)
    question = (f"Delete {len(items)} old {storage_rules.LABELS.get(rule_kind, rule_kind).lower()} "
                f"({gb:.0f} GB) on {inc.get('host_label') or inc['host_id']}?"
                + (f" The newest {keeps} stay." if keeps else ""))
    reason = (f"{inc.get('cause_text') or 'Storage is critical.'} Tiers 1–2 freed {done_gb:.0f} GB; "
              f"{free_now:.0f} % is free, the target is {_f(inc.get('target_pct') or 25):.0f} %. "
              "This cannot be undone, so it needs a person — once: tick the rule and next time it "
              "is automatic. " + _LEFT_ALONE_LINE)
    rec = {"action": "storage_delete", "incident_id": inc["id"], "host_id": inc["host_id"],
           "tool": step["tool"], "args": step.get("args") or {}, "plan_hash": step.get("plan_hash"),
           "items": len(items), "gb": round(gb, 2), "keeps": keeps,
           "to_delete": list(items)[:500], "kept": [],
           "already_done_gb": round(done_gb, 2), "free_pct_now": round(free_now, 1),
           "target_pct": _f(inc.get("target_pct") or 25),
           "paused_runs": int(inc.get("paused_runs") or 0),
           "rule_proposal": {"kind": rule_kind, "rule": proposed, "preticked": bool(preticked)},
           "alternatives": ([{"label": "Keep 20 instead", "args": {**proposed, "keep_newest": 20}}]
                            if "keep_newest" in proposed else []),
           "never_automatic": ["unlabelled", "data volumes", "non-scratch databases"],
           "left_alone": list(LEFT_ALONE),
           "ui": "StorageDeleteDecision"}
    decision_id = create_blocking_decision(c, inc.get("project_id") or storage_incidents.default_project(c),
                                           None, question, rec, {"estimated_cost_usd": 0.0}, reason,
                                           risk_tier=3, default_action="hold")
    from .typed_forms import normalize_form

    c.execute(text("UPDATE decisions SET form_schema = CAST(:f AS JSONB) WHERE id = :id"),
              {"f": json.dumps(normalize_form(ASK_FORM)), "id": decision_id})
    step.update({"state": "ask", "decision_id": decision_id, "asked_at": _iso(_now())})
    c.execute(text("UPDATE storage_actions SET decision_id = :d WHERE id = :id"),
              {"d": decision_id, "id": step.get("action_id")})
    _save_plan(c, inc["id"], plan)
    storage_incidents.set_status(c, inc["id"], "asking")
    return {"incident_id": inc["id"], "state": "asking", "tool": step["tool"], "decision_id": decision_id}


def on_delete_decision(connection: Any, engine: Any, decision: Mapping[str, Any], *, verdict: str,
                       actor: str, make_rule: Optional[bool] = None, args: Optional[Mapping[str, Any]] = None
                       ) -> Optional[dict[str, Any]]:
    """The decisions service's hook for `storage_delete`. Approve: (optionally) write the rule,
    then run exactly the dry run's plan_hash — or re-plan when the person chose other arguments
    or the dry run went stale. Reject ("Not now"): re-asked in an hour or on a further fall."""
    from . import storage_incidents

    rec = _json(decision.get("recommendation"), {}) or {}
    from . import backup_scheduler

    if rec.get("tool") == backup_scheduler.CAP_TOOL:
        # Scheduled backups past their cap (BOSSAPP-1459): the same card, no incident behind it.
        return backup_scheduler.on_cap_decision(connection, {**dict(decision), "recommendation": rec},
                                                verdict=verdict, actor=actor, make_rule=make_rule,
                                                args=args)
    incident_id = str(rec.get("incident_id") or "")
    inc, plan = _plan(connection, incident_id)
    if inc is None:
        return None
    step = next((s for s in plan if s.get("decision_id") == decision.get("id")), None)
    if step is None:
        return None
    if verdict != "approve":
        step.update({"state": "refused", "reason": "not now", "ended_at": _iso(_now()),
                     "ask_again_at": _iso(_now() + ASK_AGAIN_AFTER)})
        _save_plan(connection, incident_id, plan)
        if inc["status"] == "asking":
            storage_incidents.set_status(connection, incident_id, inc.get("severity") or "critical")
        return {"state": "refused", "ask_again_at": step["ask_again_at"]}
    proposal = rec.get("rule_proposal") or {}
    # The approve body's typed answer decides; absent, the card's pre-tick does. An explicit
    # False creates NO rule, whatever the pre-tick said.
    want_rule = bool(proposal.get("preticked")) if make_rule is None else bool(make_rule)
    if want_rule and proposal.get("kind"):
        storage_rules.upsert(connection, kind=str(proposal["kind"]), rule=dict((args or proposal.get("rule")) or {}),
                             host_id=str(inc["host_id"]), source="learned", actor=actor,
                             decision_id=str(decision.get("id") or ""))
    step["tier"] = 2 if want_rule else 3
    step["approved_by"] = actor          # the report says "by whose say-so": a person's, here
    if inc["status"] == "asking":
        storage_incidents.set_status(connection, incident_id, inc.get("severity") or "critical")
    stale = not step.get("dry_run_at") or datetime.fromisoformat(step["dry_run_at"]) < _now() - DRY_RUN_FRESH_FOR
    if args or stale:
        # Other arguments, or a dry run that went stale: plan again (and ask again if still tier 3).
        step.update({"state": "planned", "args": {**(step.get("args") or {}), **dict(args or {})},
                     "plan_hash": None, "items": None, "gb": None, "decision_id": None})
        _save_plan(connection, incident_id, plan)
        return {"state": "replanned"}
    items = [str(i) for i in (rec.get("to_delete") or [])]
    if step["where"] == "host":
        return _execute_host(connection, inc, plan, step, items, actor=actor)
    return _execute_api(connection, engine, inc, plan, step, items, actor=actor,
                        plan_hash_given=str(rec.get("plan_hash") or ""))


def replan_step(connection: Any, incident_id: str, *, tool: str, args: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The console's "Keep 20 instead": the step is planned again with these arguments."""
    inc, plan = _plan(connection, incident_id)
    if inc is None:
        return None
    for step in plan:
        if step["tool"] == tool and step.get("state") in ("ask", "refused", "planned"):
            if step.get("decision_id"):
                withdraw_card(connection, str(step["decision_id"]), reason="planned again with other arguments")
            step.update({"state": "planned", "args": {**(step.get("args") or {}), **dict(args)},
                         "plan_hash": None, "items": None, "gb": None, "decision_id": None, "reason": None})
            _save_plan(connection, incident_id, plan)
            if inc.get("status") == "asking":
                from . import storage_incidents

                storage_incidents.set_status(connection, incident_id, inc.get("severity") or "critical")
            return step
    return None


def withdraw_card(connection: Any, decision_id: str, *, reason: str) -> bool:
    """An open tier-3 card that no longer matches its plan is withdrawn, never left approvable."""
    from .decision_actions import OPEN_STATUSES

    return bool(connection.execute(text(
        "UPDATE decisions SET status = 'withdrawn', resolution_note = :n, updated_at = now() "
        "WHERE id = :d AND status = ANY(:open) RETURNING id"),
        {"d": decision_id, "n": reason[:300], "open": sorted(OPEN_STATUSES)}).first())


# ── API-side tools (Postgres) ───────────────────────────────────────────────────────────────

def run_api_tool(engine: Any, connection: Any, tool: str, args: Mapping[str, Any], *, dry_run: bool,
                 expected: Optional[Sequence[str]] = None) -> tuple[list[str], int]:
    """(items, bytes). `expected` is the dry run's item list for a real run: only those are
    touched, so a thing that appeared since the dry run is never removed on that approval."""
    if tool == "storage.remove_pg_temp":
        return _remove_pg_temp(connection, int(args.get("idle_minutes") or IDLE_IN_TRANSACTION_MINUTES),
                               dry_run=dry_run, expected=expected)
    if tool == "storage.drop_scratch_dbs":
        return _drop_scratch_dbs(engine, connection, int(args.get("ttl_hours") or 24), dry_run=dry_run,
                                 expected=expected)
    if tool == "storage.drop_partitions":
        return _drop_partitions(engine, connection, int(args.get("keep_days") or 30), dry_run=dry_run,
                                expected=expected)
    raise ValueError(f"{tool} is not an API-side tool")


def _remove_pg_temp(connection: Any, idle_minutes: int, *, dry_run: bool,
                    expected: Optional[Sequence[str]]) -> tuple[list[str], int]:
    """End sessions idle in a transaction longer than `idle_minutes` (their temp files go with
    them) and count temp files of sessions that no longer exist."""
    rows = connection.execute(text("""
        SELECT pid FROM pg_stat_activity
         WHERE state = 'idle in transaction' AND pid <> pg_backend_pid()
           AND now() - state_change > make_interval(mins => :m)"""), {"m": idle_minutes}).all()
    items = [f"pid:{r[0]}" for r in rows]
    if expected is not None:
        items = [i for i in items if i in set(expected)]
    temp_bytes = 0
    try:
        # A savepoint: a role that may not list the temp dir must not abort the plan's transaction.
        with connection.begin_nested():
            temp_bytes = int(connection.execute(text(
                "SELECT COALESCE(sum(size), 0) FROM pg_ls_tmpdir()")).scalar() or 0)
    except Exception:  # noqa: BLE001 — not every role may list the temp dir
        pass
    if dry_run:
        return items, temp_bytes if items else 0
    for item in items:
        try:
            connection.execute(text("SELECT pg_terminate_backend(:p)"), {"p": int(item.split(":", 1)[1])})
        except Exception:  # noqa: BLE001 — a session that ended by itself
            continue
    return items, temp_bytes


def _drop_scratch_dbs(engine: Any, connection: Any, ttl_hours: int, *, dry_run: bool,
                      expected: Optional[Sequence[str]]) -> tuple[list[str], int]:
    """DROP DATABASE for `boss_scratch_<purpose>_<stamp>` databases past the ttl, with no
    connections. Any other name is refused by construction: the candidate query only ever
    returns the prefix.

    THE BIRTHDAY IS THE LABEL (ST-14). A throwaway carries its creation time in its name
    (dev/test_databases: `scratch_database_name`, the same stamp the test session's own sweep
    reads), so the age is the stamp's — never a file time, which is the last WRITE, not the
    birth, and needs a server-file privilege whose refusal would abort the caller's transaction.
    A `boss_scratch_*` name with no stamp has no known age: it is not BOSS's to drop on its own.
    The bytes returned are those of exactly the databases returned."""
    from ..dev.test_databases import parse_stamped

    rows = connection.execute(text("""
        SELECT d.datname, pg_database_size(d.datname) AS bytes,
               (SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) AS conns
          FROM pg_database d WHERE d.datname LIKE :like AND NOT d.datistemplate
           AND d.datname <> current_database()"""), {"like": SCRATCH_PREFIX + "%"}).mappings().all()
    sizes: dict[str, int] = {}
    for r in rows:
        name = str(r["datname"])
        if not name.startswith(SCRATCH_PREFIX) or int(r["conns"] or 0):
            continue
        stamped = parse_stamped(name)
        if stamped is None or stamped.kind != "scratch" or not stamped.expired(float(ttl_hours)):
            continue
        sizes[name] = int(r["bytes"] or 0)
    items = sorted(sizes)
    if expected is not None:
        wanted = set(expected)
        items = [i for i in items if i in wanted]
    if dry_run or not items:
        return items, sum(sizes[i] for i in items)
    dropped = []
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as auto:
        for name in items:
            if not name.startswith(SCRATCH_PREFIX):
                continue  # the one rule that never bends
            try:
                auto.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
                dropped.append(name)
            except Exception:  # noqa: BLE001 — in use again: left alone
                _log.info("scratch database %s not dropped", name, exc_info=True)
    return dropped, sum(sizes[n] for n in dropped)


def _drop_partitions(engine: Any, connection: Any, keep_days: int, *, dry_run: bool,
                     expected: Optional[Sequence[str]] = None) -> tuple[list[str], int]:
    """Telemetry past its retention, now rather than on the hourly clock (ST-8,
    telemetry_retention): expired monthly partitions of `events` dropped whole (audit events
    archived first), the rest by batched deletes, every table under its own per-table policy and
    floor. The dry run's items are stable names (`partition:events_2026_08`, `rows:receipts`), so
    the real run carries the same plan_hash and touches only those. `keep_days` is the old single
    knob and is not used: the per-table days live in the `telemetry` rule's `tables` map."""
    from . import telemetry_retention

    del connection, keep_days
    # A real run takes the same advisory lock as the hourly pass: never two sweeps at once.
    report = telemetry_retention.sweep_locked(engine, dry_run=dry_run, only=expected)
    if report is None:
        _log.info("telemetry sweep already running; this step removes nothing itself")
        return [], 0
    return list(report["items"]), int(report["bytes"] or 0)


__all__ = ["ASK_AGAIN_AFTER", "DRY_RUN_FRESH_FOR", "SCRATCH_PREFIX", "TERMINAL", "TOOLS", "TOOL_BY_NAME",
           "ToolDef", "advance", "advance_all", "build_plan", "dry_run_matches", "install_id",
           "on_delete_decision", "plan_hash", "replan_step", "run_api_tool", "skip_remaining",
           "target_reached", "tier_of"]
