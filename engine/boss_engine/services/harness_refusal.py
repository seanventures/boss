"""A RUN'S ENGINE WAS REFUSED (401): say so where the operator looks, and offer a way on.

A governed run whose CLI harness was refused its login (`harness_failures` kind auth_refused)
used to end as a failed run only. Setup kept the last proof's green "Working" (the proof reads the
control plane's own login, the run reads its own copy), and nothing offered a way forward
(Codex, laptop, 2026-11-17: two failed reviews, Setup "Working" throughout).

`on_auth_refused` does the two things that were missing, once per refusal, never raising:
- the subscription source is marked rejected (the same mark the keep-alive and the governed
  proof write), so the Setup row, the pill and the router's headroom read "Sign-in expired —
  reconnect" instead of the last proof's Working;
- a blocking decision card offers "Reconnect" or "Use <fallback> for this job until
  reconnected", the fallback being a connected, working engine other than the refused one.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import core, model_failures

_log = logging.getLogger("boss.harness_refusal")

CARD_ACTION = "engine_refused_reconnect_or_fallback"


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
        text("SELECT 1 FROM decisions WHERE project_id = :p AND status = 'pending' "
             "AND recommendation->>'action' = :a AND recommendation->>'source_id' = :s LIMIT 1"),
        {"p": project_id, "a": CARD_ACTION, "s": source_id}).first()
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


def _record_refusal(connection, project_id, run_id, engine, settings, task_id,
                                   source_id):
    if True:
        # Keyed by the account's source id when the run knows it (several accounts per
        # provider); the engine's one default source otherwise.
        source_id = source_id or source_for(engine, settings)
        if not source_id:
            return None
        model_failures.mark_login(connection, project_id, source_id, rejected=True)
        if _open_card(connection, project_id, source_id):
            return None
        who = model_failures.provider_name(engine)
        fallback = fallback_engine(connection, project_id, engine, settings)
        options = [{"id": "reconnect", "label": "Reconnect",
                    "detail": f"Sign in to {who} again in Setup, then retry the run."}]
        if fallback:
            options.append({"id": "use_fallback", "engine": fallback,
                            "label": f"Use {model_failures.provider_name(fallback)} for this "
                                     "job until reconnected",
                            "detail": "Retry this job on that engine; it moves back when "
                                      f"{who} is reconnected."})
        return core.create_blocking_decision(
            connection, project_id, task_id,
            f"{who} sign-in expired: run {run_id} was refused. Reconnect, or use "
            f"{model_failures.provider_name(fallback)} meanwhile?" if fallback else
            f"{who} sign-in expired: run {run_id} was refused. Reconnect?",
            {"action": CARD_ACTION, "source_id": source_id, "engine": engine, "run_id": run_id,
             "options": options, "fallback_engine": fallback,
             "recommended": "Reconnect" if not fallback else f"Use {fallback} meanwhile, "
                                                              "then Reconnect",
             "why_a_person": "Only the account's owner can sign in to it."},
            {}, reason=f"harness auth_refused for {engine} on run {run_id}", risk_tier=2)
