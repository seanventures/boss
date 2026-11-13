"""Declarative skill-access policy: the org's own rules over who may do what with skills (BOSSAPP-890).

WHY. "No one imports skills from outside our GitHub org", "agents never create tier-3 skills",
"only the release agent may hold deploy skills" were each a code change or a habit. They are now
rows: an ordered list of allow/deny rules, written by a person, read at every seam where a skill
is made, brought in, given to an agent or run.

A RULE says: for these ACTIONS (create, edit, import, install, test, remove, hold, invoke — or
`*`), on skills whose key matches SKILL_PATTERN, by a ROLE matching ROLE_PATTERN, done by an
ACTOR_KIND (any | person | agent), from a SOURCE matching SOURCE_PATTERN (imports and installs:
`github:acme/*`, `url:https://…`, `bundle:<publisher>`), for skills at or above MIN_RISK_TIER —
ALLOW or DENY. Patterns are shell globs, case-insensitive.

ORDER: position ascending, then a project's rules before its org's, then id. THE FIRST MATCH
DECIDES. NO MATCH ALLOWS — an install without rules behaves exactly as it did before, and every
seam's other checks (grants, allowed_callers, consent, risk floor, budget) still apply on top: a
rule can refuse what those allow, never allow what they refuse.

THE SEAMS (each calls `check`, which raises SkillPolicyDenied with the rule and its reason):
  create / edit — skill_definitions.save_skill (the one writer) and draft_for_approval
  import        — skill_sources.import_source, before anything is fetched
  install       — skill_packaging.CapabilityBundleService.import_skill
  test          — skill_tests.run_cases
  remove        — skill_definitions.delete_skill
  hold          — skill_definitions.assign_skill_to_role (a person's or an agent's grant; the
                  baseline seed is not a grant anybody chose and is not checked)
  invoke        — SkillRuntimeService.invoke (a denial is recorded like any refused call)

`simulate` answers the same question without counting a hit, for the console's simulator.
"""
from __future__ import annotations

import fnmatch
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id
from .skill_definitions import SkillDefinitionError

ACTIONS = ("create", "edit", "import", "install", "test", "remove", "hold", "invoke")
EFFECTS = ("allow", "deny")
ACTOR_KINDS = ("any", "person", "agent")
STATUSES = ("active", "disabled", "revoked")
SCOPES = ("project", "org")
PATTERN_MAX = 200


class SkillPolicyError(SkillDefinitionError):
    """A rule that cannot be written as given."""


class SkillPolicyDenied(SkillDefinitionError):
    """A rule refused this action. The message names the rule; `rule_id` carries it."""

    status = 403

    def __init__(self, message: str, rule_id: str) -> None:
        super().__init__(message)
        self.rule_id = rule_id


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _iso(value: Any) -> Any:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def public(row: Any) -> dict[str, Any]:
    actions = _loads(row["actions"]) or ["*"]
    return {"id": row["id"], "scope": "project" if row["project_id"] else "org",
            "project_id": row["project_id"], "org_id": row["org_id"],
            "position": int(row["position"]), "effect": row["effect"],
            "actions": [str(a) for a in actions], "skill_pattern": row["skill_pattern"],
            "role_pattern": row["role_pattern"], "actor_kind": row["actor_kind"],
            "source_pattern": row["source_pattern"], "min_risk_tier": row["min_risk_tier"],
            "note": row["note"], "status": row["status"], "hit_count": int(row["hit_count"] or 0),
            "last_hit_at": _iso(row["last_hit_at"]), "created_by": row["created_by"],
            "updated_by": row["updated_by"], "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"])}


def _org_of(connection: Connection, project_id: str) -> Optional[str]:
    return connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()


def list_rules(connection: Connection, project_id: str, *,
               include_revoked: bool = False) -> list[dict[str, Any]]:
    """The project's and its org's rules, in the order they are read."""
    org = _org_of(connection, project_id)
    rows = connection.execute(text(
        "SELECT * FROM skill_policy_rules WHERE (project_id = :p OR (project_id IS NULL "
        "AND org_id = :o)) AND (:all OR status <> 'revoked') "
        "ORDER BY position, (project_id IS NULL), id"),
        {"p": project_id, "o": org, "all": bool(include_revoked)}).mappings().all()
    return [public(r) for r in rows]


def _active_rules(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Active rules, or [] when the table is not there (an older schema, a SQLite stub): no rules
    is the default, never an error at a seam."""
    try:
        with connection.begin_nested():
            return [r for r in list_rules(connection, project_id) if r["status"] == "active"]
    except Exception:  # noqa: BLE001 — a policy store that cannot answer adds no rule
        return []


def _glob(pattern: str, value: Optional[str]) -> bool:
    pattern = (pattern or "*").strip().lower()
    if pattern == "*":
        return True
    if value is None or value == "":
        return False
    return fnmatch.fnmatchcase(str(value).strip().lower(), pattern)


def matches(rule: dict[str, Any], *, action: str, skill_key: str, role_key: Optional[str],
            actor_kind: str, source: Optional[str], risk_tier: Optional[int]) -> bool:
    actions = rule.get("actions") or ["*"]
    if "*" not in actions and action not in actions:
        return False
    if not _glob(rule.get("skill_pattern") or "*", skill_key):
        return False
    if not _glob(rule.get("role_pattern") or "*", role_key):
        return False
    kind = rule.get("actor_kind") or "any"
    if kind != "any" and kind != actor_kind:
        return False
    if not _glob(rule.get("source_pattern") or "*", source):
        return False
    floor = rule.get("min_risk_tier")
    if floor is not None and (risk_tier is None or int(risk_tier) < int(floor)):
        return False
    return True


def _explain(rule: Optional[dict[str, Any]], action: str, skill_key: str) -> str:
    if rule is None:
        return f"No rule matches {action} on {skill_key!r}: allowed (no rule is the default)."
    said = f"{rule['effect'].upper()}: rule {rule['id']} (#{rule['position']}, {rule['scope']}) matched first"
    return said + (f" — {rule['note']}" if rule.get("note") else "") + "."


def evaluate(connection: Connection, project_id: str, *, action: str, skill_key: str,
             role_key: Optional[str] = None, actor_kind: Optional[str] = "person",
             source: Optional[str] = None, risk_tier: Optional[int] = None,
             count_hit: bool = False, actor: Optional[str] = None) -> dict[str, Any]:
    """{decision, rule_id, explanation} for one action. First match decides; none allows.

    `actor_kind=None` derives the kind (and, when `role_key` is None, the role) from `actor` —
    only once a rule exists, so a project without rules pays one read and nothing else."""
    if action not in ACTIONS:
        raise SkillPolicyError(f"action must be one of: {', '.join(ACTIONS)}")
    rules = _active_rules(connection, project_id)
    if not rules:
        return {"decision": "allow", "rule_id": None,
                "explanation": _explain(None, action, str(skill_key or "")), "matched_rule": None}
    if actor_kind is None:
        try:
            with connection.begin_nested():
                derived_kind, derived_role = actor_role(connection, project_id, actor)
        except Exception:  # noqa: BLE001 — unknown is a person, as everywhere else
            derived_kind, derived_role = "person", None
        actor_kind = derived_kind
        role_key = role_key if role_key is not None else derived_role
    kind = actor_kind if actor_kind in ("person", "agent") else "person"
    hit = next((r for r in rules if matches(
        r, action=action, skill_key=str(skill_key or ""), role_key=role_key, actor_kind=kind,
        source=source, risk_tier=risk_tier)), None)
    if hit is not None and count_hit:
        try:
            with connection.begin_nested():
                connection.execute(text(
                    "UPDATE skill_policy_rules SET hit_count = hit_count + 1, last_hit_at = now() "
                    "WHERE id = :i"), {"i": hit["id"]})
        except Exception:  # noqa: BLE001 — a counter never fails the action it counts
            pass
    return {"decision": hit["effect"] if hit else "allow", "rule_id": hit["id"] if hit else None,
            "explanation": _explain(hit, action, str(skill_key or "")),
            "matched_rule": hit}


def check(connection: Connection, project_id: str, *, action: str, skill_key: str,
          role_key: Optional[str] = None, actor_kind: Optional[str] = None,
          source: Optional[str] = None, risk_tier: Optional[int] = None,
          actor: str = "") -> None:
    """Raise SkillPolicyDenied when the first matching rule denies; otherwise return.
    `actor_kind` None: derived from `actor` (an agent's role, or a person)."""
    out = evaluate(connection, project_id, action=action, skill_key=skill_key,
                   role_key=role_key, actor_kind=actor_kind, source=source,
                   risk_tier=risk_tier, count_hit=True, actor=actor)
    if out["decision"] != "deny":
        return
    rule = out["matched_rule"]
    try:
        with connection.begin_nested():
            emit_event(connection, project_id, "skill.policy_denied", actor or "system",
                       "skill_policy_rule", rule["id"],
                       {"action": action, "skill_key": skill_key, "role_key": role_key,
                        "actor_kind": actor_kind, "source": source})
    except Exception:  # noqa: BLE001 — the refusal stands whether or not it was logged
        pass
    who = f" for {role_key}" if role_key else ""
    raise SkillPolicyDenied(
        f"The skill-access policy does not allow {action} on {skill_key!r}{who}: rule "
        f"{rule['id']} (#{rule['position']}, {rule['scope']})"
        + (f" — {rule['note']}" if rule.get("note") else "")
        + ". A person can change the rule in Skills & MCP › Rules.", rule["id"])


def actor_role(connection: Connection, project_id: str, actor: Optional[str]) -> tuple[str, Optional[str]]:
    """(actor_kind, role_key) for an actor: an agent's role, or a person with no role."""
    from .skill_definitions import is_agent_actor

    value = str(actor or "").strip()
    if is_agent_actor(connection, project_id, value):
        return "agent", value.removeprefix("agent:").lower()
    return "person", None


# ── writing rules ────────────────────────────────────────────────────────────────────────────

def _clean_pattern(value: Any, field: str) -> str:
    said = str(value if value is not None else "*").strip() or "*"
    if len(said) > PATTERN_MAX:
        raise SkillPolicyError(f"{field} is longer than {PATTERN_MAX} characters")
    return said


def _clean(fields: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "effect" in fields or not partial:
        effect = str(fields.get("effect") or "").strip().lower()
        if effect not in EFFECTS:
            raise SkillPolicyError("effect must be allow or deny")
        out["effect"] = effect
    if "actions" in fields or not partial:
        raw = fields.get("actions") or ["*"]
        if isinstance(raw, str):
            raw = [raw]
        actions = sorted({str(a).strip().lower() for a in raw if str(a).strip()})
        bad = [a for a in actions if a != "*" and a not in ACTIONS]
        if bad or not actions:
            raise SkillPolicyError(f"actions must be * or some of: {', '.join(ACTIONS)}")
        out["actions"] = ["*"] if "*" in actions else actions
    for field in ("skill_pattern", "role_pattern", "source_pattern"):
        if field in fields or not partial:
            out[field] = _clean_pattern(fields.get(field), field)
    if "actor_kind" in fields or not partial:
        kind = str(fields.get("actor_kind") or "any").strip().lower()
        if kind not in ACTOR_KINDS:
            raise SkillPolicyError(f"actor_kind must be one of: {', '.join(ACTOR_KINDS)}")
        out["actor_kind"] = kind
    if "min_risk_tier" in fields or not partial:
        tier = fields.get("min_risk_tier")
        if tier not in (None, ""):
            try:
                tier = int(tier)
            except (TypeError, ValueError):
                raise SkillPolicyError("min_risk_tier must be 0-3") from None
            if not 0 <= tier <= 3:
                raise SkillPolicyError("min_risk_tier must be 0-3")
        out["min_risk_tier"] = None if tier in (None, "") else tier
    if "position" in fields or not partial:
        try:
            out["position"] = int(fields.get("position", 100))
        except (TypeError, ValueError):
            raise SkillPolicyError("position must be a whole number") from None
    if "note" in fields or not partial:
        note = str(fields.get("note") or "").strip()
        out["note"] = note[:2000] or None
    if "status" in fields:
        status = str(fields.get("status") or "").strip().lower()
        if status not in ("active", "disabled"):
            raise SkillPolicyError("status must be active or disabled (revoke to end a rule)")
        out["status"] = status
    return out


def create_rule(connection: Connection, project_id: str, fields: dict[str, Any], *,
                actor: str) -> dict[str, Any]:
    scope = str(fields.get("scope") or "project").strip().lower()
    if scope not in SCOPES:
        raise SkillPolicyError("scope must be project or org")
    clean = _clean(fields, partial=False)
    org = _org_of(connection, project_id)
    if scope == "org" and not org:
        raise SkillPolicyError("this project belongs to no organization; write a project rule")
    row = connection.execute(text(
        "INSERT INTO skill_policy_rules (id, org_id, project_id, position, effect, actions, "
        "skill_pattern, role_pattern, actor_kind, source_pattern, min_risk_tier, note, "
        "created_by) VALUES (:i, :o, :p, :pos, :e, CAST(:a AS JSONB), :sp, :rp, :ak, :src, "
        ":t, :n, :by) RETURNING *"),
        {"i": make_id("skp"), "o": org, "p": project_id if scope == "project" else None,
         "pos": clean["position"], "e": clean["effect"], "a": json.dumps(clean["actions"]),
         "sp": clean["skill_pattern"], "rp": clean["role_pattern"], "ak": clean["actor_kind"],
         "src": clean["source_pattern"], "t": clean["min_risk_tier"], "n": clean["note"],
         "by": actor}).mappings().one()
    rule = public(row)
    emit_event(connection, project_id, "skill.policy_rule_created", actor, "skill_policy_rule",
               rule["id"], {k: rule[k] for k in ("scope", "position", "effect", "actions",
                                                  "skill_pattern", "role_pattern", "actor_kind",
                                                  "source_pattern", "min_risk_tier")})
    return rule


def _owned(connection: Connection, project_id: str, rule_id: str) -> dict[str, Any]:
    rule = next((r for r in list_rules(connection, project_id, include_revoked=True)
                 if r["id"] == rule_id), None)
    if rule is None:
        raise LookupError(f"no skill-policy rule {rule_id!r} for this project or its org")
    return rule


def update_rule(connection: Connection, project_id: str, rule_id: str, fields: dict[str, Any],
                *, actor: str) -> dict[str, Any]:
    rule = _owned(connection, project_id, rule_id)
    if rule["status"] == "revoked":
        raise SkillPolicyError("a revoked rule is kept for the audit and cannot change")
    clean = _clean(fields, partial=True)
    if not clean:
        return rule
    sets = ", ".join(f"{k} = CAST(:{k} AS JSONB)" if k == "actions" else f"{k} = :{k}"
                     for k in clean)
    params = {**clean, "actions": json.dumps(clean["actions"])} if "actions" in clean else dict(clean)
    row = connection.execute(text(
        f"UPDATE skill_policy_rules SET {sets}, updated_by = :by, updated_at = now() "
        "WHERE id = :i RETURNING *"), {**params, "by": actor, "i": rule_id}).mappings().one()
    out = public(row)
    emit_event(connection, project_id, "skill.policy_rule_changed", actor, "skill_policy_rule",
               rule_id, {"changed": sorted(clean)})
    return out


def revoke_rule(connection: Connection, project_id: str, rule_id: str, *,
                actor: str) -> dict[str, Any]:
    _owned(connection, project_id, rule_id)
    row = connection.execute(text(
        "UPDATE skill_policy_rules SET status = 'revoked', updated_by = :by, updated_at = now() "
        "WHERE id = :i RETURNING *"), {"by": actor, "i": rule_id}).mappings().one()
    emit_event(connection, project_id, "skill.policy_rule_revoked", actor, "skill_policy_rule",
               rule_id, {})
    return public(row)


def simulate(connection: Connection, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """What would happen, decided as a real check would be. Counts nothing, records nothing."""
    action = str(body.get("action") or "").strip().lower()
    skill_key = str(body.get("skill_key") or "").strip()
    if not skill_key:
        raise SkillPolicyError("name the skill to simulate")
    tier = body.get("risk_tier")
    out = evaluate(connection, project_id, action=action, skill_key=skill_key,
                   role_key=(str(body.get("role_key") or "").strip().lower() or None),
                   actor_kind=str(body.get("actor_kind") or "person"),
                   source=(str(body.get("source") or "").strip() or None),
                   risk_tier=None if tier in (None, "") else int(tier))
    return {"decision": out["decision"], "rule_id": out["rule_id"],
            "explanation": out["explanation"], "matched_rule": out["matched_rule"]}
