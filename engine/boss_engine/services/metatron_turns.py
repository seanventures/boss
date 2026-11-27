"""MT-10: BOSS server-side streaming agent turn with client-side tools, durable SSE events,
per-step BudgetGovernor/ModelGateway/Receipt authority, and pause/resume over tool_results.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping, Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from ..identity import authz, lineage
from ..identity.permissions_gen import P
from ..identity.principal import AuthContext
from ..identity.scope import ResolvedScope
from ..tools.builtin import build_discuss_read_registry
from . import mcp_registry, model_routing, tool_policy_engine
from .agent_service import (
    AgentService,
    AgentThreadNotFoundError,
    AgentThreadOwnershipError,
)
from .architecture_spend import ArchitectureSpend, settled_text_cost
from .core import emit_event, make_id
from .jobs import JOB_PRIORITY_INTERACTIVE, enqueue_job
from .redaction import redact_secrets
from .typed_decision_spend import (
    TypedDecisionReconciliationRequired,
    TypedDecisionSpend,
    TypedDecisionSpendError,
    _Authority,
    _result_of,
)

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_BYTES = 65_536
DEFAULT_TURN_DEADLINE_S = 900
DEFAULT_HEARTBEAT_S = 15.0
TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "cancelled"})
VALID_MODES = frozenset({"read-only", "ask", "risky-only", "act-on-my-behalf"})
READ_ONLY_RISK_CLASSES = frozenset({"read", "read-only", "local-read", "remote-read"})


class MetatronTurnError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            }
        }


class MetatronBudgetError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _TurnWaiter:
    turn_id: str
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    runner_active: bool = False


_LIVE_TURNS_LOCK = threading.Lock()
_LIVE_TURNS: dict[str, _TurnWaiter] = {}


def _waiter_for(turn_id: str) -> _TurnWaiter:
    with _LIVE_TURNS_LOCK:
        waiter = _LIVE_TURNS.get(turn_id)
        if waiter is None:
            waiter = _TurnWaiter(turn_id=turn_id)
            _LIVE_TURNS[turn_id] = waiter
        return waiter


def _cleanup_waiter(turn_id: str) -> None:
    with _LIVE_TURNS_LOCK:
        waiter = _LIVE_TURNS.get(turn_id)
        if waiter is not None and not waiter.runner_active:
            _LIVE_TURNS.pop(turn_id, None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_obj(v) for k, v in value.items()}
    return value


def _sse_frame(turn_id: str, seq: int, event_type: str, payload: Mapping[str, Any]) -> bytes:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"id: {turn_id}:{seq}\nevent: {event_type}\ndata: {data}\n\n".encode("utf-8")


def append_turn_event(connection: Any, turn_id: str, event_type: str, payload: Mapping[str, Any]) -> int:
    """Persist an ordered SSE event row for `turn_id` and return its sequence number."""
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"mturn_ev:{turn_id}"})
    next_seq = int(
        connection.execute(
            text("SELECT COALESCE(MAX(seq), 0) + 1 FROM metatron_turn_events WHERE turn_id = :t"),
            {"t": turn_id},
        ).scalar()
        or 1
    )
    event_id = f"{turn_id}:{next_seq}"
    connection.execute(
        text(
            "INSERT INTO metatron_turn_events (turn_id, seq, event_id, event_type, payload) "
            "VALUES (:t, :s, :eid, :et, CAST(:p AS JSONB))"
        ),
        {
            "t": turn_id,
            "s": next_seq,
            "eid": event_id,
            "et": event_type,
            "p": json.dumps(dict(payload), ensure_ascii=False, default=str),
        },
    )
    return next_seq


def _notify(turn_id: str) -> None:
    waiter = _waiter_for(turn_id)
    with waiter.cond:
        waiter.cond.notify_all()


class MetatronStepSpend(ArchitectureSpend):
    """Per-model-round-trip authority envelope for MT-10 turns and server-side reviews."""

    job_kind = "metatron.turn"
    agent_id = "metatron"
    skill_id = "agent.conversation"
    preferred_capabilities = ("writing",)
    stage = "converse"
    llm_call_recorded = False
    max_reply_bytes = 1_048_576
    max_usage_bytes = 65_536

    def _enqueue(self, connection, dedupe, role):
        return enqueue_job(
            connection,
            self.project_id,
            self.job_kind,
            {
                "agent_id": self.agent_id,
                "actor": self.actor,
                "turn_id": self.run_id,
                "step": self.step_id,
                "role": role,
            },
            priority=JOB_PRIORITY_INTERACTIVE,
            max_attempts=1,
            dedupe_key=f"job:{dedupe}",
            workload_class="owner_sandbox",
            data_boundary="internal",
            on_behalf_of=getattr(self, "acting", None) or None,
        )

    def _capacity_refused(self, connection, job_id, job_lease_token, error):
        self.jobs.fail(
            connection,
            self.worker_id,
            job_id,
            job_lease_token,
            {"message": "Metatron model capacity refused."},
            False,
            0,
        )

    def _prepare(self, connection, *args, **kwargs):
        authority = TypedDecisionSpend._prepare(self, connection, *args, **kwargs)
        self.authority = authority
        if authority.outcome == "ready":
            metadata = {
                "turn_id": self.run_id,
                "step_id": self.step_id,
                "request_actor": self.actor,
                "agent_id": self.agent_id,
            }
            connection.execute(
                text(
                    "UPDATE model_gateway_invocations SET metadata=(metadata-'workflow_run_id') "
                    "|| CAST(:m AS jsonb) WHERE id=:i"
                ),
                {"m": json.dumps(metadata), "i": authority.invocation_id},
            )
            self.receipts.merge_result(connection, authority.receipt_id, metadata)
        return authority

    def _emit(self, connection, event_type, entity_id, data):
        event_type = (
            event_type.replace("typed_decision", "metatron_turn").replace("architecture", "metatron_turn")
        )
        data = {k: v for k, v in data.items() if k not in {"run_id", "step_id"}}
        data.update(turn_id=self.run_id, step=self.step_id)
        emit_event(
            connection,
            self.project_id,
            event_type,
            self.actor,
            "metatron_turn",
            entity_id,
            data,
        )

    def _decision(self, connection, question, reason, detail):
        mapped_reason = (
            "conversation_reconciliation"
            if reason in ("architecture_reconciliation", "typed_decision_reconciliation")
            else reason.replace("typed_decision", "conversation").replace("architecture", "conversation")
        )
        decision = TypedDecisionSpend._decision(
            self,
            connection,
            question.replace("typed decision", "Metatron turn").replace("Architecture", "Metatron"),
            mapped_reason,
            detail,
        )
        if detail.get("receipt_id"):
            self.receipts.merge_result(
                connection, detail["receipt_id"], {"reconciliation_decision_id": decision}
            )
        return decision

    def _existing(self, connection, identity, dedupe):
        rows = self._family(connection, identity)
        done = next(
            (r for r in rows if r.get("status") == "succeeded" and "step_output" in _result_of(r)),
            None,
        )
        if done is not None:
            return _Authority(
                outcome="replay",
                output=_result_of(done),
                receipt_id=done["id"],
                invocation_dedupe=dedupe,
            )
        row = next((r for r in rows if r.get("status") != "failed"), None)
        if row is None:
            return None
        authority = _Authority(
            outcome="reconcile",
            receipt_id=row["id"],
            invocation_dedupe=dedupe,
            message="Metatron turn step needs receipt reconciliation.",
        )
        self._reconcile(connection, authority, RuntimeError("Previous turn step did not settle."))
        return authority

    def run_chat_step(
        self,
        settings: Settings,
        adapter: Any,
        *,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        cancel_event: Optional[threading.Event] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        """Reserve budget/job/lease/receipt before contacting `adapter`, stream or call, and settle."""
        from .call_deadline import TurnDeadlineExceeded, until
        from .included_spend import source_by_id, source_is_included
        from .model_router.failover import FailoverChatAdapter, RecordedChatAdapter
        from .openai_adapter import single_attempt

        choice = getattr(adapter, "_boss_choice", None)
        source_id = getattr(choice, "source_id", None) or getattr(adapter, "source_id", None)
        if not source_id:
            raise TypedDecisionSpendError("Metatron turn needs an identified connected model source.")
        while isinstance(adapter, (FailoverChatAdapter, RecordedChatAdapter)):
            adapter = adapter._inner

        material = json.dumps(
            {"messages": messages, "tools": tools or []}, sort_keys=True, default=str
        )
        identity = (
            f"metatron_turn:{self.project_id}:{self.run_id}:{self.step_id}:"
            + hashlib.sha256(material.encode()).hexdigest()[:32]
        )
        authority = self.commit(
            lambda c: self._prepare(c, settings, {}, {}, identity, "primary", source_id)
        )
        if authority.outcome == "replay":
            out = (authority.output or {}).get("step_output") or {}
            return (
                out.get("message") or {"content": "", "tool_calls": None},
                out.get("usage") or {},
                str(authority.receipt_id or ""),
            )
        if authority.outcome != "ready":
            if authority.outcome == "reconcile" or authority.refusal_kind in (
                "reconciliation_required",
                "legacy_reconciliation_required",
            ):
                raise TypedDecisionReconciliationRequired(authority.message)
            if authority.refusal_kind in (
                "cap_exceeded",
                "headroom",
                "budget_cap",
                "allocation_exceeded",
                "member_monthly_cap",
                "tier2_frozen",
            ):
                raise MetatronBudgetError("budget_exhausted", authority.message)
            raise TypedDecisionSpendError(authority.message)

        try:
            self._ensure_lease(authority)
        except Exception as error:
            reason = error
            try:
                self.commit(lambda c: self._cancel_unused(c, authority, reason))
            except Exception as cleanup_error:
                cleanup_reason = cleanup_error
                self.commit(lambda c: self._reconcile(c, authority, cleanup_reason))
                raise TypedDecisionReconciliationRequired(
                    "Unused turn authority changed; reconcile its receipt."
                ) from cleanup_error
            raise TypedDecisionSpendError("Turn authority ended before provider contact.") from error

        deadline = time.monotonic() + max(5, min(600, int(getattr(settings, "chat_turn_seconds", 300) or 300)))
        stop = threading.Event()
        heartbeat_errors: list[Exception] = []

        def heartbeat() -> None:
            while not stop.wait(max(0.1, min(self.ttl_seconds / 3, deadline - time.monotonic()))):
                try:
                    if time.monotonic() >= deadline:
                        raise TurnDeadlineExceeded("Metatron turn step deadline ended.")
                    self.commit(
                        lambda c: self._heartbeat(
                            c,
                            authority.job_id,
                            authority.job_lease_token,
                            authority.lease_id,
                            authority.lease_token,
                        )
                    )
                except Exception as err:
                    heartbeat_errors.append(err)
                    return

        hb_thread = threading.Thread(target=heartbeat, name="metatron-turn-lease-heartbeat", daemon=True)
        hb_thread.start()

        contacted = False
        settled = False
        text_chunks: list[str] = []
        tool_calls: Optional[list[dict[str, Any]]] = None
        usage: dict[str, Any] = {}
        reply_bytes = 0

        try:
            stream_fn = getattr(adapter, "stream_chat_completion", None)
            if callable(stream_fn):
                iterator = (
                    stream_fn(messages, tools=tools, tool_choice="auto")
                    if tools
                    else stream_fn(messages, tools=None)
                )
                try:
                    while True:
                        if heartbeat_errors:
                            raise heartbeat_errors[0]
                        if cancel_event is not None and cancel_event.is_set():
                            raise TypedDecisionSpendError("Turn cancelled by operator.")
                        if time.monotonic() >= deadline:
                            raise TurnDeadlineExceeded("Metatron turn step deadline ended.")
                        with single_attempt(), until(deadline):
                            try:
                                contacted = True
                                item = next(iterator)
                            except StopIteration:
                                break
                        if isinstance(item, tuple) and len(item) == 2:
                            kind, val = item
                            if kind == "delta":
                                piece = str(val or "")
                                if piece:
                                    reply_bytes += len(piece.encode("utf-8"))
                                    if reply_bytes > self.max_reply_bytes:
                                        raise RuntimeError("Metatron turn reply exceeded 1 MiB limit.")
                                    text_chunks.append(piece)
                                    if on_delta is not None:
                                        on_delta(piece)
                            elif kind == "tool_calls":
                                tool_calls = list(val) if val else None
                            elif kind == "usage" and isinstance(val, dict):
                                if len(json.dumps(val).encode("utf-8")) > self.max_usage_bytes:
                                    raise RuntimeError("Metatron usage evidence exceeded 64 KiB limit.")
                                usage = val
                        elif isinstance(item, dict):
                            if item.get("type") == "delta" and "text" in item:
                                piece = str(item["text"] or "")
                                if piece:
                                    reply_bytes += len(piece.encode("utf-8"))
                                    if reply_bytes > self.max_reply_bytes:
                                        raise RuntimeError("Metatron turn reply exceeded 1 MiB limit.")
                                    text_chunks.append(piece)
                                    if on_delta is not None:
                                        on_delta(piece)
                            elif item.get("type") == "tool_calls":
                                tool_calls = list(item.get("tool_calls") or []) or None
                            elif item.get("type") == "usage":
                                usage = {k: v for k, v in item.items() if k != "type"}
                            else:
                                usage = item
                        elif item is not None:
                            piece = str(item)
                            if piece:
                                reply_bytes += len(piece.encode("utf-8"))
                                if reply_bytes > self.max_reply_bytes:
                                    raise RuntimeError("Metatron turn reply exceeded 1 MiB limit.")
                                text_chunks.append(piece)
                                if on_delta is not None:
                                    on_delta(piece)
                finally:
                    close_fn = getattr(iterator, "close", None)
                    if callable(close_fn):
                        close_fn()
                message: dict[str, Any] = {
                    "content": "".join(text_chunks),
                    "tool_calls": tool_calls,
                }
            else:
                with single_attempt(), until(deadline):
                    contacted = True
                    if tools:
                        message, usage = adapter.create_chat_completion(
                            messages, tools=tools, tool_choice="auto"
                        )
                    else:
                        message, usage = adapter.create_chat_completion(messages, tools=None)
                usage = dict(usage or {})
                content = str((message or {}).get("content") or "")
                if content:
                    if len(content.encode("utf-8")) > self.max_reply_bytes:
                        raise RuntimeError("Metatron turn reply exceeded 1 MiB limit.")
                    text_chunks.append(content)
                    if on_delta is not None:
                        on_delta(content)
                tool_calls = (message or {}).get("tool_calls")
                message = {"content": content, "tool_calls": tool_calls}

            if heartbeat_errors:
                raise heartbeat_errors[0]
            included = self.commit(
                lambda c: source_is_included(source_by_id(c, self.project_id, source_id))
            )
            cost = settled_text_cost(usage, included=included)
            batch = SimpleNamespace(
                cost_usd_micros=cost,
                usage=usage,
                backend=source_id,
                model=usage.get("model") or getattr(choice, "reason", None) or source_id,
            )
            step_output = {"message": message, "usage": usage}
            raw = {
                "step_output": step_output,
                "text": message.get("content") or "",
                "usage": usage,
                "turn_id": self.run_id,
                "step_id": self.step_id,
                "request_actor": self.actor,
            }
            self.commit(lambda c: self._settle_success(c, authority, batch, raw))
            settled = True
            return message, {**usage, "cost_usd_micros": cost}, str(authority.receipt_id or "")
        except BaseException as error:
            reason = error
            if not settled:
                if contacted:

                    def retain(c):
                        self.receipts.merge_result(
                            c,
                            authority.receipt_id,
                            {
                                "unsettled_metatron_reply": {
                                    "text": "".join(text_chunks),
                                    "usage": usage,
                                },
                                "spend_status": "needs_reconciliation",
                            },
                        )
                        self._reconcile(c, authority, reason)

                    self.commit(retain)
                else:
                    self.commit(lambda c: self._cancel_unused(c, authority, reason))
            if contacted:
                raise TypedDecisionReconciliationRequired(
                    "Metatron turn outcome needs reconciliation; its receipt and financial hold remain."
                ) from error
            raise TypedDecisionSpendError(
                f"Metatron turn stopped before provider contact: {redact_secrets(str(error))}"
            ) from error
        finally:
            stop.set()
            hb_thread.join(timeout=max(1, self.ttl_seconds))


def _make_step_spend(
    engine: Any,
    settings: Settings,
    *,
    project_id: str,
    turn_id: str,
    step_index: int,
    actor: str,
    principal_id: str,
    org_id: Optional[str] = None,
    ctx: Any = None,
) -> MetatronStepSpend:
    from .budget import BudgetGovernor
    from .jobs import JobLeaseService
    from .model_gateway import ModelGatewayService
    from .receipts import ReceiptService

    def commit(fn: Callable[[Any], Any]) -> Any:
        with begin(engine) as conn:
            return fn(conn)

    spend = MetatronStepSpend(
        project_id=project_id,
        run_id=turn_id,
        step_id=f"step-{step_index}",
        attempt=1,
        actor=actor,
        worker_id=f"metatron:{turn_id}:step-{step_index}",
        budget=BudgetGovernor(settings),
        jobs=JobLeaseService(settings),
        gateway=ModelGatewayService(settings),
        receipts=ReceiptService(),
        commit=commit,
    )
    if isinstance(ctx, AuthContext):
        spend.acting = lineage.Acting.from_context(ctx)
    else:
        spend.acting = lineage.current() or lineage.Acting(
            principal_id=principal_id, org_id=org_id or ""
        )
    return spend


def _check_principal_permission(ctx: Any, permission: str, project_id: str, connection: Any) -> bool:
    if ctx is None:
        return True
    org_id = ctx.principal.org_id
    decision = authz.check(ctx, permission, ResolvedScope.in_project(org_id, project_id), db=connection)
    return bool(decision.allowed)


def _filter_tools_for_turn(
    connection: Any,
    settings: Settings,
    *,
    project_id: str,
    org_id: Optional[str],
    agent_role: str,
    mode: str,
    client_tools: list[dict[str, Any]],
    ctx: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Filter server tools + client_tools by permissions, mode, and tool_policies_v2."""
    rules = tool_policy_engine.load_rules(connection, project_id, org_id)
    offered_schemas: list[dict[str, Any]] = []
    server_registry_tools: dict[str, Any] = {}
    client_tool_map: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, str]] = []

    reg = build_discuss_read_registry()
    for spec in reg.specs():
        if mode == "read-only" and spec.kind == "write":
            dropped.append({"name": spec.name, "reason": "read_only_mode"})
            continue
        perm = spec.permission
        if perm and not _check_principal_permission(ctx, perm, project_id, connection):
            dropped.append({"name": spec.name, "reason": f"missing_permission:{perm}"})
            continue
        pctx = tool_policy_engine.PolicyContext(
            server=tool_policy_engine.BOSS_SERVER,
            tool=spec.name,
            project=project_id,
            org=org_id,
            role=agent_role or None,
            base="allow",
        )
        verdict = tool_policy_engine.decide(pctx, rules=rules)
        if verdict["decision"] == "deny":
            dropped.append(
                {
                    "name": spec.name,
                    "reason": f"policy:{verdict.get('rule_id') or verdict.get('reason') or 'denied'}",
                }
            )
            continue
        server_registry_tools[spec.name] = spec
        offered_schemas.append(spec.openai())

    for srv in mcp_registry.list_mcp_servers(connection, project_id, enabled_only=True):
        srv_name = str(srv.get("name") or srv.get("id") or "")
        for tool in srv.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            tname = str(tool.get("name") or "").strip()
            if not tname:
                continue
            fqname = f"mcp__{srv_name}__{tname}"
            pctx = tool_policy_engine.PolicyContext(
                server=srv_name,
                tool=tname,
                project=project_id,
                org=org_id,
                server_id=str(srv.get("id") or ""),
                role=agent_role or None,
                base=mcp_registry.tool_policy(srv, tname),
            )
            verdict = tool_policy_engine.decide(pctx, rules=rules)
            if verdict["decision"] == "deny":
                dropped.append(
                    {
                        "name": fqname,
                        "reason": f"policy:{verdict.get('rule_id') or verdict.get('reason') or 'denied'}",
                    }
                )
                continue
            if mode == "read-only" and mcp_registry.tool_tier(srv, tname) >= 1:
                dropped.append({"name": fqname, "reason": "read_only_mode"})
                continue
            server_registry_tools[fqname] = {"mcp_server": srv, "mcp_tool": tname}
            offered_schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": fqname,
                        "description": str(tool.get("description") or tname),
                        "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )

    for ct in client_tools:
        if not isinstance(ct, Mapping):
            continue
        cname = str(ct.get("name") or "").strip()
        if not cname:
            continue
        risk_class = str(ct.get("risk_class") or "read").strip()
        if mode == "read-only" and risk_class not in READ_ONLY_RISK_CLASSES:
            dropped.append({"name": cname, "reason": "read_only_mode"})
            continue
        if cname in server_registry_tools or cname in client_tool_map:
            dropped.append({"name": cname, "reason": "duplicate_tool_name"})
            continue
        pctx = tool_policy_engine.PolicyContext(
            server="metatron",
            tool=cname,
            project=project_id,
            org=org_id,
            role=agent_role or None,
            base="allow",
        )
        verdict = tool_policy_engine.decide(pctx, rules=rules)
        if verdict["decision"] == "deny":
            dropped.append(
                {
                    "name": cname,
                    "reason": f"policy:{verdict.get('rule_id') or verdict.get('reason') or 'denied'}",
                }
            )
            continue
        entry = {
            "name": cname,
            "description": str(ct.get("description") or ""),
            "input_schema": ct.get("input_schema") or {"type": "object", "properties": {}},
            "risk_class": risk_class,
            "exposure": str(ct.get("exposure") or "direct"),
        }
        client_tool_map[cname] = entry
        offered_schemas.append(
            {
                "type": "function",
                "function": {
                    "name": cname,
                    "description": entry["description"],
                    "parameters": entry["input_schema"],
                },
            }
        )

    return offered_schemas, {"registry": reg, "tools": server_registry_tools}, client_tool_map, dropped


def _build_system_prompt(context: Optional[Mapping[str, Any]], mode: str) -> str:
    parts = [
        "You are BOSS (Business Operating System Sentinel) operating through the Metatron CLI.",
        f"Current approval mode: {mode}.",
    ]
    ctx = dict(context or {})
    if ctx.get("cwd_label"):
        parts.append(f"Operator working directory label: {ctx['cwd_label']}.")
    if ctx.get("agents_md"):
        parts.append(f"Workspace AGENTS.md instructions:\n{str(ctx['agents_md'])[:8000]}")
    return "\n\n".join(parts)


def start_turn(
    engine: Any,
    settings: Settings,
    *,
    project_id: str,
    actor: str,
    principal_id: str,
    org_id: Optional[str],
    payload: Mapping[str, Any],
    ctx: Any = None,
    idempotency_key: Optional[str] = None,
    request_hash: Optional[str] = None,
) -> dict[str, Any]:
    """Admit a new Metatron turn, persist thread/messages/turn/turn.started, and return turn state."""
    mode = str(payload.get("mode") or "ask").strip()
    if mode not in VALID_MODES:
        raise MetatronTurnError(422, "invalid_mode", f"Unsupported mode {mode!r}.")
    message = str(payload.get("message") or "").strip()
    if not message:
        raise MetatronTurnError(422, "invalid_message", "message is required.")
    agent_role = str(payload.get("agent") or "").strip().lower()
    thread_id = payload.get("thread_id")
    client_tools = list(payload.get("client_tools") or [])
    context = payload.get("context") if isinstance(payload.get("context"), Mapping) else {}
    tool_deadline_s = int(payload.get("tool_deadline_s") or DEFAULT_TURN_DEADLINE_S)
    tool_deadline_s = max(1, min(DEFAULT_TURN_DEADLINE_S, tool_deadline_s))

    svc = AgentService(settings)
    with begin(engine) as connection:
        org_id = org_id or tool_policy_engine.org_of(connection, project_id)
        try:
            thread = svc.start_or_load_thread(
                connection,
                project_id=project_id,
                actor=actor,
                surface="chat",
                thread_id=str(thread_id) if thread_id else None,
                agent_role=agent_role,
            )
        except AgentThreadNotFoundError as exc:
            raise MetatronTurnError(404, "thread_not_found", str(exc)) from exc
        except AgentThreadOwnershipError as exc:
            raise MetatronTurnError(403, "thread_forbidden", str(exc)) from exc

        history = svc.list_messages(connection, thread["id"])[-40:]
        user_msg = svc.append_message(
            connection,
            thread_id=thread["id"],
            role="user",
            content=message,
            agent_role=agent_role,
        )
        offered_schemas, server_bundle, client_tool_map, dropped = _filter_tools_for_turn(
            connection,
            settings,
            project_id=project_id,
            org_id=org_id,
            agent_role=agent_role,
            mode=mode,
            client_tools=client_tools,
            ctx=ctx,
        )
        try:
            adapter = model_routing.project_chat_adapter(connection, settings, project_id)
        except Exception as exc:  # noqa: BLE001
            adapter = None
            model_name = "unconfigured"
            adapter_error = str(exc)
        else:
            choice = getattr(adapter, "_boss_choice", None)
            model_name = str(
                getattr(choice, "reason", None)
                or getattr(choice, "engine", None)
                or getattr(settings, "openai_model", "gpt-4o")
            )
            adapter_error = ""

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _build_system_prompt(context, mode)}
        ]
        for h in history:
            r = str(h.get("role") or "user")
            if r in ("user", "assistant"):
                messages.append({"role": r, "content": str(h.get("content") or "")})
        messages.append({"role": "user", "content": message})

        turn_id = make_id("mturn")
        offered_names = [
            str((t.get("function") or {}).get("name") or "")
            for t in offered_schemas
            if (t.get("function") or {}).get("name")
        ]
        connection.execute(
            text(
                "INSERT INTO metatron_turns "
                "(id, project_id, thread_id, principal_id, actor, agent_role, mode, status, "
                " idempotency_key, request_hash, user_message_id, model, tools_offered, "
                " tools_dropped, client_tools, messages) "
                "VALUES (:id, :p, :th, :pr, :ac, :ar, :m, 'running', :ik, :rh, :um, :mod, "
                " CAST(:to AS JSONB), CAST(:td AS JSONB), CAST(:ct AS JSONB), CAST(:msgs AS JSONB))"
            ),
            {
                "id": turn_id,
                "p": project_id,
                "th": thread["id"],
                "pr": principal_id,
                "ac": actor,
                "ar": agent_role,
                "m": mode,
                "ik": idempotency_key,
                "rh": request_hash,
                "um": user_msg["id"],
                "mod": model_name,
                "to": json.dumps(offered_names),
                "td": json.dumps(dropped),
                "ct": json.dumps(list(client_tool_map.values())),
                "msgs": json.dumps(messages),
            },
        )
        append_turn_event(
            connection,
            turn_id,
            "turn.started",
            {
                "turn_id": turn_id,
                "thread_id": thread["id"],
                "model": model_name,
                "mode": mode,
                "tools": offered_names,
                "dropped": dropped,
            },
        )

    return {
        "turn_id": turn_id,
        "thread_id": thread["id"],
        "project_id": project_id,
        "org_id": org_id,
        "principal_id": principal_id,
        "actor": actor,
        "agent_role": agent_role,
        "mode": mode,
        "model": model_name,
        "adapter": adapter,
        "adapter_error": adapter_error,
        "offered_schemas": offered_schemas,
        "server_bundle": server_bundle,
        "client_tool_map": client_tool_map,
        "messages": messages,
        "tool_deadline_s": tool_deadline_s,
        "ctx": ctx,
    }


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        except ValueError:
            return {"raw": raw}
    return {}


def _run_turn_loop(
    engine: Any,
    settings: Settings,
    state: dict[str, Any],
    *,
    start_step: int = 1,
) -> None:
    """Execute the turn loop across model steps, server tools, and client-tool pauses."""
    turn_id = state["turn_id"]
    project_id = state["project_id"]
    org_id = state.get("org_id")
    actor = state["actor"]
    principal_id = state["principal_id"]
    agent_role = state.get("agent_role") or ""
    mode = state.get("mode") or "ask"
    thread_id = state["thread_id"]
    offered_schemas = state["offered_schemas"]
    server_bundle = state["server_bundle"]
    client_tool_map = state["client_tool_map"]
    messages: list[dict[str, Any]] = list(state["messages"])
    tool_deadline_s = int(state.get("tool_deadline_s") or DEFAULT_TURN_DEADLINE_S)
    ctx = state.get("ctx")
    waiter = _waiter_for(turn_id)

    with waiter.cond:
        waiter.runner_active = True

    step = start_step
    max_steps = 16
    final_reply = ""
    try:
        while step <= max_steps:
            if waiter.cancel_event.is_set():
                with begin(engine) as c:
                    c.execute(
                        text(
                            "UPDATE metatron_turns SET status = 'cancelled', stop_reason = 'cancelled', "
                            "updated_at = now(), finished_at = now() WHERE id = :t"
                        ),
                        {"t": turn_id},
                    )
                    append_turn_event(
                        c, turn_id, "turn.completed", {"turn_id": turn_id, "stop_reason": "cancelled"}
                    )
                _notify(turn_id)
                return

            with begin(engine) as c:
                adapter = state.get("adapter")
                if adapter is None:
                    try:
                        adapter = model_routing.project_chat_adapter(c, settings, project_id)
                    except Exception as exc:
                        c.execute(
                            text(
                                "UPDATE metatron_turns SET status = 'failed', error_code = 'model_unavailable', "
                                "error_message = :m, retryable = false, updated_at = now(), finished_at = now() "
                                "WHERE id = :t"
                            ),
                            {"t": turn_id, "m": redact_secrets(str(exc))[:500]},
                        )
                        append_turn_event(
                            c,
                            turn_id,
                            "turn.failed",
                            {
                                "turn_id": turn_id,
                                "code": "model_unavailable",
                                "message": redact_secrets(str(exc))[:500],
                                "retryable": False,
                            },
                        )
                        _notify(turn_id)
                        return

            spend = _make_step_spend(
                engine,
                settings,
                project_id=project_id,
                turn_id=turn_id,
                step_index=step,
                actor=actor,
                principal_id=principal_id,
                org_id=org_id,
                ctx=ctx,
            )

            def _emit_delta(piece: str) -> None:
                with begin(engine) as c:
                    append_turn_event(c, turn_id, "text.delta", {"text": piece})
                _notify(turn_id)

            try:
                msg, usage, _receipt_id = spend.run_chat_step(
                    settings,
                    adapter,
                    messages=messages,
                    tools=offered_schemas or None,
                    cancel_event=waiter.cancel_event,
                    on_delta=_emit_delta,
                )
            except MetatronBudgetError as exc:
                with begin(engine) as c:
                    c.execute(
                        text(
                            "UPDATE metatron_turns SET status = 'failed', error_code = :code, "
                            "error_message = :msg, retryable = false, updated_at = now(), finished_at = now() "
                            "WHERE id = :t"
                        ),
                        {"t": turn_id, "code": exc.code, "msg": exc.message[:500]},
                    )
                    append_turn_event(
                        c,
                        turn_id,
                        "turn.failed",
                        {
                            "turn_id": turn_id,
                            "code": exc.code,
                            "message": exc.message[:500],
                            "retryable": False,
                        },
                    )
                _notify(turn_id)
                return
            except TypedDecisionReconciliationRequired as exc:
                with begin(engine) as c:
                    c.execute(
                        text(
                            "UPDATE metatron_turns SET status = 'failed', error_code = 'unresolved_receipt', "
                            "error_message = :msg, retryable = false, updated_at = now(), finished_at = now() "
                            "WHERE id = :t"
                        ),
                        {"t": turn_id, "msg": str(exc)[:500]},
                    )
                    append_turn_event(
                        c,
                        turn_id,
                        "turn.failed",
                        {
                            "turn_id": turn_id,
                            "code": "unresolved_receipt",
                            "message": str(exc)[:500],
                            "retryable": False,
                        },
                    )
                _notify(turn_id)
                return
            except TypedDecisionSpendError as exc:
                if waiter.cancel_event.is_set():
                    with begin(engine) as c:
                        c.execute(
                            text(
                                "UPDATE metatron_turns SET status = 'cancelled', stop_reason = 'cancelled', "
                                "updated_at = now(), finished_at = now() WHERE id = :t"
                            ),
                            {"t": turn_id},
                        )
                        append_turn_event(
                            c, turn_id, "turn.completed", {"turn_id": turn_id, "stop_reason": "cancelled"}
                        )
                    _notify(turn_id)
                    return
                with begin(engine) as c:
                    c.execute(
                        text(
                            "UPDATE metatron_turns SET status = 'failed', error_code = 'provider_error', "
                            "error_message = :msg, retryable = true, updated_at = now(), finished_at = now() "
                            "WHERE id = :t"
                        ),
                        {"t": turn_id, "msg": str(exc)[:500]},
                    )
                    append_turn_event(
                        c,
                        turn_id,
                        "turn.failed",
                        {
                            "turn_id": turn_id,
                            "code": "provider_error",
                            "message": str(exc)[:500],
                            "retryable": True,
                        },
                    )
                _notify(turn_id)
                return

            input_toks = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_toks = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            cost_micros = int(usage.get("cost_usd_micros") or 0)
            with begin(engine) as c:
                append_turn_event(
                    c,
                    turn_id,
                    "usage",
                    {
                        "input_tokens": input_toks,
                        "output_tokens": output_toks,
                        "cost": {
                            "amount": round(cost_micros / 1_000_000, 6),
                            "currency": "USD",
                            "source": str(usage.get("model") or state.get("model") or "boss"),
                        },
                    },
                )
            _notify(turn_id)

            assistant_text = str(msg.get("content") or "")
            if assistant_text:
                final_reply = assistant_text
            tool_calls = list(msg.get("tool_calls") or [])
            if not tool_calls:
                svc = AgentService(settings)
                with begin(engine) as c:
                    reply_msg = svc.append_message(
                        c,
                        thread_id=thread_id,
                        role="assistant",
                        content=final_reply,
                        agent_role=agent_role,
                    )
                    c.execute(
                        text(
                            "UPDATE metatron_turns SET status = 'completed', stop_reason = 'end_turn', "
                            "reply_message_id = :rm, messages = CAST(:msgs AS JSONB), usage = CAST(:u AS JSONB), "
                            "updated_at = now(), finished_at = now() WHERE id = :t"
                        ),
                        {
                            "t": turn_id,
                            "rm": reply_msg["id"],
                            "msgs": json.dumps(messages + [{"role": "assistant", "content": final_reply}]),
                            "u": json.dumps(usage),
                        },
                    )
                    append_turn_event(
                        c, turn_id, "turn.completed", {"turn_id": turn_id, "stop_reason": "end_turn"}
                    )
                _notify(turn_id)
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "tool_calls": [
                        {
                            "id": str(tc.get("id") or f"call_{uuid.uuid4().hex[:10]}"),
                            "type": "function",
                            "function": {
                                "name": str(tc.get("name") or (tc.get("function") or {}).get("name") or ""),
                                "arguments": (
                                    tc.get("arguments")
                                    if isinstance(tc.get("arguments"), str)
                                    else json.dumps(tc.get("arguments") or {})
                                ),
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            pending_client_calls: list[dict[str, Any]] = []
            for tc in tool_calls:
                call_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:10]}")
                fn_block = tc.get("function") if isinstance(tc.get("function"), Mapping) else {}
                tname = str(tc.get("name") or fn_block.get("name") or "")
                raw_args = tc.get("arguments") if "arguments" in tc else fn_block.get("arguments")
                parsed_args = _parse_tool_args(raw_args)

                if tname in server_bundle.get("tools", {}):
                    spec_or_mcp = server_bundle["tools"][tname]
                    with begin(engine) as c:
                        rules = tool_policy_engine.load_rules(c, project_id, org_id)
                        srv_name = (
                            spec_or_mcp["mcp_server"].get("name")
                            if isinstance(spec_or_mcp, dict)
                            else tool_policy_engine.BOSS_SERVER
                        )
                        real_tool = (
                            spec_or_mcp["mcp_tool"] if isinstance(spec_or_mcp, dict) else tname
                        )
                        verdict = tool_policy_engine.decide(
                            tool_policy_engine.PolicyContext(
                                server=str(srv_name),
                                tool=str(real_tool),
                                arguments=parsed_args,
                                project=project_id,
                                org=org_id,
                                role=agent_role or None,
                                base="allow",
                            ),
                            rules=rules,
                        )
                        if verdict["decision"] == "deny":
                            append_turn_event(
                                c,
                                turn_id,
                                "policy.denied",
                                {
                                    "call_id": call_id,
                                    "reason": verdict.get("explanation") or "policy_denied",
                                    "rule_id": verdict.get("rule_id"),
                                },
                            )
                            _notify(turn_id)
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": json.dumps(
                                        {"ok": False, "error": "policy_denied", "reason": verdict.get("explanation")}
                                    ),
                                }
                            )
                            continue
                        append_turn_event(
                            c,
                            turn_id,
                            "server_tool.started",
                            {"call_id": call_id, "name": tname, "summary": tname, "ok": True},
                        )
                    _notify(turn_id)

                    if isinstance(spec_or_mcp, dict):
                        with begin(engine) as c:
                            try:
                                result = mcp_registry.call_tool_with_receipt(
                                    c,
                                    settings,
                                    project_id=project_id,
                                    server_id=spec_or_mcp["mcp_server"]["id"],
                                    tool=spec_or_mcp["mcp_tool"],
                                    arguments=parsed_args,
                                    actor=actor,
                                )
                            except Exception as exc:  # noqa: BLE001
                                result = {"ok": False, "error": redact_secrets(str(exc))}
                    else:
                        reg = server_bundle["registry"]
                        with begin(engine) as c:
                            try:
                                result = reg.invoke(
                                    tname,
                                    SimpleNamespace(db=c, settings=settings, durable_workspace_configured=True),
                                    project_id=project_id,
                                    actor=actor,
                                    args=parsed_args,
                                    auth=ctx,
                                )
                            except Exception as exc:  # noqa: BLE001
                                result = {"ok": False, "error": redact_secrets(str(exc))}

                    ok_flag = bool((result or {}).get("ok", True))
                    with begin(engine) as c:
                        append_turn_event(
                            c,
                            turn_id,
                            "server_tool.finished",
                            {
                                "call_id": call_id,
                                "name": tname,
                                "summary": f"{tname}: {'ok' if ok_flag else 'error'}",
                                "ok": ok_flag,
                            },
                        )
                    _notify(turn_id)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(_redact_obj(result), default=str),
                        }
                    )
                elif tname in client_tool_map:
                    ct_entry = client_tool_map[tname]
                    with begin(engine) as c:
                        rules = tool_policy_engine.load_rules(c, project_id, org_id)
                        verdict = tool_policy_engine.decide(
                            tool_policy_engine.PolicyContext(
                                server="metatron",
                                tool=tname,
                                arguments=parsed_args,
                                project=project_id,
                                org=org_id,
                                role=agent_role or None,
                                base="allow",
                            ),
                            rules=rules,
                        )
                        if verdict["decision"] == "deny" or (
                            mode == "read-only" and ct_entry["risk_class"] not in READ_ONLY_RISK_CLASSES
                        ):
                            append_turn_event(
                                c,
                                turn_id,
                                "policy.denied",
                                {
                                    "call_id": call_id,
                                    "reason": verdict.get("explanation") or "policy_denied",
                                    "rule_id": verdict.get("rule_id"),
                                },
                            )
                            _notify(turn_id)
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": json.dumps(
                                        {"ok": False, "error": "policy_denied", "reason": verdict.get("explanation")}
                                    ),
                                }
                            )
                            continue
                    pending_client_calls.append(
                        {
                            "call_id": call_id,
                            "name": tname,
                            "arguments": parsed_args,
                            "risk_hint": ct_entry["risk_class"],
                            "deadline_s": tool_deadline_s,
                        }
                    )
                else:
                    with begin(engine) as c:
                        append_turn_event(
                            c,
                            turn_id,
                            "policy.denied",
                            {"call_id": call_id, "reason": "tool_not_offered", "rule_id": None},
                        )
                    _notify(turn_id)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps({"ok": False, "error": "tool_not_offered"}),
                        }
                    )

            if pending_client_calls:
                deadline_at = _now() + timedelta(seconds=tool_deadline_s)
                with begin(engine) as c:
                    c.execute(
                        text(
                            "UPDATE metatron_turns SET status = 'awaiting_tool_result', "
                            "pending_tool_calls = CAST(:ptc AS JSONB), messages = CAST(:msgs AS JSONB), "
                            "tool_deadline_at = :dl, updated_at = now() WHERE id = :t"
                        ),
                        {
                            "t": turn_id,
                            "ptc": json.dumps(pending_client_calls),
                            "msgs": json.dumps(messages),
                            "dl": deadline_at,
                        },
                    )
                    for pc in pending_client_calls:
                        append_turn_event(c, turn_id, "tool_call", pc)
                _notify(turn_id)

                expected_ids = [pc["call_id"] for pc in pending_client_calls]
                collected: dict[str, dict[str, Any]] = {}
                while True:
                    if waiter.cancel_event.is_set():
                        with begin(engine) as c:
                            c.execute(
                                text(
                                    "UPDATE metatron_turns SET status = 'cancelled', stop_reason = 'cancelled', "
                                    "pending_tool_calls = '[]'::jsonb, updated_at = now(), finished_at = now() "
                                    "WHERE id = :t"
                                ),
                                {"t": turn_id},
                            )
                            append_turn_event(
                                c,
                                turn_id,
                                "turn.completed",
                                {"turn_id": turn_id, "stop_reason": "cancelled"},
                            )
                        _notify(turn_id)
                        return

                    with begin(engine) as c:
                        rows = c.execute(
                            text(
                                "SELECT call_id, status, output, decision FROM metatron_turn_tool_results "
                                "WHERE turn_id = :t AND call_id = ANY(:ids)"
                            ),
                            {"t": turn_id, "ids": expected_ids},
                        ).mappings().all()
                        for r in rows:
                            collected[r["call_id"]] = {
                                "status": r["status"],
                                "output": _loads(r["output"], {}),
                                "decision": _loads(r["decision"], {}),
                            }
                    if len(collected) >= len(expected_ids):
                        break
                    remaining_s = (deadline_at - _now()).total_seconds()
                    if remaining_s <= 0:
                        with begin(engine) as c:
                            c.execute(
                                text(
                                    "UPDATE metatron_turns SET status = 'failed', error_code = 'tool_result_timeout', "
                                    "error_message = 'Timed out waiting for client tool result.', "
                                    "pending_tool_calls = '[]'::jsonb, retryable = false, "
                                    "updated_at = now(), finished_at = now() WHERE id = :t"
                                ),
                                {"t": turn_id},
                            )
                            append_turn_event(
                                c,
                                turn_id,
                                "turn.failed",
                                {
                                    "turn_id": turn_id,
                                    "code": "tool_result_timeout",
                                    "message": "Timed out waiting for client tool result.",
                                    "retryable": False,
                                },
                            )
                        _notify(turn_id)
                        return
                    with waiter.cond:
                        waiter.cond.wait(timeout=min(1.0, max(0.05, remaining_s)))

                for pc in pending_client_calls:
                    res = collected[pc["call_id"]]
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": pc["call_id"],
                            "content": json.dumps(
                                _redact_obj(
                                    {
                                        "status": res["status"],
                                        "output": res["output"],
                                        "decision": res["decision"],
                                    }
                                ),
                                default=str,
                            ),
                        }
                    )
                with begin(engine) as c:
                    cur_status = c.execute(
                        text("SELECT status FROM metatron_turns WHERE id = :t FOR UPDATE"),
                        {"t": turn_id},
                    ).scalar()
                    if cur_status != "awaiting_tool_result":
                        return
                    c.execute(
                        text(
                            "UPDATE metatron_turns SET status = 'running', pending_tool_calls = '[]'::jsonb, "
                            "messages = CAST(:msgs AS JSONB), tool_deadline_at = NULL, updated_at = now() "
                            "WHERE id = :t AND status = 'awaiting_tool_result'"
                        ),
                        {"t": turn_id, "msgs": json.dumps(messages)},
                    )

            step += 1

        with begin(engine) as c:
            c.execute(
                text(
                    "UPDATE metatron_turns SET status = 'completed', stop_reason = 'max_tokens', "
                    "updated_at = now(), finished_at = now() WHERE id = :t"
                ),
                {"t": turn_id},
            )
            append_turn_event(
                c, turn_id, "turn.completed", {"turn_id": turn_id, "stop_reason": "max_tokens"}
            )
        _notify(turn_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("metatron turn %s crashed", turn_id)
        with begin(engine) as c:
            c.execute(
                text(
                    "UPDATE metatron_turns SET status = 'failed', error_code = 'internal_error', "
                    "error_message = :msg, retryable = false, updated_at = now(), finished_at = now() "
                    "WHERE id = :t AND status NOT IN ('completed', 'failed', 'cancelled')"
                ),
                {"t": turn_id, "msg": redact_secrets(str(exc))[:500]},
            )
            append_turn_event(
                c,
                turn_id,
                "turn.failed",
                {
                    "turn_id": turn_id,
                    "code": "internal_error",
                    "message": redact_secrets(str(exc))[:500],
                    "retryable": False,
                },
            )
        _notify(turn_id)
    finally:
        with waiter.cond:
            waiter.runner_active = False
            waiter.cond.notify_all()
        _cleanup_waiter(turn_id)


def launch_turn_runner(engine: Any, settings: Settings, state: dict[str, Any]) -> threading.Thread:
    waiter = _waiter_for(state["turn_id"])
    with waiter.cond:
        waiter.runner_active = True
    thread = threading.Thread(
        target=_run_turn_loop,
        args=(engine, settings, state),
        name=f"metatron-turn-{state['turn_id']}",
        daemon=True,
    )
    thread.start()
    return thread


def stream_turn_events(
    engine: Any,
    turn_id: str,
    *,
    after_seq: int = 0,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    on_complete: Optional[Callable[[], None]] = None,
) -> Iterator[bytes]:
    """Yield persisted + live SSE frames for `turn_id` starting after `after_seq`."""
    cursor = max(0, int(after_seq))
    last_emit = time.monotonic()
    waiter = _waiter_for(turn_id)
    while True:
        with begin(engine) as c:
            row = c.execute(
                text("SELECT status FROM metatron_turns WHERE id = :t"), {"t": turn_id}
            ).mappings().first()
            events = c.execute(
                text(
                    "SELECT seq, event_type, payload FROM metatron_turn_events "
                    "WHERE turn_id = :t AND seq > :after ORDER BY seq ASC"
                ),
                {"t": turn_id, "after": cursor},
            ).mappings().all()

        saw_terminal = False
        for ev in events:
            cursor = int(ev["seq"])
            etype = str(ev["event_type"])
            payload = _loads(ev["payload"], {})
            yield _sse_frame(turn_id, cursor, etype, payload)
            last_emit = time.monotonic()
            if etype in ("turn.completed", "turn.failed"):
                saw_terminal = True

        status = str(row["status"]) if row else "failed"
        if saw_terminal or (status in TERMINAL_TURN_STATUSES and not events):
            if on_complete is not None:
                on_complete()
            return

        now = time.monotonic()
        if now - last_emit >= heartbeat_s:
            yield b": ping\n\n"
            last_emit = now

        wait_for = max(0.05, min(1.0, heartbeat_s - (time.monotonic() - last_emit)))
        with waiter.cond:
            waiter.cond.wait(timeout=wait_for)


def load_turn_for_caller(
    connection: Any,
    turn_id: str,
    *,
    ctx: Any,
    enforced: bool,
) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM metatron_turns WHERE id = :t"), {"t": turn_id}
    ).mappings().first()
    if not row:
        raise MetatronTurnError(404, "turn_not_found", f"Turn {turn_id} was not found.")
    turn = dict(row)
    if ctx is not None:
        if turn["principal_id"] != str(ctx.principal.id):
            raise MetatronTurnError(403, "forbidden", "Turn belongs to a different principal.")
        decision = authz.check(
            ctx,
            P.ASSISTANT_CHAT,
            ResolvedScope.in_project(ctx.principal.org_id, turn["project_id"]),
            db=connection,
        )
        if enforced and not decision.allowed:
            raise MetatronTurnError(decision.status, "forbidden", "Missing assistant:chat permission.")
    return turn


def submit_tool_result(
    engine: Any,
    settings: Settings,
    *,
    turn_id: str,
    call_id: str,
    status: str,
    output: Any,
    decision: Any,
    ctx: Any,
    enforced: bool,
) -> dict[str, Any]:
    if status not in ("ok", "error", "denied", "cancelled"):
        raise MetatronTurnError(422, "invalid_status", f"Invalid tool result status {status!r}.")
    serialized = json.dumps(
        {
            "output": output if output is not None else {},
            "decision": decision if decision is not None else {},
        },
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    if len(serialized.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
        raise MetatronTurnError(
            413,
            "tool_result_too_large",
            f"Tool result payload exceeds {MAX_TOOL_RESULT_BYTES} bytes.",
        )

    redacted_output = _redact_obj(output if isinstance(output, Mapping) else {"value": output})
    redacted_decision = _redact_obj(decision if isinstance(decision, Mapping) else {})

    with begin(engine) as c:
        turn = load_turn_for_caller(c, turn_id, ctx=ctx, enforced=enforced)
        if turn["status"] in TERMINAL_TURN_STATUSES:
            raise MetatronTurnError(409, "turn_already_finished", f"Turn {turn_id} is already {turn['status']}.")
        pending = _loads(turn.get("pending_tool_calls"), []) or []
        pending_ids = {str(p.get("call_id") or "") for p in pending if isinstance(p, Mapping)}
        if pending_ids and call_id not in pending_ids:
            raise MetatronTurnError(409, "unexpected_call_id", f"Call {call_id!r} is not pending on {turn_id}.")
        c.execute(
            text(
                "INSERT INTO metatron_turn_tool_results (turn_id, call_id, status, output, decision) "
                "VALUES (:t, :cid, :st, CAST(:out AS JSONB), CAST(:dec AS JSONB)) "
                "ON CONFLICT (turn_id, call_id) DO NOTHING"
            ),
            {
                "t": turn_id,
                "cid": call_id,
                "st": status,
                "out": json.dumps(redacted_output, ensure_ascii=False, default=str),
                "dec": json.dumps(redacted_decision, ensure_ascii=False, default=str),
            },
        )
        c.execute(
            text("UPDATE metatron_turns SET heartbeat_at = now(), updated_at = now() WHERE id = :t"),
            {"t": turn_id},
        )
        all_done = False
        if pending_ids:
            got_count = int(
                c.execute(
                    text(
                        "SELECT count(*) FROM metatron_turn_tool_results "
                        "WHERE turn_id = :t AND call_id = ANY(:ids)"
                    ),
                    {"t": turn_id, "ids": sorted(pending_ids)},
                ).scalar()
                or 0
            )
            all_done = got_count >= len(pending_ids)

    _notify(turn_id)

    # If the original runner is gone (e.g., process restart) and all pending results are now present,
    # resume the turn in the background so GET /events subscribers receive completion.
    waiter = _waiter_for(turn_id)
    with waiter.cond:
        active = waiter.runner_active
    if all_done and not active:
        _resume_after_restart(engine, settings, turn_id, ctx)

    return {"accepted": True, "turn_id": turn_id, "call_id": call_id}


def _resume_after_restart(engine: Any, settings: Settings, turn_id: str, ctx: Any) -> None:
    with begin(engine) as c:
        row = c.execute(
            text("SELECT * FROM metatron_turns WHERE id = :t FOR UPDATE"), {"t": turn_id}
        ).mappings().first()
        if not row or row["status"] != "awaiting_tool_result":
            return
        pending = _loads(row["pending_tool_calls"], []) or []
        messages = _loads(row["messages"], []) or []
        expected_ids = [str(p["call_id"]) for p in pending if isinstance(p, Mapping)]
        results = {
            r["call_id"]: r
            for r in c.execute(
                text(
                    "SELECT call_id, status, output, decision FROM metatron_turn_tool_results "
                    "WHERE turn_id = :t AND call_id = ANY(:ids)"
                ),
                {"t": turn_id, "ids": expected_ids},
            ).mappings().all()
        }
        if len(results) < len(expected_ids):
            return
        for pc in pending:
            cid = str(pc["call_id"])
            res = results[cid]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": cid,
                    "content": json.dumps(
                        _redact_obj(
                            {
                                "status": res["status"],
                                "output": _loads(res["output"], {}),
                                "decision": _loads(res["decision"], {}),
                            }
                        ),
                        default=str,
                    ),
                }
            )
        c.execute(
            text(
                "UPDATE metatron_turns SET status = 'running', pending_tool_calls = '[]'::jsonb, "
                "messages = CAST(:msgs AS JSONB), tool_deadline_at = NULL, updated_at = now() WHERE id = :t"
            ),
            {"t": turn_id, "msgs": json.dumps(messages)},
        )
        resolved_org_id = (
            ctx.principal.org_id if ctx is not None else None
        ) or tool_policy_engine.org_of(c, row["project_id"])
        offered_schemas, server_bundle, client_tool_map, _ = _filter_tools_for_turn(
            c,
            settings,
            project_id=row["project_id"],
            org_id=resolved_org_id,
            agent_role=row["agent_role"],
            mode=row["mode"],
            client_tools=_loads(row["client_tools"], []) or [],
            ctx=ctx,
        )
        state = {
            "turn_id": turn_id,
            "thread_id": row["thread_id"],
            "project_id": row["project_id"],
            "org_id": resolved_org_id,
            "principal_id": row["principal_id"],
            "actor": row["actor"],
            "agent_role": row["agent_role"],
            "mode": row["mode"],
            "model": row["model"],
            "adapter": None,
            "offered_schemas": offered_schemas,
            "server_bundle": server_bundle,
            "client_tool_map": client_tool_map,
            "messages": messages,
            "tool_deadline_s": DEFAULT_TURN_DEADLINE_S,
            "ctx": ctx,
        }
    launch_turn_runner(engine, settings, state)


def cancel_turn(
    engine: Any,
    *,
    turn_id: str,
    ctx: Any,
    enforced: bool,
) -> dict[str, Any]:
    with begin(engine) as c:
        turn = load_turn_for_caller(c, turn_id, ctx=ctx, enforced=enforced)
        if turn["status"] in TERMINAL_TURN_STATUSES:
            return {"turn_id": turn_id, "status": turn["status"]}
        c.execute(
            text("UPDATE metatron_turns SET cancel_requested_at = now(), updated_at = now() WHERE id = :t"),
            {"t": turn_id},
        )
    waiter = _waiter_for(turn_id)
    with waiter.cond:
        waiter.cancel_event.set()
        active = waiter.runner_active
        waiter.cond.notify_all()

    if not active:
        with begin(engine) as c:
            updated = c.execute(
                text(
                    "UPDATE metatron_turns SET status = 'cancelled', stop_reason = 'cancelled', "
                    "pending_tool_calls = '[]'::jsonb, updated_at = now(), finished_at = now() "
                    "WHERE id = :t AND status NOT IN ('completed', 'failed', 'cancelled') RETURNING status"
                ),
                {"t": turn_id},
            ).scalar()
            if updated:
                append_turn_event(
                    c, turn_id, "turn.completed", {"turn_id": turn_id, "stop_reason": "cancelled"}
                )
        _notify(turn_id)
        return {"turn_id": turn_id, "status": "cancelled"}
    return {"turn_id": turn_id, "status": "cancelling"}
