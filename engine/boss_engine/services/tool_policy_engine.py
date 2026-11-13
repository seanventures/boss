"""THE ORDERED TOOL-POLICY ENGINE (BOSSAPP-815) AND "ALWAYS ALLOW" FROM AN APPROVAL (BOSSAPP-817).

WHY THIS EXISTS
---------------
Before this, a tool's fate was a per-server Allow / Ask / Off map (mcp_registry.tool_policy)
plus tier floors, read separately by the MCP gateway's ladder (mcp_gateway.tool_policy_verdict),
the receipt door (mcp_registry.call_tool_with_receipt) and nothing at all on the kernel's own
tool set. An operator could not say "this agent may never call delete_* on any server", "ask
whenever the path starts with /prod", or "at most 20 searches an hour", and an approval could not
become a standing permission.

WHAT IT IS
----------
Rules are DATA, rows in ``tool_policies_v2``:

  scope      org | project | agent:<role_key> | workflow:<workflow_id>
  priority   lower runs first (10 before 100)
  effect     allow | ask | deny | rate_limit
  selector   {"server": <glob>, "tool": <glob>}  (fnmatch, case-sensitive; '*' when absent)
  conditions [{"path": "$.a.b[0]", "op": "equals|prefix|regex", "value": ...}, ...]  — ALL hold
  rate_limit {"max_calls": n, "window_seconds": s}  (effect rate_limit only)
  source     operator | decision (an "Always allow" from an approval, BOSSAPP-817)
  schema_sha256  a pin on the tool's input schema hash (BOSSAPP-818); a pinned rule never
             applies to a changed tool, and a decision rule is revoked when it changes
  expires_at, hit_count, last_hit_at, status (active | disabled | revoked)

ONE function decides: :func:`decide` ``(ctx) -> {decision, matched_policy_ids, explanation, ...}``
with ``decision`` in allow | ask | deny. Rules are walked in order (priority, then the more
specific scope, then the stricter effect, then age); the first terminal match decides. A
rate_limit rule is terminal only when its window is spent (then deny); otherwise it is noted and
the walk goes on. No match: the registry's own Allow / Ask / Off stands.

THE CEILINGS NO RULE CAN LIFT
-----------------------------
  * Off stays Off: a tool turned off in Skills & MCP is denied before any rule is read.
  * The TIER FLOOR (mcp_registry.policy_floor): a tier >= 2 tool can never come out allow.
  * QUARANTINE (BOSSAPP-818): a tool whose definition changed comes out ask, never allow.
  * An UNREVIEWED server's tool comes out ask, never allow (mcp_registry.needs_person).
A rule may always make a call STRICTER (ask, deny, rate limit), never looser than these.

WHERE IT IS ASKED (the three plug points, which must agree)
-----------------------------------------------------------
  (a) mcp_gateway.tool_policy_verdict — the build agent's proxied call;
  (b) mcp_registry.call_tool_with_receipt — the one effect every road to an MCP tool ends in;
      the audit row carries the real matched ids (and, when a rule or a ceiling decided, the
      explanation under ``screen.policy``);
  (c) kernel.toolset.RegistryToolSet.invoke — BOSS's own tools (server ``boss``); only a
      MATCHED rule changes anything there (deny refuses, ask holds for a person).

THE CONDITION LANGUAGE IS SMALL AND TOTAL
-----------------------------------------
A path is ``$`` followed by ``.key`` / ``[index]`` steps (or a bare ``a.b.0``). Three operators:
``equals`` (JSON equality), ``prefix`` and ``regex`` (``re.search``; strings and numbers only).
Every condition evaluates to True or False and never raises: a missing path, a wrong type or a
bad pattern is False — so a malformed condition can only make a rule NOT match, which for an
allow rule means the call is NOT loosened.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

log = logging.getLogger(__name__)

EFFECTS = ("allow", "ask", "deny", "rate_limit")
SOURCES = ("operator", "decision")
STATUSES = ("active", "disabled", "revoked")
OPS = ("equals", "prefix", "regex")
#: The server name BOSS's own tools (the kernel's RegistryToolSet) are decided under.
BOSS_SERVER = "boss"
MAX_CONDITIONS = 8
MAX_PATTERN = 200
MAX_PATH_STEPS = 12
#: Expiry choices the "Always allow" option offers (BOSSAPP-817); None = until revoked.
EXPIRY_CHOICES = {"1h": timedelta(hours=1), "1d": timedelta(days=1), "7d": timedelta(days=7),
                  "30d": timedelta(days=30), "never": None}

_SCOPE_RANK = {"workflow": 0, "agent": 1, "project": 2, "org": 3}
_EFFECT_RANK = {"deny": 0, "rate_limit": 1, "ask": 2, "allow": 3}

SCHEMA_SQL = """
-- BOSSAPP-815-817
CREATE TABLE IF NOT EXISTS tool_policies_v2 (
  id TEXT PRIMARY KEY,
  org_id TEXT,
  project_id TEXT,
  scope TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  effect TEXT NOT NULL,
  selector JSONB NOT NULL DEFAULT '{}'::jsonb,
  conditions JSONB NOT NULL DEFAULT '[]'::jsonb,
  rate_limit JSONB,
  source TEXT NOT NULL DEFAULT 'operator',
  source_decision_id TEXT,
  schema_sha256 TEXT,
  note TEXT,
  expires_at TIMESTAMPTZ,
  hit_count BIGINT NOT NULL DEFAULT 0,
  last_hit_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  revoked_by TEXT,
  revoked_reason TEXT,
  CONSTRAINT tool_policies_v2_effect CHECK (effect IN ('allow', 'ask', 'deny', 'rate_limit')),
  CONSTRAINT tool_policies_v2_source CHECK (source IN ('operator', 'decision')),
  CONSTRAINT tool_policies_v2_status CHECK (status IN ('active', 'disabled', 'revoked')),
  CONSTRAINT tool_policies_v2_scope CHECK (
    scope IN ('org', 'project') OR scope LIKE 'agent:_%' OR scope LIKE 'workflow:_%'),
  CONSTRAINT tool_policies_v2_owner CHECK (scope = 'org' OR project_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_tool_policies_v2_project
  ON tool_policies_v2 (project_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_tool_policies_v2_org
  ON tool_policies_v2 (org_id, status) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_tool_policies_v2_decision
  ON tool_policies_v2 (source_decision_id) WHERE source_decision_id IS NOT NULL;
"""


class ToolPolicyError(ValueError):
    """A rule that cannot be stored as written. The API answers 422 with the sentence."""


class ToolPolicyNotFound(LookupError):
    pass


# ── the context ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PolicyContext:
    """Everything one decision reads. Built by :func:`context_for` from a registry server, or
    by hand (the kernel, the simulate box)."""

    server: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    tier: int = 0
    project: Optional[str] = None
    org: Optional[str] = None
    run: Optional[str] = None
    role: Optional[str] = None
    workflow: Optional[str] = None
    server_id: Optional[str] = None
    #: The registry's own Allow / Ask / Off for this tool (floor and quarantine already in it).
    base: str = "allow"
    quarantined: bool = False
    unreviewed: bool = False
    #: The tool's current input-schema hash (BOSSAPP-818), for pinned rules.
    schema_sha256: Optional[str] = None


def context_for(server: Mapping[str, Any], tool: str, arguments: Any = None, *,
                project_id: Optional[str] = None, org_id: Optional[str] = None,
                run_id: Optional[str] = None, role_key: Optional[str] = None,
                workflow_id: Optional[str] = None) -> PolicyContext:
    """The context of one call to a REGISTRY server's tool: tier, base policy, quarantine and
    review read through mcp_registry's own statements, so nothing here re-derives them."""
    from .mcp_registry import (_declared_tier, needs_person, tool_policy, tool_quarantined,
                               tool_tier)

    srv = dict(server or {})
    declared = _declared_tier(srv, tool)
    entry = next((t for t in srv.get("tools") or []
                  if isinstance(t, dict) and t.get("name") == tool), {})
    return PolicyContext(
        server=str(srv.get("name") or srv.get("id") or ""), tool=str(tool or ""),
        arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
        tier=int(tool_tier(srv, tool, declared)),
        project=project_id or (str(srv.get("project_id")) if srv.get("project_id") else None),
        org=org_id, run=run_id or None, role=role_key or None, workflow=workflow_id or None,
        server_id=str(srv.get("id")) if srv.get("id") else None,
        base=tool_policy(srv, tool, declared),
        quarantined=tool_quarantined(srv, tool), unreviewed=needs_person(srv),
        schema_sha256=(str(entry.get("schema_sha256")) if entry.get("schema_sha256") else None))


# ── the condition language ───────────────────────────────────────────────────────────────────

_STEP = re.compile(r"\.([A-Za-z0-9_\-]+)|\[(\d+)\]|\[\"([^\"]*)\"\]")


def parse_path(path: Any) -> Optional[list]:
    """``$.a.b[0]`` / ``$["a b"]`` / ``a.b.0`` → ['a', 'b', 0]; None when it is not a path."""
    if not isinstance(path, str):
        return None
    p = path.strip()
    if not p:
        return None
    if not p.startswith("$"):
        steps: list = [int(s) if s.isdigit() else s for s in p.split(".")]
        return steps if all(s != "" for s in steps) and len(steps) <= MAX_PATH_STEPS else None
    rest, steps, at = p[1:], [], 0
    while at < len(rest):
        m = _STEP.match(rest, at)
        if m is None:
            return None
        key, idx, quoted = m.groups()
        steps.append(int(idx) if idx is not None else (quoted if quoted is not None else key))
        at = m.end()
    return steps if 0 < len(steps) <= MAX_PATH_STEPS else None


_MISSING = object()


def _at(value: Any, steps: Sequence) -> Any:
    for step in steps:
        if isinstance(value, list) and (isinstance(step, int) or str(step).isdigit()):
            step = int(step)
            if not 0 <= step < len(value):
                return _MISSING
            value = value[step]
        elif isinstance(value, Mapping):
            key = str(step)
            if key not in value:
                return _MISSING
            value = value[key]
        else:
            return _MISSING
    return value


def _scalar_text(value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def condition_holds(condition: Mapping[str, Any], arguments: Mapping[str, Any]) -> bool:
    """TOTAL: True or False for any input, never an exception."""
    try:
        steps = parse_path(condition.get("path"))
        op = condition.get("op")
        want = condition.get("value")
        if steps is None or op not in OPS:
            return False
        got = _at(arguments, steps)
        if got is _MISSING:
            return False
        if op == "equals":
            return json.dumps(got, sort_keys=True, default=str) == json.dumps(
                want, sort_keys=True, default=str)
        said = _scalar_text(got)
        if said is None or not isinstance(want, str):
            return False
        if op == "prefix":
            return said.startswith(want)
        if len(want) > MAX_PATTERN:
            return False
        return re.search(want, said) is not None
    except Exception:  # noqa: BLE001 — total by construction: anything odd does not match
        return False


def _scope_matches(scope: str, ctx: PolicyContext) -> bool:
    if scope in ("org", "project"):
        return True  # ownership (org / project) is filtered when the rules are loaded
    kind, _, value = scope.partition(":")
    if kind == "agent":
        return bool(ctx.role) and value == ctx.role
    if kind == "workflow":
        return bool(ctx.workflow) and value == ctx.workflow
    return False


def _glob(pattern: Any, *values: Optional[str]) -> bool:
    pat = str(pattern) if isinstance(pattern, str) and pattern else "*"
    return any(v is not None and fnmatch.fnmatchcase(v, pat) for v in values)


def rule_matches(rule: Mapping[str, Any], ctx: PolicyContext) -> bool:
    """Scope, selector and every condition — not expiry, pins or status (the walk's job)."""
    if not _scope_matches(str(rule.get("scope") or ""), ctx):
        return False
    selector = rule.get("selector") or {}
    if not _glob(selector.get("server"), ctx.server, ctx.server_id):
        return False
    if not _glob(selector.get("tool"), ctx.tool):
        return False
    return all(condition_holds(c, ctx.arguments) for c in (rule.get("conditions") or [])
               if isinstance(c, Mapping))


def _rank(rule: Mapping[str, Any]) -> tuple:
    kind = str(rule.get("scope") or "").partition(":")[0]
    created = rule.get("created_at")
    return (int(rule.get("priority") or 0), _SCOPE_RANK.get(kind, 9),
            _EFFECT_RANK.get(str(rule.get("effect")), 9),
            str(created.isoformat() if hasattr(created, "isoformat") else created or ""),
            str(rule.get("id") or ""))


def order_rules(rules: Sequence[Mapping[str, Any]]) -> list:
    """The order the walk reads: priority, then the more specific scope, then the stricter
    effect, then the older rule, then the id (total, so two runs never disagree)."""
    return sorted(rules, key=_rank)


def _expired(rule: Mapping[str, Any], now: datetime) -> bool:
    when = rule.get("expires_at")
    if not when:
        return False
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return True  # an unreadable expiry is treated as passed: it can only loosen
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= now


def _pin_stale(rule: Mapping[str, Any], ctx: PolicyContext) -> bool:
    pin = rule.get("schema_sha256")
    return bool(pin) and pin != ctx.schema_sha256


def _describe(rule: Mapping[str, Any]) -> str:
    sel = rule.get("selector") or {}
    what = f"{sel.get('tool') or '*'} on {sel.get('server') or '*'}"
    conds = [f"{c.get('path')} {c.get('op')} {json.dumps(c.get('value'))}"
             for c in rule.get("conditions") or [] if isinstance(c, Mapping)]
    tail = f" when {' and '.join(conds)}" if conds else ""
    who = "an approval's Always allow" if rule.get("source") == "decision" else "rule"
    return (f"{who} {rule.get('id')} (priority {rule.get('priority')}, {rule.get('scope')}, "
            f"{rule.get('effect')} {what}{tail})")


# ── the one decision ─────────────────────────────────────────────────────────────────────────

def decide(ctx: PolicyContext, *, rules: Optional[Sequence[Mapping[str, Any]]] = None,
           connection: Any = None, record: bool = False,
           now: Optional[datetime] = None) -> dict:
    """THE ONE DECISION: ``{decision, matched_policy_ids, explanation, rule_id, base, stale}``.

    ``decision`` is allow | ask | deny. ``matched_policy_ids`` lists, in order, every rule that
    matched (a rate limit with room left included), then the deciding entry — a rule id, or
    ``base:<allow|ask>`` when no rule decided — then any ceiling that lowered it
    (``guard:off``, ``guard:tier_floor``, ``guard:quarantine``, ``guard:unreviewed``).
    ``rule_id`` is the deciding rule, None when the registry's own policy stood.

    ``rules`` default to the active rules :func:`load_rules` reads with ``connection`` (none
    and no connection: no rules). ``record`` (the plug points, never the simulate box) counts
    the deciding rule's hit, emits mcp.always_allow_used for a decision rule, and revokes a
    decision rule whose pinned schema no longer matches the tool (mcp.always_allow_revoked).
    """
    now = now or datetime.now(timezone.utc)
    base = ctx.base if ctx.base in ("allow", "ask", "off") else "ask"
    if base == "off":
        return {"decision": "deny", "matched_policy_ids": ["guard:off"], "rule_id": None,
                "base": base, "stale": [], "reason": "off",
                "explanation": (f"DENY: {ctx.tool} is turned Off for '{ctx.server}' in Skills & MCP; "
                                "no rule can turn it back on.")}
    if rules is None:
        rules = load_rules(connection, ctx.project, ctx.org) if connection is not None else []
    ids: list[str] = []
    notes: list[str] = []
    stale: list[str] = []
    chosen: Optional[Mapping[str, Any]] = None
    limited = False
    for rule in order_rules(rules):
        if str(rule.get("status") or "active") != "active" or _expired(rule, now):
            continue
        if not rule_matches(rule, ctx):
            continue
        if _pin_stale(rule, ctx):
            stale.append(str(rule.get("id")))
            continue
        if rule.get("effect") == "rate_limit":
            spent, used, cap = _rate_spent(connection, rule, ctx)
            if spent:
                chosen, limited = rule, True
                break
            ids.append(str(rule.get("id")))
            notes.append(f"{_describe(rule)} has room ({used} of {cap} used)")
            continue
        chosen = rule
        break
    if chosen is None:
        decision = base
        ids.append(f"base:{base}")
        notes.append(f"no rule decided, so the tool's own setting in Skills & MCP stands "
                     f"({base})")
        reason = base
    else:
        effect = str(chosen.get("effect"))
        decision = "deny" if effect in ("deny", "rate_limit") else effect
        ids.append(str(chosen.get("id")))
        if limited:
            cap = (chosen.get("rate_limit") or {})
            notes.append(f"{_describe(chosen)} is spent: {cap.get('max_calls')} calls per "
                         f"{cap.get('window_seconds')}s")
            reason = "rate_limited"
        else:
            notes.append(f"{_describe(chosen)} matched first")
            reason = "policy" if decision != "allow" else "allow"
    # THE CEILINGS: a rule may only make a call stricter than these. The registry's own policy
    # (``base``) already carries the tier floor and the quarantine (mcp_registry.tool_policy), so
    # those two are re-applied to a RULE's allow; the review rule applies to every allow.
    if decision == "allow":
        from .mcp_registry import policy_floor

        if chosen is not None and policy_floor(ctx.tier) == "ask":
            decision, reason = "ask", "ask"
            ids.append("guard:tier_floor")
            notes.append(f"a tier {ctx.tier} tool changes the world, so it always asks a person")
        elif chosen is not None and ctx.quarantined:
            decision, reason = "ask", "ask"
            ids.append("guard:quarantine")
            notes.append("its definition changed and waits for review, so it asks a person")
        elif ctx.unreviewed:
            decision = "ask"
            ids.append("guard:unreviewed")
            notes.append("the server is not reviewed yet, so it asks a person")
    if decision == "ask" and ctx.unreviewed:
        reason = "unreviewed"
    if stale:
        notes.append(f"pinned to an older definition of the tool, so not applied: "
                     f"{', '.join(stale)}")
    result = {"decision": decision, "matched_policy_ids": ids,
              "rule_id": str(chosen.get("id")) if chosen is not None else None,
              "base": base, "stale": stale, "reason": reason,
              "explanation": f"{decision.upper()}: " + "; ".join(notes) + "."}
    if record and connection is not None:
        _record(connection, ctx, chosen, stale, rules)
    return result


def _rate_spent(connection: Any, rule: Mapping[str, Any], ctx: PolicyContext
                ) -> tuple[bool, int, int]:
    """Whether the rule's window is spent, counted from the tool-call audit (calls that ran in
    this project, this server and tool, within the window). No connection: never spent."""
    limit = rule.get("rate_limit") or {}
    try:
        cap = int(limit.get("max_calls"))
        window = int(limit.get("window_seconds"))
    except (TypeError, ValueError):
        return False, 0, 0
    if connection is None or not ctx.project:
        return False, 0, cap
    try:
        with connection.begin_nested():
            used = int(connection.execute(text(
                "SELECT count(*) FROM tool_call_audit WHERE project_id = :p AND tool = :t "
                "AND (CAST(:sid AS TEXT) IS NULL OR server_id = :sid) "
                "AND outcome IN ('succeeded', 'deduplicated', 'failed') "
                "AND recorded_at > now() - make_interval(secs => CAST(:w AS DOUBLE PRECISION))"),
                {"p": ctx.project, "t": ctx.tool, "sid": ctx.server_id, "w": window},
            ).scalar() or 0)
    except Exception:  # noqa: BLE001 — a count that cannot be read spends the window
        log.warning("tool policy: rate count unreadable for %s", rule.get("id"), exc_info=True)
        return True, cap, cap
    return used >= cap, used, cap


def _record(connection: Any, ctx: PolicyContext, chosen: Optional[Mapping[str, Any]],
            stale: list, rules: Sequence[Mapping[str, Any]]) -> None:
    """Hit counts and the always-allow lifecycle. Never fails the call it is about."""
    from .core import emit_event

    by_id = {str(r.get("id")): r for r in rules}
    try:
        with connection.begin_nested():
            for rid in stale:
                rule = by_id.get(rid) or {}
                if rule.get("source") == "decision":
                    revoke_rule(connection, rule.get("project_id") or ctx.project or "", rid,
                                actor="boss", reason="schema_changed")
            if chosen is not None:
                connection.execute(text(
                    "UPDATE tool_policies_v2 SET hit_count = hit_count + 1, last_hit_at = now() "
                    "WHERE id = :id"), {"id": chosen.get("id")})
                if chosen.get("source") == "decision" and ctx.project:
                    emit_event(connection, ctx.project, "mcp.always_allow_used", "boss",
                               "tool_policy", str(chosen.get("id")),
                               {"tool": ctx.tool, "server": ctx.server, "run_id": ctx.run,
                                "role_key": ctx.role})
    except Exception:  # noqa: BLE001 — the count is bookkeeping; the decision already stands
        log.warning("tool policy: hit not recorded", exc_info=True)


# ── storage ──────────────────────────────────────────────────────────────────────────────────

_COLUMNS = ("id, org_id, project_id, scope, priority, effect, selector, conditions, rate_limit, "
            "source, source_decision_id, schema_sha256, note, expires_at, hit_count, "
            "last_hit_at, status, created_by, created_at, updated_at, revoked_at, revoked_by, "
            "revoked_reason")


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _row(row: Mapping[str, Any]) -> dict:
    out = dict(row)
    out["selector"] = _loads(out.get("selector"), {}) or {}
    out["conditions"] = _loads(out.get("conditions"), []) or []
    out["rate_limit"] = _loads(out.get("rate_limit"), None)
    return out


def public(rule: Mapping[str, Any]) -> dict:
    """A rule as the API answers it (timestamps as ISO text)."""
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in rule.items()}


def org_of(connection: Any, project_id: Optional[str]) -> Optional[str]:
    if not project_id:
        return None
    try:
        with connection.begin_nested():
            found = connection.execute(text(
                "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE p.id = :p"), {"p": project_id}).scalar()
    except Exception:  # noqa: BLE001 — a project with no org reads only its own rules
        return None
    return str(found) if found else None


def load_rules(connection: Any, project_id: Optional[str], org_id: Optional[str] = None
               ) -> list[dict]:
    """The ACTIVE, unexpired rules a call in this project is decided by: the project's own and
    its org's. A missing table is not swallowed: a rule an operator wrote must never be skipped
    silently."""
    org = org_id or org_of(connection, project_id)
    rows = connection.execute(text(
        f"SELECT {_COLUMNS} FROM tool_policies_v2 WHERE status = 'active' "
        "AND (expires_at IS NULL OR expires_at > now()) "
        "AND (project_id = :p OR (project_id IS NULL AND scope = 'org' "
        "     AND (org_id IS NULL OR org_id = :o)))"),
        {"p": project_id, "o": org}).mappings().all()
    return [_row(r) for r in rows]


def list_rules(connection: Any, project_id: str, *, source: Optional[str] = None,
               include_revoked: bool = False) -> list[dict]:
    """The console's table: this project's rules and its org's, in the order they are read."""
    org = org_of(connection, project_id)
    rows = connection.execute(text(
        f"SELECT {_COLUMNS} FROM tool_policies_v2 WHERE "
        "(project_id = :p OR (project_id IS NULL AND scope = 'org' "
        " AND (org_id IS NULL OR org_id = :o))) "
        "AND (CAST(:s AS TEXT) IS NULL OR source = :s) "
        "AND (:rev OR status <> 'revoked')"),
        {"p": project_id, "o": org, "s": source, "rev": bool(include_revoked)},
    ).mappings().all()
    return order_rules([_row(r) for r in rows])


def get_rule(connection: Any, project_id: str, rule_id: str) -> dict:
    org = org_of(connection, project_id)
    row = connection.execute(text(
        f"SELECT {_COLUMNS} FROM tool_policies_v2 WHERE id = :id AND "
        "(project_id = :p OR (project_id IS NULL AND scope = 'org' "
        " AND (org_id IS NULL OR org_id = :o)))"),
        {"id": rule_id, "p": project_id, "o": org}).mappings().first()
    if row is None:
        raise ToolPolicyNotFound(f"no tool policy {rule_id!r} in this project.")
    return _row(row)


def _validate_conditions(conditions: Any) -> list[dict]:
    if conditions in (None, ""):
        return []
    if not isinstance(conditions, list):
        raise ToolPolicyError("conditions must be a list of {path, op, value}.")
    if len(conditions) > MAX_CONDITIONS:
        raise ToolPolicyError(f"at most {MAX_CONDITIONS} conditions per rule.")
    out = []
    for c in conditions:
        if not isinstance(c, Mapping):
            raise ToolPolicyError("each condition is {path, op, value}.")
        if parse_path(c.get("path")) is None:
            raise ToolPolicyError(f"{c.get('path')!r} is not a path like $.args.path or "
                                  "$.items[0].name.")
        op = c.get("op")
        if op not in OPS:
            raise ToolPolicyError("op must be equals, prefix or regex.")
        value = c.get("value")
        if op in ("prefix", "regex"):
            if not isinstance(value, str) or not value:
                raise ToolPolicyError(f"{op} needs a non-empty text value.")
            if len(value) > MAX_PATTERN:
                raise ToolPolicyError(f"a {op} value is at most {MAX_PATTERN} characters.")
            if op == "regex":
                try:
                    re.compile(value)
                except re.error as error:
                    raise ToolPolicyError(f"the pattern does not compile: {error}") from error
        out.append({"path": str(c.get("path")).strip(), "op": op, "value": value})
    return out


def validate(fields: Mapping[str, Any], *, partial: bool = False,
             current_effect: Optional[str] = None) -> dict:
    """A rule's fields, checked and normalised. ``partial`` (an edit) checks only what is given."""
    out: dict[str, Any] = {}
    if not partial or "scope" in fields:
        scope = str(fields.get("scope") or "project").strip()
        kind, _, value = scope.partition(":")
        if not (scope in ("org", "project") or (kind in ("agent", "workflow") and value.strip())):
            raise ToolPolicyError("scope must be org, project, agent:<role> or workflow:<id>.")
        out["scope"] = scope
    if not partial or "priority" in fields:
        try:
            out["priority"] = int(fields.get("priority", 100))
        except (TypeError, ValueError) as error:
            raise ToolPolicyError("priority must be a whole number.") from error
        if not -100000 <= out["priority"] <= 100000:
            raise ToolPolicyError("priority must be between -100000 and 100000.")
    if not partial or "effect" in fields:
        effect = fields.get("effect")
        if effect not in EFFECTS:
            raise ToolPolicyError("effect must be allow, ask, deny or rate_limit.")
        out["effect"] = effect
    if not partial or "selector" in fields:
        sel = fields.get("selector") or {}
        if not isinstance(sel, Mapping):
            raise ToolPolicyError("selector is {server, tool} globs.")
        out["selector"] = {k: str(sel.get(k) or "*").strip()[:200] or "*"
                           for k in ("server", "tool")}
    if not partial or "conditions" in fields:
        out["conditions"] = _validate_conditions(fields.get("conditions"))
    if not partial or "rate_limit" in fields or "effect" in fields:
        effect = out.get("effect") or fields.get("effect") or current_effect
        limit = fields.get("rate_limit")
        if effect == "rate_limit":
            try:
                cap = int((limit or {}).get("max_calls"))
                window = int((limit or {}).get("window_seconds"))
            except (TypeError, ValueError, AttributeError) as error:
                raise ToolPolicyError("a rate limit needs max_calls and window_seconds.") from error
            if cap < 0 or not 1 <= window <= 30 * 86400:
                raise ToolPolicyError("max_calls >= 0 and window_seconds 1..2592000.")
            out["rate_limit"] = {"max_calls": cap, "window_seconds": window}
        elif "rate_limit" in fields or "effect" in fields:
            out["rate_limit"] = None
    for key in ("schema_sha256", "note"):
        if key in fields:
            value = fields.get(key)
            out[key] = str(value)[:2000] if value not in (None, "") else None
    if "expires_at" in fields:
        when = fields.get("expires_at")
        if when in (None, ""):
            out["expires_at"] = None
        else:
            try:
                parsed = (when if isinstance(when, datetime)
                          else datetime.fromisoformat(str(when).replace("Z", "+00:00")))
            except ValueError as error:
                raise ToolPolicyError("expires_at must be an ISO date-time.") from error
            out["expires_at"] = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if "status" in fields:
        if fields.get("status") not in ("active", "disabled"):
            raise ToolPolicyError("status must be active or disabled (revoke to remove).")
        out["status"] = fields.get("status")
    return out


def _db(values: Mapping[str, Any]) -> dict:
    return {k: (json.dumps(v) if k in ("selector", "conditions", "rate_limit") and v is not None
                else v) for k, v in values.items()}


def create_rule(connection: Any, project_id: str, fields: Mapping[str, Any], *, actor: str,
                source: str = "operator", source_decision_id: Optional[str] = None) -> dict:
    """Store one rule. An ORG rule may only make calls stricter (ask, deny, rate_limit): it is
    written from one project's screen and reaches every project in the org."""
    from .core import emit_event, make_id

    if source not in SOURCES:
        raise ToolPolicyError("source must be operator or decision.")
    values = validate(fields)
    if values["scope"] == "org" and values["effect"] == "allow":
        raise ToolPolicyError("an org-wide rule may ask, deny or rate-limit, never allow: "
                              "loosen a tool per project or per agent.")
    rid = make_id("tpol")
    org = org_of(connection, project_id)
    row = {"id": rid, "org_id": org, "project_id": None if values["scope"] == "org" else project_id,
           "source": source, "source_decision_id": source_decision_id, "created_by": actor,
           "rate_limit": None, "schema_sha256": None, "note": None, "expires_at": None,
           "status": values.get("status", "active"), **values}
    cols = ", ".join(row)
    vals = ", ".join(f"CAST(:{k} AS JSONB)" if k in ("selector", "conditions", "rate_limit")
                     else f":{k}" for k in row)
    connection.execute(text(f"INSERT INTO tool_policies_v2 ({cols}) VALUES ({vals})"), _db(row))
    event = "mcp.always_allow_created" if source == "decision" else "mcp.tool_policy_created"
    emit_event(connection, project_id, event, actor, "tool_policy", rid,
               {"scope": row["scope"], "effect": row["effect"], "selector": row["selector"],
                "decision_id": source_decision_id})
    return get_rule(connection, project_id, rid)


def update_rule(connection: Any, project_id: str, rule_id: str, fields: Mapping[str, Any], *,
                actor: str) -> dict:
    from .core import emit_event

    current = get_rule(connection, project_id, rule_id)
    if current["status"] == "revoked":
        raise ToolPolicyError("a revoked rule cannot be changed; write a new one.")
    values = validate(fields, partial=True, current_effect=current["effect"])
    merged = {**current, **values}
    if merged["effect"] == "rate_limit" and not merged.get("rate_limit"):
        raise ToolPolicyError("a rate limit needs max_calls and window_seconds.")
    if merged["scope"] == "org" and merged["effect"] == "allow":
        raise ToolPolicyError("an org-wide rule may ask, deny or rate-limit, never allow.")
    if "scope" in values and (values["scope"] == "org") != (current["scope"] == "org"):
        raise ToolPolicyError("a rule cannot move between org-wide and project; write a new one.")
    if not values:
        return current
    sets = ", ".join(f"{k} = CAST(:{k} AS JSONB)" if k in ("selector", "conditions", "rate_limit")
                     else f"{k} = :{k}" for k in values)
    connection.execute(text(f"UPDATE tool_policies_v2 SET {sets}, updated_at = now() "
                            "WHERE id = :rid"), {**_db(values), "rid": rule_id})
    emit_event(connection, project_id, "mcp.tool_policy_updated", actor, "tool_policy", rule_id,
               {"changed": sorted(values)})
    return get_rule(connection, project_id, rule_id)


def revoke_rule(connection: Any, project_id: str, rule_id: str, *, actor: str,
                reason: str = "revoked") -> dict:
    """Mark a rule revoked (kept for the audit, never read again). Revoking twice is a no-op."""
    from .core import emit_event

    current = get_rule(connection, project_id, rule_id)
    if current["status"] == "revoked":
        return current
    connection.execute(text(
        "UPDATE tool_policies_v2 SET status = 'revoked', revoked_at = now(), revoked_by = :a, "
        "revoked_reason = :r, updated_at = now() WHERE id = :id AND status <> 'revoked'"),
        {"a": actor, "r": reason[:200], "id": rule_id})
    event = ("mcp.always_allow_revoked" if current.get("source") == "decision"
             else "mcp.tool_policy_revoked")
    emit_event(connection, current.get("project_id") or project_id, event, actor, "tool_policy",
               rule_id, {"reason": reason, "decision_id": current.get("source_decision_id")})
    return get_rule(connection, project_id, rule_id)


def drop_pinned_rules(connection: Any, project_id: str, server_id: str, tool: str,
                      schema_sha256: Optional[str]) -> list[str]:
    """818 saw this tool's definition change: every ACTIVE decision rule pinned to another hash
    for it is revoked now (audited), not merely skipped at the next call. Rules are matched by
    the selector the approval wrote (server id or name, exact tool)."""
    rows = connection.execute(text(
        "SELECT r.id FROM tool_policies_v2 r WHERE r.project_id = :p AND r.source = 'decision' "
        "AND r.status = 'active' AND r.schema_sha256 IS NOT NULL "
        "AND r.schema_sha256 IS DISTINCT FROM CAST(:h AS TEXT) "
        "AND r.selector->>'tool' = :t AND (r.selector->>'server' = :sid OR r.selector->>'server' "
        "= (SELECT name FROM mcp_servers WHERE id = :sid AND project_id = :p))"),
        {"p": project_id, "h": schema_sha256, "t": tool, "sid": server_id}).all()
    dropped = [str(r[0]) for r in rows]
    for rid in dropped:
        revoke_rule(connection, project_id, rid, actor="boss", reason="schema_changed")
    return dropped


# ── "Always allow" from an approval (BOSSAPP-817) ────────────────────────────────────────────

def always_allow_offer(server: Mapping[str, Any], tool: str, *, reason: str = "") -> dict:
    """Whether an Ask card about this call may offer "Always allow this", and the pin it would
    carry. Only a tier <= 1 tool on a reviewed server whose definition is not under review."""
    from .mcp_registry import _declared_tier, needs_person, tool_quarantined, tool_tier

    srv = dict(server or {})
    tier = int(tool_tier(srv, tool, _declared_tier(srv, tool)))
    entry = next((t for t in srv.get("tools") or []
                  if isinstance(t, dict) and t.get("name") == tool), {})
    why = ""
    if tier >= 2:
        why = "it changes the world (tier 2 or higher), so it always asks"
    elif needs_person(srv) or reason == "unreviewed":
        why = "the server is not reviewed yet"
    elif tool_quarantined(srv, tool):
        why = "its definition changed and waits for review"
    return {"eligible": not why, "why_not": why or None, "tier": tier,
            "schema_sha256": entry.get("schema_sha256"),
            "expiry_choices": list(EXPIRY_CHOICES)}


def grant_from_decision(connection: Any, row: Mapping[str, Any], request: Mapping[str, Any], *,
                        actor: str) -> Optional[dict]:
    """The approved Ask card's "Always allow this": one ``source=decision`` allow rule scoped to
    the asking agent in this project (or the whole project when no agent asked), selecting the
    exact server and tool, optionally conditioned on argument values the person ticked from the
    held call's preview, pinned to the tool's schema hash, expiring as chosen.

    Refused (ToolPolicyError, the approval with it) for a tool the card did not offer it on."""
    rec = row.get("recommendation") or {}
    if isinstance(rec, str):
        rec = _loads(rec, {}) or {}
    offer = rec.get("always_allow_offer") or {}
    if not offer.get("eligible"):
        raise ToolPolicyError("Always allow is not offered for this call: "
                              f"{offer.get('why_not') or 'only a tier 0-1 tool may be'}.")
    tool = str(rec.get("tool") or "")
    server_ref = str(rec.get("server_id") or rec.get("server") or "")
    if not tool or not server_ref:
        raise ToolPolicyError("the card does not name the tool it is about.")
    # Re-checked NOW, against the server as it stands: the card may be old.
    project_id = str(row["project_id"])
    if rec.get("server_id"):
        from .mcp_registry import get_mcp_server

        server = get_mcp_server(connection, project_id, str(rec["server_id"]))
        if server is None:
            raise ToolPolicyError("the server this card is about is gone.")
        now = always_allow_offer(server, tool, reason=str(rec.get("policy_reason") or ""))
        if not now["eligible"]:
            raise ToolPolicyError(f"Always allow is no longer possible: {now['why_not']}.")
        if offer.get("schema_sha256") and now.get("schema_sha256") != offer["schema_sha256"]:
            raise ToolPolicyError("the tool changed since this card was raised; approve the "
                                  "call alone, and choose Always allow on the next card.")
    preview = rec.get("args_preview") if isinstance(rec.get("args_preview"), Mapping) else {}
    conditions = []
    for key in request.get("match_args") or []:
        key = str(key)
        if key not in preview:
            raise ToolPolicyError(f"{key!r} is not an argument of the held call.")
        value = preview[key]
        if value == "[redacted]" or isinstance(value, (dict, list)):
            raise ToolPolicyError(f"{key!r} cannot be matched (redacted or not a plain value).")
        conditions.append({"path": f"$.{key}" if re.fullmatch(r"[A-Za-z0-9_\-]+", key)
                           else f'$["{key}"]', "op": "equals", "value": value})
    expiry = str(request.get("expiry") or "never")
    if expiry not in EXPIRY_CHOICES:
        raise ToolPolicyError(f"expiry must be one of {', '.join(EXPIRY_CHOICES)}.")
    span = EXPIRY_CHOICES[expiry]
    role = str(rec.get("role_key") or "").strip()
    return create_rule(connection, project_id, {
        "scope": f"agent:{role}" if role else "project",
        "priority": int(request.get("priority") or 50),
        "effect": "allow",
        "selector": {"server": str(rec.get("server_id") or rec.get("server")), "tool": tool},
        "conditions": conditions,
        "schema_sha256": offer.get("schema_sha256"),
        "expires_at": (datetime.now(timezone.utc) + span) if span else None,
        "note": f"Always allow, from approval {row.get('id')}",
    }, actor=actor, source="decision", source_decision_id=str(row.get("id")))


def simulate(connection: Any, project_id: str, body: Mapping[str, Any]) -> dict:
    """The console's simulate box: a hypothetical call decided exactly as a real one would be,
    with nothing recorded. ``server`` names a registered server (id or name) or ``boss``."""
    from .mcp_registry import find_mcp_server

    server_ref = str(body.get("server") or "").strip()
    tool = str(body.get("tool") or "").strip()
    if not server_ref or not tool:
        raise ToolPolicyError("simulate needs a server and a tool.")
    arguments = body.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        raise ToolPolicyError("arguments must be an object.")
    org = org_of(connection, project_id)
    server = None if server_ref == BOSS_SERVER else find_mcp_server(connection, project_id,
                                                                     server_ref)
    if server is not None:
        ctx = context_for(server, tool, arguments, project_id=project_id, org_id=org,
                          run_id=body.get("run_id"), role_key=body.get("role_key"),
                          workflow_id=body.get("workflow_id"))
    elif server_ref == BOSS_SERVER:
        ctx = PolicyContext(server=BOSS_SERVER, tool=tool, arguments=dict(arguments),
                            tier=int(body.get("tier") or 0), project=project_id, org=org,
                            run=body.get("run_id"), role=body.get("role_key"),
                            workflow=body.get("workflow_id"), base="allow")
    else:
        raise ToolPolicyError(f"no MCP server {server_ref!r} in this project (or use 'boss').")
    rules = load_rules(connection, project_id, org)
    out = decide(ctx, rules=rules, connection=connection, record=False)
    by_id = {str(r["id"]): public(r) for r in rules}
    out["matched_rules"] = [by_id[i] for i in out["matched_policy_ids"] if i in by_id]
    out["context"] = {"server": ctx.server, "tool": ctx.tool, "tier": ctx.tier, "base": ctx.base,
                      "quarantined": ctx.quarantined, "unreviewed": ctx.unreviewed,
                      "role": ctx.role, "workflow": ctx.workflow}
    return out


__all__ = ["BOSS_SERVER", "EFFECTS", "EXPIRY_CHOICES", "PolicyContext", "SCHEMA_SQL",
           "ToolPolicyError", "ToolPolicyNotFound", "always_allow_offer", "condition_holds",
           "context_for", "create_rule", "decide", "drop_pinned_rules", "get_rule",
           "grant_from_decision", "list_rules", "load_rules", "order_rules", "parse_path",
           "public", "revoke_rule", "rule_matches", "simulate", "update_rule", "validate"]
