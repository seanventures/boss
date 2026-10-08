"""Every console-wide identity banner's id, policy and lifetime, in one place (GET /v1/identity/me
`banners`; POST /v1/identity/me/banners/{id}/dismiss).

A banner is produced by its source (identity_me for new_device and MFA enrolment, posture for the
rest) as a raw dict {kind, message, until, instance}. `instance` names the event or condition the
banner is about (an audit seq, a key id, a recovery-code batch, a cutoff date), so the banner's id
is `kind.instance` and a NEW event (another new device, the next key) is a new id that shows again
even after the last one was closed.

Policies:
  persistent   can never be closed while its condition holds (MFA required and not enrolled, an
               unclaimed network bridge, a legacy countdown in its final 3 days).
  dismissible  × hides it for that person on every device: for 24 h (legacy countdowns), or for as
               long as the instance lasts (new device, low recovery codes, an expiring key).
  acknowledge  a security notice: × reads "Got it" and the dismissal is audited
               (`banner.acknowledged`).

Lifetimes that are not a dismissal live in the sources: new_device shows for 48 h after the sign-in,
owner_recovered for 7 days after the recovery.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Optional

from sqlalchemy import text

Policy = Literal["dismissible", "acknowledge", "persistent"]

#: How long a closed legacy countdown stays closed; it never closes in its final days.
COUNTDOWN_SNOOZE = timedelta(hours=24)
COUNTDOWN_FINAL = timedelta(days=3)
#: A sign-in from a new device is news for this long.
NEW_DEVICE_HOURS = 48
#: Dismissals are dropped after this long, so the table holds a handful of rows per person. Old
#: events have aged out of their sources by then; a condition that still holds a month later
#: (recovery codes still low) is worth saying again.
PRUNE_AFTER = timedelta(days=30)

BANNER_ID_RE = re.compile(r"^[a-z_]{1,40}(\.[A-Za-z0-9_-]{1,80})?$")


@dataclass(frozen=True)
class Rule:
    policy: Policy
    #: None: hidden while the instance lasts; else hidden for this long after the dismissal.
    snooze: Optional[timedelta] = None
    #: A countdown: persistent once `until` is this close.
    final: Optional[timedelta] = None


#: Every banner kind (contracts.MeBanner.kind) and its rule. test_identity_banner_dismissal pins
#: that the two lists are the same set.
RULES: dict[str, Rule] = {
    "mfa_enrolment_required": Rule("persistent"),
    "network_bridge": Rule("dismissible", snooze=COUNTDOWN_SNOOZE, final=COUNTDOWN_FINAL),
    "legacy_password": Rule("dismissible", snooze=COUNTDOWN_SNOOZE, final=COUNTDOWN_FINAL),
    "legacy_api_key": Rule("dismissible", snooze=COUNTDOWN_SNOOZE, final=COUNTDOWN_FINAL),
    "new_device": Rule("dismissible"),
    "recovery_codes_low": Rule("dismissible"),
    "api_key_expiring": Rule("dismissible"),
    "owner_recovered": Rule("acknowledge"),
}

_ORDER = {"persistent": 0, "acknowledge": 1, "dismissible": 2}


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def banner_id(kind: str, instance: Optional[str]) -> str:
    return f"{kind}.{instance}" if instance else kind


def policy_of(raw: dict[str, Any], now: datetime) -> Policy:
    rule = RULES[raw["kind"]]
    if raw["kind"] == "network_bridge" and raw.get("until") is None:
        return "persistent"         # unclaimed: anyone who can reach it acts as the operator
    until = raw.get("until")
    if rule.final is not None and until is not None and until - now <= rule.final:
        return "persistent"
    return rule.policy


def shaped(raw: Iterable[dict[str, Any]], now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """The raw banners with their id and policy (nothing hidden): MeBanner dicts, most binding first."""
    now = _now(now)
    out = []
    for b in raw:
        out.append({"id": banner_id(b["kind"], b.get("instance")), "kind": b["kind"],
                    "policy": policy_of(b, now), "message": b["message"], "until": b.get("until")})
    out.sort(key=lambda b: _ORDER[b["policy"]])
    return out


def dismissed(db: Any, principal_id: str, now: Optional[datetime] = None) -> set[str]:
    """The banner ids this principal has closed that are still closed now."""
    rows = db.execute(text(
        "SELECT banner_id FROM identity_banner_dismissals WHERE principal_id = :p AND "
        "(hidden_until IS NULL OR hidden_until > :now)"), {"p": principal_id, "now": _now(now)})
    return {r[0] for r in rows}


def visible(db: Any, principal_id: str, raw: Iterable[dict[str, Any]],
            now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """What the person sees: every persistent banner, and the others they have not closed."""
    now = _now(now)
    closed = dismissed(db, principal_id, now)
    return [b for b in shaped(raw, now) if b["policy"] == "persistent" or b["id"] not in closed]


class NotDismissible(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def dismiss(db: Any, principal_id: str, wanted: str, raw: Iterable[dict[str, Any]],
            now: Optional[datetime] = None) -> dict[str, Any]:
    """Close banner `wanted` for this principal. Only a banner they are shown now can be closed
    (404 otherwise: no table rows for made-up ids); a persistent one is refused (409)."""
    now = _now(now)
    banner = next((b for b in shaped(raw, now) if b["id"] == wanted), None)
    if banner is None:
        raise NotDismissible("not_found", 404)
    if banner["policy"] == "persistent":
        raise NotDismissible("banner_persistent", 409)
    rule = RULES[banner["kind"]]
    hidden_until = now + rule.snooze if rule.snooze else None
    db.execute(text(
        "INSERT INTO identity_banner_dismissals (principal_id, banner_id, kind, dismissed_at, hidden_until) "
        "VALUES (:p, :b, :k, :now, :h) ON CONFLICT (principal_id, banner_id) DO UPDATE SET "
        "dismissed_at = EXCLUDED.dismissed_at, hidden_until = EXCLUDED.hidden_until"),
        {"p": principal_id, "b": wanted, "k": banner["kind"], "now": now, "h": hidden_until})
    db.execute(text(
        "DELETE FROM identity_banner_dismissals WHERE principal_id = :p AND dismissed_at < :old"),
        {"p": principal_id, "old": now - PRUNE_AFTER})
    return {**banner, "hidden_until": hidden_until}
