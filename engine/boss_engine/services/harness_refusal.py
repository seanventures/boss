"""A RUN'S ENGINE WAS REFUSED ITS LOGIN: say so where the operator looks (BOSSAPP-1347).

A harness verdict of kind auth_refused used to end as a failed run only. Setup kept the last
proof's green "Working" (the proof reads the control plane's own login, the run reads its own
copy). `note` is called from `harness_failures.trip`, the one place every harness refusal passes
(a build, a review seat, a workflow step, a discovery), and does two things, once per refusal,
never raising:
- the subscription source is marked rejected (the same mark the keep-alive and the governed
  proof write), so the Setup row, pill and the router's headroom read "Sign-in expired";
- ONE decision card (the keep-alive's own `sign_in_to_subscription_again`, deduped per source,
  so a dead login is one card whichever path saw it first) says to reconnect and names another
  connected engine as an option the person can pin for the job.

ONLY A REFUSAL FROM THE PROVIDER'S OWN TRANSPORT COUNTS. An app's own "expected 200, got 401" in
an agent's summary classifies as auth_refused too (model_failures._HTTP_401), and must never flip
a subscription: `is_provider_refusal` accepts the CLI's own transport / login words only.

Accounts: a refused pool account ("pool:<member id>" on the verdict) is already taken out of
rotation by subscription_pools.mark_needs_reauth; the engine's default source is not touched for
it, and the card is keyed by that account.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from . import core, model_failures
from .decision_actions import OPEN_STATUSES

_log = logging.getLogger("boss.harness_refusal")

#: The existing keep-alive card: one action for "this subscription's login is dead".
CARD_ACTION = "sign_in_to_subscription_again"

#: What the provider's own CLI says on its STDERR when its login is refused. Deliberately narrow:
#: transport words only. Generic "failed to authenticate" / "authentication_error" are an app's
#: words as often as a provider's, so they count for nothing here.
_PROVIDER_TRANSPORT = re.compile(
    r"codex_api::|responses_websocket|invalidated oauth token|please run /login|"
    r"(?:api\.openai\.com|api\.anthropic\.com|chatgpt\.com)[^\n]{0,120}\b401\b|"
    r"\b401\b[^\n]{0,120}(?:api\.openai\.com|api\.anthropic\.com|chatgpt\.com)",
    re.I)


def is_provider_refusal(verdict: Any) -> bool:
    """Only the CLI's own stderr (`transport_text`) is read — never the agent's summary or the
    verdict's message, which can quote an app's 401 or be cut before the real line."""
    return bool(_PROVIDER_TRANSPORT.search(getattr(verdict, "transport_text", "") or ""))


def source_for(engine: str, settings: Any) -> Optional[str]:
    from .cli_harness import load_specs

    spec = load_specs(settings).get(engine)
    return (spec.entitlement_source_id or None) if spec is not None else None


def fallback_engine(connection, project_id: str, refused: str, settings: Any) -> Optional[str]:
    """A connected, working engine other than `refused`, or None."""
    try:
        from .setup_service import connected_engines

        found = connected_engines(connection, settings, project_id)
    except Exception:  # noqa: BLE001 — no fallback is an answer, not a failure
        return None
    for name, entry in (found or {}).items():
        if name != refused and entry.get("working", True) and not (
                entry.get("capacity") or {}).get("exhausted"):
            return name
    return None


def _open_card(connection, project_id: str, source_id: str) -> bool:
    from sqlalchemy import text

    row = connection.execute(
        text("SELECT 1 FROM decisions WHERE project_id = :p AND status = ANY(:open) "
             "AND recommendation->>'action' = :a AND recommendation->>'source_id' = :s LIMIT 1"),
        {"p": project_id, "a": CARD_ACTION, "s": source_id,
         "open": sorted(OPEN_STATUSES)}).first()
    return row is not None


def on_auth_refused(connection, project_id: str, run_id: str, engine: str, settings: Any,
                    *, task_id: Optional[str] = None,
                    source_id: Optional[str] = None) -> Optional[str]:
    """Record the refusal. Returns the decision id when a card was raised."""
    try:
        # A savepoint: a failed statement here must not poison the caller's transaction.
        with connection.begin_nested():
            return _record_refusal(connection, project_id, run_id, engine, settings, task_id,
                                   source_id)
    except Exception:  # noqa: BLE001 — recording a refusal never masks the run's own failure
        _log.warning("could not record the refused %s login", engine, exc_info=True)
        return None


def _record_refusal(connection, project_id, run_id, engine, settings, task_id, source_id=None):
    pooled = bool(source_id) and str(source_id).startswith("pool:")
    entitlement = None if pooled else (source_id or source_for(engine, settings))
    card_key = source_id or entitlement
    if not card_key:
        return None
    if entitlement:
        model_failures.mark_login(connection, project_id, entitlement, rejected=True)
    if _open_card(connection, project_id, card_key):
        return None
    who = model_failures.provider_name(engine)
    fallback = fallback_engine(connection, project_id, engine, settings)
    meanwhile = (f" Meanwhile {model_failures.provider_name(fallback)} is connected and can be "
                 "pinned for the job." if fallback else "")
    return core.create_blocking_decision(
        connection, project_id, task_id,
        f"The {who} subscription login was refused on run {run_id}: sign in again?",
        {"action": CARD_ACTION, "source_id": card_key, "engine": engine, "run_id": run_id,
         "fallback_engine": fallback,
         "recommended": "Sign in again in Setup, then retry the run." + meanwhile,
         "why_a_person": "Only the account's owner can sign in to it."},
        {}, reason=f"a run's {engine} harness was refused its login (run {run_id})", risk_tier=2)


def _project_and_engine(run_id: Optional[str], settings: Any):
    """The db and project this refusal belongs to: the llm context's project, else the project of
    the run it happened in — a build (code_task_runs), a workflow run, or a discovery."""
    from sqlalchemy import text

    from .llm_telemetry import get_llm_context

    ctx = get_llm_context() or {}
    db = ctx.get("db_engine")
    project_id = ctx.get("project_id")
    if db is None:
        from ..db import get_engine

        db = get_engine(settings)
    if not project_id:
        lookups = []
        if ctx.get("workflow_run_id"):
            lookups.append(("workflow_runs", ctx["workflow_run_id"]))
        for rid in (run_id, ctx.get("run_id")):
            if rid:
                lookups += [("code_task_runs", rid), ("discovery_runs", rid)]
        for table, rid in lookups:
            try:
                with db.connect() as c:
                    project_id = c.execute(
                        text(f"SELECT project_id FROM {table} WHERE id = :i"),  # fixed names
                        {"i": rid}).scalar()
            except Exception:  # noqa: BLE001 — a table this edition lacks is not a failure
                project_id = None
            if project_id:
                break
    return db, project_id


def note(verdict: Any, run_id: Optional[str] = None) -> Optional[str]:
    """The shared hook (harness_failures.trip). Records a PROVIDER-sourced refusal (the CLI's own
    stderr names the provider transport); an app's own 401 in a summary is ignored. Not deduped
    in memory: mark_login only moves on the transition and `_open_card` holds one card per dead
    login, so a refusal after a reconnect + retry flips Setup and asks again."""
    if verdict.kind != "auth_refused" or not is_provider_refusal(verdict):
        return None
    try:
        from ..config import get_settings

        settings = get_settings()
        db, project_id = _project_and_engine(run_id, settings)
        if not project_id:
            return None
        with db.begin() as c:
            return on_auth_refused(c, project_id, run_id or "", verdict.engine, settings,
                                   source_id=getattr(verdict, "source_id", "") or None)
    except Exception:  # noqa: BLE001
        _log.warning("could not record the refused %s login", verdict.engine, exc_info=True)
        return None
