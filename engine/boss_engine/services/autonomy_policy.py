"""DEV AUTONOMY: when BOSS's agents act without asking (BOSSAPP-1441).

The operator, 2026-11-23, the third time: "Let's offer more independence for BOSS agents please,
I dont like everytime asking me to do stuff.. it is unnecessary ask, that too for development, it
makes sense for PROD deployment and other fixes that release to PROD. But for local development I
dont need to validate just make the output.. then human can validate it."

ONE POLICY, an org default plus an optional per-project row that may only be STRICTER:

    autonomy:
      dev: act              # act | act_capped | ask   — outside production
      prod: ask             # fixed: production always asks
      unknown_remote: prod  # fixed: a place BOSS cannot classify is production
      dev_topup_usd: {per_item: 1.00, per_day: 10.00}   # spend past a cap without asking
      explicit_stops: honour  # fixed: a stop a PERSON set ("Review the plan first", a tool set
                              # to ask, a blocking stage) is always honoured

No row anywhere means the built-in default above. Production is resolved by
services/production.py; services/autonomy_gate.py applies this policy to every card.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping, Optional

ACT = "act"
ACT_CAPPED = "act_capped"
ASK = "ask"
DEV_MODES = (ACT, ACT_CAPPED, ASK)
#: Stricter is later in this order.
_STRICTNESS = {ACT: 0, ACT_CAPPED: 1, ASK: 2}

DEFAULT_TOPUP = {"per_item": 1.0, "per_day": 10.0}
#: A top-up beyond this is not a top-up; raise the budget instead.
MAX_TOPUP = {"per_item": 50.0, "per_day": 500.0}

ORG = "org"
PROJECT = "project"


class PolicyLooser(ValueError):
    """A project tried to loosen the org's policy, or a fixed field was changed."""


@dataclass(frozen=True)
class Policy:
    dev: str = ACT
    prod: str = ASK
    unknown_remote: str = "prod"
    dev_topup_usd: dict = field(default_factory=lambda: dict(DEFAULT_TOPUP))
    explicit_stops: str = "honour"
    #: Where each value came from: default | org | project.
    source: str = "default"
    set_by: Optional[str] = None
    set_at: Optional[str] = None
    #: The org default this project's row was checked against (None on the org row itself).
    org_dev: Optional[str] = None

    @property
    def dev_acts(self) -> bool:
        return self.dev in (ACT, ACT_CAPPED)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["dev_topup_usd"] = dict(self.dev_topup_usd)
        return out

    def stored(self) -> dict[str, Any]:
        return {"dev": self.dev, "prod": self.prod, "unknown_remote": self.unknown_remote,
                "dev_topup_usd": dict(self.dev_topup_usd),
                "explicit_stops": self.explicit_stops}


DEFAULT = Policy()


def _topup(raw: Any, base: Mapping[str, float]) -> dict[str, float]:
    out = dict(base)
    if isinstance(raw, Mapping):
        for key in ("per_item", "per_day"):
            if raw.get(key) is not None:
                try:
                    out[key] = max(0.0, float(raw[key]))
                except (TypeError, ValueError) as error:
                    raise ValueError(f"dev_topup_usd.{key} must be a number") from error
    return out


def coerce(raw: Optional[Mapping[str, Any]], *, base: Policy = DEFAULT) -> Policy:
    """A policy from stored/posted JSON over `base`. Refuses a fixed field set to anything but
    its fixed value, and an unknown dev mode."""
    raw = dict(raw or {})
    if isinstance(raw.get("autonomy"), Mapping):
        raw = dict(raw["autonomy"])
    dev = str(raw.get("dev", base.dev) or base.dev).strip().lower()
    if dev not in DEV_MODES:
        raise ValueError(f"dev must be one of {', '.join(DEV_MODES)}")
    if str(raw.get("prod", ASK)).strip().lower() != ASK:
        raise PolicyLooser("production always asks: prod can only be 'ask'")
    if str(raw.get("unknown_remote", "prod")).strip().lower() != "prod":
        raise PolicyLooser("a place BOSS cannot classify is production: unknown_remote is 'prod'")
    if str(raw.get("explicit_stops", "honour")).strip().lower() not in ("honour", "honor"):
        raise PolicyLooser("a stop a person set is always honoured: explicit_stops is 'honour'")
    topup = _topup(raw.get("dev_topup_usd"), base.dev_topup_usd)
    for key, cap in MAX_TOPUP.items():
        if topup[key] > cap:
            raise ValueError(f"dev_topup_usd.{key} is at most ${cap:,.0f}; raise the budget instead")
    return replace(base, dev=dev, dev_topup_usd=topup)


def validate_stricter(project: Policy, org: Policy) -> None:
    """A project may only be stricter than its org: dev no looser, top-ups no larger."""
    if _STRICTNESS[project.dev] < _STRICTNESS[org.dev]:
        raise PolicyLooser(f"the org default is dev: {org.dev}; a project may only be stricter")
    for key in ("per_item", "per_day"):
        if float(project.dev_topup_usd.get(key, 0)) > float(org.dev_topup_usd.get(key, 0)):
            raise PolicyLooser(f"the org's top-up {key} is ${org.dev_topup_usd[key]:,.2f}; "
                               "a project may only lower it")


# ---- storage ----------------------------------------------------------------------------------

def _stored(connection: Any, kind: str, scope_id: str) -> Optional[dict]:
    from sqlalchemy import text

    point = connection.begin_nested()
    try:
        row = connection.execute(text(
            "SELECT policy, set_by, set_at FROM autonomy_policies "
            "WHERE scope_kind = :k AND scope_id = :s"), {"k": kind, "s": scope_id}
        ).mappings().first()
    except Exception:  # noqa: BLE001 — an install without the table: the built-in default
        point.rollback()
        return None
    point.commit()
    if not row:
        return None
    policy = row["policy"]
    if isinstance(policy, str):
        try:
            policy = json.loads(policy)
        except ValueError:
            policy = {}
    at = row["set_at"]
    return {"policy": policy or {}, "set_by": row["set_by"],
            "set_at": at.isoformat() if hasattr(at, "isoformat") else at}


def _org_of(connection: Any, project_id: Optional[str]) -> Optional[str]:
    if not project_id:
        return None
    from ..multirepo.common import org_of_project

    point = connection.begin_nested()
    try:
        found = org_of_project(connection, project_id)
    except Exception:  # noqa: BLE001 — no workspace tables: no org row to read
        point.rollback()
        return None
    point.commit()
    return found


def load_org(connection: Any, org_id: Optional[str]) -> Policy:
    found = _stored(connection, ORG, org_id) if org_id else None
    if not found:
        return DEFAULT
    try:
        policy = coerce(found["policy"])
    except ValueError:
        # A row that no longer validates is not permission: the strictest dev mode.
        return replace(DEFAULT, dev=ASK, source="org", set_by=found["set_by"])
    return replace(policy, source="org", set_by=found["set_by"], set_at=found["set_at"])


def load(connection: Any, project_id: Optional[str], *, org_id: Optional[str] = None) -> Policy:
    """The policy in force for a project: its own row over the org's, never looser."""
    org = load_org(connection, org_id or _org_of(connection, project_id))
    found = _stored(connection, PROJECT, project_id) if project_id else None
    if not found:
        return org
    try:
        own = coerce(found["policy"], base=org)
    except ValueError:
        # A row that no longer validates is not permission: the strictest dev mode.
        own = replace(org, dev=ASK)
    # The org may have tightened after the project saved: the stricter of the two holds.
    dev = own.dev if _STRICTNESS[own.dev] >= _STRICTNESS[org.dev] else org.dev
    topup = {k: min(float(own.dev_topup_usd.get(k, 0)), float(org.dev_topup_usd.get(k, 0)))
             for k in ("per_item", "per_day")}
    own = replace(own, dev=dev, dev_topup_usd=topup)
    return replace(own, source="project", set_by=found["set_by"], set_at=found["set_at"],
                   org_dev=org.dev)


def save(connection: Any, *, kind: str, scope_id: str, raw: Mapping[str, Any],
         actor: str, event_project_id: Optional[str] = None) -> Policy:
    """Save the org default (kind 'org') or a project's stricter row (kind 'project')."""
    from sqlalchemy import text

    from .core import emit_event

    if not str(actor or "").strip():
        raise ValueError("an autonomy change requires an actor")
    if kind not in (ORG, PROJECT):
        raise ValueError("kind is 'org' or 'project'")
    if kind == PROJECT:
        org = load_org(connection, _org_of(connection, scope_id))
        policy = coerce(raw, base=org)
        validate_stricter(policy, org)
    else:
        policy = coerce(raw)
    connection.execute(text(
        "INSERT INTO autonomy_policies (scope_kind, scope_id, policy, set_by, set_at) "
        "VALUES (:k, :s, CAST(:p AS JSONB), :a, now()) "
        "ON CONFLICT (scope_kind, scope_id) DO UPDATE SET policy = EXCLUDED.policy, "
        "set_by = EXCLUDED.set_by, set_at = now()"),
        {"k": kind, "s": scope_id, "p": json.dumps(policy.stored()), "a": actor})
    project_for_event = scope_id if kind == PROJECT else event_project_id
    if project_for_event:
        emit_event(connection, project_for_event, "autonomy.policy_set", actor,
                   "autonomy_policy", f"{kind}:{scope_id}", policy.stored())
    return load(connection, scope_id) if kind == PROJECT else load_org(connection, scope_id)


def clear_project(connection: Any, project_id: str, *, actor: str) -> Policy:
    """Drop a project's own row: it follows the org default again (never looser than it)."""
    from sqlalchemy import text

    from .core import emit_event

    connection.execute(text("DELETE FROM autonomy_policies WHERE scope_kind = 'project' "
                            "AND scope_id = :s"), {"s": project_id})
    emit_event(connection, project_id, "autonomy.policy_cleared", actor, "autonomy_policy",
               f"project:{project_id}", {})
    return load(connection, project_id)


def describe(policy: Policy) -> dict[str, Any]:
    """The policy plus the one line the console shows."""
    dev_words = {ACT: "agents act", ACT_CAPPED: "agents act within the cap",
                 ASK: "you decide"}[policy.dev]
    return {**policy.as_dict(),
            "line": f"Development: {dev_words} · Production: you decide",
            "fixed": ["prod", "unknown_remote", "explicit_stops"],
            "modes_available": list(DEV_MODES)}


def dev_acts(connection: Any, project_id: Optional[str]) -> bool:
    """Whether the project's policy acts in development (False when it cannot be read)."""
    try:
        return load(connection, project_id).dev_acts
    except Exception:  # noqa: BLE001
        return False


def tool_threshold(connection: Any, project_id: Optional[str], base: int) -> int:
    """The agent tool-call autonomy ceiling: a tier-2 write acts without asking under dev
    autonomy (its effect stays in the run's sandbox or branch); tier 3 always asks."""
    try:
        policy = load(connection, project_id)
    except Exception:  # noqa: BLE001 — unknown: the install's own threshold
        return base
    if policy.dev_acts:
        return max(int(base), 2)
    return int(base)
