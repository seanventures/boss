"""Knobs an operator can turn without a deploy.

How hard BOSS runs — how many agents at once, how much it may spend — lived entirely in
environment variables. That is the right home for a secret and the wrong home for a dial
somebody wants to try at 20 and then at 5: changing one meant an ops action and a container
restart, so in practice nobody changed them and the defaults became the product.

A project setting overrides the environment; the environment remains the default. Nothing here
invents a value: a knob nobody has touched reads exactly as it did before this existed.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

#: The knobs the console may set, with the settings attribute each one overrides and the
#: bounds a value has to fall inside. An unlisted key is refused rather than stored: a settings
#: table that accepts anything becomes a place where typos live forever.
KNOBS: dict[str, dict[str, Any]] = {
    "code_agent_max_concurrency": {
        "attr": "code_agent_max_concurrency",
        "label": "Agents at once",
        "help": "How many coding agents may run in parallel on this project.",
        "kind": "int", "min": 1, "max": 16,
    },
    "code_agent_max_turns": {
        "attr": "code_agent_max_turns",
        "label": "Turn budget per agent",
        "help": "How long one agent may keep working before it must hand back.",
        "kind": "int", "min": 10, "max": 200,
    },
    "captain_leads_projects": {
        "attr": "captain_leads_projects",
        "label": "Captain leads every project",
        "help": "Attach the Engineering Captain — PRD, QA, UX, certification panel — to every "
                "project, not only benchmark runs. 1 to lead, 0 to leave builds unled.",
        "kind": "int", "min": 0, "max": 1,
    },
    "code_review_inrun_max_rounds": {
        "attr": "code_review_inrun_max_rounds",
        "label": "Review rounds",
        "help": "How many times reviewers and the implementer may converse before the run "
                "falls back to a colder path.",
        "kind": "int", "min": 0, "max": 5,
    },
    # WHICH MODEL BUILDS, as a dial rather than a deployment. Choosing a provider meant editing
    # an environment variable and recreating the containers — so an installation that had
    # connected three subscriptions could still only use the one it was deployed with, and
    # "switch to Codex for this project" was an ops task. The credentials are already stored;
    # only the choice was missing.
    "code_agent_engine": {
        "attr": "code_agent_engine",
        "label": "Implementer",
        "help": "Which harness writes the code on this project.",
        "kind": "choice", "choices": "harnesses",
    },
    "code_review_engine": {
        "attr": "code_review_engine",
        "label": "Adversarial reviewer",
        "help": "Which harness reviews the code and writes the line-by-line findings. Worth "
                "differing from the implementer — a reviewer that is the same model rarely "
                "disagrees with itself. Empty means the implementer reviews its own work.",
        "kind": "choice", "choices": "harnesses",
    },
    "code_agent_strong_engine": {
        "attr": "code_agent_strong_engine",
        "label": "Hard problems",
        "help": "Used when a task escalates — a repair after repeated failure, or work the "
                "router judges difficult. Empty means the implementer handles it.",
        "kind": "choice", "choices": "harnesses",
    },
    "code_agent_low_cost_engine": {
        "attr": "code_agent_low_cost_engine",
        "label": "Routine work",
        "help": "Used for the cheap end — small, well-specified changes. Empty means the "
                "implementer handles those too.",
        "kind": "choice", "choices": "harnesses",
    },
    # AND THE MODEL THAT TALKS. Separate from the four above because it is a separate
    # question: an operator who connected a subscription for BUILDS was told "OPENAI_API_KEY
    # is not configured" by the chat, which is true and unhelpful — the subscription they had
    # just connected can answer perfectly well, nothing had selected it.
    "chat_llm_engine": {
        "attr": "chat_llm_engine",
        "label": "Console chat, agents and meetings",
        "help": "Which model answers in the chat, runs the agent panels and speaks in "
                "meetings. Empty means BOSS uses whichever subscription is connected, and "
                "the API key from this install's environment when one is set.",
        "kind": "choice", "choices": "chat_engines",
    },
}

#: Engines that can hold a CONVERSATION, as opposed to drive a coding CLI.
#:
#: Not the same set as the harnesses, and offering the harness list here would be a green tick
#: over nothing: `make_agent_chat_adapter` honours exactly these three and sends everything
#: else to the OpenAI adapter, so "openrouter" in this box would silently mean "OpenAI".
CHAT_ENGINES = ("claude_code", "codex", "native")


def _allowed_choices(spec: dict[str, Any]) -> list[str]:
    """The values a choice knob will accept, asked of the registry rather than hardcoded.

    A list written here would drift the moment a harness was added, and the failure would be a
    provider the operator has connected and cannot select.
    """
    if spec.get("choices") == "chat_engines":
        return list(CHAT_ENGINES)
    if spec.get("choices") != "harnesses":
        return list(spec.get("choices") or [])
    try:
        from .harness_registry import available_harnesses

        return sorted(h["name"] for h in available_harnesses())
    except Exception:  # noqa: BLE001 — a knob must not break because a registry did
        return []


def offered_choices(connection, settings, project_id: str,
                    spec: dict[str, Any], current: Any = None) -> list[str]:
    """The values worth OFFERING for this knob on this project, as opposed to accepting.

    Two different questions, and conflating them is what made the role pickers useless:
    `_allowed_choices` answers "could BOSS drive this at all", which is the right bound for
    validation and the wrong list for a dropdown. A picker should show what this project has
    actually connected — otherwise ten of its eleven options are credentials the operator
    does not have, and choosing one fails later, inside a build, looking like a bad model.

    `current` is always included. A model can be un-connected after being chosen, and a
    picker that silently drops the value it is displaying rewrites the operator's setting the
    next time they touch anything else on the form.
    """
    allowed = _allowed_choices(spec)
    if spec.get("choices") not in ("harnesses", "chat_engines") or not allowed:
        return allowed
    try:
        from .setup_service import connected_engines

        connected = set(connected_engines(connection, settings, project_id))
    except Exception:  # noqa: BLE001 — a knob must not break because a lookup did
        return allowed
    offered = [name for name in allowed if name in connected]
    if current and current not in offered and current in allowed:
        offered.append(str(current))
    # Nothing connected yet: show everything rather than an empty dropdown, which would read
    # as "this install cannot do it" instead of "connect a model first".
    return sorted(offered) or allowed


class UnknownKnob(ValueError):
    """The key is not one of ours. Refused rather than stored."""


def coerce(key: str, value: Any) -> Any:
    """The value this knob will actually take, or an error saying why it will not.

    Bounds are enforced HERE rather than at the edge, so every caller — console, API, a script
    — gets the same answer. A concurrency of 400 is not a preference, it is an outage.
    """
    spec = KNOBS.get(key)
    if spec is None:
        raise UnknownKnob(f"'{key}' is not a setting BOSS knows about.")
    if spec["kind"] == "int":
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{spec['label']} has to be a whole number.") from error
        if not (spec["min"] <= number <= spec["max"]):
            raise ValueError(
                f"{spec['label']} has to be between {spec['min']} and {spec['max']}.")
        return number
    if spec["kind"] == "choice":
        name = str(value or "").strip().lower()
        allowed = _allowed_choices(spec)
        if allowed and name not in allowed:
            # Name what IS available. "Invalid value" sends somebody to the source to find out
            # what the valid ones are, and the answer changes as harnesses are registered.
            raise ValueError(
                f"{spec['label']}: '{name}' is not a harness BOSS can drive. "
                f"Available: {', '.join(allowed)}.")
        return name
    return value


def get_all(connection, project_id: str) -> dict[str, Any]:
    """Every knob this project has set. Absent keys are not defaults — they are silence."""
    rows = connection.execute(
        text("SELECT key, value FROM project_settings WHERE project_id = :p"),
        {"p": project_id},
    ).mappings().all()
    return {r["key"]: r["value"] for r in rows}


def set_one(connection, project_id: str, key: str, value: Any, actor: str = "operator") -> Any:
    clean = coerce(key, value)
    connection.execute(
        text("INSERT INTO project_settings (project_id, key, value, updated_by, updated_at) "
             "VALUES (:p, :k, CAST(:v AS JSONB), :by, now()) "
             "ON CONFLICT (project_id, key) DO UPDATE "
             "SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
             "    updated_at = now()"),
        {"p": project_id, "k": key, "v": json.dumps(clean), "by": actor},
    )
    return clean


def clear_one(connection, project_id: str, key: str) -> None:
    """Hand the knob back to the environment. Deleting is how a project says 'use the default'
    — storing the current default instead would freeze it against a later change."""
    connection.execute(
        text("DELETE FROM project_settings WHERE project_id = :p AND key = :k"),
        {"p": project_id, "k": key},
    )


def applied(connection, project_id: Optional[str], settings: Any) -> Any:
    """`settings` with this project's choices layered on top.

    One place, rather than a lookup at each use. The alternative is threading `effective(...)`
    through every read of every knob — `code_agent_strong_engine` alone is read in four
    different functions — and the reads that got missed would be the ones that quietly kept
    using the deployment default while the console showed the operator's choice.

    Never raises, and returns the original object when there is nothing to layer: a settings
    table that is unreachable must not stop work.
    """
    if not project_id:
        return settings
    try:
        chosen = get_all(connection, project_id)
    except Exception:  # noqa: BLE001 — the environment is a complete answer on its own
        return settings
    overrides = {}
    for key, value in chosen.items():
        spec = KNOBS.get(key)
        if spec and value not in (None, ""):
            overrides[spec["attr"]] = value
    if not overrides:
        return settings
    try:
        return settings.model_copy(update=overrides)
    except Exception:  # noqa: BLE001
        return settings


def effective(connection, project_id: Optional[str], settings: Any, key: str) -> Any:
    """What this knob is worth right now: the project's value, else the environment's.

    Never raises. A settings table that is unreachable must not stop work — the environment is
    a complete answer on its own, and was the only answer until this existed.
    """
    spec = KNOBS.get(key)
    fallback = getattr(settings, spec["attr"], None) if spec else None
    if not project_id or spec is None:
        return fallback
    try:
        row = connection.execute(
            text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
            {"p": project_id, "k": key},
        ).scalar()
    except Exception:  # noqa: BLE001 — the environment is a complete answer
        return fallback
    return fallback if row is None else row
