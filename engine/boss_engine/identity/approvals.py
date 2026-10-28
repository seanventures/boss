"""Approvals and seats: separation of duties (IDENTITY.md §6.13). FX-B P0-7.

To APPROVE a decision under identity, all four hold — checked here, in the one place every approval
passes through (decisions.transition), whatever the path (the HTTP route, the MCP `decide` tool):

  1. `decision:approve` — the route's / tool's declaration (not delegable: no agent ever approves);
  2. the SEAT the decision's risk tier requires (default_safety.yaml `risk_tier_requirements`:
     tier 2 -> ceo, tier 3 -> ceo or cao), from `identity_approval_seats`. A seat nobody active holds
     is VACANT, and the org's active Owners act in it until an Owner assigns it (no install is left
     unable to approve after an upgrade; posture names vacant seats);
  3. a HUMAN session — API keys and run tokens cannot approve. An MCP confirmation keeps its
     human-approval flow: the person who approved the confirmation is the approver. The one
     exception is a person's own APPROVAL DELEGATE (approval_delegations.py, §6.15): its key
     approves as that person, inside the grant, never past the hard limits;
  4. for tier 3, TWO DISTINCT PEOPLE, unless the org policy `single_operator_waiver` is on (D29:
     automatic = on exactly while the org has one active person). The first approval is recorded
     in `identity_decision_approvals` and the decision stays pending (ApprovalPending -> HTTP 202).

Identity decides an approval when the call is identity-enforced and the org has been claimed;
before the claim (and in shadow/off) today's member rules decide, unchanged.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from sqlalchemy import text

from .principal import AuthContext

#: tiers that need two distinct people (§6.13 condition 4)
TWO_PERSON_TIERS = frozenset({3})

#: Set by the API for the one request an MCP confirmation released: the reviewer the
#: confirmation was approved by (a principal id or a legacy member id). Condition 3 is that person.
MCP_REVIEWER: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "boss_identity_mcp_reviewer", default=None)


class ApprovalRefused(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


class ApprovalPending(Exception):
    """A tier-3 decision has its first of two approvals: recorded, still pending."""

    def __init__(self, decision_id: str, approvals: list[dict[str, Any]], needed: int) -> None:
        super().__init__("awaiting_second_approval")
        self.decision_id, self.approvals, self.needed = decision_id, approvals, needed

    def body(self) -> dict[str, Any]:
        return {"status": "awaiting_second_approval", "decision_id": self.decision_id,
                "approvals": self.approvals, "needed": self.needed}


# ---- the tier -> seats map -------------------------------------------------------------------------

@lru_cache(maxsize=1)
def tier_seats() -> dict[int, tuple[str, ...]]:
    """{tier: seats that may approve it} from the specpack's default_safety.yaml."""
    try:
        import yaml

        from ..config import get_settings

        path = get_settings().specpack_root / "policies" / "default_safety.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — the shipped defaults (today's rules) when the file is absent
        return {0: (), 1: (), 2: ("ceo",), 3: ("ceo", "cao")}
    tiers = ((payload.get("rules") or {}).get("approvals") or {}).get("risk_tier_requirements") or {}
    return {int(t): tuple(str(s) for s in ((v or {}).get("approvals") or ())) for t, v in tiers.items()}


def known_seats() -> list[str]:
    from .claim import DEFAULT_SEATS

    out = set(DEFAULT_SEATS)
    for seats in tier_seats().values():
        out |= set(seats)
    return sorted(out)


def seats_for_tier(tier: Any) -> tuple[str, ...]:
    try:
        return tier_seats().get(int(tier or 0), ())
    except (TypeError, ValueError):
        return ()


# ---- people, seats, waiver -------------------------------------------------------------------------

def _active_people(db: Any, org_id: str) -> list[str]:
    from .legacy import is_bridge_principal

    rows = db.execute(text(
        "SELECT id FROM identity_principals WHERE org_id = :o AND kind = 'user' AND status = 'active' "
        "AND NOT network_bound"), {"o": org_id}).scalars().all()
    return [r for r in rows if not is_bridge_principal(r)]


def waiver(db: Any, org_id: str) -> dict[str, Any]:
    """D29: {"effective", "setting", "active_people"}."""
    from .policy import get_policy

    setting = get_policy(org_id, db=db).single_operator_waiver
    people = len(_active_people(db, org_id))
    effective = bool(setting) if setting is not None else people == 1
    return {"effective": effective, "setting": setting, "active_people": people}


def holders(db: Any, org_id: str) -> dict[str, list[str]]:
    """{seat: active people who hold it}."""
    rows = db.execute(text(
        "SELECT s.seat_key, s.principal_id FROM identity_approval_seats s JOIN identity_principals p "
        "ON p.id = s.principal_id WHERE s.org_id = :o AND p.status = 'active' AND p.kind = 'user'"),
        {"o": org_id}).all()
    out: dict[str, list[str]] = {}
    for seat, pid in rows:
        out.setdefault(seat, []).append(pid)
    return out


def seats_held(db: Any, org_id: str, principal_id: str, wanted: tuple[str, ...]) -> list[str]:
    """The seats among `wanted` this person may approve in: the ones assigned to them, and every
    VACANT one (no active holder) when they are an active Owner."""
    from .bindings import owner_ids

    held = holders(db, org_id)
    mine = [s for s in wanted if principal_id in held.get(s, ())]
    vacant = [s for s in wanted if not held.get(s)]
    if vacant and principal_id in owner_ids(db, org_id):
        mine += vacant
    return mine


# ---- who is approving ------------------------------------------------------------------------------

def deciding_context() -> Optional[AuthContext]:
    """The verified caller of this approval when identity ENFORCES this call, else None (shadow,
    off, or no identity on this path: today's member rules decide)."""
    from . import lineage

    ctx = lineage.current_context()
    if ctx is None:
        try:
            from ..tools.registry import CURRENT_TOOL_AUTH

            ctx = CURRENT_TOOL_AUTH.get()
        except Exception:  # noqa: BLE001
            ctx = None
    if ctx is None:
        return None
    enforced = lineage.request_enforced()
    if enforced is None:
        from ..config import get_settings
        from .enforcement import AuthzMode, mode_for

        settings = get_settings()
        enforced = mode_for("mcp", global_mode=settings.authz_mode,
                            router_modes=settings.authz_router_modes) == AuthzMode.ENFORCE
    return ctx if enforced else None


def _reviewer_principal(db: Any, org_id: str, reviewer: str) -> Optional[str]:
    if reviewer.startswith("usr_"):
        return reviewer
    from . import migration

    found = migration.mapping(db, org_id, migration.LegacyKind.MEMBER_ID, reviewer)
    return found["principal_id"] if found else None


def _approver(db: Any, ctx: AuthContext) -> str:
    """Condition 3: the person approving, in a human session (or the human an MCP confirmation
    names). Raises ApprovalRefused otherwise."""
    from .legacy import is_bridge_principal

    reviewer = MCP_REVIEWER.get()
    if reviewer:
        pid = _reviewer_principal(db, ctx.principal.org_id, reviewer)
        if pid:
            return pid
    if ctx.is_human_session and not is_bridge_principal(ctx.principal.id):
        return ctx.principal.id
    raise ApprovalRefused(403, "human_session_required",
                          "Only a person signed in to BOSS can approve. API keys and agents cannot.")


def _names(db: Any, ids: list[str]) -> dict[str, str]:
    """Each person's name, told apart from anyone ACTIVE in the same org with the same name: a
    seat refusal said "held by … Seed Owner" while the refused person was also named Seed Owner,
    so it read as "you hold it, and you may not approve". A shared name gets the person's email,
    else the day they joined; never their id."""
    if not ids:
        return {}
    rows = db.execute(text(
        "SELECT p.id, p.display_name, p.created_at, u.email, "
        "       (SELECT count(*) FROM identity_principals q WHERE q.org_id = p.org_id "
        "          AND q.kind = p.kind AND q.status = 'active' "
        "          AND lower(q.display_name) = lower(p.display_name)) AS same "
        "FROM identity_principals p LEFT JOIN identity_users u ON u.principal_id = p.id "
        "WHERE p.id = ANY(:i)"), {"i": ids}).mappings().all()
    out: dict[str, str] = {}
    for r in rows:
        name = r["display_name"]
        if name and (r["same"] or 0) > 1:
            tell = r["email"] or (f"joined {r['created_at']:%b} {r['created_at'].day}"
                                  if r["created_at"] else None)
            name = f"{name} ({tell})" if tell else name
        out[r["id"]] = name
    return out


def recorded(db: Any, decision_id: str) -> list[dict[str, Any]]:
    rows = db.execute(text(
        "SELECT principal_id, seat_key, approved_at FROM identity_decision_approvals "
        "WHERE decision_id = :d ORDER BY approved_at"), {"d": decision_id}).mappings().all()
    names = _names(db, [r["principal_id"] for r in rows])
    return [{"principal_id": r["principal_id"], "display_name": names.get(r["principal_id"]),
             "seat_key": r["seat_key"], "at": r["approved_at"].isoformat()} for r in rows]


#: The console screen where seat holders are changed (apps/web/app/identity/seats).
SEATS_SCREEN = "/identity/seats"


def _people_words(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def seat_refusal(db: Any, org_id: str, tier: int, wanted: tuple[str, ...]) -> ApprovalRefused:
    """Who to ask, not just "ask an Admin": the refusal names the people who hold the seat(s) now
    and where seats are changed (Identity › Approval seats). A vacant seat says an Owner stands in."""
    held = holders(db, org_id)
    ids = list(dict.fromkeys(pid for seat in wanted for pid in held.get(seat, ())))
    names = _names(db, ids)
    listed = [{"principal_id": pid, "display_name": names.get(pid, pid)} for pid in ids]
    seat_words = " or ".join(wanted)
    if listed:
        who = (f"held by {_people_words([h['display_name'] for h in listed])}. Ask "
               f"{'them' if len(listed) == 1 else 'one of them'} to approve it")
    else:
        who = "which nobody holds, so an Owner approves it"
    message = (f"A tier-{tier} decision is approved by the {seat_words} seat, {who}, or change who "
               f"holds the seat in Identity › Approval seats ({SEATS_SCREEN}).")
    return ApprovalRefused(403, "seat_required", message, seats=list(wanted), tier=tier,
                           holders=listed, href=SEATS_SCREEN, href_label="Approval seats")


@dataclass(frozen=True)
class Approval:
    approver: str
    seat: Optional[str]


def _still_counts(db: Any, org: str, row: dict[str, Any], wanted: tuple[str, ...],
                  content_hash: Optional[str]) -> Optional[str]:
    """C-3: why an EARLIER approval no longer counts toward the two (None when it does): it was of
    other content than what is approved now, or its person is no longer active or no longer holds
    a seat for this tier. Checked when the next approval would complete the decision."""
    if content_hash is not None and row.get("content_hash") != content_hash:
        return "content_changed"
    status = db.execute(text("SELECT status FROM identity_principals WHERE id = :p AND kind = 'user'"),
                        {"p": row["principal_id"]}).scalar()
    if status != "active":
        return "approver_not_active"
    if wanted and not seats_held(db, org, row["principal_id"], wanted):
        return "approver_seat_lost"
    return None


def require_approval(db: Any, ctx: AuthContext, decision: dict[str, Any], *,
                     content_hash: Optional[str] = None, delegated: Any = None) -> Approval:
    """Conditions 2-4 for one approve. Returns the Approval when the decision may now be approved;
    raises ApprovalRefused (403/409) or ApprovalPending (the first of two, recorded).

    `content_hash` (C-3) is the hash of the preview being approved, ALREADY checked against what
    the approver saw (decisions.transition checks it first). Each approval records it; an earlier
    approval of other content, or by a person since suspended or un-seated, is voided and does not
    count — so two people always approve the same content, both entitled at completion."""
    from .audit import Outcome, SecurityEvent, record

    org = ctx.principal.org_id
    # §6.15: a delegate approves AS the person whose live delegation `approval_delegations.authorize`
    # already checked for this decision; the seat and two-person rules are then that person's.
    approver = delegated.person_principal_id if delegated is not None else _approver(db, ctx)
    tier = int(decision.get("risk_tier") or 0)
    wanted = seats_for_tier(tier)
    seat: Optional[str] = None
    if wanted:
        mine = seats_held(db, org, approver, wanted)
        if not mine:
            raise seat_refusal(db, org, tier, wanted)
        seat = mine[0]
    if tier in TWO_PERSON_TIERS and not waiver(db, org)["effective"]:
        decision_id = str(decision["id"])
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"identity_approval:{decision_id}"})
        rows = [dict(r) for r in db.execute(text(
            "SELECT principal_id, seat_key, content_hash FROM identity_decision_approvals "
            "WHERE decision_id = :d ORDER BY approved_at"), {"d": decision_id}).mappings().all()]
        for row in rows:
            why = _still_counts(db, org, row, wanted, content_hash)
            if why is None:
                continue
            db.execute(text("DELETE FROM identity_decision_approvals WHERE decision_id = :d AND "
                            "principal_id = :p"), {"d": decision_id, "p": row["principal_id"]})
            record(SecurityEvent.from_context(ctx, "decision.approval_voided", Outcome.INFO,
                                              target_type="decision", target_id=decision_id,
                                              details={"approver": row["principal_id"], "reason": why,
                                                       "tier": tier}), db=db)
        before = recorded(db, decision_id)
        if any(a["principal_id"] == approver for a in before):
            raise ApprovalRefused(409, "second_approver_must_differ",
                                  "You have approved this already. A tier-3 decision needs a second person.")
        db.execute(text(
            "INSERT INTO identity_decision_approvals (decision_id, org_id, principal_id, seat_key, "
            "content_hash) VALUES (:d, :o, :p, :s, :h)"),
            {"d": decision_id, "o": org, "p": approver, "s": seat, "h": content_hash})
        record(SecurityEvent.from_context(ctx, "decision.approval_recorded", Outcome.INFO,
                                          target_type="decision", target_id=decision_id,
                                          details={"seat": seat, "tier": tier,
                                                   "approvals": len(before) + 1}), db=db)
        if len(before) + 1 < 2:
            raise ApprovalPending(decision_id, recorded(db, decision_id), 2)
    return Approval(approver=approver, seat=seat)


def status(db: Any, org_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """GET /v1/identity/decisions/{id}/approvals."""
    tier = int(decision.get("risk_tier") or 0)
    waived = waiver(db, org_id)["effective"]
    return {"decision_id": decision["id"], "tier": tier,
            "needed": 2 if tier in TWO_PERSON_TIERS and not waived else 1,
            "approvals": recorded(db, str(decision["id"])), "seats": list(seats_for_tier(tier)),
            "waiver": waived}


def require_delegate_target(db: Any, ctx: AuthContext, delegate_to: Optional[str]) -> None:
    """Delegating a decision names an active person of this org (by principal id or the legacy
    member id they took over); anything else is refused, as the member check refused it before."""
    target = (delegate_to or "").strip()
    pid = _reviewer_principal(db, ctx.principal.org_id, target) if target else None
    if not pid or pid not in _active_people(db, ctx.principal.org_id):
        raise ApprovalRefused(422, "invalid_delegate", "Delegate to an active person in this org.")


# ---- seat administration (PUT /v1/identity/seats/{seat_key}) ----------------------------------------

def set_seat(db: Any, ctx: AuthContext, seat_key: str, principal_ids: list[str]) -> dict[str, Any]:
    from .audit import Outcome, SecurityEvent, record
    from .authz import bump_epoch
    from .roles import AccessError

    org = ctx.principal.org_id
    if seat_key not in known_seats():
        raise AccessError(422, "unknown_seat", "BOSS has no such approval seat.")
    wanted = list(dict.fromkeys(str(p) for p in principal_ids))
    people = set(_active_people(db, org))
    current = set(holders(db, org).get(seat_key, ()))
    seated = set(db.execute(text(
        "SELECT principal_id FROM identity_approval_seats WHERE org_id = :o AND seat_key = :s"),
        {"o": org, "s": seat_key}).scalars().all())   # whatever their status: holders() is active only
    # Only the people being ADDED must be active. The console sends the whole list, so checking
    # the holders already there let one inactive leftover (boss-1, 2026-10-28: a migrated
    # "Founder / CEO" never activated) refuse every edit of the seat — adding the owner too.
    for pid in wanted:
        if pid not in people and pid not in seated:
            raise AccessError(422, "not_a_person", "A seat is held by an active person in this org.",
                              {"principal_id": pid[:80]})
    if not wanted:
        for tier, seats in sorted(tier_seats().items()):
            if seat_key in seats and not any(holders(db, org).get(s) for s in seats if s != seat_key):
                raise AccessError(409, "seat_required",
                                  f"Tier {tier} decisions need someone in this seat. Name someone first.",
                                  {"tier": tier})
    db.execute(text("DELETE FROM identity_approval_seats WHERE org_id = :o AND seat_key = :s "
                    "AND NOT (principal_id = ANY(:keep))"), {"o": org, "s": seat_key, "keep": wanted})
    for pid in wanted:
        db.execute(text("INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) "
                        "VALUES (:o, :s, :p, :b) ON CONFLICT DO NOTHING"),
                   {"o": org, "s": seat_key, "p": pid, "b": ctx.principal.id})
    added, removed = sorted(set(wanted) - current), sorted(current - set(wanted))
    record(SecurityEvent.from_context(ctx, "approval_seat.changed", Outcome.SUCCESS, target_type="seat",
                                      target_id=seat_key, details={"added": added, "removed": removed}), db=db)
    bump_epoch(org, db=db, reason="approval_seat.changed")
    return {"seat_key": seat_key, "holders": seat_rows(db, org, seat_key)}


def seat_rows(db: Any, org_id: str, seat_key: Optional[str] = None) -> list[dict[str, Any]]:
    rows = db.execute(text(
        "SELECT s.seat_key, s.principal_id, p.display_name, s.assigned_by, s.assigned_at "
        "FROM identity_approval_seats s JOIN identity_principals p ON p.id = s.principal_id "
        "WHERE s.org_id = :o AND (CAST(:k AS TEXT) IS NULL OR s.seat_key = :k) ORDER BY s.seat_key, p.display_name"),
        {"o": org_id, "k": seat_key}).mappings().all()
    return [{**dict(r), "assigned_at": r["assigned_at"].isoformat() if r["assigned_at"] else None} for r in rows]


def seat_vacant_tiers(db: Any, org_id: str) -> list[int]:
    """Tiers whose seats nobody active holds (posture: the Owners stand in)."""
    held = holders(db, org_id)
    return [t for t, seats in sorted(tier_seats().items()) if seats and not any(held.get(s) for s in seats)]


def seat_the_first_owner(db: Any, org_id: str, owner_id: str) -> None:
    """At the claim: the first Owner holds every tier seat nobody holds yet (a fresh install has
    no one else to approve)."""
    held = holders(db, org_id)
    for seat in sorted({s for seats in tier_seats().values() for s in seats}):
        if not held.get(seat):
            db.execute(text("INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) "
                            "VALUES (:o, :s, :p, 'claim') ON CONFLICT DO NOTHING"),
                       {"o": org_id, "s": seat, "p": owner_id})
