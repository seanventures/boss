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
from typing import Any, Mapping, Optional

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


#: Where an operator raises the cap.
BUDGET_HREF = "/?area=budget"

#: The job gate's reasons for holding work by budget state (jobs._budget_state_policy_for_job).
BUDGET_HOLD_REASONS = (
    "red_halts_non_p0_work", "orange_defers_non_critical_work",
    "red_freezes_infra_provisioning", "red_freezes_tier2_effects",
)
_HELD_PREFIX = "held — the budget is"
_HELD_DETAIL = re.compile(r"held — the budget is (\w+) \(([^)]*)\)", re.I)
#: "It resets at 00:00 UTC, or raise the daily cap in Budget." — the way out of a period cap.
_RESETS = re.compile(r"It resets at [^.]*? in Budget\.")
_REFUSED_BY_CAP = re.compile(r"budget cap exceeded: (.+?)\. It resets at", re.I)


def _usd(micros: int) -> str:
    dollars = int(micros or 0) / 1_000_000
    if dollars == int(dollars):
        return f"${dollars:,.0f}"
    if dollars < 0.01:  # a test-sized cap of a few micros is not "$0.00"
        return f"${dollars:.6f}".rstrip("0")
    return f"${dollars:,.2f}"


def cap_used_sentence(period: Mapping[str, Any]) -> str:
    """How much of a period's cap is used, for a period from BudgetGovernor.status_for_project
    (the snapshot itself, or one of its `periods`):
    "today's budget is used ($10.20 of $10)", "this month's budget is 95% used ($190 of $200)"."""
    whose = period.get("whose") or "the"
    cap = int(period.get("cap_usd_micros") or 0)
    committed = int(period.get("committed_usd_micros") or 0)
    ratio = float(period.get("utilization_ratio") or 0)
    used = "used" if ratio >= 1 else f"{round(ratio * 100):.0f}% used"
    return f"{whose} budget is {used} ({_usd(committed)} of {_usd(cap)})"


def resets_sentence(period: Mapping[str, Any]) -> str:
    """"It resets at 00:00 UTC, or raise the daily cap in Budget." — every cap measures its own
    period, so the first way out is waiting for it to turn over."""
    return (f"It resets at {period.get('resets_at_label') or '00:00 UTC'}, "
            f"or raise the {period.get('cap_name') or 'cap'} in Budget.")


def budget_hold_sentence(budget_state: str, utilization_ratio: Optional[float],
                         cap_usd_micros: Optional[int],
                         period: Optional[Mapping[str, Any]] = None) -> str:
    """THE ONE WORDING for work the budget state holds — the job's last_error, the run's and the
    discovery's attention, and readiness all say this, never "no worker has picked it up".

    With the budget's `period` (the status snapshot, BudgetGovernor.status_for_project):
    "Held — the budget is red (95% of today's $10 used), so only P0 work runs. It resets at
    00:00 UTC, or raise the daily cap in Budget."

    The money it counts is money: a subscription's calls are not in it (included_spend.py), and
    only the money of the cap's own period (budget.period_spend)."""
    state = str(budget_state or "unknown")
    whose = (period or {}).get("whose")
    used = ""
    if cap_usd_micros:
        percent = round(float(utilization_ratio or 0) * 100)
        cap = f"{whose} {_usd(cap_usd_micros)}" if whose else _usd(cap_usd_micros)
        used = f" ({percent:.0f}% of {cap} used)"
    only = {"orange": ", so only P0 and P1 work runs",
            "red": ", so only P0 work runs"}.get(state, "")
    way_out = (resets_sentence(period) if period and period.get("resets_at_label")
               else "Raise the cap in Budget to let it run.")
    return f"Held — the budget is {state}{used}{only}. {way_out}"


def held_headline(sentence: object) -> str:
    """A hold sentence without its way out: "Held — the budget is red (…), so only P0 work
    runs" — for a readiness detail or a headline that shows the way out beside it."""
    return re.split(r"\. (?:It resets at|Raise the cap)", str(sentence or "").strip(),
                    maxsplit=1)[0].rstrip(".")


def is_budget_hold(error: object) -> bool:
    lowered = str(error or "").lower()
    return (_HELD_PREFIX in lowered
            or any(reason in lowered for reason in BUDGET_HOLD_REASONS)
            or bool(re.search(r"budget state \w+ deferred job", lowered)))


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
    # HELD BY THE BUDGET STATE (the job gate). Not a failure and not a missing worker: the job
    # is queued with a later run_after, and it runs once the money spent is back under the line.
    if is_budget_hold(text):
        found = _HELD_DETAIL.search(text)
        state = (found.group(1).lower() if found else
                 "red" if "red" in lowered else "orange" if "orange" in lowered else "over")
        headline = (held_headline(text) + "." if found
                    else f"Held — the budget is {state}.")
        resets = _RESETS.search(text)
        return {
            "kind": "budget_hold",
            "headline": headline,
            "action": ((resets.group(0) + " " if resets else "Raise the cap in Budget to let it "
                        "run. ") + "Work on a subscription costs no money and does not count "
                       "against it."),
            "href": BUDGET_HREF, "retry": False,
        }
    # Before the cap below: the governor's headroom refusal mentions the budget too, and it
    # clears by itself when the work holding the reservations finishes (BudgetGovernor.reserve).
    if ("held by other work" in lowered
            or "waiting for other work to release the budget" in lowered):
        return {
            "kind": "budget_wait",
            "headline": "Waiting for other work to release the budget it is holding.",
            "action": "Nothing to do — it resumes by itself when the work holding the budget "
                      "finishes. Raise the daily or monthly cap in Budget if this happens often.",
            "href": "/?area=budget", "retry": True,
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
        refused = _REFUSED_BY_CAP.search(text)
        resets = _RESETS.search(text)
        if refused and resets:
            # A period cap: "today's budget is used ($10.20 of $10)" — and when it resets.
            return {
                "kind": "budget_refused",
                "headline": ("The budget refused this work. "
                             f"{refused.group(1)[0].upper()}{refused.group(1)[1:]}."),
                "action": f"{resets.group(0)} Then retry.",
                "href": "/?area=budget", "retry": False,
            }
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
    # A SKILL THAT NEEDS ONE PROVIDER'S API (image generation, speech-to-text) and the project
    # has not connected it. The exception carries its own fix (model_routing.ProviderRequirement).
    from .model_routing import PROVIDER_REQUIREMENTS, ProviderRequirementMissing

    if ProviderRequirementMissing.MARKER in lowered:
        named = next((r for r in PROVIDER_REQUIREMENTS.values()
                      if r.label.lower() in lowered), None)
        return {
            "kind": "provider_requirement",
            "headline": (f"This step needs {named.label[0].lower() + named.label[1:]}, and this "
                         "project has not connected one." if named else
                         "This step needs a provider capability this project has not "
                         "connected."),
            "action": named.fix if named else "Connect the provider it names in Repo and model.",
            "href": SETUP_HREF, "retry": False,
        }
    if "no_eligible_source" in lowered or "no eligible model" in lowered \
            or "openai_api_key is not configured" in lowered:
        # "OPENAI_API_KEY is not configured" reaches here only when the project's model routing
        # (model_routing.project_chat_adapter) found nothing connected and fell through to the
        # OpenAI adapter — i.e. no model at all, not "you need OpenAI".
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


#: Failures that are FACTS about the project's setup — nothing connected that can answer, a
#: provider capability not connected, the budget refusing a model turn. A retry re-reads the
#: same setup and reaches the same sentence, so a step failing on one is settled at once.
#: Deliberately narrow: "no eligible source" can be a window that resets, so it still retries.
_SETTLED_MARKERS = ("openai_api_key is not configured",)


def settled_failure(error: object) -> Optional[str]:
    """The explained sentence for a failure retrying cannot fix, or None.

    Called where EVERY workflow step failure is recorded (`workflows.fail_step`), so the
    classification does not depend on which kind of step raised it: an agent step, a skill, a
    typed decision ("typed decision failed: OPENAI_API_KEY is not configured.") or the runner's
    catch-all. It was done only for agent steps, and every other path retried on a schedule and
    deadlettered on the raw sentence.
    """
    text = str(error or "")
    lowered = text.lower()
    from .model_routing import ModelSpendRefused

    said = explain(text)
    settled = (said["kind"] == "provider_requirement"
               or (said["kind"] == "no_model" and any(m in lowered for m in _SETTLED_MARKERS))
               or ModelSpendRefused.MARKER in lowered)
    if not settled:
        return None
    if text.startswith(said["headline"]):
        return text            # already explained once; do not wrap it twice
    return f"{said['headline']} {said['action']} ({text})"


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
