"""One explicit connection proof on durable model-job authority.

Candidate credentials are encrypted before the lease, restricted to this proof,
never advertised as ready, and never substituted for a working connection. An
unknown provider outcome retains its liability and is not an automatic retry.
"""

from __future__ import annotations

import hashlib
import json
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

from .typed_decision_spend import (
    TypedDecisionSpend,
    TypedDecisionSpendError,
    _Authority,
    _result_of,
    model_receipt_dedupe,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _safe(error: BaseException) -> RuntimeError:
    from .redaction import redact_secrets

    return RuntimeError(f"{type(error).__name__}: {redact_secrets(str(error))}"[:500])


class ConnectionSpend(TypedDecisionSpend):
    job_kind = "connection.proof"
    agent_id = "connection_proof"
    skill_id = "connection.proof"
    preferred_capabilities = ("reasoning",)
    stage = "connection_proof"
    max_cost_usd_micros = 50_000

    def _capacity_refused(self, c, job_id, job_lease_token, error):
        self.capacity_denied = True
        # Model capacity was refused before an invocation or provider submission.
        # Keep the existing budget refusal policy, but do not leave this explicit
        # proof's positively unused job running until its lease expires.
        self.jobs.fail(
            c,
            self.worker_id,
            job_id,
            job_lease_token,
            {"message": str(self._safe_error(error))},
            False,
            0,
        )

    def _ensure_lease(self, authority):
        self.commit(
            lambda c: self._heartbeat(
                c,
                authority.job_id,
                authority.job_lease_token,
                authority.lease_id,
                authority.lease_token,
            )
        )

    def _cancel_unused(self, c, authority, error):
        from .jobs import LeaseNotFoundError
        from .model_gateway import ModelGatewayLeaseError

        try:
            with c.begin_nested():
                self._fail(c, authority, error, event_type="connection.cancelled_before_submission")
        except (LeaseNotFoundError, ModelGatewayLeaseError):
            self.gateway.abandon_expired_attempt(
                c,
                self.project_id,
                authority.lease_id,
                worker_id=self.worker_id,
                invocation_id=authority.invocation_id,
                reason=str(_safe(error)),
                lease_token=authority.lease_token,
                job_lease_token=authority.job_lease_token,
            )
            self.jobs.fail(
                c,
                self.worker_id,
                authority.job_id,
                authority.job_lease_token,
                {"message": str(_safe(error))},
                False,
                0,
            )

    def _safe_error(self, error):
        # Provider bodies may contain the supplied value even if it has no usual
        # token prefix. Mask this attempt's exact credential as well as patterns.
        message = str(error)
        if getattr(self, "credential", ""):
            message = message.replace(self.credential, "[REDACTED]")
        return _safe(RuntimeError(message))

    def _reconcile(self, c, authority, error):
        return super()._reconcile(c, authority, self._safe_error(error))

    def _fail(self, c, authority, error, **kwargs):
        return super()._fail(c, authority, self._safe_error(error), **kwargs)

    def _emit(self, c, event_type, entity_id, data):
        return super()._emit(c, event_type.replace("typed_decision", "connection"), entity_id, data)

    def _decision(self, c, question, reason, detail):
        return super()._decision(
            c,
            question.replace("typed decision", "connection proof"),
            reason.replace("typed_decision", "connection"),
            detail,
        )

    def _existing(self, c, identity, dedupe):
        rows = self.receipts.list_by_dedupe_prefix(
            c, self._scope(c)["org_id"], model_receipt_dedupe(self.project_id, identity)
        )
        key = model_receipt_dedupe(self.project_id, dedupe)
        row = next((r for r in rows if r.get("dedupe_key") == key), None)
        if row is None:
            return None
        result = _result_of(row)
        if result.get("connection_fingerprint") != self.fingerprint:
            self.intent_conflict = True
            return _Authority(
                outcome="refused", message="Connection intent changed; use a new explicit intent."
            )
        proof = result.get("connection_proof")
        if row["status"] in ("succeeded", "failed") and isinstance(proof, dict):
            return _Authority(outcome="replay", output=proof, receipt_id=row["id"])
        authority = _Authority(
            outcome="reconcile",
            receipt_id=row["id"],
            invocation_dedupe=dedupe,
            message="Connection proof needs receipt reconciliation before retry.",
        )
        self._reconcile(c, authority, RuntimeError(authority.message))
        return authority

    def _prepare(self, c, settings, binding, state, identity, role, source_id=None):
        # Serialize exact-intent initialization on the canonical project. The
        # durable receipt, not an in-process HTTP cache, decides replay.
        c.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"connection:{self.project_id}:{identity}"},
        )
        existing = self._existing(c, identity, identity)
        if existing is not None:
            return existing
        from .entitlements import EntitlementBrokerService

        record = {
            **self.candidate,
            "project_id": self.project_id,
            "source_id": source_id,
            "connection_metadata": {
                "proof_only": True,
                "capacity_group": self.capacity_group,
                "proof_actor": self.actor,
            },
        }
        EntitlementBrokerService(settings).upsert_source(c, record, actor=self.actor)
        self.staged = True
        env_name = self.candidate.get("credential_env_name")
        if env_name:
            from .core import make_id
            from .env_refs import env_name_allowed

            if not env_name_allowed(env_name):
                raise TypedDecisionSpendError(
                    "Connection proof cannot reference this host variable."
                )
            ref_id = make_id("sref")
            c.execute(
                text(
                    "INSERT INTO secret_refs(id,project_id,scope_id,ref_kind,ref_key,storage_backend,"
                    "provider,secret_kind,created_by) VALUES(:i,:p,:p,'env',:k,'environment',:v,'api_token',:a)"
                ),
                {
                    "i": ref_id,
                    "p": self.project_id,
                    "k": env_name,
                    "v": self.candidate["provider"],
                    "a": self.actor,
                },
            )
            self.gateway.bind_credential_secret_ref(
                c, self.project_id, source_id, ref_id, actor=self.actor
            )
        elif self.credential:
            self.gateway.upload_credential(
                c,
                self.project_id,
                source_id,
                value=self.credential,
                backend=self.backend,
                actor=self.actor,
            )
        authority = super()._prepare(c, settings, binding, state, identity, role, source_id)
        if authority.outcome == "ready":
            self.receipts.merge_result(
                c,
                authority.receipt_id,
                {
                    "connection_fingerprint": self.fingerprint,
                    "connection_actor": self.actor,
                    "connection_provider": self.provider,
                    "connection_intent": _digest(self.intent),
                    "connection_source_id": source_id,
                },
            )
        return authority

    def prove(
        self,
        settings,
        *,
        provider,
        intent,
        candidate,
        credential="",
        backend="database",
        capacity_group="",
        call,
        before_call=None,
    ):
        self.staged = False
        self.capacity_denied = False
        self.intent_conflict = False
        self.provider, self.intent = provider, intent
        self.candidate, self.credential, self.backend = candidate, credential, backend
        self.capacity_group = capacity_group or f"connection:{provider}"
        # Internal fingerprint checks request consistency without ever persisting
        # plaintext credential or copying it into a receipt/event/error.
        self.fingerprint = _digest(
            {
                "candidate": candidate,
                "credential": credential,
                "actor": self.actor,
                "capacity_group": self.capacity_group,
            }
        )
        identity = f"connection:{self.project_id}:{self.actor}:{_digest(intent)}"
        source_id = "proof-" + _digest(identity)[:32]
        authority = self.commit(
            lambda c: self._prepare(c, settings, {}, {}, identity, "primary", source_id)
        )
        if authority.outcome == "replay":
            return authority.output or {}
        if authority.outcome != "ready":
            if self.staged:
                self.commit(lambda c: _retire_candidate(c, self.project_id, source_id, self.actor))
            return {
                "ok": False,
                "state": "failed",
                "error": authority.message,
                "receipt_id": authority.receipt_id,
                "reconciliation_required": authority.outcome == "reconcile",
                "budget_refused": authority.outcome == "refused" and not (
                    self.capacity_denied or self.intent_conflict
                ),
                "capacity_refused": self.capacity_denied,
                "intent_conflict": self.intent_conflict,
            }
        try:
            if before_call:
                before_call()
            self._ensure_lease(authority)
            bound = self.commit(
                lambda c: self.gateway.resolve_execution_binding(
                    c,
                    self.project_id,
                    authority.lease_id,
                    worker_id=self.worker_id,
                    lease_token=authority.lease_token,
                    job_lease_token=authority.job_lease_token,
                )
            )
            if self.credential and bound.get("credential") != self.credential:
                raise TypedDecisionSpendError(
                    "Connection candidate credential changed before submission."
                )
        except Exception as error:
            reason = self._safe_error(error)
            try:
                self.commit(lambda c: self._cancel_unused(c, authority, reason))
            except Exception as cleanup_error:
                cleanup_reason = self._safe_error(cleanup_error)
                self.commit(lambda c: self._reconcile(c, authority, cleanup_reason))
                return {
                    "ok": False,
                    "state": "failed",
                    "receipt_id": authority.receipt_id,
                    "error": "Unused connection authority changed; reconcile its receipt.",
                    "reconciliation_required": True,
                }
            self.commit(lambda c: _retire_candidate(c, self.project_id, source_id, self.actor))
            return {
                "ok": False,
                "state": "failed",
                "receipt_id": authority.receipt_id,
                "error": str(reason),
            }
        stop = Event()
        errors = []

        def heartbeat():
            while not stop.wait(max(0.1, self.ttl_seconds / 3)):
                try:
                    self._ensure_lease(authority)
                except Exception as error:
                    errors.append(self._safe_error(error))
                    return

        thread = Thread(target=heartbeat, name="connection-proof-heartbeat", daemon=True)
        thread.start()
        proof = {}
        try:
            proof = call(bound)
            if not isinstance(proof, dict):
                raise RuntimeError("Connection proof returned no typed result.")
        except Exception as error:
            errors.append(self._safe_error(error))
        finally:
            stop.set()
            thread.join()
        try:
            if errors:
                raise errors[0]
            # HTTP authentication/rate/model rejection is explicit no-completion
            # evidence. A timeout, 5xx, CLI error or malformed success is unknown.
            rejected = proof.get("ok") is False and proof.get("status") in (
                400,
                401,
                402,
                403,
                404,
                405,
                413,
                422,
                429,
            )
            if rejected:
                self.commit(
                    lambda c: self._fail(
                        c,
                        authority,
                        RuntimeError(
                            str(proof.get("error") or "Provider rejected connection proof.")
                        ),
                        event_type="connection.proof_rejected",
                    )
                )
                safe_proof = _proof_fields(proof, credential=self.credential)
                safe_proof["error"] = f"HTTP {proof['status']}"
                safe_proof["receipt_id"] = authority.receipt_id
                safe_proof["checked_at"] = self.commit(
                    lambda c: c.execute(text("SELECT now()")).scalar_one().isoformat()
                )
                self.commit(
                    lambda c: self.receipts.merge_result(
                        c, authority.receipt_id, {"connection_proof": safe_proof}
                    )
                )
                return safe_proof
            if proof.get("ok") is not True:
                raise RuntimeError("Connection provider outcome is unknown.")
            cost = _actual_cost(
                proof, included=self.candidate["cost_mode"] in ("subscription", "local_capacity")
            )
            safe_proof = _proof_fields(proof, credential=self.credential)
            safe_proof.update(receipt_id=authority.receipt_id, capacity_group=self.capacity_group)
            safe_proof["checked_at"] = self.commit(
                lambda c: c.execute(text("SELECT now()")).scalar_one().isoformat()
            )
            batch = SimpleNamespace(
                cost_usd_micros=cost,
                usage=safe_proof.get("usage") or {},
                backend=provider,
                model=self.candidate["supported_models"][0],
            )
            self.commit(
                lambda c: self._settle_success(
                    c,
                    authority,
                    batch,
                    {
                        "connection_fingerprint": self.fingerprint,
                        "connection_proof": safe_proof,
                        "connection_actor": self.actor,
                    },
                )
            )
            return safe_proof
        except Exception as error:
            reason = self._safe_error(error)
            self.commit(lambda c: self._reconcile(c, authority, reason))
            return {
                "ok": False,
                "state": "failed",
                "receipt_id": authority.receipt_id,
                "error": "Connection proof needs reconciliation; its financial hold remains.",
                "reconciliation_required": True,
            }
        finally:
            # Preserve receipt/hold lineage, not a routable candidate credential.
            self.commit(lambda c: _retire_candidate(c, self.project_id, source_id, self.actor))


def _proof_fields(proof, *, credential=""):
    # Retain the typed probe contract, never an arbitrary provider body or reply.
    from .redaction import redact_secrets

    def scalar(value):
        if isinstance(value, str):
            if credential:
                value = value.replace(credential, "[REDACTED]")
            return redact_secrets(value)[:500]
        return value if value is None or isinstance(value, (int, float, bool)) else None

    fields = {
        k: scalar(v)
        for k, v in proof.items()
        if k
        in (
            "ok",
            "state",
            "status",
            "latency_ms",
            "host",
            "retry_at",
            "model",
            "base_url",
            "skipped",
        )
    }
    usage = proof.get("usage")
    if isinstance(usage, dict):
        fields["usage"] = {
            k: scalar(v)
            for k, v in usage.items()
            if k
            in (
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cost_usd_micros",
                "model",
                "provider",
                "base_url",
            )
        }
    return fields


def _actual_cost(proof, *, included):
    from .openai_adapter import estimate_cost_usd_micros, price_for_model

    usage = dict(proof.get("usage") or {})
    cost = proof.get("cost_usd_micros", usage.get("cost_usd_micros"))
    known = isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0
    if included:
        return cost if known else 0
    if known:
        return cost
    keys = ("prompt_tokens", "completion_tokens")
    priced = (
        usage.get("model")
        and all(
            isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0
            for k in keys
        )
        and sum(usage[k] for k in keys) > 0
        and all(
            k not in usage
            or (isinstance(usage[k], int) and not isinstance(usage[k], bool) and usage[k] >= 0)
            for k in ("cache_read_tokens", "cache_write_tokens")
        )
        and price_for_model(
            str(usage["model"]),
            provider=str(usage.get("provider") or ""),
            endpoint=str(usage.get("base_url") or ""),
        )[2]
    )
    if not priced:
        raise RuntimeError("Paid connection reply has no usable cost or priced usage evidence.")
    usage.pop("cost_usd_micros", None)
    return estimate_cost_usd_micros(usage)


def _retire_candidate(c, project_id, source_id, actor):
    c.execute(
        text(
            "UPDATE secret_refs SET status='revoked', revoked_at=now(), revoked_by=:a "
            "WHERE project_id=:p AND id=(SELECT credential_secret_ref_id FROM "
            "entitlement_sources WHERE project_id=:p AND source_id=:s "
            "AND connection_metadata->>'proof_actor'=:a "
            "AND connection_metadata->>'proof_only'='true')"
        ),
        {"p": project_id, "s": source_id, "a": actor},
    )
    c.execute(
        text(
            "UPDATE entitlement_sources SET status='inactive', connection_status='revoked' "
            "WHERE project_id=:p AND source_id=:s AND "
            "connection_metadata->>'proof_actor'=:a "
            "AND connection_metadata->>'proof_only'='true'"
        ),
        {"p": project_id, "s": source_id, "a": actor},
    )
