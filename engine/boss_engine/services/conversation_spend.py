"""Live conversation tokens on the canonical model-job authority envelope.

One turn is one pinned provider attempt. Delivery loss never proves that the
provider rejected it, and a finished HTTP stream never grants another attempt.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, Callable

from sqlalchemy import text

from .architecture_spend import ArchitectureSpend, settled_text_cost
from .core import emit_event
from .jobs import JOB_PRIORITY_INTERACTIVE, enqueue_job
from .typed_decision_spend import (
    TypedDecisionReconciliationRequired,
    TypedDecisionSpend,
    TypedDecisionSpendError,
)


@dataclass(frozen=True)
class ConversationAuthority:
    principal: str
    acting: Any
    check_work: Callable[[], None]
    protect: Callable[[Any], None] | None = None
    incomplete: Callable[[], None] | None = None


class ConversationCallSpend(ArchitectureSpend):
    job_kind = "agent.conversation"
    skill_id = "agent.conversation"
    preferred_capabilities = ("writing",)
    stage = "converse"
    max_reply_bytes = 1_048_576
    max_usage_bytes = 65_536
    producer_failed = False

    def _enqueue(self, connection, dedupe, role):
        return enqueue_job(
            connection,
            self.project_id,
            self.job_kind,
            {
                "agent_id": self.agent_id,
                "actor": self.actor,
                "conversation_id": self.run_id,
                "turn_id": self.step_id,
                "role": role,
            },
            priority=JOB_PRIORITY_INTERACTIVE,
            max_attempts=1,
            dedupe_key=f"job:{dedupe}",
            workload_class="owner_sandbox",
            data_boundary="internal",
            on_behalf_of=self.acting,
        )

    def _capacity_refused(self, connection, job_id, job_lease_token, error):
        # No invocation or provider contact exists on this branch.
        self.jobs.fail(
            connection,
            self.worker_id,
            job_id,
            job_lease_token,
            {"message": "Conversation model capacity refused."},
            False,
            0,
        )

    def _prepare(self, connection, *args, **kwargs):
        authority = TypedDecisionSpend._prepare(self, connection, *args, **kwargs)
        self.authority = authority
        if authority.outcome == "ready":
            metadata = {
                "conversation_id": self.run_id,
                "turn_id": self.step_id,
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
            changed = connection.execute(
                text(
                    "UPDATE agent_conversation_turns SET receipt_id=:r, payload=payload || "
                    "CAST(:p AS jsonb) WHERE turn_id=:t AND conversation_id=:c AND status='streaming'"
                ),
                {
                    "r": authority.receipt_id,
                    "t": self.step_id,
                    "c": self.run_id,
                    "p": json.dumps(
                        {
                            "reservation_id": authority.reservation_id,
                            "spend_status": "admitted",
                            "request_actor": self.actor,
                        }
                    ),
                },
            )
            if changed.rowcount != 1:
                raise TypedDecisionSpendError("The conversation turn no longer owns this call.")
        return authority

    def _emit(self, connection, event_type, entity_id, data):
        event_type = event_type.replace("typed_decision", "conversation").replace(
            "architecture", "conversation"
        )
        data = {k: v for k, v in data.items() if k not in {"run_id", "step_id"}}
        data.update(conversation_id=self.run_id, turn_id=self.step_id)
        emit_event(
            connection,
            self.project_id,
            event_type,
            self.actor,
            "conversation_turn",
            entity_id,
            data,
        )

    def _decision(self, connection, question, reason, detail):
        decision = TypedDecisionSpend._decision(
            self,
            connection,
            question.replace("typed decision", "conversation turn").replace(
                "Architecture", "Conversation"
            ),
            reason.replace("typed_decision", "conversation").replace(
                "architecture", "conversation"
            ),
            detail,
        )
        if detail.get("receipt_id"):
            self.receipts.merge_result(
                connection, detail["receipt_id"], {"reconciliation_decision_id": decision}
            )
        return decision

    def stream(self, settings, adapter, *, prompt, system_prompt, check_work):
        """Yield live chunks, then a settled reply; preserve unknown liability on close."""
        from .call_deadline import TurnDeadlineExceeded, until
        from .included_spend import source_by_id, source_is_included
        from .model_router.failover import FailoverChatAdapter
        from .openai_adapter import single_attempt

        check_work()
        choice = getattr(adapter, "_boss_choice", None)
        source_id = getattr(choice, "source_id", None) or getattr(adapter, "source_id", None)
        if not source_id:
            raise TypedDecisionSpendError(
                "Conversation needs an identified connected model source."
            )
        if isinstance(adapter, FailoverChatAdapter):
            adapter = adapter._inner  # one pinned provider, never a router fallback
        material = json.dumps({"prompt": prompt, "system_prompt": system_prompt}, sort_keys=True)
        identity = (
            f"conversation:{self.project_id}:{self.run_id}:{self.step_id}:"
            + hashlib.sha256(material.encode()).hexdigest()
        )
        authority = self.commit(
            lambda c: self._prepare(c, settings, {}, {}, identity, "primary", source_id)
        )
        if authority.outcome == "replay":
            check_work()
            output = authority.output or {}
            yield "done", (output.get("text", ""), output.get("usage") or {})
            return
        if authority.outcome != "ready":
            if authority.outcome == "reconcile":
                raise TypedDecisionReconciliationRequired(authority.message)
            raise TypedDecisionSpendError(authority.message)
        try:
            check_work()
            self._ensure_lease(authority)
        except Exception as error:
            reason = error
            try:
                self.commit(lambda c: self._cancel_unused(c, authority, reason))
            except Exception as cleanup_error:
                cleanup_reason = cleanup_error
                self.commit(lambda c: self._reconcile(c, authority, cleanup_reason))
                raise TypedDecisionReconciliationRequired(
                    "Unused conversation authority changed; reconcile its receipt."
                ) from cleanup_error
            raise TypedDecisionSpendError(
                "Conversation authority ended before provider contact."
            ) from error

        # The provider adapters receive the same existing turn deadline. The heartbeat
        # also stops at that bound even if an adapter fails to return promptly.
        deadline = time.monotonic() + max(5, min(600, int(settings.chat_turn_seconds)))
        stop = Event()
        heartbeat_errors = []

        def heartbeat():
            while not stop.wait(max(0.1, min(self.ttl_seconds / 3, deadline - time.monotonic()))):
                try:
                    if time.monotonic() >= deadline:
                        raise TurnDeadlineExceeded("Conversation turn deadline ended.")
                    self.commit(
                        lambda c: self._heartbeat(
                            c,
                            authority.job_id,
                            authority.job_lease_token,
                            authority.lease_id,
                            authority.lease_token,
                        )
                    )
                except Exception as error:
                    heartbeat_errors.append(error)
                    return

        provider_iterator = None

        def pieces():
            nonlocal provider_iterator
            stream = getattr(adapter, "stream_text_response", None)
            if callable(stream):
                provider_iterator = iter(stream(prompt, system_prompt=system_prompt))
                yield from provider_iterator
            else:
                reply = adapter.create_text_response(prompt, system_prompt=system_prompt)
                if isinstance(reply, tuple):
                    yield str(reply[1] if len(reply) > 1 else reply[0] or "")
                    yield reply[2] if len(reply) > 2 and isinstance(reply[2], dict) else {}
                else:
                    yield str(reply or "")

        thread = Thread(target=heartbeat, name="conversation-lease-heartbeat", daemon=True)
        iterator = pieces()
        chunks, usage = [], {}
        reply_bytes = 0
        contacted = False
        settled = False
        thread.start()
        try:
            while True:
                if heartbeat_errors:
                    raise heartbeat_errors[0]
                check_work()
                if time.monotonic() >= deadline:
                    raise TurnDeadlineExceeded("Conversation turn deadline ended.")
                # Starlette advances a sync generator under a fresh copied context.
                # ContextVar tokens must never remain entered across our live yields.
                with single_attempt(), until(deadline):
                    try:
                        contacted = True
                        piece = next(iterator)
                    except StopIteration:
                        break
                if isinstance(piece, dict):
                    if len(json.dumps(piece).encode("utf-8")) > self.max_usage_bytes:
                        raise RuntimeError("Conversation usage evidence exceeded its byte limit.")
                    usage = piece
                else:
                    chunk = str(piece)
                    reply_bytes += len(chunk.encode("utf-8"))
                    if reply_bytes > self.max_reply_bytes:
                        raise RuntimeError("Conversation reply exceeded its byte limit.")
                    chunks.append(chunk)
                    yield "chunk", chunk
            if heartbeat_errors:
                raise heartbeat_errors[0]
            check_work()
            full = "".join(chunks)
            included = self.commit(
                lambda c: source_is_included(source_by_id(c, self.project_id, source_id))
            )
            cost = settled_text_cost(usage, included=included)
            batch = SimpleNamespace(
                cost_usd_micros=cost,
                usage=usage,
                backend=source_id,
                model=usage.get("model") or source_id,
            )
            raw = {
                "text": full,
                "usage": usage,
                "conversation_id": self.run_id,
                "turn_id": self.step_id,
                "request_actor": self.actor,
                "agent_id": self.agent_id,
            }
            self.commit(lambda c: self._settle_success(c, authority, batch, raw))
            settled = True
            yield "done", (full, usage)
        except BaseException as error:
            reason = error
            if not settled:
                if contacted:

                    def retain(c):
                        self.receipts.merge_result(
                            c,
                            authority.receipt_id,
                            {
                                "unsettled_conversation_reply": {
                                    "text": "".join(chunks),
                                    "usage": usage,
                                },
                                "spend_status": "needs_reconciliation",
                            },
                        )
                        self._reconcile(c, authority, reason)

                    self.commit(retain)
                else:
                    self.commit(lambda c: self._cancel_unused(c, authority, reason))
            if isinstance(error, GeneratorExit):
                raise
            if contacted:
                raise TypedDecisionReconciliationRequired(
                    "Conversation outcome needs reconciliation; its receipt and financial hold remain."
                ) from error
            raise TypedDecisionSpendError(
                "Conversation stopped before provider contact."
            ) from error
        finally:
            stop.set()
            thread.join(timeout=max(1, self.ttl_seconds))
            try:
                with single_attempt(), until(deadline):
                    iterator.close()
                    close_provider = getattr(provider_iterator, "close", None)
                    if close_provider:
                        close_provider()
            except BaseException:
                self.producer_failed = True
                raise
