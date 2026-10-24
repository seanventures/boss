"""A PERSON APPROVES, THE HOST INSTALLS, THE STEP PROVES IT: approval, tickets, install, rollback.

BOSSAPP-571 (spec §5.2 Stages 9–12, §6.4, §6.5, §8.2, §8.3, decisions 1–5).

THE APPROVAL (R5, §8.2). Every install is a governed decision (`self_fix.install`), approved by
a PERSON holding the tier's seat: tier 2 (only tier-2 paths, no flags) is one person; tier 3
(any tier-3 path or a host-effect flag — decision 5) is two people, or one under the
single-operator waiver. The card's content hash binds the approval to the exact commit, tree,
diff, tests, gates and review shown (C-3): a repair round after the card was drawn voids it.
No policy, agent or API key approves a self-fix in v1: auto-install is OFF (decision 3) and
`approve_by_policy` refuses this action.

THE TICKET (decision 1, F4). Nothing in a container can build images or restart services (no
Docker socket, by design). An approved decision becomes a SIGNED, SINGLE-USE INSTALL TICKET:
EdDSA with the install's signing key, bound to the decision, the base, the fix commit and its
tree hash, expiring in an hour. The HOST self-installer (scripts/install/self_fix.sh) — pulled,
not pushed — verifies the signature against the install's JWKS, takes the ticket (a second take
is refused), re-reads that the decision is approved, checks the commit and tree, and does
nothing else. The person can always run the same install by hand; the card shows the command.

INSTALL AND ROLLBACK (R6, R7, F6). The self-installer reports each phase (backup, apply, build,
start, health, done). Health regressing, or the resumed step failing for the SAME cause
(decision 4), rolls back by re-tagging the previous images (`:prev`) and moving the stack ref
back — never a rebuild, and never code from the fix (scripts/install/** is `never`).

THE STACK (R8, decision 2, F7). Installed fixes are commits on `refs/boss/stack/<release>`, a
SEPARATE ref over the release; the checkout's HEAD stays exactly on the release, so upgrade.sh's
fast-forward rule still holds for the base, and an upgrade re-applies the stack
(`upgrade_plan`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import timedelta
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from . import self_fix as sf
from .core import make_id, now_utc

log = logging.getLogger(__name__)

ACTION = "self_fix.install"
#: Remove one installed self-fix (a rebuild of the stack without it), or every local fix (back
#: to the plain release): a person's decision, like the install (spec §6.5 "same install path,
#: same approval rules"); the host self-installer runs the `remove` / `remove_all` ticket.
REMOVE_ACTION = "self_fix.remove"
PERSON_ACTIONS = (ACTION, REMOVE_ACTION)
TICKET_TYP = "boss-self-fix-ticket+jwt"
TICKET_TTL = timedelta(hours=1)
DECISION_TTL = timedelta(hours=72)
SIGNING_FAMILY = "actor"
KINDS = ("install", "rollback", "remove", "remove_all")
PHASES = ("backup", "apply", "build", "start", "health", "done", "rollback")
MANUAL_COMMAND = "boss self-fix install {sfx}"


class InstallRefused(RuntimeError):
    """An install move BOSS refuses (a ticket that is not this one's to take, a bad report)."""


# ── the signer ───────────────────────────────────────────────────────────────────────────────

class IdentitySigner:
    """The install's own EdDSA key (identity/signing_keys.py). Never an invented key: without a
    provisioned one no ticket is issued (fail closed)."""

    def sign(self, connection: Connection, claims: Mapping[str, Any]) -> str:
        from ..identity import jwks, signing_keys

        key = signing_keys.active_key(SIGNING_FAMILY, db=connection)
        return jwks.sign({"alg": "EdDSA", "typ": TICKET_TYP, "kid": key.kid}, claims,
                         key.private_key)

    def jwks(self, connection: Connection) -> dict[str, Any]:
        from ..identity import signing_keys

        return signing_keys.jwks_document(db=connection)


_signer: Any = IdentitySigner()


def use_signer(signer: Any) -> Any:
    """Tests only: an engine test without the identity schema signs with its own key. Returns the
    previous signer so the test restores it."""
    global _signer
    previous, _signer = _signer, signer
    return previous


def signer() -> Any:
    return _signer


# ── the card (Stage 9) ───────────────────────────────────────────────────────────────────────

def content_hash(sfx: Mapping[str, Any]) -> str:
    """What the approver saw: the commit, the tree, the diff, the tests, the gates, the review,
    the tier and the flags. Any change after the card was drawn changes this."""
    pr = sfx.get("pr") or {}
    red_green = sfx.get("red_green") or {}
    gates = sfx.get("gates") or {}
    review = (sfx.get("review") or {}).get("seats") or {}
    body = {
        "self_fix": sfx.get("id"), "commit": sfx.get("fix_commit_sha"),
        "tree": sfx.get("fix_tree_sha"), "base": sfx.get("base_sha"),
        "diff": hashlib.sha256(str(pr.get("diff") or "").encode()).hexdigest(),
        "tests": sorted(red_green.get("tests") or []),
        "red": (red_green.get("base") or {}).get("exit_code"),
        "green": (red_green.get("head") or {}).get("exit_code"),
        "gates": {k: bool((v or {}).get("ok")) for k, v in gates.items()
                  if isinstance(v, Mapping)},
        "review": {k: (v or {}).get("verdict") for k, v in review.items()
                   if isinstance(v, Mapping)},
        "tier": sfx.get("tier"), "flags": sorted(sfx.get("flags") or []),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def card(sfx: Mapping[str, Any]) -> dict[str, Any]:
    """The one card a person needs to say yes (§10.2)."""
    evidence = sfx.get("evidence") or {}
    diagnosis = sfx.get("diagnosis") or {}
    gates = sfx.get("gates") or {}
    review = sfx.get("review") or {}
    stat = sfx.get("diff_stat") or {}
    red_green = sfx.get("red_green") or {}
    tier = int(sfx.get("tier") or 2)
    affected = (gates.get("affected_suites") or {}).get("detail") or {}
    p0 = (gates.get("p0_tier") or {}).get("detail") or {}
    flag_detail = ((gates.get("protected_paths") or {}).get("detail") or {}).get(
        "flag_detail") or {}
    return {
        "self_fix_id": sfx.get("id"),
        "title": (sfx.get("pr") or {}).get("title"),
        "tier": tier, "flags": list(sfx.get("flags") or []),
        "what_broke": evidence.get("symptom"),
        "why": diagnosis.get("hypothesis"),
        "rejected": diagnosis.get("rejected") or [],
        "confidence": {"level": sfx.get("confidence"),
                       "reasons": [r.get("words") for r in sfx.get("confidence_reasons") or []
                                   if isinstance(r, Mapping)]},
        "change": {"files": [str(f.get("path") if isinstance(f, Mapping) else f)
                             for f in stat.get("files") or []], "added": stat.get("added"),
                   "removed": stat.get("removed"),
                   "host_effect": flag_detail.get(sp_flag()) or []},
        "tests": {"new": red_green.get("tests") or [], "red_on_base": True,
                  "green_with_fix": True, "affected": affected.get("tests", 0),
                  "p0": p0.get("tests", 0), "none_weakened":
                      bool((gates.get("tests_not_weakened") or {}).get("ok"))},
        "review": {k: {"verdict": (v or {}).get("verdict"), "engine": (v or {}).get("engine"),
                       "ok": bool((v or {}).get("ok")),
                       "findings": [f"{f.get('severity', '')} {f.get('what', '')}".strip()[:200]
                                    for f in (v or {}).get("findings") or []
                                    if isinstance(f, Mapping)][:3]}
                   for k, v in (review.get("seats") or {}).items()},
        "security_on_another_engine": bool(review.get("security_on_another_engine")),
        "review_note": review.get("note") or "",
        "cost_usd": round(int(sfx.get("cost_micros") or 0) / 1e6, 2),
        "minutes": _build_minutes(sfx),
        "engine": sfx.get("engine"),
        "on_approve": ("Builds BOSS with this fix, restarts it (about two minutes of "
                       "downtime), checks its health, then resumes the step."),
        "undo": ("Automatic if health drops or the step fails the same way; or Remove, any "
                 "time, on the Self-fixes page."),
        "people": ("Two people approve on a shared install; the single-operator waiver makes "
                   "it one here." if tier >= 3 else "One person holding the approver seat."),
        "command": MANUAL_COMMAND.format(sfx=sfx.get("id")),
    }


def _build_minutes(sfx: Mapping[str, Any]) -> int:
    """How long Mender took to reach the card: from the start to the moment it asked."""
    from datetime import datetime

    start = sfx.get("started_at") or sfx.get("created_at")
    asked = (sfx.get("progress") or {}).get("at") if sfx.get("status") == sf.AWAITING_APPROVAL \
        else None
    try:
        if start and asked:
            return max(0, int((datetime.fromisoformat(str(asked)) - datetime.fromisoformat(
                str(start))).total_seconds() // 60))
    except (TypeError, ValueError):
        pass
    return int(sf.step_facts(sfx)["minutes"])


def sp_flag() -> str:
    from .self_fix_policy import HOST_EFFECT

    return HOST_EFFECT


def request_approval(connection: Connection, settings: Any, sfx_id: str) -> str:
    """The decision for one self-fix's install. A pending earlier card for the same self-fix (a
    repair round) is withdrawn: it showed a fix that no longer exists."""
    sfx = sf.get(connection, sfx_id)
    if sfx is None:
        raise InstallRefused(f"no self-fix {sfx_id}")
    if not sfx.get("fix_commit_sha") or not sfx.get("fix_tree_sha"):
        raise InstallRefused("the self-fix has no commit to install")
    connection.execute(text(
        "UPDATE decisions SET status = 'withdrawn', resolution_note = :n, updated_at = now() "
        "WHERE recommendation->>'self_fix_id' = :i AND status IN ('pending', 'delegated')"),
        {"i": sfx_id, "n": "the self-fix changed after this card was drawn"})
    tier = max(2, int(sfx.get("tier") or 2))
    drawn = card(sfx)
    hashed = content_hash(sfx)
    decision_id = make_id("dec")
    recommendation = {
        "action": ACTION, "self_fix_id": sfx_id, "card": drawn,
        "title": drawn["title"], "reason_code": "self_fix_install",
        "action_preview": {"content_hash": hashed, "summary": drawn["title"],
                           "commit": sfx.get("fix_commit_sha"), "tier": tier},
    }
    connection.execute(text(
        "INSERT INTO decisions (id, project_id, task_id, question, recommendation, options, "
        "risk_tier, cost_summary, deadline_at, default_action, status) VALUES (:id, :p, NULL, "
        ":q, CAST(:r AS JSONB), CAST(:o AS JSONB), :t, CAST(:c AS JSONB), :d, 'hold', "
        "'pending')"),
        {"id": decision_id, "p": sfx["project_id"],
         "q": f"Install a BOSS self-fix? {drawn['title']}"[:500],
         "r": json.dumps(recommendation, default=str),
         "o": json.dumps([{"action": "approve", "label": "Approve and install"},
                          {"action": "revise", "label": "Request changes"},
                          {"action": "reject", "label": "Reject"}]),
         "t": tier, "c": json.dumps({"usd": drawn["cost_usd"], "category": "self_fix"}),
         "d": now_utc() + DECISION_TTL})
    from .core import emit_event

    emit_event(connection, str(sfx["project_id"]), "decision.created", sf.ACTOR_AGENT,
               "decision", decision_id, {"reason": "self_fix_install", "risk_tier": tier,
                                         "self_fix_id": sfx_id, "default_action": "hold"})
    return decision_id


def refuse_policy_approval(row: Mapping[str, Any], *, system_policy: bool, actor: str) -> None:
    """A self-fix install is approved by a PERSON, never by a policy or an agent (v1)."""
    rec = row.get("recommendation") or {}
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    if (rec or {}).get("action") not in PERSON_ACTIONS:
        return
    if system_policy or actor in (sf.SYSTEM_ACTOR, "system", sf.ACTOR_AGENT) \
            or str(actor).startswith(("agt_", "sys_")):
        raise InstallRefused("A person approves every BOSS self-fix install and removal; no "
                             "policy, agent or system account can.")


# ── removing installed fixes (spec §6.5) ─────────────────────────────────────────────────────

def request_removal(connection: Connection, settings: Any, *, project_id: str, actor: str,
                    sfx_id: Optional[str] = None) -> dict[str, Any]:
    """A person asks to take one self-fix (`sfx_id`), or every local fix, off this one-box. The
    ask is a tier-2 decision a person approves; approving it issues the signed `remove` /
    `remove_all` ticket. Refused when there is nothing installed to remove."""
    installed = stack(connection)
    if sfx_id:
        target = next((f for f in installed if f["self_fix_id"] == sfx_id), None)
        if target is None:
            raise InstallRefused("this self-fix is not on the local stack; there is nothing to "
                                 "remove")
        anchor = sfx_id
        title = f"Remove the BOSS self-fix “{target.get('title') or sfx_id}”?"
        effect = ("Rebuilds BOSS without this fix (the other local fixes stay), restarts it, "
                  "and checks its health; puts the current build back if health drops.")
        kind = "remove"
    else:
        if not installed:
            raise InstallRefused("no local self-fixes are installed; BOSS runs its release as "
                                 "it is")
        anchor = str(installed[-1]["self_fix_id"])
        title = f"Remove all {len(installed)} local BOSS self-fix(es)?"
        effect = ("Puts the plain release back (no local fixes), restarts BOSS and checks its "
                  "health; puts the current build back if health drops.")
        kind = "remove_all"
    from .decision_actions import OPEN_STATUSES

    open_one = connection.execute(text(
        "SELECT id FROM decisions WHERE status IN :open_statuses AND "
        "recommendation->>'action' = :a AND COALESCE(recommendation->>'self_fix_id', '') = :i"
    ).bindparams(bindparam("open_statuses", expanding=True)),
        {"open_statuses": list(OPEN_STATUSES), "a": REMOVE_ACTION, "i": sfx_id or ""}).scalar()
    if open_one:
        return {"decision_id": str(open_one), "kind": kind, "existing": True}
    decision_id = make_id("dec")
    recommendation = {"action": REMOVE_ACTION, "self_fix_id": sfx_id, "anchor": anchor,
                      "kind": kind, "title": title, "reason_code": "self_fix_remove",
                      "why": effect, "stack": [f["self_fix_id"] for f in installed],
                      "requested_by": actor}
    connection.execute(text(
        "INSERT INTO decisions (id, project_id, task_id, question, recommendation, options, "
        "risk_tier, cost_summary, deadline_at, default_action, status) VALUES (:id, :p, NULL, "
        ":q, CAST(:r AS JSONB), CAST(:o AS JSONB), 2, '{}'::jsonb, :d, 'hold', 'pending')"),
        {"id": decision_id, "p": project_id, "q": title[:500],
         "r": json.dumps(recommendation, default=str),
         "o": json.dumps([{"action": "approve", "label": "Remove"},
                          {"action": "reject", "label": "Keep it"}]),
         "d": now_utc() + DECISION_TTL})
    from .core import emit_event

    emit_event(connection, project_id, "decision.created", actor, "decision", decision_id,
               {"reason": "self_fix_remove", "risk_tier": 2, "self_fix_id": sfx_id,
                "default_action": "hold"})
    sfx = sf.get(connection, anchor)
    if sfx:
        sf.emit(connection, sfx, "removal_requested", actor=actor,
                sentence=(f"{actor} asked to "
                          f"{'remove this fix' if sfx_id else 'remove every local fix'}; "
                          "waiting for the approval."),
                data={"decision_id": decision_id, "kind": kind})
    return {"decision_id": decision_id, "kind": kind}


def on_remove_approved(connection: Connection, settings: Any, row: Mapping[str, Any],
                       actor: str) -> dict[str, Any]:
    rec = row.get("recommendation") or {}
    if isinstance(rec, str):
        rec = json.loads(rec)
    kind = "remove" if rec.get("kind") == "remove" else "remove_all"
    anchor = str(rec.get("anchor") or rec.get("self_fix_id") or "")
    if kind == "remove" and not any(f["self_fix_id"] == anchor for f in stack(connection)):
        raise InstallRefused("this self-fix is no longer on the local stack")
    if kind == "remove_all" and not stack(connection):
        raise InstallRefused("no local self-fixes are installed any more")
    return issue_ticket(connection, settings, anchor, kind=kind, approved_by=actor,
                        trigger=f"{actor} approved removing "
                                f"{'this fix' if kind == 'remove' else 'every local fix'}",
                        decision_id=str(row.get("id") or ""))


# ── the decision's answers ───────────────────────────────────────────────────────────────────

def _sfx_of(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    rec = row.get("recommendation") or {}
    if isinstance(rec, str):
        rec = json.loads(rec)
    return {"id": rec.get("self_fix_id")} if rec.get("self_fix_id") else None


def on_approved(connection: Connection, settings: Any, row: Mapping[str, Any], actor: str
                ) -> dict[str, Any]:
    """The person said yes: the self-fix is approved and its install ticket issued. Refused
    (the approval is not recorded) when the stack moved since the fix was checked."""
    ref = _sfx_of(row)
    sfx = sf.get(connection, ref["id"]) if ref else None
    if sfx is None:
        raise InstallRefused("this card's self-fix is gone")
    if sfx["status"] != sf.AWAITING_APPROVAL or sfx.get("decision_id") != row.get("id"):
        raise InstallRefused(f"this self-fix is {sfx['status']}; this card is no longer the one "
                             "to answer")
    running = sf.running_stack_head()
    if running and sfx.get("base_sha") and running != sfx["base_sha"]:
        raise InstallRefused("BOSS's own stack changed since this fix was checked (another fix "
                             "was installed or BOSS was upgraded). BOSS checks it again on the "
                             "new stack and asks you again.")
    sf.transition(connection, sfx["id"], sf.APPROVED, actor=actor,
                  sentence=f"Approved by {actor}: BOSS installs its fix next.",
                  data={"decision_id": row.get("id"), "approved_by": actor})
    return issue_ticket(connection, settings, sfx["id"], kind="install", approved_by=actor)


def on_rejected(connection: Connection, row: Mapping[str, Any], actor: str,
                note: Optional[str]) -> None:
    ref = _sfx_of(row)
    sfx = sf.get(connection, ref["id"]) if ref else None
    if sfx is None or sfx["status"] != sf.AWAITING_APPROVAL:
        return
    sf.transition(connection, sfx["id"], sf.REJECTED, actor=actor,
                  sentence=f"A person declined the self-fix: {note or 'no reason given'}.",
                  data={"note": (note or "")[:600]})


def on_revise(connection: Connection, row: Mapping[str, Any], actor: str,
              note: Optional[str]) -> None:
    """Request changes: one more repair round with the person's note, then a new card."""
    ref = _sfx_of(row)
    sfx = sf.get(connection, ref["id"]) if ref else None
    if sfx is None or sfx["status"] != sf.AWAITING_APPROVAL:
        return
    rounds = int(sfx.get("repair_rounds") or 0) + 1
    sf.transition(connection, sfx["id"], sf.FIXING, actor=actor,
                  sentence=f"{actor} asked for changes; {sf.AGENT_NAME} is fixing it again.",
                  data={"note": (note or "")[:600]}, repair_rounds=rounds)
    sf.enqueue_build(connection, sf.get(connection, sfx["id"]),
                     round_note=f"A person asked for changes: {note or ''}")


# ── tickets ──────────────────────────────────────────────────────────────────────────────────

def _release_ref() -> str:
    from .. import __version__

    return (os.environ.get("BOSS_RELEASE_REF") or "").strip() or f"v{__version__}"


def issue_ticket(connection: Connection, settings: Any, sfx_id: str, *, kind: str,
                 approved_by: Optional[str] = None, trigger: str = "",
                 decision_id: Optional[str] = None) -> dict[str, Any]:
    """Sign one single-use ticket for the host self-installer. Without a signing key nothing is
    issued: the self-fix stays approved and says why (fail closed)."""
    if kind not in KINDS:
        raise InstallRefused(f"unknown ticket kind {kind}")
    sfx = sf.get(connection, sfx_id)
    if sfx is None:
        raise InstallRefused(f"no self-fix {sfx_id}")
    revoke_tickets(connection, sfx_id, why=f"a new {kind} ticket was issued",
                   kinds=(kind,))
    ticket_id = make_id("sft")
    now = now_utc()
    expires = now + TICKET_TTL
    release = _release_ref()
    claims = {
        "iss": "boss", "typ": "self_fix_ticket", "ticket_id": ticket_id, "kind": kind,
        "sfx_id": sfx_id, "decision_id": decision_id or sfx.get("decision_id"),
        "base_sha": sfx.get("base_sha"), "fix_commit_sha": sfx.get("fix_commit_sha"),
        "fix_tree_sha": sfx.get("fix_tree_sha"), "branch": sfx.get("branch"),
        "release": release, "stack_ref": f"refs/boss/stack/{release}",
        "approved_by": approved_by, "trigger": trigger[:300],
        "health_minutes": sf._int_setting(connection, settings, "self_fix_health_minutes"),
        **({"remove": [sfx_id] if kind == "remove" else [f["self_fix_id"] for f in
                                                         stack(connection)]}
           if kind in ("remove", "remove_all") else {}),
        "iat": int(now.timestamp()), "exp": int(expires.timestamp()),
        "nonce": base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("="),
    }
    try:
        token = signer().sign(connection, claims)
    except Exception as error:  # noqa: BLE001 — no key: no ticket, said so
        sf.say(connection, sfx_id, f"BOSS could not sign the install ticket "
                                   f"({type(error).__name__}); provision the install's signing "
                                   "keys, then reissue it.")
        return {"ticket_id": None, "error": str(error)[:300]}
    connection.execute(text(
        "INSERT INTO self_fix_tickets (id, self_fix_id, kind, body, token, status, expires_at) "
        "VALUES (:i, :f, :k, CAST(:b AS JSONB), :t, 'issued', :e)"),
        {"i": ticket_id, "f": sfx_id, "k": kind, "b": json.dumps(claims), "t": token,
         "e": expires})
    sfx = sf.get(connection, sfx_id)
    command = MANUAL_COMMAND.format(sfx=sfx_id)
    sf.emit(connection, sfx, "ticket_issued", actor=sf.SYSTEM_ACTOR,
            sentence=(f"Install ticket ready: the self-installer installs it, or run "
                      f"`{command}` on the host." if kind == "install" else
                      f"Removal ticket ready: the self-installer rebuilds BOSS without "
                      f"{'this fix' if kind == 'remove' else 'its local fixes'}, or run "
                      f"`boss self-fix {kind.replace('_', '-')}"
                      f"{' ' + sfx_id if kind == 'remove' else ''}` on the host."
                      if kind in ("remove", "remove_all") else
                      f"Rollback ticket ready ({trigger})."),
            data={"ticket_id": ticket_id, "kind": kind, "command": command})
    sf.audit(connection, "self_fix.ticket_issued", actor=approved_by or sf.SYSTEM_ACTOR, sfx=sfx,
             details={"ticket_id": ticket_id, "kind": kind})
    return {"ticket_id": ticket_id, "kind": kind, "expires_at": expires.isoformat(),
            "command": command}


def revoke_tickets(connection: Connection, sfx_id: str, *, why: str,
                   kinds: tuple[str, ...] = KINDS) -> int:
    return int(connection.execute(text(
        "UPDATE self_fix_tickets SET status = 'revoked', updated_at = now(), "
        "phases = phases || CAST(:p AS JSONB) WHERE self_fix_id = :i AND status = 'issued' "
        "AND kind = ANY(:k)"),
        {"i": sfx_id, "k": list(kinds), "p": json.dumps([{"phase": "revoked", "why": why[:300],
                                                          "at": now_utc().isoformat()}])}
    ).rowcount or 0)


def issue_rollback(connection: Connection, settings: Any, sfx_id: str, *, trigger: str
                   ) -> dict[str, Any]:
    """R7: put the previous build back. Bound to the same approved decision; the self-installer
    re-tags `:prev` and moves the stack ref back — no new approval is needed to undo."""
    sfx = sf.get(connection, sfx_id)
    if sfx is None:
        raise InstallRefused(f"no self-fix {sfx_id}")
    if sfx["status"] in (sf.NOT_RESOLVED, sf.INSTALLED, sf.VERIFIED, sf.HEALTH_WINDOW,
                         sf.INSTALLING):
        sf.transition(connection, sfx_id, sf.ROLLING_BACK, actor=sf.SYSTEM_ACTOR,
                      sentence=f"Rolling back: {trigger}.", data={"trigger": trigger})
    return issue_ticket(connection, settings, sfx_id, kind="rollback", trigger=trigger)


def expire_tickets(connection: Connection) -> int:
    return int(connection.execute(text(
        "UPDATE self_fix_tickets SET status = 'expired', updated_at = now() WHERE status = "
        "'issued' AND expires_at < now()")).rowcount or 0)


def pending_tickets(connection: Connection, settings: Any) -> list[dict[str, Any]]:
    """What the self-installer may take now. Also the moment stale work is tidied: expired
    tickets, and cards whose fix was checked on a stack that is no longer running."""
    expire_tickets(connection)
    rebase_stale(connection, settings)
    # SWITCHED OFF: no new install is handed out; a rollback always is (§8.7).
    on = not sf.env_off() and sf.switch(connection)["on"]
    kinds = list(KINDS) if on else ["rollback"]
    rows = connection.execute(text(
        "SELECT t.id, t.kind, t.token, t.self_fix_id, t.expires_at FROM self_fix_tickets t "
        "WHERE t.status = 'issued' AND t.kind = ANY(:k) ORDER BY t.created_at"),
        {"k": kinds}).mappings().all()
    return [{"ticket_id": r["id"], "kind": r["kind"], "token": r["token"],
             "self_fix_id": r["self_fix_id"], "expires_at": r["expires_at"].isoformat()}
            for r in rows]


def reissue(connection: Connection, settings: Any, sfx_id: str) -> dict[str, Any]:
    """The person runs `boss self-fix install <sfx>` after the ticket expired: a fresh one for
    the same approved decision (an approval is not spent by an unused ticket)."""
    sfx = sf.get(connection, sfx_id)
    if sfx is None:
        raise InstallRefused(f"no self-fix {sfx_id}")
    if sfx["status"] == sf.APPROVED:
        if _decision_status(connection, sfx.get("decision_id")) != "approved":
            raise InstallRefused("the self-fix's decision is not approved")
        return issue_ticket(connection, settings, sfx_id, kind="install",
                            approved_by=_approver(connection, sfx.get("decision_id")))
    if sfx["status"] in (sf.ROLLING_BACK,):
        return issue_ticket(connection, settings, sfx_id, kind="rollback",
                            trigger="reissued rollback")
    raise InstallRefused(f"this self-fix is {sfx['status']}; there is nothing to install")


def _decision_status(connection: Connection, decision_id: Any) -> str:
    if not decision_id:
        return ""
    return str(connection.execute(text("SELECT status FROM decisions WHERE id = :i"),
                                  {"i": decision_id}).scalar() or "")


def _approver(connection: Connection, decision_id: Any) -> Optional[str]:
    if not decision_id:
        return None
    return connection.execute(text("SELECT approved_by FROM decisions WHERE id = :i"),
                              {"i": decision_id}).scalar()


def take(connection: Connection, ticket_id: str) -> dict[str, Any]:
    """The self-installer takes a ticket: ONCE. Returns what it needs to check and apply it —
    the token, the decision's status as it is NOW, and the fix's commit as a git bundle."""
    on = not sf.env_off() and sf.switch(connection)["on"]
    row = connection.execute(text(
        "UPDATE self_fix_tickets SET status = 'taken', taken_at = now(), updated_at = now() "
        "WHERE id = :i AND status = 'issued' AND expires_at > now() "
        "AND (CAST(:on AS BOOLEAN) OR kind = 'rollback') RETURNING id, self_fix_id, "
        "kind, token, body"), {"i": ticket_id, "on": on}).mappings().first()
    if row is None:
        state = connection.execute(text("SELECT status, expires_at FROM self_fix_tickets "
                                        "WHERE id = :i"), {"i": ticket_id}).mappings().first()
        if state is None:
            raise InstallRefused("no such ticket")
        raise InstallRefused(f"this ticket is {state['status']}"
                             + (" (expired)" if state["status"] == "issued" else "")
                             + "; a ticket is used once")
    sfx = sf.get(connection, str(row["self_fix_id"]))
    bundle = connection.execute(text("SELECT bundle FROM self_fixes WHERE id = :i"),
                                {"i": row["self_fix_id"]}).scalar()
    sf.emit(connection, sfx, "ticket_taken", actor=sf.INSTALLER_ACTOR,
            sentence="The self-installer took the ticket.", data={"ticket_id": ticket_id})
    return {"ticket_id": row["id"], "kind": row["kind"], "token": row["token"],
            "decision_status": _decision_status(connection, (sfx or {}).get("decision_id")),
            "self_fix_status": (sfx or {}).get("status"),
            "bundle_b64": base64.b64encode(bytes(bundle)).decode() if bundle else None}


def report(connection: Connection, settings: Any, ticket_id: str, *, phase: str, ok: bool,
           detail: str = "", stack_head: Optional[str] = None) -> dict[str, Any]:
    """One install phase, from the self-installer. Moves the self-fix as the phase says."""
    if phase not in PHASES:
        raise InstallRefused(f"unknown phase {phase}")
    row = connection.execute(text(
        "SELECT id, self_fix_id, kind, status FROM self_fix_tickets WHERE id = :i FOR UPDATE"),
        {"i": ticket_id}).mappings().first()
    if row is None:
        raise InstallRefused("no such ticket")
    if row["status"] not in ("taken",):
        raise InstallRefused(f"this ticket is {row['status']}; only a taken ticket reports")
    from .self_fix_privacy import mask_hosts

    clean = mask_hosts(detail)[:2000]
    connection.execute(text(
        "UPDATE self_fix_tickets SET phases = phases || CAST(:p AS JSONB), updated_at = now() "
        "WHERE id = :i"),
        {"i": ticket_id, "p": json.dumps([{"phase": phase, "ok": bool(ok), "detail": clean,
                                           "at": now_utc().isoformat()}])})
    sfx_id = str(row["self_fix_id"])
    sfx = sf.get(connection, sfx_id)
    sf.emit(connection, sfx, "install_phase", actor=sf.INSTALLER_ACTOR,
            sentence=f"Install: {phase} {'done' if ok else 'failed'}"
                     + (f" — {clean[:200]}" if clean and not ok else ""),
            data={"phase": phase, "ok": bool(ok), "ticket_id": ticket_id})
    status = str(sfx["status"])
    if row["kind"] == "install":
        _install_phase(connection, settings, sfx, ticket_id, phase, ok, clean, stack_head)
    elif row["kind"] == "rollback":
        _rollback_phase(connection, sfx, ticket_id, phase, ok, clean)
    elif row["kind"] in ("remove", "remove_all"):
        _removal_phase(connection, sfx, ticket_id, str(row["kind"]), phase, ok, clean)
    out = sf.get(connection, sfx_id)
    return {"self_fix_id": sfx_id, "was": status, "status": out["status"]}


def _finish(connection: Connection, ticket_id: str, status: str) -> None:
    connection.execute(text("UPDATE self_fix_tickets SET status = :s, finished_at = now(), "
                            "updated_at = now() WHERE id = :i"), {"s": status, "i": ticket_id})


def _install_phase(connection: Connection, settings: Any, sfx: dict[str, Any], ticket_id: str,
                   phase: str, ok: bool, detail: str, stack_head: Optional[str]) -> None:
    sfx_id = str(sfx["id"])
    status = str(sfx["status"])
    if status == sf.APPROVED and phase in ("backup", "apply", "build", "start", "health"):
        sf.transition(connection, sfx_id, sf.INSTALLING, actor=sf.INSTALLER_ACTOR,
                      sentence="Installing BOSS's fix on this machine.")
        status = sf.INSTALLING
    if not ok:
        if phase in ("backup", "apply", "build"):
            # Nothing was restarted: the running BOSS is untouched.
            _finish(connection, ticket_id, "failed")
            if status in (sf.APPROVED, sf.INSTALLING):
                sf.transition(connection, sfx_id, sf.INSTALL_FAILED, actor=sf.INSTALLER_ACTOR,
                              sentence=f"The install stopped at {phase}; nothing was restarted. "
                                       f"{detail[:300]}")
        elif phase in ("start", "health"):
            # The self-installer rolls back by itself (re-tag :prev, up); its `rollback` report
            # settles it. Said now so the step's line is true while it happens.
            if status in (sf.INSTALLING, sf.HEALTH_WINDOW):
                sf.transition(connection, sfx_id, sf.ROLLING_BACK, actor=sf.INSTALLER_ACTOR,
                              sentence=f"Rolling back: the {phase} check failed ({detail[:200]}).",
                              data={"trigger": f"{phase}: {detail[:200]}"})
        elif phase == "rollback":
            _rollback_phase(connection, sf.get(connection, sfx_id), ticket_id, phase, ok, detail)
        return
    if phase == "start" and status == sf.INSTALLING:
        sf.transition(connection, sfx_id, sf.HEALTH_WINDOW, actor=sf.INSTALLER_ACTOR,
                      sentence="BOSS restarted with its fix; watching its health.",
                      installed_stack_head=stack_head or sfx.get("fix_commit_sha"))
    elif phase == "rollback":
        _rollback_phase(connection, sfx, ticket_id, phase, ok, detail)
    elif phase == "done" and status in (sf.HEALTH_WINDOW, sf.INSTALLING):
        head = stack_head or sfx.get("fix_commit_sha")
        sf.transition(connection, sfx_id, sf.INSTALLED, actor=sf.INSTALLER_ACTOR,
                      sentence="Installed and healthy; the step resumes next.",
                      installed_stack_head=head, installed_at=now_utc(), stack_state="on_stack")
        _stack(connection, sf.get(connection, sfx_id))
        _finish(connection, ticket_id, "done")


def _rollback_phase(connection: Connection, sfx: dict[str, Any], ticket_id: str, phase: str,
                    ok: bool, detail: str) -> None:
    if phase not in ("rollback", "done"):
        return
    status = str(sfx["status"])
    if ok:
        _finish(connection, ticket_id, "rolled_back")
        _unstack(connection, str(sfx["id"]))
        if status in (sf.ROLLING_BACK, sf.NOT_RESOLVED, sf.INSTALLING, sf.HEALTH_WINDOW):
            sf.transition(connection, str(sfx["id"]), sf.ROLLED_BACK, actor=sf.INSTALLER_ACTOR,
                          sentence="Rolled back: the previous build is running again.",
                          rolled_back_at=now_utc(), stack_state="removed",
                          data={"detail": detail[:300]})
        return
    # THE ROLLBACK ITSELF FAILED: stop, raise an incident a person sees, print the one command.
    _finish(connection, ticket_id, "failed")
    if status == sf.ROLLING_BACK:
        sf.transition(connection, str(sfx["id"]), sf.INSTALL_FAILED, actor=sf.INSTALLER_ACTOR,
                      sentence=("The rollback did not start either. Restore the backup the "
                                "installer printed: scripts/install/restore.sh."),
                      data={"detail": detail[:300]})
    _incident(connection, sfx, detail)


def _removal_phase(connection: Connection, sfx: Mapping[str, Any], ticket_id: str, kind: str,
                   phase: str, ok: bool, detail: str) -> None:
    """A removal's phases. `done`: the fixes it named are off the stack (their status stays —
    they were verified — and their stack state says removed). A failed start or health puts the
    current build back (the self-installer's `rollback`), and nothing is removed."""
    body = connection.execute(text("SELECT body FROM self_fix_tickets WHERE id = :i"),
                              {"i": ticket_id}).scalar() or {}
    if isinstance(body, str):
        body = json.loads(body)
    removed = [str(x) for x in (body or {}).get("remove") or [sfx["id"]]]
    if phase == "done" and ok:
        _finish(connection, ticket_id, "done")
        for sfx_id in removed:
            _unstack(connection, sfx_id)
            connection.execute(text("UPDATE self_fixes SET stack_state = 'removed', updated_at "
                                    "= now() WHERE id = :i"), {"i": sfx_id})
            one = sf.get(connection, sfx_id) or {"id": sfx_id, "project_id": sfx["project_id"]}
            sf.emit(connection, one, "removed", actor=sf.INSTALLER_ACTOR,
                    sentence="Removed from this one-box: BOSS was rebuilt without it.",
                    data={"ticket_id": ticket_id, "kind": kind})
            sf.audit(connection, "self_fix.removed", actor=sf.INSTALLER_ACTOR, sfx=one,
                     details={"kind": kind})
        return
    if phase == "rollback":
        _finish(connection, ticket_id, "rolled_back" if ok else "failed")
        if not ok:
            _incident(connection, sfx, detail)
        return
    if not ok and phase in ("backup", "apply", "build"):
        _finish(connection, ticket_id, "failed")


def _incident(connection: Connection, sfx: Mapping[str, Any], detail: str) -> None:
    decision_id = make_id("dec")
    connection.execute(text(
        "INSERT INTO decisions (id, project_id, task_id, question, recommendation, options, "
        "risk_tier, cost_summary, deadline_at, default_action, status) VALUES (:id, :p, NULL, "
        ":q, CAST(:r AS JSONB), CAST(:o AS JSONB), 3, '{}'::jsonb, :d, 'hold', 'pending')"),
        {"id": decision_id, "p": sfx["project_id"],
         "q": "BOSS could not roll back a self-fix: restore the backup now",
         "r": json.dumps({"action": "review_rollback_or_retry", "self_fix_id": sfx["id"],
                          "command": "scripts/install/restore.sh", "detail": detail[:600]}),
         "o": json.dumps([{"action": "approve", "label": "I restored it"}]),
         "d": now_utc() + DECISION_TTL})


def _stack(connection: Connection, sfx: Mapping[str, Any]) -> None:
    release = _release_ref()
    position = int(connection.execute(text(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM self_fix_stack WHERE release_ref = :r"),
        {"r": release}).scalar() or 1)
    connection.execute(text(
        "INSERT INTO self_fix_stack (release_ref, position, self_fix_id, commit_sha) VALUES "
        "(:r, :n, :f, :c)"),
        {"r": release, "n": position, "f": sfx["id"], "c": sfx.get("fix_commit_sha")})


def _unstack(connection: Connection, sfx_id: str) -> None:
    connection.execute(text("DELETE FROM self_fix_stack WHERE self_fix_id = :i"), {"i": sfx_id})


def stack(connection: Connection, release: Optional[str] = None) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT s.release_ref, s.position, s.self_fix_id, s.commit_sha, f.status, f.pr->>'title' "
        "AS title, f.class_signature FROM self_fix_stack s JOIN self_fixes f ON f.id = "
        "s.self_fix_id WHERE s.release_ref = :r ORDER BY s.position"),
        {"r": release or _release_ref()}).mappings().all()
    return [dict(r) for r in rows]


def rebase_stale(connection: Connection, settings: Any) -> list[str]:
    """A card whose fix was checked on a stack that is no longer running (another fix was
    installed, or BOSS was upgraded) is withdrawn, and the fix is checked again on the new one."""
    running = sf.running_stack_head()
    if not running:
        return []
    moved = []
    for row in connection.execute(text(
            "SELECT id FROM self_fixes WHERE status = 'awaiting_approval' AND base_sha <> :h"),
            {"h": running}).all():
        sfx_id = str(row[0])
        connection.execute(text(
            "UPDATE decisions SET status = 'withdrawn', resolution_note = :n, updated_at = now() "
            "WHERE recommendation->>'self_fix_id' = :i AND status IN ('pending', 'delegated')"),
            {"i": sfx_id, "n": "BOSS's stack changed; the fix is checked again on the new one"})
        sfx = sf.transition(connection, sfx_id, sf.FIXING, actor=sf.SYSTEM_ACTOR,
                            sentence="BOSS's stack changed since this fix was checked; checking "
                                     "it again on the new one.", base_sha=running,
                            repair_rounds=int((sf.get(connection, sfx_id) or {}).get(
                                "repair_rounds") or 0))
        sf.enqueue_build(connection, sfx, round_note="rebase onto the running stack")
        moved.append(sfx_id)
    return moved


INSTALLER_SEEN_KEY = "self_fix_installer"
#: The watcher polls every 10 s: seen within a minute is "watching".
WATCHING_SECONDS = 60


def seen_installer(connection: Connection) -> None:
    """The host self-installer asked for tickets: Govern › Self-fix says it is watching."""
    try:
        with connection.begin_nested():
            connection.execute(text(
                "INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), "
                "now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = "
                "now()"), {"k": INSTALLER_SEEN_KEY,
                           "v": json.dumps({"last_seen_at": now_utc().isoformat()})})
    except Exception:  # noqa: BLE001 — a status line, never a reason to refuse tickets
        log.debug("self-installer sighting not recorded")


def installer_seen(connection: Connection) -> dict[str, Any]:
    """{last_seen_at, watching}: when the host self-installer last asked for tickets."""
    from datetime import datetime

    try:
        with connection.begin_nested():
            value = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                                       {"k": INSTALLER_SEEN_KEY}).scalar()
    except Exception:  # noqa: BLE001
        value = None
    if isinstance(value, str):
        value = json.loads(value)
    at = (value or {}).get("last_seen_at") if isinstance(value, dict) else None
    watching = False
    if at:
        try:
            watching = (now_utc() - datetime.fromisoformat(str(at))).total_seconds() \
                <= WATCHING_SECONDS
        except (TypeError, ValueError):
            watching = False
    return {"last_seen_at": at, "watching": watching}


def store_bundle(connection: Connection, sfx_id: str, bundle: bytes) -> None:
    connection.execute(text("UPDATE self_fixes SET bundle = :b WHERE id = :i"),
                       {"b": bundle, "i": sfx_id})


# ── the health window the self-installer asks about ──────────────────────────────────────────

def health_report(connection: Connection, *, since_seconds: int = 600) -> dict[str, Any]:
    """What the self-installer's health window reads from inside BOSS: the build that answers,
    the worker's last heartbeat, and the job error rate now against the hour before."""
    from . import step_recovery

    def scalar(sql: str, **p: Any) -> Any:
        try:
            with connection.begin_nested():
                return connection.execute(text(sql), p).scalar()
        except Exception:  # noqa: BLE001
            return None

    heartbeat = scalar("SELECT EXTRACT(EPOCH FROM (now() - MAX(last_heartbeat_at))) FROM "
                       "worker_instances")
    def rate(start: int, end: int) -> Optional[float]:
        total = scalar("SELECT count(*) FROM jobs WHERE updated_at > now() - make_interval("
                       "secs => :a) AND updated_at <= now() - make_interval(secs => :b) AND "
                       "status IN ('succeeded', 'failed', 'deadlettered')", a=start, b=end)
        failed = scalar("SELECT count(*) FROM jobs WHERE updated_at > now() - make_interval("
                        "secs => :a) AND updated_at <= now() - make_interval(secs => :b) AND "
                        "status IN ('failed', 'deadlettered')", a=start, b=end)
        if not total:
            return None
        return round(float(failed or 0) / float(total), 3)
    now_rate = rate(since_seconds, 0)
    before = rate(since_seconds + 3600, since_seconds)
    regressed = now_rate is not None and before is not None and now_rate > before + 0.05
    return {"source_sha": sf.source_sha(), "stack_head": sf.running_stack_head(),
            "boss_build": step_recovery.boss_build(),
            "worker_heartbeat_age_seconds": float(heartbeat) if heartbeat is not None else None,
            "error_rate": now_rate, "error_rate_before": before, "regressed": regressed}


# ── upgrades keep local fixes (R8, §6.4) ─────────────────────────────────────────────────────

def _installer_module() -> Any:
    """scripts/install/self_fix_installer.py: one implementation of the stack plan, for the host
    (upgrade.sh) and for BOSS (this API) alike."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "install" / "self_fix_installer.py"
    spec = importlib.util.spec_from_file_location("boss_self_fix_installer", path)
    if spec is None or spec.loader is None:
        raise InstallRefused("the self-installer is not in this image")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade_plan(connection: Connection, settings: Any, *, to_ref: str,
                 behaviour: Optional[Callable[[str, str], bool]] = None) -> dict[str, Any]:
    """Each installed fix, against the release `to_ref`, in stack order: dropped_upstream (by
    commit or trailer; or by behaviour when `behaviour(commit, to_sha)` says its regression test
    passes on `to_ref` without it), carried, or needs_rework — read from the mirror."""
    from . import self_fix_source as src

    fixes = stack(connection)
    mirror = src.mirror_of(settings)
    installer = _installer_module()
    plan = installer.plan_stack(str(mirror), to_ref, [
        {"self_fix_id": f["self_fix_id"], "commit": f["commit_sha"]} for f in fixes],
        behaviour=behaviour)
    return {"to": to_ref, "fixes": plan, "release": _release_ref()}


def record_upgrade(connection: Connection, plan: list[Mapping[str, Any]], *,
                   release: str, actor: str = sf.INSTALLER_ACTOR) -> list[dict[str, Any]]:
    """The host applied an upgrade's stack plan: each fix's fate, recorded (nothing silently)."""
    from . import boss_recommendations

    out = []
    for item in plan:
        sfx = sf.get(connection, str(item.get("self_fix_id") or ""))
        if sfx is None:
            continue
        outcome = str(item.get("outcome") or "")
        state = {"dropped_upstream": "dropped_upstream", "carried": "on_stack",
                 "needs_rework": "needs_rework", "removed": "removed"}.get(outcome)
        if state is None:
            continue
        sf.update(connection, sfx["id"], stack_state=state)
        if outcome == "dropped_upstream" and sfx["status"] in (sf.VERIFIED, sf.INSTALLED):
            sf.transition(connection, sfx["id"], sf.SUPERSEDED, actor=actor,
                          sentence=f"In release {release}: upstream has this fix "
                                   f"({item.get('how') or 'commit'}).")
        elif outcome == "removed" and sfx.get("recommendation_id"):
            boss_recommendations.record_attempt(connection, str(sfx["project_id"]), str(
                sfx["recommendation_id"]), {"self_fix_id": sfx["id"], "outcome": "removed",
                                            "why": f"upgraded to {release} without it",
                                            "at": now_utc().isoformat()})
        sf.emit(connection, sf.get(connection, sfx["id"]), "stack_planned", actor=actor,
                sentence=f"Upgrade to {release}: {outcome.replace('_', ' ')}.",
                data={"outcome": outcome, "release": release})
        out.append({"self_fix_id": sfx["id"], "outcome": outcome})
    if out:
        connection.execute(text("DELETE FROM self_fix_stack WHERE release_ref <> :r"),
                           {"r": release})
        position = 0
        for item in plan:
            if item.get("outcome") == "carried" and item.get("new_commit"):
                position += 1
                connection.execute(text(
                    "INSERT INTO self_fix_stack (release_ref, position, self_fix_id, commit_sha) "
                    "VALUES (:r, :n, :f, :c) ON CONFLICT (release_ref, position) DO UPDATE SET "
                    "self_fix_id = EXCLUDED.self_fix_id, commit_sha = EXCLUDED.commit_sha"),
                    {"r": release, "n": position, "f": item["self_fix_id"],
                     "c": item["new_commit"]})
    return out


__all__ = ["ACTION", "IdentitySigner", "PERSON_ACTIONS", "REMOVE_ACTION", "installer_seen",
           "on_remove_approved", "request_removal", "seen_installer", "InstallRefused", "KINDS", "MANUAL_COMMAND", "PHASES",
           "TICKET_TYP", "card", "content_hash", "expire_tickets", "health_report",
           "issue_rollback", "issue_ticket", "on_approved", "on_rejected", "on_revise",
           "pending_tickets", "rebase_stale", "record_upgrade", "refuse_policy_approval",
           "reissue", "report", "request_approval", "revoke_tickets", "signer", "stack",
           "store_bundle", "take", "upgrade_plan", "use_signer"]
