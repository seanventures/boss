"""A builder's sentence, saved as a rule, for one live app action (BOSSAPP-1270).

The sentence is parsed once, at record time, into an effect (allow or block), an
action pattern and an optional predicate. Evaluation reads those saved columns.
It does not read the sentence again and it does not call a model.

No matching rule blocks. The first matching rule, lowest position then oldest,
decides. A block does not run. An allow runs, then one decision row is written.
The same dedupe key returns that row and does not run again.

Sentence shape, after the words are collapsed:

    allow <action>
    block <action>
    allow <action> when <field> is <value>
    block <action> when <field> is over <number>

<action> is a lowercase name, a glob (refund*), or *. Comparison words are
"is", "equals", "is over", "is under", "is at least" and "is at most".
"""
from __future__ import annotations

import fnmatch
import json
import re
from decimal import Decimal
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id

_SHAPE = (
    "Write allow or block, then the action. "
    "Example: block refund when amount is over 50"
)
_RULE = re.compile(
    r"^(allow|block)\s+"
    r"(\*|[a-z][a-z0-9_.*-]{0,63})"
    r"(?:\s+when\s+([a-z][a-z0-9_]{0,63})\s+"
    r"(is over|is under|is at least|is at most|equals|is|>=|<=|>|<|=)\s+"
    r"(\S(?:.*\S)?))?$",
    re.IGNORECASE,
)
_OPS = {
    "is over": "gt", ">": "gt",
    "is under": "lt", "<": "lt",
    "is at least": "gte", ">=": "gte",
    "is at most": "lte", "<=": "lte",
    "is": "eq", "equals": "eq", "=": "eq",
}
_APP_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ACTION = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DEDUPE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_EXECUTOR = Callable[[str, dict[str, Any]], Any]


class LiveActionRuleError(ValueError):
    """The sentence or the attempt cannot be taken as given."""


class LiveActionFailed(LiveActionRuleError):
    """The action was allowed, then failed. Nothing was recorded."""


def parse_description(description: str) -> dict[str, Any]:
    """The sentence as saved columns. The same words always return the same dict."""
    collapsed = " ".join((description or "").split())
    match = _RULE.match(collapsed)
    if match is None:
        raise LiveActionRuleError(_SHAPE)
    effect, action, field, op, raw_value = match.groups()
    action = action.lower()
    if action != "*" and not _ACTION.match(action.replace("*", "x").replace(".", "x")):
        raise LiveActionRuleError(_SHAPE)
    predicate: dict[str, Any] = {}
    if field is not None:
        predicate = {
            "field": field.lower(),
            "op": _OPS[op.lower()],
            "value": _literal(raw_value),
        }
        # "is over" with no number is an unfinished comparison, not the value "over".
        if predicate["op"] == "eq" and str(predicate["value"]).lower() in {
                "over", "under", "at least", "at most"}:
            raise LiveActionRuleError(_SHAPE)
    return {"effect": effect.lower(), "action_pattern": action, "predicate": predicate}


def record_rule(connection: Connection, project_id: str, description: str, *,
                actor: str, app_key: str, position: int = 100) -> dict[str, Any]:
    """Store the sentence and the columns evaluation will read."""
    _require_project(connection, project_id)
    who = _actor(actor)
    app = _app_key(app_key)
    parsed = parse_description(description)
    place = _position(position)
    saved = " ".join(description.split())
    if len(saved) > 500:
        raise LiveActionRuleError("Keep the rule to 500 characters.")
    row = connection.execute(text(
        "INSERT INTO live_action_rules (id, project_id, app_key, description, effect, "
        "action_pattern, predicate, position, created_by, created_at) "
        "VALUES (:i, :p, :app, :d, :e, :a, CAST(:pred AS JSONB), :pos, :by, "
        "clock_timestamp()) RETURNING *"),
        {"i": make_id("lar"), "p": project_id, "app": app, "d": saved,
         "e": parsed["effect"], "a": parsed["action_pattern"],
         "pred": json.dumps(parsed["predicate"]), "pos": place, "by": who},
    ).mappings().one()
    return _public_rule(row)


def list_rules(connection: Connection, project_id: str, *,
               app_key: Optional[str] = None) -> list[dict[str, Any]]:
    """Rules in the order evaluation reads them."""
    app = _app_key(app_key) if app_key else None
    rows = connection.execute(text(
        "SELECT * FROM live_action_rules WHERE project_id = :p "
        "AND (CAST(:app AS TEXT) IS NULL OR app_key = :app) "
        "ORDER BY position, created_at, id"),
        {"p": project_id, "app": app}).mappings().all()
    return [_public_rule(row) for row in rows]


def attempt(connection: Connection, project_id: str, *, app_key: str, action: str,
            actor: str, payload: Optional[dict[str, Any]] = None,
            dedupe_key: Optional[str] = None,
            executor: Optional[_EXECUTOR] = None) -> dict[str, Any]:
    """Decide from the saved rules. Run the action only when one allows it."""
    _require_project(connection, project_id)
    app = _app_key(app_key)
    name = _action(action)
    who = _actor(actor)
    body = _payload(payload)
    key = _dedupe(dedupe_key)
    if key is not None:
        existing = _decision_for(connection, project_id, app, key)
        if existing is not None:
            return _public_attempt(existing, repeated=True)
    rule = _first_match(connection, project_id, app, name, who, body)
    if rule is None or rule["effect"] != "allow":
        return {"decision": "block", "ran": False, "repeated": False,
                "rule_id": None if rule is None else rule["id"], "decision_id": None,
                "action": name, "app_key": app, "result": None}
    run = executor or _default_executor
    try:
        result = run(name, body)
    except LiveActionFailed:
        raise
    except Exception as exc:  # noqa: BLE001 — the failure is the caller's error
        raise LiveActionFailed(f"{name} failed: {exc}") from exc
    if not isinstance(result, dict):
        raise LiveActionFailed(f"{name} failed: the action did not return a record")
    try:
        encoded = json.dumps(result)
    except (TypeError, ValueError) as exc:
        raise LiveActionFailed(f"{name} failed: {exc}") from exc
    row = connection.execute(text(
        "INSERT INTO live_action_decisions (id, project_id, rule_id, app_key, action, "
        "effect, actor, payload, result, dedupe_key, created_at) "
        "VALUES (:i, :p, :rule, :app, :action, 'allow', :who, "
        "CAST(:payload AS JSONB), CAST(:result AS JSONB), :dedupe, clock_timestamp()) "
        "RETURNING *"),
        {"i": make_id("lad"), "p": project_id, "rule": rule["id"], "app": app,
         "action": name, "who": who, "payload": json.dumps(body), "result": encoded,
         "dedupe": key}).mappings().one()
    return _public_attempt(row, repeated=False)


def _default_executor(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The local stand-in for the live action. It does not leave this process."""
    return {"status": "ran", "action": action}


def _first_match(connection: Connection, project_id: str, app_key: str, action: str,
                 actor: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    for rule in list_rules(connection, project_id, app_key=app_key):
        if rule["status"] != "active":
            continue
        pattern = rule["action_pattern"]
        if not fnmatch.fnmatchcase(action, pattern):
            continue
        if _predicate_matches(rule["predicate"], actor, payload):
            return rule
    return None


def _predicate_matches(predicate: dict[str, Any], actor: str,
                       payload: dict[str, Any]) -> bool:
    if not predicate:
        return True
    field = str(predicate.get("field") or "")
    op = str(predicate.get("op") or "")
    expected = predicate.get("value")
    if field == "actor" and field not in payload:
        actual: Any = actor
    elif field not in payload:
        return False
    else:
        actual = payload[field]
    if op == "eq":
        if _is_number(expected):
            try:
                return _number(actual) == _number(expected)
            except (TypeError, ValueError):
                return False
        return str(actual).strip().lower() == str(expected).strip().lower()
    try:
        left, right = _number(actual), _number(expected)
    except (TypeError, ValueError):
        return False
    if op == "gt":
        return left > right
    if op == "lt":
        return left < right
    if op == "gte":
        return left >= right
    if op == "lte":
        return left <= right
    return False


def _literal(raw: str) -> Any:
    text_value = raw.strip()
    if len(text_value) >= 2 and text_value[0] == text_value[-1] and text_value[0] in "\"'":
        text_value = text_value[1:-1].strip()
    if not text_value or len(text_value) > 200:
        raise LiveActionRuleError(_SHAPE)
    if re.fullmatch(r"-?\d+", text_value):
        return int(text_value)
    if re.fullmatch(r"-?\d+\.\d+", text_value):
        return float(text_value)
    return text_value


def _require_project(connection: Connection, project_id: str) -> None:
    found = connection.execute(
        text("SELECT 1 FROM projects WHERE id = :p"), {"p": project_id}).scalar()
    if not found:
        raise LiveActionRuleError("Project not found.")


def _actor(actor: str) -> str:
    who = " ".join(str(actor or "").split())
    if not who or len(who) > 200:
        raise LiveActionRuleError("Name who is asking.")
    return who


def _app_key(app_key: str) -> str:
    app = str(app_key or "").strip().lower()
    if not _APP_KEY.match(app):
        raise LiveActionRuleError("Name the app with a letter, then letters or dashes.")
    return app


def _action(action: str) -> str:
    name = str(action or "").strip().lower()
    if not _ACTION.match(name):
        raise LiveActionRuleError("Name the action with a letter, then letters or dashes.")
    return name


def _position(position: int) -> int:
    try:
        place = int(position)
    except (TypeError, ValueError):
        raise LiveActionRuleError("Position must be a whole number.") from None
    if place < 0 or place > 10000:
        raise LiveActionRuleError("Position must be from 0 to 10000.")
    return place


def _dedupe(dedupe_key: Optional[str]) -> Optional[str]:
    if dedupe_key is None:
        return None
    key = str(dedupe_key).strip()
    if not key:
        return None
    if not _DEDUPE.match(key):
        raise LiveActionRuleError("The dedupe key has letters, numbers and . _ : - only.")
    return key


def _payload(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise LiveActionRuleError("The payload must be an object.")
    if len(payload) > 20:
        raise LiveActionRuleError("Keep the payload to 20 fields.")
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key).strip().lower()
        if not _FIELD.match(name):
            raise LiveActionRuleError("A payload field starts with a letter.")
        if isinstance(value, bool) or value is None or _is_number(value) or isinstance(value, str):
            if isinstance(value, str) and len(value) > 200:
                raise LiveActionRuleError("Keep each payload value to 200 characters.")
            clean[name] = value
            continue
        raise LiveActionRuleError("A payload value must be text or a number.")
    return clean


def _decision_for(connection: Connection, project_id: str, app_key: str,
                  dedupe_key: str) -> Optional[Any]:
    return connection.execute(text(
        "SELECT * FROM live_action_decisions WHERE project_id = :p AND app_key = :app "
        "AND dedupe_key = :k"),
        {"p": project_id, "app": app_key, "k": dedupe_key}).mappings().first()


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise TypeError("not a number")
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()):
        return float(value)
    raise TypeError("not a number")


def _predicate_out(value: Any) -> dict[str, Any]:
    raw = _loads(value) or {}
    if not isinstance(raw, dict) or not raw:
        return {}
    out = {"field": raw.get("field"), "op": raw.get("op"), "value": _plain(raw.get("value"))}
    return out


def _public_rule(row: Any) -> dict[str, Any]:
    created = row["created_at"]
    return {
        "id": row["id"], "project_id": row["project_id"], "app_key": row["app_key"],
        "description": row["description"], "effect": row["effect"],
        "action_pattern": row["action_pattern"], "predicate": _predicate_out(row["predicate"]),
        "position": int(row["position"]), "status": row["status"],
        "created_by": row["created_by"],
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
    }


def _public_attempt(row: Any, *, repeated: bool) -> dict[str, Any]:
    return {
        "decision": "allow", "ran": True, "repeated": repeated,
        "rule_id": row["rule_id"], "decision_id": row["id"],
        "action": row["action"], "app_key": row["app_key"],
        "result": _loads(row["result"]) or {},
    }
