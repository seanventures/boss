"""TYPED QUESTIONS THAT RESUME THE AGENT (BOSSAPP-832).

A decision could only be answered approve / reject / request changes plus free text, so an
agent that needed "which of these three databases?" or "accept these five findings?" asked in
prose and parsed the note. A decision may now carry a FORM (``decisions.form_schema``) and the
answer is recorded TYPED (``decisions.answer``, ``answered_by``, ``answered_at``) when the person
submits it — the approve transition, validated before anything is written.

FIELD TYPES (``{"fields": [...]}``, each ``{"key", "type", "label", "required"?}``):

* ``text``             — ``{"multiline"?}``                       → answer: str
* ``select``           — ``{"options": [str | {value, label}]}``    → answer: one option value
* ``checkbox``         —                                          → answer: bool
* ``item_verdicts``    — ``{"items": [{id, label}], "verdicts"?}`` (default accept / reject)
                                                                 → answer: {item_id: verdict}
* ``task_suggestions`` — ``{"suggestions": [{title, description?, assignee?}]}``
                                                                 → answer: [{index, accepted,
                                                                    title?}]
  Accepted suggestions become canonical work items (BOSSAPP-830) under the asking item, ALL in
  the answer's own transaction: one that cannot be made refuses the whole answer.

THE AGENT RESUMES. ``ask_human`` from inside an agent run (agent_loop's native tool) parks the
run ``awaiting_approval`` with ``pending.kind = "ask_human"``; the answer queues the same
``agent.resume`` job an approved tool call does, and :func:`resume_with_answer` replaces the
parked tool result with the typed answer and appends it as a structured user message before the
loop continues. Declining (reject) resumes it too, told the person declined and why.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event

log = logging.getLogger(__name__)

ACTION = "answer_form"
FIELD_TYPES = ("text", "select", "checkbox", "item_verdicts", "task_suggestions")
DEFAULT_VERDICTS = ("accept", "reject")
MAX_FIELDS = 20
TOOL_NAME = "ask_human"


class FormError(ValueError):
    """A form that cannot be asked, or an answer that does not fit its form."""


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _option_values(options: Any) -> list[dict[str, str]]:
    out = []
    for o in options or []:
        if isinstance(o, Mapping):
            value = str(o.get("value") or o.get("label") or "").strip()
            label = str(o.get("label") or value).strip()
        else:
            value = label = str(o).strip()
        if value:
            out.append({"value": value[:200], "label": label[:200]})
    return out


def normalize_form(schema: Any) -> dict[str, Any]:
    """Validate a form and fill its defaults. Accepts {"fields": [...]} or the bare list."""
    schema = _json(schema, schema)
    fields = schema.get("fields") if isinstance(schema, Mapping) else schema
    if not isinstance(fields, list) or not fields:
        raise FormError("a form needs at least one field")
    if len(fields) > MAX_FIELDS:
        raise FormError(f"at most {MAX_FIELDS} fields")
    out, seen = [], set()
    for i, f in enumerate(fields):
        if not isinstance(f, Mapping):
            raise FormError(f"field {i + 1} must be an object")
        kind = str(f.get("type") or "").strip()
        if kind not in FIELD_TYPES:
            raise FormError(f"field {i + 1}: type must be one of {', '.join(FIELD_TYPES)}")
        key = str(f.get("key") or f"field_{i + 1}").strip()[:60]
        if key in seen:
            raise FormError(f"field key {key!r} is used twice")
        seen.add(key)
        field: dict[str, Any] = {"key": key, "type": kind,
                                 "label": str(f.get("label") or key).strip()[:300],
                                 "required": bool(f.get("required", kind != "checkbox"))}
        if f.get("help"):
            field["help"] = str(f["help"])[:500]
        if kind == "text":
            field["multiline"] = bool(f.get("multiline"))
        elif kind == "select":
            field["options"] = _option_values(f.get("options"))
            if len(field["options"]) < 2:
                raise FormError(f"field {key!r}: a select needs at least two options")
        elif kind == "item_verdicts":
            items = []
            for it in f.get("items") or []:
                if isinstance(it, Mapping) and str(it.get("id") or "").strip():
                    items.append({"id": str(it["id"])[:120],
                                  "label": str(it.get("label") or it["id"])[:300]})
            if not items:
                raise FormError(f"field {key!r}: item_verdicts needs items [{{id, label}}]")
            field["items"] = items
            field["verdicts"] = [str(v) for v in (f.get("verdicts") or DEFAULT_VERDICTS)][:6]
        elif kind == "task_suggestions":
            suggestions = []
            for s in f.get("suggestions") or []:
                s = {"title": s} if isinstance(s, str) else s
                if isinstance(s, Mapping) and str(s.get("title") or "").strip():
                    suggestions.append({
                        "title": str(s["title"]).strip()[:300],
                        "description": str(s.get("description") or "")[:4000],
                        **({"assignee": str(s["assignee"])} if s.get("assignee") else {})})
            if not suggestions:
                raise FormError(f"field {key!r}: task_suggestions needs suggestions [{{title}}]")
            field["suggestions"] = suggestions
            field["required"] = False  # accepting none is an answer
        out.append(field)
    return {"fields": out}


def validate_answer(schema: Mapping[str, Any], answer: Any) -> dict[str, Any]:
    """The answer, typed against its form. Raises FormError naming the first field that does
    not fit. Unknown keys are refused, so a typo is never silently dropped."""
    answer = _json(answer, answer)
    if not isinstance(answer, Mapping):
        raise FormError("the answer is an object keyed by the form's field keys")
    fields = {f["key"]: f for f in schema.get("fields") or []}
    extra = sorted(set(answer) - set(fields))
    if extra:
        raise FormError(f"the form has no field {extra[0]!r}")
    out: dict[str, Any] = {}
    for key, f in fields.items():
        value = answer.get(key)
        kind = f["type"]
        missing = value is None or (isinstance(value, str) and not value.strip())
        if missing:
            if f.get("required") and kind not in ("checkbox", "task_suggestions"):
                raise FormError(f"{f['label']}: an answer is required")
            if kind == "checkbox":
                out[key] = False
            elif kind == "task_suggestions":
                out[key] = []
            continue
        if kind == "text":
            out[key] = str(value).strip()[:8000]
        elif kind == "select":
            allowed = [o["value"] for o in f["options"]]
            if str(value) not in allowed:
                raise FormError(f"{f['label']}: choose one of {', '.join(allowed)}")
            out[key] = str(value)
        elif kind == "checkbox":
            if not isinstance(value, bool):
                raise FormError(f"{f['label']}: true or false")
            out[key] = value
        elif kind == "item_verdicts":
            if not isinstance(value, Mapping):
                raise FormError(f"{f['label']}: a verdict per item, {{item_id: verdict}}")
            ids = [it["id"] for it in f["items"]]
            unknown = [k for k in value if k not in ids]
            if unknown:
                raise FormError(f"{f['label']}: no item {unknown[0]!r}")
            verdicts = {}
            for item_id in ids:
                v = value.get(item_id)
                if v is None:
                    if f.get("required"):
                        raise FormError(f"{f['label']}: give every item a verdict")
                    continue
                if str(v) not in f["verdicts"]:
                    raise FormError(f"{f['label']}: a verdict is one of {', '.join(f['verdicts'])}")
                verdicts[item_id] = str(v)
            out[key] = verdicts
        elif kind == "task_suggestions":
            if not isinstance(value, list):
                raise FormError(f"{f['label']}: a list of [{{index, accepted}}]")
            picks = []
            for entry in value:
                if not isinstance(entry, Mapping):
                    raise FormError(f"{f['label']}: each pick is {{index, accepted}}")
                try:
                    index = int(entry.get("index"))
                except (TypeError, ValueError) as error:
                    raise FormError(f"{f['label']}: each pick names its suggestion index") from error
                if not 0 <= index < len(f["suggestions"]):
                    raise FormError(f"{f['label']}: no suggestion {index}")
                pick: dict[str, Any] = {"index": index, "accepted": bool(entry.get("accepted"))}
                if str(entry.get("title") or "").strip():
                    pick["title"] = str(entry["title"]).strip()[:300]
                picks.append(pick)
            out[key] = picks
    return out


# ── asking ───────────────────────────────────────────────────────────────────────────────────

def ask_human(connection: Connection, *, project_id: str, question: str, form: Any,
              asker: str, run_id: Optional[str] = None, work_item_id: Optional[str] = None,
              task_id: Optional[str] = None, context: str = "") -> dict[str, Any]:
    """Raise a form decision. `run_id` is the agent run that waits for the answer (it resumes
    with it); `work_item_id` is the item it is about (accepted task suggestions go under it).

    The recommendation deliberately does NOT use `work_item_id`/`asked_by`: those two together
    route a decision to the ambient board's answer path (decision_actions.is_ambient_card)."""
    question = str(question or "").strip()
    if not question:
        raise FormError("ask a question")
    schema = normalize_form(form)
    if work_item_id and connection.execute(text(
            "SELECT 1 FROM work_items WHERE id = :w AND project_id = :p"),
            {"w": work_item_id, "p": project_id}).first() is None:
        raise LookupError(f"no work item {work_item_id} in this project")
    recommendation = {
        "action": ACTION, "asker": asker, "run_id": run_id, "about_work_item_id": work_item_id,
        "summary": (str(context or "").strip()[:1000]
                    or f"{asker} asks: answer the form to let it continue"),
        "fields": [f["key"] for f in schema["fields"]],
    }
    decision_id = create_blocking_decision(
        connection, project_id, task_id, question[:500], recommendation, {},
        "agent_asked_a_typed_question", risk_tier=1, default_action="hold")
    connection.execute(text(
        "UPDATE decisions SET form_schema = CAST(:f AS JSONB) WHERE id = :id"),
        {"f": json.dumps(schema), "id": decision_id})
    emit_event(connection, project_id, "decision.form_asked", asker, "decision", decision_id,
               {"run_id": run_id, "work_item_id": work_item_id,
                "field_types": [f["type"] for f in schema["fields"]]})
    return {"decision_id": decision_id, "form": schema}


# ── answering (DecisionService.transition) ───────────────────────────────────────────────────

def record_answer(connection: Connection, decision: Mapping[str, Any], answer: Any, *,
                  actor: str) -> Optional[dict[str, Any]]:
    """Called by the approve transition BEFORE it records the approval: a form decision must be
    answered with a valid typed answer, and that answer is written. Returns the typed answer, or
    None for a decision with no form (an `answer` sent to one is refused)."""
    schema = _json(decision.get("form_schema"), None)
    if not schema:
        if answer:
            raise FormError("this decision has no form to answer")
        return None
    if answer is None:
        raise FormError("this question is answered with its form")
    typed = validate_answer(schema, answer)
    connection.execute(text(
        "UPDATE decisions SET answer = CAST(:a AS JSONB), answered_by = :by, answered_at = now() "
        "WHERE id = :id"), {"a": json.dumps(typed), "by": actor, "id": decision["id"]})
    return typed


def on_answered(connection: Connection, settings: Any, decision: Mapping[str, Any], *,
                verdict: str, actor: str, note: Optional[str] = None) -> dict[str, Any]:
    """The follow-on of a form decision: accepted task suggestions become work items (all or
    none — this runs inside the transition's transaction, and any failure refuses the answer),
    then the waiting agent run is queued to resume with the answer."""
    from . import work_items

    rec = _json(decision.get("recommendation"), {}) or {}
    project_id = decision["project_id"]
    created: list[dict[str, Any]] = []
    if verdict == "approve":
        stored = connection.execute(text(
            "SELECT form_schema, answer FROM decisions WHERE id = :id"),
            {"id": decision["id"]}).mappings().one()
        schema = _json(stored["form_schema"], {}) or {}
        answer = _json(stored["answer"], {}) or {}
        parent = rec.get("about_work_item_id")
        for field in schema.get("fields") or []:
            if field["type"] != "task_suggestions":
                continue
            for pick in answer.get(field["key"]) or []:
                if not pick.get("accepted"):
                    continue
                suggestion = field["suggestions"][pick["index"]]
                item = work_items.create_item(
                    connection, settings, project_id=project_id,
                    title=pick.get("title") or suggestion["title"],
                    description=suggestion.get("description") or "",
                    parent_id=parent, assignee=suggestion.get("assignee"), status="todo",
                    subject_type="item", actor=actor)
                created.append({"id": item["id"], "human_key": item["human_key"],
                                "title": item["title"]})
        if created:
            connection.execute(text(
                "UPDATE decisions SET answer = answer || jsonb_build_object('_created_work_items', "
                "CAST(:c AS JSONB)) WHERE id = :id"), {"c": json.dumps(created), "id": decision["id"]})
    emit_event(connection, project_id, "decision.form_answered", actor, "decision",
               decision["id"], {"verdict": verdict, "created_work_items": [c["id"] for c in created]})
    run_id = rec.get("run_id")
    if run_id:
        queue_agent_resume(connection, project_id=project_id, run_id=str(run_id), actor=actor,
                           task_id=decision.get("task_id"), decision_id=decision["id"])
    return {"created_work_items": created, "resumes": run_id}


def queue_agent_resume(connection: Connection, *, project_id: str, run_id: str, actor: str,
                       task_id: Optional[str] = None, decision_id: Optional[str] = None) -> str:
    """The same `agent.resume` job an approved tool call queues (decisions.py resume_agent_run):
    the worker resumes the run, and agent_loop.resume_agent_run hands an ask_human pause to
    :func:`resume_with_answer`."""
    from .jobs import JobLeaseService
    from ..config import get_settings

    job = JobLeaseService(get_settings()).create_job(
        connection, project_id=project_id, task_id=task_id, kind="agent.resume", priority=45,
        payload={"run_id": run_id})
    emit_event(connection, project_id, "agent.resume_queued", actor, "agent_run", run_id,
               {"decision_id": decision_id, "why": "typed answer"})
    return str(job.get("id") if isinstance(job, Mapping) else job)


def answer_message(decision: Mapping[str, Any]) -> dict[str, Any]:
    """What the agent reads: the question, the verdict and the typed answer, as data."""
    status = str(decision.get("status") or "")
    answer = _json(decision.get("answer"), None)
    created = (answer or {}).pop("_created_work_items", []) if isinstance(answer, dict) else []
    body: dict[str, Any] = {
        "type": "human_answer", "decision_id": decision["id"], "question": decision.get("question"),
        "status": "answered" if status == "approved" else "declined",
        "answered_by": decision.get("answered_by") or decision.get("approved_by"),
    }
    if status == "approved":
        body["answer"] = answer or {}
        if created:
            body["created_work_items"] = created
    else:
        body["reason"] = decision.get("resolution_note") or ""
    return body


def resume_with_answer(connection: Connection, settings: Any, run: Mapping[str, Any]) -> dict:
    """Resume an agent run parked on ask_human: the typed answer replaces the parked tool
    result and arrives as a structured user message; the loop then continues."""
    from . import agent_loop

    pending = run.get("pending") or {}
    decision = connection.execute(text(
        "SELECT id, question, status, answer, answered_by, approved_by, resolution_note "
        "FROM decisions WHERE id = :id AND project_id = :p"),
        {"id": str(pending.get("decision_id") or ""), "p": run["project_id"]}).mappings().first()
    if decision is None:
        return {"status": "denied", "run_id": run["id"], "reason": "the question was not found"}
    if decision["status"] not in ("approved", "rejected"):
        return {"status": "denied", "run_id": run["id"], "decision_status": decision["status"],
                "reason": "the question is not answered yet"}
    body = answer_message(dict(decision))
    messages = run["messages"] if isinstance(run.get("messages"), list) else []
    agent_loop._replace_deferred_tool_result(messages, body)
    messages.append({
        "role": "user",
        "content": ("HUMAN ANSWER (structured): " + json.dumps(body)[:6000]
                    + "\nUse this answer and continue the task."),
    })
    transcript = run["transcript"] if isinstance(run.get("transcript"), list) else []
    transcript.append({"step": int(run.get("step") or 0), "tool": TOOL_NAME,
                       "args": pending.get("args"), "result_preview": body["status"],
                       "deferred": False, "resumed": True})
    agent_loop._save_run(
        connection, run["id"], run["project_id"], run.get("task_id"), "running", messages,
        transcript, run.get("usage") or agent_loop._zero_usage(), run.get("finalize_ctx") or {},
        step=int(run.get("step") or 0), pending=None)
    emit_event(connection, run["project_id"], "agent.run_resumed", "human_answer", "agent_run",
               run["id"], {"tool": TOOL_NAME, "decision_id": decision["id"],
                           "status": body["status"]})
    return agent_loop._drive_run(connection, settings, run["id"], None)


ASK_HUMAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Ask a person a TYPED question and wait for the answer: the run pauses and resumes "
            "with the answer as structured data. Use when you need a choice, a confirmation, a "
            "verdict per item, or which of your suggested follow-up tasks to create. Field types: "
            "text, select (options), checkbox, item_verdicts (items [{id,label}], verdicts), "
            "task_suggestions (suggestions [{title, description}] — accepted ones become work "
            "items)."),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "object"}},
                "work_item_id": {"type": "string",
                                 "description": "the work item this is about (optional)"},
                "context": {"type": "string", "description": "one or two sentences of why"},
            },
            "required": ["question", "fields"],
        },
    },
}


__all__ = ["ACTION", "FIELD_TYPES", "FormError", "normalize_form", "validate_answer",
           "ask_human", "record_answer", "on_answered", "queue_agent_resume", "answer_message",
           "resume_with_answer", "ASK_HUMAN_SCHEMA", "TOOL_NAME"]
