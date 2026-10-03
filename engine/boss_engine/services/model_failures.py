"""Say why model work is not moving, in words an operator can act on.

Discovery sat at "queued" through three different failures on boss-1 (2026-10-01): the API
refused the worker (409), the budget's per-minute cap refused every stage, and the Claude
subscription's stored credential was rejected (401). Each was recorded somewhere — a job's
last_error, an event, a run's error column — and none was shown where the operator was looking,
so the run looked like it was waiting for nothing. This turns a raw failure into one sentence and
the next step, and every surface that shows model work uses it.
"""
from __future__ import annotations

import re
from typing import Optional

#: Where an operator fixes a model connection.
SETUP_HREF = "/?area=setup&step=model"

_LOGIN_REJECTED = (
    "invalid bearer token", "failed to authenticate", "authentication_error",
    "invalid api key", "invalid x-api-key", "not logged in", "please run /login",
    "oauth token has expired", "token has expired", "login was rejected",
)
_HTTP_401 = re.compile(r"(?<![\w.])401(?![\w.])")
_PROVIDER_NAMES = {
    "anthropic_claude_code": "Claude Code subscription",
    "claude_code": "Claude Code subscription",
    "openai_codex_chatgpt": "Codex subscription",
    "codex": "Codex subscription",
    "typesafe_jev": "TypeSafe Jev",
    "typesafe": "TypeSafe Jev",
}


def provider_name(source_or_engine: Optional[str]) -> str:
    return _PROVIDER_NAMES.get(str(source_or_engine or ""), "the model")


def is_login_rejected(error: object) -> bool:
    text = str(error or "").lower()
    return any(term in text for term in _LOGIN_REJECTED) or bool(_HTTP_401.search(text))


def explain(error: object, *, source_id: Optional[str] = None) -> dict:
    """{kind, headline, action, href, retry} for a failure. Never repeats a credential."""
    text = str(error or "")
    lowered = text.lower()
    who = provider_name(source_id)
    if is_login_rejected(lowered):
        return {
            "kind": "login_rejected",
            "headline": f"The {who} login was rejected, so no model call can run.",
            "action": (f"Reconnect the {who} in Setup → Model. For Claude Code, run "
                       "`claude setup-token` and paste the long sk-ant-… value it prints."),
            "href": SETUP_HREF, "retry": False,
        }
    if "burst cap" in lowered or "waiting for the per-minute budget" in lowered:
        return {
            "kind": "budget_wait",
            "headline": "Waiting for the per-minute budget to free up.",
            "action": "Nothing to do — it resumes by itself within about a minute. Raise the "
                      "per-minute cap in Budget if this happens often.",
            "href": "/?area=budget", "retry": True,
        }
    if any(term in lowered for term in ("budget cap", "daily cap", "monthly cap",
                                        "budget reservation denied", "insufficient",
                                        "budget allocation", "frozen at this budget")):
        return {
            "kind": "budget_refused",
            "headline": "The budget refused this work.",
            "action": "Raise the daily or monthly cap in Budget, or wait for the period to reset, "
                      "then retry.",
            "href": "/?area=budget", "retry": False,
        }
    if "cannot be pooled" in lowered or "owner scoped" in lowered:
        return {
            "kind": "capacity_owner",
            "headline": f"The {who} belongs to someone else on this install and cannot be used "
                        "for this work.",
            "action": "Connect your own subscription in Setup → Model.",
            "href": SETUP_HREF, "retry": False,
        }
    if "no_eligible_source" in lowered or "no eligible model" in lowered:
        return {
            "kind": "no_model",
            "headline": "No connected model can run this work.",
            "action": "Connect a model in Setup → Model.",
            "href": SETUP_HREF, "retry": False,
        }
    if re.search(r"(?<![\w.])409(?![\w.])", lowered) and "conflict" in lowered:
        return {
            "kind": "refused_by_api",
            "headline": "BOSS's API refused the worker that runs this work (409).",
            "action": "This is a BOSS defect, not something you configured: update BOSS, then "
                      "retry from the run.",
            "href": None, "retry": False,
        }
    if "reconcil" in lowered:
        return {
            "kind": "reconcile",
            "headline": "A model call may have been charged without its answer being kept.",
            "action": "Review the reconciliation decision before retrying, so nothing is paid "
                      "for twice.",
            "href": "/?area=decisions", "retry": False,
        }
    return {
        "kind": "failed",
        "headline": "The model call failed.",
        "action": "Open the run's activity for the full error, then retry.",
        "href": None, "retry": True,
        "detail": text[:300],
    }


def mark_login(connection, project_id: str, source_id: Optional[str], *, rejected: bool) -> None:
    """Record on the model source whether its login was just rejected or just worked, so Setup
    shows a rejected subscription as broken instead of green "Connected"."""
    if not source_id:
        return
    from sqlalchemy import text

    connection.execute(
        text("UPDATE entitlement_sources SET observed_capacity = "
             "COALESCE(observed_capacity, '{}'::jsonb) || jsonb_build_object("
             "'credential_state', CAST(:state AS text), 'credential_checked_at', now()::text), "
             "updated_at = now() WHERE project_id = :p AND source_id = :s"),
        {"state": "rejected" if rejected else "ok", "p": project_id, "s": source_id},
    )


def clear_login_state(connection, project_id: str, source_id: Optional[str]) -> None:
    """A new credential has not been tried yet: neither rejected nor proven."""
    if not source_id or connection is None:
        return
    from sqlalchemy import text

    connection.execute(
        text("UPDATE entitlement_sources SET observed_capacity = "
             "COALESCE(observed_capacity, '{}'::jsonb) - 'credential_state', updated_at = now() "
             "WHERE project_id = :p AND source_id = :s"),
        {"p": project_id, "s": source_id},
    )
