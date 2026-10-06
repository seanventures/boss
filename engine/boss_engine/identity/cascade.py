"""The revocation cascade: one transaction plus NOTIFY, effective within 5 s. Owned by S3.

IDENTITY.md §7.11. Effects, by trigger:

  grant revoked/expired   grant + every child grant -> revoked/expired; every run token carrying
                          them -> revoked (jti ledger); their egress credentials -> revoked; their
                          runs -> halted "authority revoked"; dependent schedules paused; after
                          commit, the GitHub installation tokens minted for those runs are
                          revoked AT GITHUB (egress hook).
  person disabled         sessions revoked, PATs suspended, grants suspended, runs halted, tokens
                          revoked. Vault items become unusable (owner inactive).
  person deleted          all of the above, grants revoked, and their vault items revoked at the
                          provider and crypto-shredded.
  passport suspended      every run whose chain holds the agent halted, its tokens revoked; grants
                          naming it suspended.
  passport revoked        terminal; grants naming it revoked; Passports it bore (the whole subtree,
                          not only direct children) suspended pending review.
  api key / session       revoked now (the next request is a hash lookup).
  binding removed         epoch bump: every OBO token of that person narrows at its next call.

Why ≤5 s holds: every verification (API, MCP, egress) re-reads the jti ledger and the live state,
so the effect is immediate at commit; NOTIFY `identity_revocation` and the after-commit hooks only
make the halts and the GitHub DELETE prompt instead of waiting for the next call.
`dry_run=True` computes the same report and changes nothing (the console's cascade preview).
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

from .delegation import CascadeReport, CascadeTrigger
from .principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

log = logging.getLogger(__name__)

NOTIFY_CHANNEL = "identity_revocation"

#: kind -> halt(entity_id, reason, db). The run id convention is `run_<kind>:<entity>[/<child>…]`.
HALTERS: dict[str, Callable[[str, str, Any], None]] = {}
#: Called AFTER commit with the run ids whose authority ended (e.g. the egress proxy revoking
#: GitHub installation tokens). Must be fast and must not raise.
AFTER_COMMIT: list[Callable[[list[str], str], None]] = []
_hooks_lock = threading.Lock()


def system_actor(org_id: str) -> AuthContext:
    return AuthContext(Principal("sys_worker", PrincipalKind.SYSTEM, org_id, PrincipalStatus.ACTIVE, "BOSS"),
                       AuthMethod.SYSTEM)


def register_after_commit(fn: Callable[[list[str], str], None]) -> None:
    with _hooks_lock:
        if fn not in AFTER_COMMIT:
            AFTER_COMMIT.append(fn)


def run_id(kind: str, entity_id: str) -> str:
    if not kind.isalnum() or "#" in entity_id or not entity_id:
        raise ValueError("bad run id parts")
    return f"run_{kind}:{entity_id}"


def parse_run_id(rid: str) -> Optional[tuple[str, str]]:
    if not rid.startswith("run_") or ":" not in rid:
        return None
    kind, _, rest = rid[4:].partition(":")
    return kind, rest.split("/", 1)[0]


def run_segments(rid: str) -> list[tuple[str, str]]:
    """Every halt-able piece of a nested run id: `run_wf:W/ct:C/forge` -> [(wf, W), (ct, C)]."""
    first = parse_run_id(rid)
    if first is None:
        return []
    out = [first]
    for seg in rid.split("/")[1:]:
        kind, sep, entity = seg.partition(":")
        if sep and kind.isalnum() and entity:
            out.append((kind, entity))
    return out


# ---- default halters for BOSS's run kinds --------------------------------------------------------------

def _table_exists(db: Any, name: str) -> bool:
    from sqlalchemy import text

    return bool(db.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": name}).scalar())


def _halt_code_task(entity: str, reason: str, db: Any) -> None:
    from sqlalchemy import text

    if not _table_exists(db, "code_task_stop_requests"):
        return
    project = db.execute(text("SELECT project_id FROM code_task_runs WHERE id = :id"), {"id": entity}).scalar() \
        if _table_exists(db, "code_task_runs") else None
    db.execute(text("INSERT INTO code_task_stop_requests (run_id, project_id, actor, reason, phase) "
                    "VALUES (:r, :p, 'identity', :why, 'authority_revoked') ON CONFLICT (run_id) DO UPDATE "
                    "SET reason = EXCLUDED.reason, requested_at = now(), honoured_at = NULL"),
               {"r": entity, "p": project or "", "why": reason})


def _halt_workflow_run(entity: str, reason: str, db: Any) -> None:
    from sqlalchemy import text

    if _table_exists(db, "workflow_runs"):
        db.execute(text("UPDATE workflow_runs SET status = 'paused', paused_at = now(), blocked_reason = :why, "
                        "updated_at = now() WHERE id = :id AND status NOT IN "
                        "('completed','failed','deadlettered','cancelled','compensated')"),
                   {"id": entity, "why": reason})


def _halt_agent_run(entity: str, reason: str, db: Any) -> None:
    from sqlalchemy import text

    if _table_exists(db, "agent_runs"):
        db.execute(text("UPDATE agent_runs SET status = 'paused', updated_at = now() WHERE id = :id "
                        "AND status IN ('running','waiting','queued')"), {"id": entity})


HALTERS.update({"ct": _halt_code_task, "wf": _halt_workflow_run, "ag": _halt_agent_run})


# ---- the cascade -----------------------------------------------------------------------------------------

def _active_runs(db: Any, where: str, params: dict) -> tuple[list[str], list[str]]:
    from sqlalchemy import text

    rows = db.execute(text(f"SELECT jti, run_id FROM identity_run_credentials WHERE revoked_at IS NULL AND {where}"),
                      params).all()
    return [r[0] for r in rows], sorted({r[1] for r in rows})


def _grants_where(db: Any, where: str, params: dict) -> list[str]:
    from sqlalchemy import text

    return list(db.execute(text(f"SELECT id FROM identity_delegation_grants WHERE {where}"), params).scalars().all())


def _schedules_for(db: Any, grant_ids: list[str]) -> list[str]:
    from sqlalchemy import text

    if not grant_ids or not _table_exists(db, "workflow_schedules"):
        return []
    # The schedule's grant_id column; input._identity only for schedules saved before it.
    return list(db.execute(text("SELECT id FROM workflow_schedules WHERE enabled AND (grant_id = ANY(:g) OR "
                                "(on_behalf_of IS NULL AND input->'_identity'->>'grant_id' = ANY(:g)))"),
                           {"g": grant_ids}).scalars().all())


def _workflow_runs_for(db: Any, grant_ids: list[str]) -> list[str]:
    """Live workflow runs whose recorded authority is one of these grants (they may have no token
    yet: a run waiting between steps is still halted)."""
    from sqlalchemy import text

    if not grant_ids or not _table_exists(db, "workflow_runs"):
        return []
    rows = db.execute(text(
        "SELECT id FROM workflow_runs WHERE (grant_id = ANY(:g) OR (on_behalf_of IS NULL AND "
        "policy_snapshot->'_identity'->>'grant_id' = ANY(:g))) AND status NOT IN "
        "('completed','failed','deadlettered','cancelled','compensated','paused')"), {"g": grant_ids}).scalars().all()
    return [f"run_wf:{r}" for r in rows]


def _queued_jobs_for(db: Any, grant_ids: list[str]) -> list[str]:
    from sqlalchemy import text

    if not grant_ids or not _table_exists(db, "jobs"):
        return []
    # The row's grant_id column; payload._identity only for rows written before the column.
    return list(db.execute(text("SELECT id FROM jobs WHERE status = 'queued' AND (grant_id = ANY(:g) OR "
                                "(on_behalf_of IS NULL AND payload->'_identity'->>'grant_id' = ANY(:g)))"),
                           {"g": grant_ids}).scalars().all())


def _egress_for(db: Any, run_ids: list[str]) -> int:
    from sqlalchemy import text

    if not run_ids:
        return 0
    return int(db.execute(text("SELECT count(*) FROM identity_egress_credentials WHERE run_id = ANY(:r) "
                               "AND revoked_at IS NULL"), {"r": run_ids}).scalar_one())


def _plan(trigger: CascadeTrigger, target: str, db: Any) -> dict[str, Any]:
    """What the cascade WOULD touch. Shared by the preview and the real run."""
    from sqlalchemy import text

    from . import grants as grants_mod, passports

    plan: dict[str, Any] = {"revoke_grants": [], "expire_grants": [], "suspend_grants": [],
                            "jtis": [], "runs": [], "passports": [], "sessions": 0, "keys": 0,
                            "vault_items": [], "schedules": [], "jobs": []}
    if trigger in (CascadeTrigger.GRANT_REVOKED, CascadeTrigger.GRANT_EXPIRED):
        live = _grants_where(db, "id = :g AND status IN ('active','suspended')", {"g": target})
        family = live + [g for g in grants_mod.descendants(db, live)]
        key = "revoke_grants" if trigger == CascadeTrigger.GRANT_REVOKED else "expire_grants"
        plan[key] = family
        if trigger == CascadeTrigger.GRANT_EXPIRED:
            plan["revoke_grants"] = [g for g in family if g != target]
        all_ids = [target] + family
        plan["jtis"], plan["runs"] = _active_runs(db, "grant_id = ANY(:g)", {"g": all_ids})
        plan["runs"] = sorted(set(plan["runs"]) | set(_workflow_runs_for(db, all_ids)))
        plan["jobs"] = _queued_jobs_for(db, all_ids)
        plan["schedules"] = _schedules_for(db, all_ids)
    elif trigger in (CascadeTrigger.PERSON_DISABLED, CascadeTrigger.PERSON_DELETED):
        mine = _grants_where(db, "user_principal_id = :u AND status IN ('active','suspended')", {"u": target})
        if trigger == CascadeTrigger.PERSON_DELETED:
            plan["revoke_grants"] = mine
        else:
            plan["suspend_grants"] = _grants_where(db, "user_principal_id = :u AND status = 'active'", {"u": target})
        plan["jtis"], plan["runs"] = _active_runs(db, "subject_principal_id = :u", {"u": target})
        plan["schedules"] = _schedules_for(db, mine)
        if _table_exists(db, "identity_sessions"):
            plan["sessions"] = int(db.execute(text("SELECT count(*) FROM identity_sessions WHERE principal_id = :u "
                                                   "AND revoked_at IS NULL"), {"u": target}).scalar_one())
        if _table_exists(db, "identity_api_credentials"):
            plan["keys"] = int(db.execute(text("SELECT count(*) FROM identity_api_credentials WHERE principal_id = :u "
                                               "AND revoked_at IS NULL AND suspended_at IS NULL"),
                                          {"u": target}).scalar_one())
        if trigger == CascadeTrigger.PERSON_DELETED:
            plan["vault_items"] = list(db.execute(text("SELECT id FROM identity_vault_items WHERE owner_principal_id = :u "
                                                       "AND status <> 'revoked'"), {"u": target}).scalars().all())
    elif trigger in (CascadeTrigger.PASSPORT_SUSPENDED, CascadeTrigger.PASSPORT_REVOKED):
        naming = _grants_where(db, "(:a = ANY(agent_ids) OR :a = ANY(allowed_children)) AND status IN ('active','suspended')",
                               {"a": target})
        if trigger == CascadeTrigger.PASSPORT_REVOKED:
            plan["revoke_grants"] = naming
            plan["passports"] = [a for a in passports.born_by_subtree(target, db=db)
                                 if (passports.get(a, db=db) or passports.Passport(a, "", "", "revoked", "", frozenset())).status == "active"]
        else:
            plan["suspend_grants"] = [g for g in naming
                                      if db.execute(text("SELECT status FROM identity_delegation_grants WHERE id = :g"),
                                                    {"g": g}).scalar() == "active"]
        agents = [target] + plan["passports"]
        pattern_rows = db.execute(text(
            "SELECT jti, run_id FROM identity_run_credentials WHERE revoked_at IS NULL AND ("
            " subject_principal_id = ANY(:a) OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(chain) h "
            " WHERE split_part(h, '#', 1) = ANY(:a)))"), {"a": agents}).all()
        plan["jtis"] = [r[0] for r in pattern_rows]
        plan["runs"] = sorted({r[1] for r in pattern_rows})
        plan["schedules"] = _schedules_for(db, naming)
    elif trigger == CascadeTrigger.API_KEY_REVOKED:
        plan["keys"] = 1
    elif trigger == CascadeTrigger.SESSION_REVOKED:
        plan["sessions"] = 1
    return plan


def revoke_cascade(trigger: CascadeTrigger, target_id: str, *, actor: AuthContext, reason: str,
                   db: Any, dry_run: bool = False) -> CascadeReport:
    from sqlalchemy import text

    from . import authz, grants as grants_mod, lineage, vault

    trigger = CascadeTrigger(trigger)
    if actor is not None and actor.run_token is not None:
        # An agent never revokes someone's authority through the cascade API (it is not delegable).
        raise PermissionError("agents_cannot_cascade")
    plan = _plan(trigger, target_id, db)
    egress = _egress_for(db, plan["runs"])
    report = CascadeReport(
        trigger=trigger, target_id=target_id,
        grants_revoked=tuple(sorted(set(plan["revoke_grants"]) | set(plan["expire_grants"]))),
        grants_suspended=tuple(plan["suspend_grants"]), run_tokens_revoked=len(plan["jtis"]),
        egress_credentials_revoked=egress, github_tokens_revoked=len(plan["runs"]),
        runs_halted=tuple(plan["runs"]), schedules_paused=tuple(plan["schedules"]),
        passports_suspended=tuple(plan["passports"]), sessions_revoked=plan["sessions"], dry_run=dry_run)
    if dry_run:
        return report

    why = f"authority revoked: {reason}"[:300]
    by = actor.principal.id if actor else None
    for gid in plan["expire_grants"]:
        db.execute(text("UPDATE identity_delegation_grants SET status = 'expired', revoked_at = now(), revoked_by = :by, "
                        "revoke_reason = :r WHERE id = :g AND status IN ('active','suspended')"),
                   {"g": gid, "by": by, "r": reason[:300]})
    for gid in plan["revoke_grants"]:
        db.execute(text("UPDATE identity_delegation_grants SET status = 'revoked', revoked_at = now(), revoked_by = :by, "
                        "revoke_reason = :r WHERE id = :g AND status IN ('active','suspended')"),
                   {"g": gid, "by": by, "r": reason[:300]})
    for gid in plan["suspend_grants"]:
        db.execute(text("UPDATE identity_delegation_grants SET status = 'suspended' WHERE id = :g AND status = 'active'"),
                   {"g": gid})
    if plan["jtis"]:
        db.execute(text("UPDATE identity_run_credentials SET revoked_at = now() WHERE jti = ANY(:j) AND revoked_at IS NULL"),
                   {"j": plan["jtis"]})
    if plan["runs"]:
        db.execute(text("UPDATE identity_egress_credentials SET revoked_at = now() WHERE run_id = ANY(:r) "
                        "AND revoked_at IS NULL"), {"r": plan["runs"]})
        halted: set[tuple[str, str]] = set()
        for rid in plan["runs"]:
            for kind, entity in run_segments(rid):
                halter = HALTERS.get(kind)
                if halter is None or (kind, entity) in halted:
                    continue
                halted.add((kind, entity))
                db.execute(text("SAVEPOINT identity_halt"))
                try:
                    halter(entity, why, db)
                    db.execute(text("RELEASE SAVEPOINT identity_halt"))
                except Exception:  # noqa: BLE001 — tokens are already dead; the halt is best effort
                    db.execute(text("ROLLBACK TO SAVEPOINT identity_halt"))
                    log.warning("could not halt %s", rid)
    if plan["jobs"]:
        # Queued work under the dead grant never starts (leased work stops at its next token check).
        cancelled = db.execute(text(
            "UPDATE jobs SET status = 'cancelled', last_error = to_jsonb(CAST(:why AS TEXT)), updated_at = now() "
            "WHERE id = ANY(:j) AND status = 'queued' RETURNING payload->>'code_task_run_id'"),
            {"j": plan["jobs"], "why": why}).scalars().all()
        # A build whose job never started still has its code-task run row, waiting: stop it too,
        # so nothing shows it "running" under a revoked grant (and a later lease cannot pick it up).
        for crun in {c for c in cancelled if c}:
            _halt_code_task(crun, why, db)
            if _table_exists(db, "code_task_runs"):
                db.execute(text("SAVEPOINT identity_crun"))
                try:
                    db.execute(text("UPDATE code_task_runs SET status = 'cancelled', error = :why, updated_at = now() "
                                    "WHERE id = :id AND status = 'running' AND phase = 'queued'"),
                               {"id": crun, "why": why})
                    db.execute(text("RELEASE SAVEPOINT identity_crun"))
                except Exception:  # noqa: BLE001 — the stop request above still stops it
                    db.execute(text("ROLLBACK TO SAVEPOINT identity_crun"))
    for sid in plan["schedules"]:
        db.execute(text("UPDATE workflow_schedules SET enabled = false, last_error = 'Renew authority: the grant "
                        "this schedule ran under has ended', updated_at = now() WHERE id = :s"), {"s": sid})
    for aid in plan["passports"]:
        db.execute(text("UPDATE identity_principals SET status = 'suspended', status_changed_at = now(), "
                        "status_changed_by = :by, status_reason = 'parent revoked: review' WHERE id = :a "
                        "AND status = 'active'"), {"a": aid, "by": by})
    if trigger in (CascadeTrigger.PERSON_DISABLED, CascadeTrigger.PERSON_DELETED):
        if _table_exists(db, "identity_sessions"):
            db.execute(text("UPDATE identity_sessions SET revoked_at = now(), revoked_reason = 'person disabled' "
                            "WHERE principal_id = :u AND revoked_at IS NULL"), {"u": target_id})
        if _table_exists(db, "identity_api_credentials"):
            db.execute(text("UPDATE identity_api_credentials SET suspended_at = now() WHERE principal_id = :u "
                            "AND revoked_at IS NULL AND suspended_at IS NULL"), {"u": target_id})
        for item in plan["vault_items"]:
            vault.revoke(item, ctx=system_actor(actor.principal.org_id if actor else ""), reason="person deleted", db=db)
    if trigger == CascadeTrigger.API_KEY_REVOKED and _table_exists(db, "identity_api_credentials"):
        db.execute(text("UPDATE identity_api_credentials SET revoked_at = now(), revoked_by = :by WHERE id = :k "
                        "AND revoked_at IS NULL"), {"k": target_id, "by": by})
    if trigger == CascadeTrigger.SESSION_REVOKED and _table_exists(db, "identity_sessions"):
        db.execute(text("UPDATE identity_sessions SET revoked_at = now(), revoked_reason = :r WHERE id = :s "
                        "AND revoked_at IS NULL"), {"s": target_id, "r": reason[:200]})
    if trigger == CascadeTrigger.BINDING_REMOVED:
        try:
            org = actor.principal.org_id if actor else ""
            authz.bump_epoch(org, db=db, reason=f"binding removed: {target_id}")
        except NotImplementedError:
            log.info("epoch bump pending S2")

    # Lineage for every grant and passport the cascade changed.
    for gid, event in ([(g, "delegation.revoked") for g in plan["revoke_grants"]]
                       + [(g, "delegation.expired") for g in plan["expire_grants"]]
                       + [(g, "delegation.suspended") for g in plan["suspend_grants"]]):
        grant = grants_mod.load(gid, db=db)
        if grant is not None:
            lineage.grant_event(db, grant, event, ctx=actor,
                                details={"trigger": trigger.value, "target": target_id, "reason": reason[:200],
                                         "runs_halted": len(plan["runs"])})
    if trigger in (CascadeTrigger.PASSPORT_SUSPENDED, CascadeTrigger.PASSPORT_REVOKED):
        from . import passports

        p = passports.get(target_id, db=db)
        if p is not None:
            event = ("agent.passport.revoked" if trigger == CascadeTrigger.PASSPORT_REVOKED
                     else "agent.passport.suspended")
            lineage.passport_event(db, p, event, ctx=actor,
                                   details={"reason": reason[:200], "runs_halted": len(plan["runs"]),
                                            "descendants_suspended": plan["passports"]})

    payload = json.dumps({"trigger": trigger.value, "target": target_id, "runs": plan["runs"][:200]})
    db.execute(text("SELECT pg_notify(:c, :p)"), {"c": NOTIFY_CHANNEL, "p": payload[:7900]})
    _after_commit(db, list(plan["runs"]), why)
    return report


def _after_commit(db: Any, run_ids: list[str], reason: str) -> None:
    """Run the hooks once the caller commits (SQLAlchemy `after_commit`), or now if there is no
    session-level transaction to wait for."""
    if not run_ids:
        return
    with _hooks_lock:
        hooks = list(AFTER_COMMIT)
    if not hooks:
        return

    def fire(*_: Any) -> None:
        for hook in hooks:
            try:
                hook(run_ids, reason)
            except Exception:  # noqa: BLE001
                log.warning("revocation hook failed")

    try:
        from sqlalchemy import event

        conn = db
        if hasattr(conn, "in_transaction") and conn.in_transaction():
            event.listen(conn, "commit", fire, once=True)
            return
    except Exception:  # noqa: BLE001
        pass
    fire()


def preview(trigger: CascadeTrigger, target_id: str, *, actor: AuthContext, db: Any) -> CascadeReport:
    return revoke_cascade(trigger, target_id, actor=actor, reason="preview", db=db, dry_run=True)


__all__ = ["CascadeTrigger", "CascadeReport", "revoke_cascade", "preview", "run_id", "parse_run_id",
           "HALTERS", "register_after_commit", "system_actor", "NOTIFY_CHANNEL"]
