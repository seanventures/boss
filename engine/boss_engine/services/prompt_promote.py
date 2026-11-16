"""Save a prompt. Turn it into an agent only after a decision is approved.

Saving writes a project_prompts row and does not touch roles. Asking opens a
decision and still creates no agent. promote() refuses unless that decision is
approved, then calls agents.create_agent with the same fields POST /v1/agents
sends. There is no second insert into roles.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id

#: Recorded on approval. This module creates the agent; the decision does not
#: carry its own follow-on, so the action stays one the catalogue already knows.
ACTION = "decide"
SUBJECT = "prompt.promote"
_LABEL = "prompt-promote"


class PromoteError(ValueError):
    """A prompt BOSS will not save, or a promotion it refuses. `status` is the HTTP answer."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _sign(digest: str) -> str:
    from ..identity.authn_support import keyed_hash

    return "hs1:" + keyed_hash(_LABEL, digest)


def _freeze(row: Mapping[str, Any]) -> dict[str, Any]:
    """The POST /v1/agents body this prompt becomes, frozen onto the decision."""
    from .agents import key_from_name

    stored = _obj(row.get("agent_body"))
    name = str(stored.get("name") or row["title"] or "").strip()
    if stored.get("purpose") is not None and "purpose" in stored:
        purpose = str(stored.get("purpose") or "")
    else:
        purpose = str(row["body"] or "")
    role_key = str(stored.get("role_key") or "").strip().lower() or key_from_name(name)
    born = stored.get("born_from")
    if not isinstance(born, dict):
        born = {"kind": "blank"}
    skills = stored.get("skills") if isinstance(stored.get("skills"), list) else []
    servers = stored.get("mcp_servers") if isinstance(stored.get("mcp_servers"), list) else []
    boundaries = stored.get("boundaries") if isinstance(stored.get("boundaries"), dict) else None
    persona = stored.get("persona") if isinstance(stored.get("persona"), dict) else None
    reports = str(stored.get("reports_to") or "").strip() or None
    team = str(stored.get("team") or "").strip() or None
    return {
        "name": name,
        "purpose": purpose,
        "role_key": role_key or None,
        "skills": [str(item) for item in skills if str(item).strip()],
        "mcp_servers": servers,
        "reports_to": reports,
        "boundaries": boundaries,
        "persona": persona,
        "born_from": born,
        "team": team,
    }


def _view(row: Mapping[str, Any]) -> dict[str, Any]:
    created = row.get("created_at")
    if hasattr(created, "isoformat"):
        created = created.isoformat()
    body = _obj(row.get("agent_body"))
    role_key = row.get("promoted_role_key")
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "title": row["title"],
        "body": row["body"],
        "decision_id": row.get("decision_id"),
        "decision_status": row.get("decision_status"),
        "role_key": role_key,
        "agent_created": bool(role_key),
        "skills": list(body.get("skills") or []),
        "mcp_servers": list(body.get("mcp_servers") or []),
        "created_by": row.get("created_by"),
        "created_at": created,
    }


def _load(connection: Connection, project_id: str, prompt_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT p.id, p.project_id, p.title, p.body, p.agent_body, p.decision_id, "
        "p.promoted_role_key, p.created_by, p.created_at, d.status AS decision_status "
        "FROM project_prompts p LEFT JOIN decisions d ON d.id = p.decision_id "
        "WHERE p.id = :id AND p.project_id = :p"),
        {"id": prompt_id, "p": project_id}).mappings().first()
    if row is None:
        raise PromoteError(f"No saved prompt {prompt_id} in this project.", status=404)
    return dict(row)


def list_prompts(connection: Connection, *, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT p.id, p.project_id, p.title, p.body, p.agent_body, p.decision_id, "
        "p.promoted_role_key, p.created_by, p.created_at, d.status AS decision_status "
        "FROM project_prompts p LEFT JOIN decisions d ON d.id = p.decision_id "
        "WHERE p.project_id = :p ORDER BY p.created_at DESC, p.id DESC"),
        {"p": project_id}).mappings().all()
    return [_view(row) for row in rows]


def get_prompt(connection: Connection, *, project_id: str, prompt_id: str) -> dict[str, Any]:
    return _view(_load(connection, project_id, prompt_id))


def _overrides(agent: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not agent:
        return {}
    kept: dict[str, Any] = {}
    for key in ("name", "purpose", "role_key", "skills", "mcp_servers", "reports_to",
                "boundaries", "persona", "born_from", "team"):
        if key in agent and agent[key] is not None:
            kept[key] = agent[key]
    return kept


def save_prompt(connection: Connection, *, project_id: str, actor: str, title: str, body: str,
                agent: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Store the prompt. Creates no role and does not call create_agent."""
    who = str(actor or "").strip()
    if not who:
        raise PromoteError("Say who is saving this prompt.", status=422)
    name = str(title or "").strip()
    if not name:
        raise PromoteError("Name the prompt before saving it.", status=422)
    words = str(body or "").strip()
    if not words:
        raise PromoteError("Write the prompt before saving it.", status=422)
    if not str(project_id or "").strip():
        raise PromoteError("Name the project this prompt belongs to.", status=422)
    extras = _overrides(agent)
    dedupe = hashlib.sha256(json.dumps(
        {"project_id": project_id, "title": name, "body": words, "agent": extras},
        sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    prompt_id = make_id("prompt")
    inserted = connection.execute(text(
        "INSERT INTO project_prompts (id, project_id, title, body, agent_body, dedupe_key, "
        "created_by) VALUES (:id, :p, :title, :body, CAST(:agent AS JSONB), :dedupe, :actor) "
        "ON CONFLICT (project_id, dedupe_key) DO NOTHING RETURNING id"),
        {"id": prompt_id, "p": project_id, "title": name, "body": words,
         "agent": json.dumps(extras), "dedupe": dedupe, "actor": who}).scalar()
    if inserted:
        emit_event(connection, project_id, "prompt.saved", who, "prompt", str(inserted),
                   {"title": name})
    else:
        inserted = connection.execute(text(
            "SELECT id FROM project_prompts WHERE project_id = :p AND dedupe_key = :d"),
            {"p": project_id, "d": dedupe}).scalar()
    return _view(_load(connection, project_id, str(inserted)))


def ask(connection: Connection, *, project_id: str, prompt_id: str, actor: str) -> dict[str, Any]:
    """Open a decision to turn this prompt into an agent. Creates no role."""
    who = str(actor or "").strip()
    if not who:
        raise PromoteError("Say who is asking.", status=422)
    row = _load(connection, project_id, prompt_id)
    if row.get("promoted_role_key"):
        raise PromoteError("This prompt is already an agent.", status=409)
    if row.get("decision_id") and row.get("decision_status") in ("pending", "delegated", "approved"):
        return {
            "prompt_id": prompt_id,
            "decision_id": row["decision_id"],
            "decision_status": row["decision_status"],
            "agent_created": False,
        }
    frozen = _freeze(row)
    if not frozen["name"]:
        raise PromoteError("Name the prompt before asking.", status=422)
    digest = _digest(frozen)
    recommendation = {
        "action": ACTION,
        "subject": SUBJECT,
        "prompt_id": prompt_id,
        "agent": frozen,
        "agent_sha256": digest,
        "agent_sig": _sign(digest),
        "summary": f"Turn {frozen['name']} into an agent.",
    }
    decision_id = create_blocking_decision(
        connection, project_id, None, f"Turn saved prompt {prompt_id} into an agent?",
        recommendation, {}, "saved_prompt_promote", risk_tier=2, default_action="hold")
    connection.execute(text(
        "UPDATE project_prompts SET decision_id = :d, updated_at = now() "
        "WHERE id = :id AND project_id = :p"),
        {"d": decision_id, "id": prompt_id, "p": project_id})
    emit_event(connection, project_id, "prompt.decision_asked", who, "prompt", prompt_id,
               {"decision_id": decision_id, "role_key": frozen["role_key"]})
    return {
        "prompt_id": prompt_id,
        "decision_id": decision_id,
        "decision_status": "pending",
        "agent_created": False,
    }


def _decision(connection: Connection, project_id: str, decision_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT id, project_id, status, approved_by, recommendation FROM decisions "
        "WHERE id = :d"), {"d": decision_id}).mappings().first()
    if row is None or str(row["project_id"]) != project_id:
        raise PromoteError("This prompt's decision is gone, so no agent was created.", status=409)
    return dict(row)


def _approved_body(decision: Mapping[str, Any], prompt_id: str) -> dict[str, Any]:
    if str(decision.get("status") or "") != "approved":
        raise PromoteError("This decision is not approved, so the prompt stays a prompt.",
                           status=409)
    rec = _obj(decision.get("recommendation"))
    if rec.get("subject") != SUBJECT or rec.get("prompt_id") != prompt_id:
        raise PromoteError("This decision does not belong to this prompt, so no agent was created.",
                           status=409)
    body = rec.get("agent")
    if not isinstance(body, dict):
        raise PromoteError("This decision has no agent to create.", status=409)
    digest = _digest(body)
    stored = rec.get("agent_sha256")
    if not isinstance(stored, str) or not hmac.compare_digest(stored, digest):
        raise PromoteError("What was approved no longer matches, so no agent was created.",
                           status=409)
    given = rec.get("agent_sig")
    expected = _sign(digest)
    if not isinstance(given, str) or not hmac.compare_digest(expected, given):
        raise PromoteError("This decision's signature does not verify, so no agent was created.",
                           status=409)
    return body


def promote(connection: Connection, *, project_id: str, prompt_id: str, actor: str) -> dict[str, Any]:
    """Create the agent from an approved decision, through agents.create_agent."""
    from .agents import AgentError, create_agent, get_agent

    row = _load(connection, project_id, prompt_id)
    if row.get("promoted_role_key"):
        agent = get_agent(connection, project_id, str(row["promoted_role_key"]))
        return {"agent_created": True, "already": True, "role_key": row["promoted_role_key"],
                "agent": agent}
    if not row.get("decision_id"):
        raise PromoteError("This prompt has no decision, so it cannot become an agent.", status=409)
    decision = _decision(connection, project_id, str(row["decision_id"]))
    body = _approved_body(decision, prompt_id)
    sponsor = str(decision.get("approved_by") or "").strip()
    if not sponsor:
        raise PromoteError("An approved decision needs the person who approved it.", status=409)
    if not str(actor or "").strip():
        raise PromoteError("Say who is promoting this prompt.", status=422)
    try:
        with connection.begin_nested():
            made = create_agent(
                connection, project_id, actor=sponsor, name=body["name"],
                purpose=body.get("purpose") or "", role_key=body.get("role_key"),
                skills=body.get("skills") or [], mcp_servers=body.get("mcp_servers") or [],
                reports_to=body.get("reports_to"), boundaries=body.get("boundaries"),
                persona=body.get("persona"), born_from=body.get("born_from"),
                team=body.get("team"))
            role_key = str(made.get("role_key") or body.get("role_key") or "")
            connection.execute(text(
                "UPDATE project_prompts SET promoted_role_key = :k, updated_at = now() "
                "WHERE id = :id AND project_id = :p"),
                {"k": role_key, "id": prompt_id, "p": project_id})
            emit_event(connection, project_id, "prompt.promoted", sponsor, "prompt", prompt_id,
                       {"role_key": role_key, "decision_id": decision["id"]})
    except AgentError as error:
        raise PromoteError(str(error), status=getattr(error, "status", 422)) from error
    return {"agent_created": True, "already": False, "role_key": made["role_key"], "agent": made}
