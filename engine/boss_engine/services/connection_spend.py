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
        # Every connection event names the provider's real outcome when there is one: the
        # status, its error type and message, the model and whether usage came back.
        outcome = getattr(self, "outcome", None)
        if outcome:
            data = {**data, "outcome": outcome}
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
        self.outcome = None
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
        model = str((self.candidate.get("supported_models") or [""])[0])
        label = provider_label(provider)
        self.outcome = outcome_of(
            proof if not errors else {"ok": None, "error": str(errors[0])},
            model=model, provider=label, credential=self.credential,
        )
        try:
            if errors:
                raise errors[0]
            # HTTP authentication/rate/model rejection is explicit no-completion
            # evidence. A timeout, 5xx, CLI error or malformed success is unknown.
            rejected = proof.get("ok") is False and proof.get("status") in REFUSED_STATUSES
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
                safe_proof["summary"] = self.outcome["summary"]
                safe_proof["checked_at"] = self.commit(
                    lambda c: c.execute(text("SELECT now()")).scalar_one().isoformat()
                )
                self.commit(
                    lambda c: self.receipts.merge_result(
                        c,
                        authority.receipt_id,
                        {"connection_proof": safe_proof, "connection_outcome": self.outcome},
                    )
                )
                return safe_proof
            if proof.get("ok") is not True:
                raise RuntimeError("Connection provider outcome is unknown.")
            cost, basis = cost_evidence(
                proof, included=self.candidate["cost_mode"] in ("subscription", "local_capacity")
            )
            # A KNOWN-GOOD REPLY SETTLES. Without a price for the model the cost is a labelled,
            # conservative estimate ("unpriced"); a hold is only for an UNKNOWN outcome.
            self.outcome = outcome_of(
                proof, model=model, provider=label, credential=self.credential, cost_basis=basis
            )
            safe_proof = _proof_fields(proof, credential=self.credential)
            safe_proof.update(
                receipt_id=authority.receipt_id,
                capacity_group=self.capacity_group,
                cost_basis=basis,
                cost_usd_micros=cost,
                summary=self.outcome["summary"],
            )
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
                        "connection_outcome": self.outcome,
                    },
                )
            )
            return safe_proof
        except Exception as error:
            reason = self._safe_error(error)
            if self.outcome and self.outcome.get("kind") == "replied_ok":
                # A known-good reply whose settlement still failed is not "unknown".
                self.outcome = {**self.outcome, "kind": "unknown",
                                "summary": f"{label} replied OK, but BOSS could not settle its "
                                           f"cost: {str(reason)[:200]}"}

            def held(c):
                self._reconcile(c, authority, reason)
                self.receipts.merge_result(
                    c, authority.receipt_id, {"connection_outcome": self.outcome}
                )

            self.commit(held)
            return {
                "ok": False,
                "state": "failed",
                "receipt_id": authority.receipt_id,
                "error": "Connection proof needs reconciliation; its financial hold remains.",
                "reconciliation_required": True,
                "outcome": self.outcome,
                "summary": (self.outcome or {}).get("summary"),
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
            "provider_error_type",
            "provider_error",
            "usage_present",
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


#: The most one connection proof can use ("Reply with OK", at most 5 output tokens), with room.
#: An OK reply that reports no token counts is estimated as if it used all of it.
PROOF_TOKEN_BOUND = {"prompt_tokens": 64, "completion_tokens": 16}


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def cost_evidence(proof, *, included):
    """(cost in micros, basis) for a reply the provider accepted.

    basis is "reported" (the provider said what it cost), "priced" (tokens x BOSS's price for the
    model), "included" (subscription/local capacity) or "unpriced" (an OK reply BOSS has no price
    for: tokens x the conservative UNPRICED_ESTIMATE_PRICE, or the proof's token bound when it
    reported none). Malformed evidence — a cost or token count that is not a whole non-negative
    number — is not evidence and raises: that reply's outcome is unknown.
    """
    from .openai_adapter import estimate_cost_usd_micros, price_for_model

    usage = dict(proof.get("usage") or {})
    cost = proof.get("cost_usd_micros", usage.get("cost_usd_micros"))
    malformed = cost is not None and not _count(cost)
    if malformed:
        cost = None
    if included:
        return (cost if cost is not None else 0), "included"
    if cost is not None:
        return cost, "reported"
    keys = ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens")
    if any(k in usage and not _count(usage[k]) for k in keys):
        raise RuntimeError("Paid connection reply reported malformed token usage.")
    usage.pop("cost_usd_micros", None)
    tokens = {k: usage[k] for k in keys if k in usage}
    if sum(tokens.values()) > 0 and usage.get("model"):
        known = price_for_model(
            str(usage["model"]),
            provider=str(usage.get("provider") or ""),
            endpoint=str(usage.get("base_url") or ""),
        )[2]
        if known:
            return estimate_cost_usd_micros(usage), "priced"
    if malformed:
        # A cost that is not a number, with no priced tokens to stand in for it, is not a
        # known-good reply: its outcome is unknown.
        raise RuntimeError("Paid connection reply reported a malformed cost.")
    return unpriced_estimate(tokens if sum(tokens.values()) > 0 else None), "unpriced"


def unpriced_estimate(tokens=None):
    """A conservative cost for a reply BOSS has no price for (labelled "unpriced")."""
    from .openai_adapter import CACHE_READ_MULT, CACHE_WRITE_MULT, UNPRICED_ESTIMATE_PRICE

    tokens = tokens or PROOF_TOKEN_BOUND
    inp, out = UNPRICED_ESTIMATE_PRICE
    return int(round(int(tokens.get("prompt_tokens") or 0) * inp
                     + int(tokens.get("completion_tokens") or 0) * out
                     + int(tokens.get("cache_read_tokens") or 0) * inp * CACHE_READ_MULT
                     + int(tokens.get("cache_write_tokens") or 0) * inp * CACHE_WRITE_MULT))


def _actual_cost(proof, *, included):
    return cost_evidence(proof, included=included)[0]


_LABELS = {"anthropic": "Anthropic", "openai": "OpenAI", "openrouter": "OpenRouter",
           "gemini": "Gemini", "xai": "xAI", "custom": "The provider", "ollama": "Ollama",
           "vllm": "The local model", "codex": "Codex", "claude_code": "Claude Code"}


def provider_label(provider: str) -> str:
    name = str(provider or "").removeprefix("setup-")
    return _LABELS.get(name, name or "The provider")


def outcome_of(proof, *, model, provider, credential="", cost_basis=None):
    """What the provider actually said to one proof, redacted, in words and in fields.

    kind: "replied_ok" | "refused" | "unknown". Never the key, never a provider body: only the
    HTTP status, the provider's error type and (redacted, short) message, the model asked for,
    and whether usage came back.
    """
    from .redaction import redact_secrets

    def clean(value, limit=300):
        value = str(value or "")
        if credential:
            value = value.replace(credential, "[REDACTED]")
        return redact_secrets(value)[:limit]

    status = proof.get("status")
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    usage = proof.get("usage") if isinstance(proof.get("usage"), dict) else {}
    usage_present = proof.get("usage_present")
    if not isinstance(usage_present, bool):
        usage_present = any(k in usage for k in ("prompt_tokens", "completion_tokens"))
    outcome = {
        "http_status": status,
        "provider_error_type": clean(proof.get("provider_error_type"), 80),
        "provider_error": clean(proof.get("provider_error") or (
            proof.get("error") if proof.get("ok") is not True else "")),
        "model": clean(model, 200),
        "usage_present": usage_present,
        "tokens": {k: usage[k] for k in ("prompt_tokens", "completion_tokens")
                   if _count(usage.get(k))},
    }
    if proof.get("ok") is True:
        outcome["kind"] = "replied_ok"
        outcome["cost_basis"] = cost_basis
    elif proof.get("ok") is False and status in REFUSED_STATUSES:
        outcome["kind"] = "refused"
    else:
        outcome["kind"] = "unknown"
    outcome["summary"] = describe_outcome(outcome, provider)
    return outcome


#: HTTP statuses that are explicit no-completion evidence (the provider refused the request).
REFUSED_STATUSES = (400, 401, 402, 403, 404, 405, 413, 422, 429)


def describe_outcome(outcome, provider="The provider"):
    """One sentence a person can act on."""
    kind = outcome.get("kind")
    model = outcome.get("model") or "this model"
    status = outcome.get("http_status")
    error_type = str(outcome.get("provider_error_type") or "").lower()
    message = str(outcome.get("provider_error") or "")
    lowered = f"{error_type} {message}".lower()
    if kind == "replied_ok":
        if outcome.get("cost_basis") == "unpriced":
            return (f"{provider} replied OK, but BOSS has no price for {model}; the cost was "
                    "estimated conservatively and labelled unpriced.")
        return f"{provider} replied OK with {model}."
    if kind == "refused":
        if "credit" in lowered or "balance" in lowered or "billing" in lowered or status == 402:
            why = "credit balance too low"
        elif status == 404 or "not_found" in lowered or "model" in lowered and "not" in lowered:
            why = f"model not found ({model})"
        elif status == 401 or "authentication" in lowered:
            why = "the key was not accepted"
        elif status == 403 or "permission" in lowered:
            why = "the key may not use this model"
        elif status == 429 or "rate_limit" in lowered:
            why = "rate limited, try again later"
        else:
            why = message[:160] or f"HTTP {status}"
        return f"{provider} refused: {why}."
    if status:
        detail = f": {message[:160]}" if message else ""
        return (f"{provider} answered HTTP {status}{detail}. BOSS cannot tell whether the "
                "call was billed, so its hold remains.")
    detail = f" ({message[:160]})" if message else ""
    return (f"No reply from {provider}{detail}. BOSS cannot tell whether the call was billed, "
            "so its hold remains.")


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


# ── A held proof, reviewed and released by a person ──────────────────────────────────────────


class HoldRefused(RuntimeError):
    """A release BOSS will not do; the message says why in words."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


#: What a proof held before BOSS kept the provider's outcome said, and what that means.
_LEGACY_UNPRICED = "no usable cost or priced usage evidence"


def _legacy_outcome(c, project_id, receipt_id, provider, model):
    """The outcome of a proof held before outcomes were recorded, read from its own event.

    The old code only reached its "no usable cost or priced usage evidence" error after the
    provider answered OK, so that message IS evidence of a successful, unpriced reply.
    """
    message = c.execute(
        text("SELECT data->>'message' FROM events WHERE project_id=:p "
             "AND event_type='connection.reconciliation_required' "
             "AND data->>'receipt_id'=:r ORDER BY created_at LIMIT 1"),
        {"p": project_id, "r": receipt_id},
    ).scalar() or ""
    outcome = {"http_status": None, "provider_error_type": "", "provider_error": "",
               "model": model, "usage_present": None, "tokens": {}, "recorded": False}
    if _LEGACY_UNPRICED in message:
        outcome.update(kind="replied_ok", cost_basis="unpriced")
        outcome["summary"] = (f"{provider} replied OK, but BOSS had no price for {model} at the "
                              "time, so the proof was held. Releasing it charges a conservative "
                              "unpriced estimate.")
        return outcome
    from .redaction import redact_secrets

    outcome.update(kind="unknown", provider_error=redact_secrets(message)[:300])
    outcome["summary"] = describe_outcome(outcome, provider)
    return outcome


def _hold_rows(c, project_id, receipt_id=None, *, lock=False):
    return c.execute(
        text(
            "SELECT r.id AS receipt_id, r.status, r.created_at, r.result, "
            "i.id AS invocation_id, l.estimated_cost_usd_micros AS held_usd_micros, "
            "l.source_id, s.supported_models "
            "FROM receipts r "
            "JOIN model_gateway_invocations i ON i.receipt_id=r.id AND i.project_id=r.project_id "
            "JOIN model_gateway_leases l ON l.id=i.model_gateway_lease_id "
            "LEFT JOIN entitlement_sources s ON s.project_id=l.project_id "
            "AND s.source_id=l.source_id "
            "WHERE r.project_id=:p AND r.result ? 'connection_fingerprint' "
            + ("AND r.id=:r " if receipt_id else
               "AND r.status='in_progress' AND EXISTS (SELECT 1 FROM decisions d "
               "WHERE d.project_id=r.project_id AND d.status='pending' "
               "AND d.cost_summary->>'receipt_id'=r.id) ")
            + "ORDER BY r.created_at DESC"
            + (" FOR UPDATE OF r" if lock else "")
        ),
        {"p": project_id, "r": receipt_id},
    ).mappings().all()


def _facts(c, project_id, row):
    from .openai_adapter import estimate_cost_usd_micros, price_for_model

    result = _result_of(dict(row))
    provider_key = str(result.get("connection_provider") or "").removeprefix("setup-")
    label = provider_label(provider_key)
    models = row["supported_models"] or []
    if isinstance(models, str):
        models = json.loads(models or "[]")
    model = str((models or [""])[0])
    outcome = result.get("connection_outcome")
    if not isinstance(outcome, dict):
        outcome = _legacy_outcome(c, project_id, row["receipt_id"], label, model)
    model = outcome.get("model") or model
    if outcome.get("kind") == "refused":
        cost, basis = 0, "refused"
    else:
        tokens = {k: v for k, v in (outcome.get("tokens") or {}).items() if _count(v)}
        if tokens and price_for_model(model, provider=provider_key)[2]:
            cost, basis = estimate_cost_usd_micros(
                {**tokens, "model": model, "provider": provider_key}), "priced"
        else:
            cost, basis = unpriced_estimate(tokens or None), "unpriced"
    return {
        "receipt_id": row["receipt_id"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "provider": provider_key,
        "provider_label": label,
        "model": model,
        "actor": result.get("connection_actor"),
        "held_usd_micros": int(row["held_usd_micros"] or 0),
        "outcome": outcome,
        "summary": outcome.get("summary") or describe_outcome(outcome, label),
        "release": {"cost_usd_micros": cost, "basis": basis,
                    "succeeded": outcome.get("kind") != "refused"},
        "invocation_id": row["invocation_id"],
    }


def held_proofs(c, project_id):
    """Connection proofs whose hold waits on a person: one entry per receipt, newest first."""
    facts = [_facts(c, project_id, row) for row in _hold_rows(c, project_id)]
    for fact in facts:
        fact.pop("invocation_id", None)
    return facts


def review_hold(c, project_id, receipt_id):
    rows = _hold_rows(c, project_id, receipt_id)
    if not rows:
        raise HoldRefused("BOSS has no connection proof with this receipt.", 404)
    fact = _facts(c, project_id, rows[0])
    fact.pop("invocation_id", None)
    return fact


def release_hold(c, settings, project_id, receipt_id, *, actor, reason):
    """A person releases a held proof: settle it, close its card, record who and why.

    Cost 0 when the provider refused; otherwise the estimate shown on Review (priced tokens, or
    the conservative unpriced estimate). After this the hold is gone and Connect works again.
    """
    from .core import emit_event
    from .model_gateway import ModelGatewayError, ModelGatewayService
    from .receipts import ReceiptService
    from .typed_decision_spend import resolve_reconciliation_decisions

    reason = " ".join(str(reason or "").split())[:300]
    if not reason:
        raise HoldRefused("Say why the hold is released; it is recorded with your name.", 422)
    rows = _hold_rows(c, project_id, receipt_id, lock=True)
    if not rows:
        raise HoldRefused("BOSS has no connection proof with this receipt.", 404)
    prior = _result_of(dict(rows[0]))
    fact = _facts(c, project_id, rows[0])
    if fact["status"] != "in_progress":
        raise HoldRefused(f"This proof is already settled ({fact['status']}); nothing is held.")
    settle = fact["release"]
    tokens = {k: v for k, v in (fact["outcome"].get("tokens") or {}).items() if _count(v)}
    try:
        with c.begin_nested():
            ModelGatewayService(settings).settle_retained_invocation(
                c, project_id, fact["invocation_id"], actor=actor,
                succeeded=settle["succeeded"],
                provider_ref=(f"{fact['provider']}:{fact['model']}"[:200]
                              if settle["succeeded"] else None),
                usage_units=tokens, actual_cost_usd_micros=settle["cost_usd_micros"],
                error=None if settle["succeeded"] else {"reason": fact["summary"]},
            )
    except (ModelGatewayError, ValueError) as refused:
        raise HoldRefused(f"BOSS could not settle this proof: {refused}") from refused
    released = {"actor": actor, "reason": reason,
                "cost_usd_micros": settle["cost_usd_micros"], "basis": settle["basis"],
                "summary": fact["summary"],
                "at": c.execute(text("SELECT now()")).scalar_one().isoformat()}
    # Settlement rewrites the receipt's result; the proof's own lineage is put back on it.
    ReceiptService().merge_result(c, receipt_id, {
        **prior,
        "connection_hold_released": released,
        "connection_outcome": fact["outcome"],
        "connection_proof": {"ok": False, "state": "released", "receipt_id": receipt_id,
                             "model": fact["model"], "summary": fact["summary"]},
    })
    resolve_reconciliation_decisions(c, project_id, receipt_id,
                                     f"Hold released by {actor}: {reason}")
    # A proof's own card is "connection_reconciliation" (ConnectionSpend._decision).
    c.execute(text("UPDATE decisions SET status='resolved', resolution_note=:note, "
                   "updated_at=clock_timestamp() WHERE project_id=:p AND status='pending' "
                   "AND recommendation->>'action'='connection_reconciliation' "
                   "AND cost_summary->>'receipt_id'=:r"),
              {"p": project_id, "r": receipt_id,
               "note": f"Hold released by {actor}: {reason}"[:500]})
    emit_event(c, project_id, "connection.hold_released", actor, "receipt", receipt_id,
               {"receipt_id": receipt_id, "provider": fact["provider"], "model": fact["model"],
                **released, "outcome": fact["outcome"]})
    fact.pop("invocation_id", None)
    fact["status"] = "succeeded" if settle["succeeded"] else "failed"
    fact["released"] = released
    return fact


def main(argv=None):
    """One-shot repair for a held proof, through the same path as the console's Release.

    python -m boss_engine.services.connection_spend <project> <receipt> --actor ceo \
        --reason "..." [--review]
    """
    import argparse

    from sqlalchemy import create_engine

    from ..config import get_settings

    parser = argparse.ArgumentParser(prog="release-connection-hold")
    parser.add_argument("project_id")
    parser.add_argument("receipt_id")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--review", action="store_true", help="show the facts, change nothing")
    args = parser.parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings.database_url)
    if args.review:
        with engine.begin() as c:
            print(json.dumps(review_hold(c, args.project_id, args.receipt_id), indent=2,
                             default=str))
        return 0
    from .members import MemberDirectoryService

    with engine.begin() as c:
        # The same permission the console's Release asks for.
        MemberDirectoryService(settings).require_member(
            c, args.project_id, args.actor, permission="entitlement_manage")
        print(json.dumps(release_hold(c, settings, args.project_id, args.receipt_id,
                                      actor=args.actor, reason=args.reason),
                         indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
