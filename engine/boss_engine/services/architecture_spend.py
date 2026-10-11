"""One architecture version, on the existing durable model-job authority envelope.

Authority commits before submission. A receipt stores the raw reply so loss of the
workflow transaction cannot pay for that version again. Unknown outcomes retain
financial liability and raise a reconciliation decision; they are never free retries.
"""
from __future__ import annotations

import hashlib
import json
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, Callable

from .typed_decision_spend import (
    TypedDecisionSpend, TypedDecisionSpendError, TypedDecisionReconciliationRequired,
    _Authority, _result_of, model_receipt_dedupe,
)


def settled_text_cost(usage: dict, *, included: bool) -> int:
    """The shared strict cost-evidence rule for a pinned text reply."""
    from .openai_adapter import estimate_cost_usd_micros, price_for_model

    token_keys = ("prompt_tokens", "completion_tokens")
    explicit_cost = usage.get("cost_usd_micros")
    known_cost = isinstance(explicit_cost, int) and not isinstance(explicit_cost, bool) \
        and explicit_cost >= 0
    priced_tokens = (usage.get("model") and all(
        isinstance(usage.get(k), int) and not isinstance(usage[k], bool)
        and usage[k] >= 0 for k in token_keys
    ) and all(k not in usage or (isinstance(usage[k], int)
        and not isinstance(usage[k], bool) and usage[k] >= 0)
        for k in ("cache_read_tokens", "cache_write_tokens"))
        and sum(usage[k] for k in token_keys) > 0 and price_for_model(
        str(usage["model"]), provider=str(usage.get("provider") or ""),
        endpoint=str(usage.get("base_url") or ""),
    )[2])
    if not included and not (known_cost or priced_tokens):
        raise RuntimeError("Architecture reply has no usable paid cost/usage evidence.")
    # The shared estimator accepts convertible explicit values. This paid
    # boundary accepts only typed nonnegative costs, and must not let an
    # invalid override replace otherwise valid priced token evidence.
    costing_usage = dict(usage)
    if not known_cost:
        costing_usage.pop("cost_usd_micros", None)
    cost = estimate_cost_usd_micros(costing_usage)
    return cost


class ArchitectureSpend(TypedDecisionSpend):
    job_kind = "architecture.draft"
    agent_id = "chief_architect"
    skill_id = "architecture.draft"
    preferred_capabilities = ("reasoning", "writing")
    stage = "architecture"

    def _ensure_lease(self, authority):
        # Architecture's caller handles known-unused cleanup with both tokens.
        # Keep the inherited typed-decision expiry contract unchanged.
        self.commit(lambda c: self._heartbeat(
            c, authority.job_id, authority.job_lease_token,
            authority.lease_id, authority.lease_token,
        ))

    def _cancel_unused(self, connection, authority, error):
        from .jobs import LeaseNotFoundError
        from .model_gateway import ModelGatewayLeaseError
        from .redaction import redact_secrets
        try:
            # Roll back the unsuccessful ordinary settlement before a fenced expiry
            # cleanup. Database errors cannot leave partial accounting changes.
            with connection.begin_nested():
                self._fail(connection, authority, error,
                           event_type="architecture.cancelled_before_submission")
        except (LeaseNotFoundError, ModelGatewayLeaseError):
            reason = redact_secrets(str(error))[:500]
            self.gateway.abandon_expired_attempt(
                connection, self.project_id, authority.lease_id,
                worker_id=self.worker_id, invocation_id=authority.invocation_id,
                reason=reason, lease_token=authority.lease_token,
                job_lease_token=authority.job_lease_token,
            )
            self.jobs.fail(connection, self.worker_id, authority.job_id,
                           authority.job_lease_token, {"message": reason}, False, 0)
            self._emit(connection, "architecture.cancelled_before_submission",
                       authority.invocation_dedupe, {"receipt_id": authority.receipt_id,
                       "attempt": self.attempt, "message": reason})

    def _prepare(self, connection, settings, binding, state, identity, role, source_id=None):
        authority = super()._prepare(connection, settings, binding, state, identity, role, source_id)
        if authority.outcome == "ready":
            self.receipts.merge_result(connection, authority.receipt_id, {
                "workflow_actor": self.actor, "workflow_run_id": self.run_id,
                "step_id": self.step_id, "agent_id": self.agent_id,
            })
        return authority

    def _reconcile(self, connection, authority, error):
        from .redaction import redact_secrets
        safe = RuntimeError(f"{type(error).__name__}: {redact_secrets(str(error))}")
        return super()._reconcile(connection, authority, safe)

    def _fail(self, connection, authority, error, **kwargs):
        from .redaction import redact_secrets
        safe = RuntimeError(f"{type(error).__name__}: {redact_secrets(str(error))}")
        return super()._fail(connection, authority, safe, **kwargs)

    def _existing(self, connection, identity, dedupe):
        rows = self.receipts.list_by_dedupe_prefix(
            connection, self._scope(connection)["org_id"],
            model_receipt_dedupe(self.project_id, identity),
        )
        # Prefix searches are shared with typed decisions. Architecture only accepts
        # this EXACT version/prompt receipt, never a longer prefix or another revision.
        key = model_receipt_dedupe(self.project_id, dedupe)
        row = next((r for r in rows if r.get("dedupe_key") == key), None)
        if row is None:
            return None
        result = _result_of(row)
        if row.get("status") == "succeeded" and "text" in result:
            return _Authority(outcome="replay", output=result, receipt_id=row["id"],
                              invocation_dedupe=dedupe)
        authority = _Authority(outcome="reconcile", receipt_id=row["id"],
                               invocation_dedupe=dedupe,
                               message="Architecture version needs receipt reconciliation.")
        self._reconcile(connection, authority, RuntimeError(authority.message))
        return authority

    def _emit(self, connection, event_type, entity_id, data):
        return super()._emit(connection, event_type.replace("typed_decision", "architecture"),
                             entity_id, data)

    def _decision(self, connection, question, reason, detail):
        return super()._decision(
            connection, question.replace("typed decision", "architecture draft"),
            reason.replace("typed_decision", "architecture"), detail,
        )

    def generate(self, settings, adapter, *, prompt: str, system_prompt: str,
                 version: int, check_work: Callable[[], None]) -> tuple[str, str, dict]:
        from .included_spend import source_by_id, source_is_included
        from .model_router.failover import FailoverChatAdapter
        from .openai_adapter import single_attempt

        check_work()
        choice = getattr(adapter, "_boss_choice", None)
        source_id = getattr(choice, "source_id", None) or getattr(adapter, "source_id", None)
        if not source_id:
            raise TypedDecisionSpendError(
                "Architecture needs a connected, identified model source for a pinned lease.")
        # Smart fallback is another source/call and needs its own authority. This
        # document version makes one pinned call; failures are visible to the operator.
        if isinstance(adapter, FailoverChatAdapter):
            adapter = adapter._inner
        material = json.dumps({"prompt": prompt, "system_prompt": system_prompt,
                               "version": version}, sort_keys=True)
        digest = hashlib.sha256(material.encode()).hexdigest()
        identity = f"architecture:{self.project_id}:{self.run_id}:{self.step_id}:v{version}:{digest}"
        authority = self.commit(lambda c: self._prepare(
            c, settings, {}, {}, identity, "primary", source_id,
        ))
        if authority.outcome == "replay":
            check_work()
            output = authority.output or {}
            return output.get("provider_ref") or "", output["text"], output.get("usage") or {}
        if authority.outcome != "ready":
            if authority.outcome == "reconcile":
                raise TypedDecisionReconciliationRequired(authority.message)
            raise TypedDecisionSpendError(authority.message)
        try:
            check_work()
            self._ensure_lease(authority)
        except Exception as error:
            reason = error
            # No adapter was contacted. Existing settlement releases only this
            # known unused reservation; an expired lease still fails closed.
            try:
                self.commit(lambda c: self._cancel_unused(c, authority, reason))
            except Exception as cleanup_error:
                cleanup_reason = cleanup_error
                self.commit(lambda c: self._reconcile(c, authority, cleanup_reason))
                raise TypedDecisionReconciliationRequired(
                    "Unused Architecture authority changed; reconcile its receipt."
                ) from cleanup_error
            from .redaction import redact_secrets
            raise TypedDecisionSpendError(redact_secrets(str(error))) from error

        stop = Event()
        heartbeat_errors: list[Exception] = []

        def heartbeat():
            while not stop.wait(max(0.1, self.ttl_seconds / 3)):
                try:
                    self.commit(lambda c: self._heartbeat(
                        c, authority.job_id, authority.job_lease_token,
                        authority.lease_id, authority.lease_token,
                    ))
                except Exception as error:
                    heartbeat_errors.append(error)
                    return

        thread = Thread(target=heartbeat, name="architecture-lease-heartbeat", daemon=True)
        thread.start()
        try:
            with single_attempt():
                provider_ref, written, usage = adapter.create_text_response(
                    prompt, system_prompt=system_prompt,
                )
        except Exception as error:
            reason = error
            # Adapter exceptions alone do not prove the provider rejected a send.
            # Conservative: retain the receipt/hold even for an unclassified failure.
            self.commit(lambda c: self._reconcile(c, authority, reason))
            raise TypedDecisionReconciliationRequired(
                "Architecture provider outcome is unknown; reconcile its receipt before retry."
            ) from error
        finally:
            stop.set()
            thread.join()
        usage = usage if isinstance(usage, dict) else {}
        raw = {"provider_ref": str(provider_ref or ""), "text": str(written or ""),
               "usage": usage, "architecture_version": version,
               "workflow_actor": self.actor, "workflow_run_id": self.run_id,
               "step_id": self.step_id, "agent_id": self.agent_id}
        try:
            if heartbeat_errors:
                raise heartbeat_errors[0]
            check_work()
            # Missing paid usage is not a zero-dollar success. An included source
            # has known zero cash liability; usage still records API-equivalent value.
            included = self.commit(lambda c: source_is_included(
                source_by_id(c, self.project_id, source_id),
            ))
            cost = settled_text_cost(usage, included=included)
            batch = SimpleNamespace(cost_usd_micros=cost, usage=usage,
                                    backend=source_id, model=usage.get("model") or source_id)
            self.commit(lambda c: self._settle_success(c, authority, batch, raw))
        except Exception as error:
            reason = error
            # Preserve received output for reconciliation without certifying a
            # cancelled/stale document or freeing an accepted/unknown liability.
            def retain(c):
                self.receipts.merge_result(c, authority.receipt_id,
                                           {"unsettled_architecture_reply": raw})
                self._reconcile(c, authority, reason)
            self.commit(retain)
            raise TypedDecisionReconciliationRequired(
                "Architecture reply needs reconciliation; its financial hold remains."
            ) from error
        return str(provider_ref or ""), str(written or ""), usage


def generate(connection: Any, settings: Any, project_id: str, *, run_id: str,
             step_id: str, adapter: Any, architect: str, prompt: str, system_prompt: str, version: int,
             check_work: Callable[[], None]) -> tuple[str, str, dict]:
    """Production seam; tests script the provider, never the authority envelope."""
    current = check_work()
    actor = str(current.get("actor") or "").strip()
    if not actor:
        raise TypedDecisionSpendError("Architecture run has no recorded principal.")
    owner = current.get("lease_owner")
    attempt = current.get("attempt")

    def bound_work():
        latest = check_work()
        if latest.get("actor") != actor or latest.get("lease_owner") != owner or \
                latest.get("attempt") != attempt:
            raise TypedDecisionSpendError("Architecture workflow authority changed.")

    spend = ArchitectureSpend.for_runner(
        settings, engine=connection.engine, project_id=project_id, run_id=run_id,
        step_id=step_id, attempt=1, actor=actor,
        worker_id=str(owner or f"architecture:{run_id}:{step_id}:revision"),
    )
    spend.agent_id = architect
    spend.max_cost_usd_micros = max(1, int(settings.agent_turn_budget_usd_micros))
    return spend.generate(settings, adapter, prompt=prompt, system_prompt=system_prompt,
                          version=version, check_work=bound_work)
