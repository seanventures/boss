"""Voice calls use the existing durable model-job authority before provider contact.

A voice receipt groups the reply/judge receipts; it is not a second cost receipt.
Provider exceptions are unknown outcomes. No automatic fallback or paid retry is admitted.
"""

from __future__ import annotations

import json
from typing import Any
from threading import Lock

from sqlalchemy import text

from .architecture_spend import ArchitectureSpend
from .core import emit_event, make_id
from .jobs import enqueue_job, JOB_PRIORITY_INTERACTIVE
from .receipts import ReceiptService
from .typed_decision_spend import TypedDecisionReconciliationRequired, TypedDecisionSpend


class VoiceCallSpend(ArchitectureSpend):
    job_kind = "agent.voice"
    skill_id = "agent.voice"
    preferred_capabilities = ("writing",)
    stage = "agent_voice"

    def _enqueue(self, connection, dedupe, role):
        # A synchronous API call still owns a canonical TTL/heartbeat job. It is not a
        # workflow run, and the process's worker label never supplies user authority.
        return enqueue_job(
            connection,
            self.project_id,
            self.job_kind,
            {
                "agent_id": self.agent_id,
                "actor": self.actor,
                "request_intent": self.run_id,
                "call": self.step_id,
                "role": role,
            },
            priority=JOB_PRIORITY_INTERACTIVE,
            max_attempts=1,
            dedupe_key=f"job:{dedupe}",
            workload_class="owner_sandbox",
            data_boundary="internal",
            on_behalf_of=self.acting,
        )

    def _prepare(self, *args, **kwargs):
        authority = TypedDecisionSpend._prepare(self, *args, **kwargs)
        if authority.outcome == "ready":
            c = args[0]
            c.execute(
                text(
                    "UPDATE model_gateway_invocations SET metadata=(metadata - 'workflow_run_id') "
                    "|| jsonb_build_object('voice_intent',CAST(:intent AS text)) WHERE id=:id"
                ),
                {"intent": self.run_id, "id": authority.invocation_id},
            )
            self.receipts.merge_result(
                c,
                authority.receipt_id,
                {"request_actor": self.actor, "voice_intent": self.run_id, "call": self.step_id},
            )
        self.authority = authority
        return authority

    def _voice_output(self, output):
        output = {
            k: v
            for k, v in output.items()
            if k not in {"architecture_version", "workflow_actor", "workflow_run_id", "step_id"}
        }
        output.update(request_actor=self.actor, voice_intent=self.run_id, call=self.step_id)
        return output

    def _settle_success(self, connection, authority, batch, output):
        return super()._settle_success(connection, authority, batch, self._voice_output(output))

    def _reconcile(self, connection, authority, error):
        # Reuse the shared liability envelope without claiming this voice request
        # belongs to an Architecture workflow. Preserve actual output and usage.
        raw = connection.execute(
            text("SELECT result->'unsettled_architecture_reply' FROM receipts WHERE id=:id"),
            {"id": authority.receipt_id},
        ).scalar_one_or_none()
        if isinstance(raw, dict):
            connection.execute(
                text(
                    "UPDATE receipts SET result=(result - 'unsettled_architecture_reply') "
                    "|| CAST(:voice AS jsonb) WHERE id=:id"
                ),
                {
                    "id": authority.receipt_id,
                    "voice": json.dumps({"unsettled_voice_reply": self._voice_output(raw)}),
                },
            )
        return super()._reconcile(connection, authority, error)

    def _emit(self, connection, event_type, entity_id, data):
        event_type = event_type.replace("typed_decision", "agent_voice").replace(
            "architecture", "agent_voice"
        )
        data = {k: v for k, v in data.items() if k not in {"run_id", "step_id"}}
        data.update(voice_intent=self.run_id, call=self.step_id)
        emit_event(
            connection, self.project_id, event_type, self.actor, "agent_voice_call", entity_id, data
        )

    def _decision(self, connection, question, reason, detail):
        question = question.replace("typed decision", "agent voice").replace(
            "architecture draft", "agent voice"
        )
        reason = reason.replace("typed_decision", "agent_voice").replace(
            "architecture", "agent_voice"
        )
        return TypedDecisionSpend._decision(self, connection, question, reason, detail)


class VoiceSpend:
    def __init__(
        self, engine, settings, project_id, receipt_id, intent, actor, role_key, acting, check_work
    ):
        self.engine = engine
        self.settings = settings
        self.project_id = project_id
        self.receipt_id = receipt_id
        self.intent = intent
        self.actor = actor
        self.role_key = role_key
        self.agent_id = role_key
        self.acting = acting
        self.check_work = check_work
        self._close_lock = Lock()
        self.calls = 0
        self.actual_usd_micros = 0
        self.receipt_ids = []
        self.closed = False
        self.uncertain = False

    def ask(self, *, system: str, prompt: str):
        from . import model_routing

        if self.closed or self.uncertain:
            raise TypedDecisionReconciliationRequired("The voice intent needs reconciliation.")
        number = self.calls + 1
        with self.engine.connect() as c:
            adapter = model_routing.project_chat_adapter(c, self.settings, self.project_id)
            c.rollback()  # no caller transaction is held across provider I/O
        spend = VoiceCallSpend.for_runner(
            self.settings,
            engine=self.engine,
            project_id=self.project_id,
            run_id=self.intent,
            step_id=str(number),
            attempt=1,
            actor=self.actor,
            worker_id=f"voice:{self.intent}",
        )
        spend.agent_id = self.agent_id
        spend.acting = self.acting
        spend.max_cost_usd_micros = int(self.settings.agent_router_budget_usd_micros)
        try:
            _, reply, usage = spend.generate(
                self.settings,
                adapter,
                prompt=prompt,
                system_prompt=system,
                version=1,
                check_work=self.check_work,
            )
        except TypedDecisionReconciliationRequired:
            self.uncertain = True
            authority = getattr(spend, "authority", None)
            if authority and authority.receipt_id:
                self.receipt_ids.append(authority.receipt_id)
            raise
        self.calls += 1
        receipt_id = spend.authority.receipt_id
        with self.engine.connect() as c:
            cash = c.execute(
                text("SELECT actual_usd_micros FROM cost_receipts WHERE receipt_id=:r"),
                {"r": receipt_id},
            ).scalar_one()
        self.receipt_ids.append(receipt_id)
        self.actual_usd_micros += int(cash)
        return reply, usage

    def close(self, error=None):
        with self._close_lock:
            self._close(error)

    def _close(self, error):
        if self.closed:
            return
        # A DB failure remains observable to the caller and leaves the receipt unresolved.
        # Do not set closed until the bookkeeping commit succeeds.
        from .redaction import redact_secrets

        data = {
            "calls": self.calls,
            "receipt_ids": self.receipt_ids,
            "actual_usd_micros": self.actual_usd_micros,
            "reconciliation_required": self.uncertain,
        }
        with self.engine.begin() as c:
            receipts = ReceiptService()
            receipts.merge_result(c, self.receipt_id, data)
            if self.uncertain:
                receipts.merge_result(
                    c,
                    self.receipt_id,
                    {
                        "message": "Provider outcome unknown; reconcile the call receipts before retry."
                    },
                )
            elif error or not self.calls:
                receipts.mark_failed(
                    c,
                    self.receipt_id,
                    {"message": redact_secrets(str(error or "No model call was made."))[:500]},
                )
            else:
                receipts.mark_succeeded(c, self.receipt_id, None, data)
            emit_event(
                c,
                self.project_id,
                "agent_voice.closed",
                self.actor,
                "receipt",
                self.receipt_id,
                data,
            )
        self.closed = True


def open_spend(
    connection: Any,
    engine: Any,
    settings: Any,
    project_id: str,
    effect: str,
    reason: str,
    payload: dict,
    *,
    intent=None,
    protect=None,
    check_work=None,
):
    from .agent_voice import VoiceError
    from .budget import BudgetGovernor
    from boss_engine.identity import lineage

    # Preserve the existing before-headers budget refusal. This quote buys no call;
    # each pinned invocation below reserves and settles its own actual source.
    budget = BudgetGovernor(settings)
    preflight = budget.reserve(
        connection, project_id, "model", int(settings.agent_router_budget_usd_micros), reason, 10
    )
    if preflight.get("status") != "reserved":
        raise VoiceError(
            {
                "code": "budget",
                "message": "The budget refused this model call.",
                "reason": preflight.get("reason") or preflight.get("status"),
            }
        )
    budget.release(
        connection,
        project_id,
        preflight["reservation_id"],
        "Voice preflight only; actual calls reserve pinned model authority.",
    )
    scope = (
        connection.execute(
            text(
                "SELECT w.org_id,p.workspace_id FROM projects p "
                "JOIN workspaces w ON w.id=p.workspace_id WHERE p.id=:p"
            ),
            {"p": project_id},
        )
        .mappings()
        .one()
    )
    intent = intent or make_id("voice-intent")
    actor = str(payload["actor"])
    receipt, created = ReceiptService().create_or_reuse(
        connection,
        scope["org_id"],
        scope["workspace_id"],
        project_id,
        f"agent-voice:{project_id}:{intent}",
        effect,
        {"project_id": project_id, "data_boundary": "internal"},
        payload,
    )
    if not created:
        raise VoiceError(
            {
                "code": "voice_intent_in_progress",
                "message": "The original voice intent already exists; reconcile it before retry.",
            }
        )
    if protect is not None:
        protect(connection)
    # Ambient verified lineage is copied now; the later stream cannot adopt another actor.
    acting = lineage.current() or lineage.Acting(principal_id=actor, org_id=scope["org_id"])
    spend = VoiceSpend(
        engine,
        settings,
        project_id,
        receipt["id"],
        intent,
        actor,
        str(payload["role_key"]),
        acting,
        check_work or (lambda: None),
    )
    spend.agent_id = str(payload.get("agent_id") or payload["role_key"])
    return spend
