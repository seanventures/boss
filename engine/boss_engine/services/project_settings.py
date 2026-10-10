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
import logging
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

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
    # A MODEL FOR EACH LANE, beside the harness picker for that lane. Until now the lanes chose
    # a harness and the CLASSES chose a model, so "review with Opus" was inexpressible: the
    # reviewer got whatever its step's class routed to. Choosing claude_code as the reviewer when
    # the builder is also claude_code therefore gave the SAME MODEL on both sides — exactly what
    # the reviewer setting exists to prevent.
    "code_review_model": {
        "attr": "code_review_model",
        "label": "Model for the reviewer",
        "help": "Which model the adversarial reviewer uses. Worth differing from the "
                "implementer's — the point of a second reviewer is a second opinion, and the "
                "same model on both sides rarely disagrees with itself. Empty means whatever "
                "the reviewer's lane would otherwise use.",
        "kind": "choice", "choices": "models_for:code_review_engine",
    },
    "code_agent_model": {
        "attr": "code_agent_model",
        "label": "Model for the implementer",
        "help": "Which model writes the code. Empty means the engine's own default, or the "
                "per-lane model below if one is set.",
        "kind": "choice", "choices": "models_for:code_agent_engine",
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
    # WHICH MODEL, not just which provider. The engine pickers above say Claude Code; they
    # cannot say Opus for review and Sonnet for routine work, so every lane ran whatever the
    # CLI defaulted to. The settings existed — code_agent_strong_model, code_agent_low_cost_model
    # and claude_cli_model — and none was reachable from the console, which is the same "built
    # and unwired" shape as the rest of this file's history.
    "code_agent_strong_model": {
        "attr": "code_agent_strong_model",
        "label": "Model for hard problems",
        "help": "Which model the hard-problems lane uses — adversarial review, security and "
                "consistency checks all run here. Opus is the one worth paying for on review. "
                "Empty means the engine's own default.",
        # THE LANE'S OWN HARNESS'S MODELS, not Claude's. This was hard-wired to claude_models,
        # so pointing the lane at codex still offered opus/sonnet/fable/haiku beside it.
        "kind": "choice", "choices": "models_for:code_agent_strong_engine",
    },
    "code_agent_low_cost_model": {
        "attr": "code_agent_low_cost_model",
        "label": "Model for routine work",
        "help": "Which model the routine lane uses — implement, write_tests and the design "
                "stages. Sonnet is the measured default; Haiku is cheaper and weaker. "
                "Empty means the engine's own default.",
        "kind": "choice", "choices": "models_for:code_agent_low_cost_engine",
    },
    # WIRED INTO THE ADAPTER FOR A LONG TIME AND REACHABLE FROM NOWHERE. codex_cli_adapter
    # reads settings.codex_cli_model on every call; the console never offered it, so an operator
    # running codex could choose the harness and not the model. Same "built and unwired" shape
    # as the rest of this file's history.
    "codex_cli_model": {
        "attr": "codex_cli_model",
        "label": "Codex model",
        "help": "Which model the Codex CLI uses when BOSS drives it. `codex --model` takes any "
                "name OpenAI offers, so the list is a suggestion rather than a limit. Empty "
                "means the CLI's own default.",
        "kind": "choice", "choices": "models_for_harness:codex",
    },
    "claude_cli_model": {
        "attr": "claude_cli_model",
        "label": "Model for chat and meetings",
        "help": "Which model answers in the console, agent chat and panels. Empty means the "
                "engine's own default.",
        "kind": "choice", "choices": "models_for_harness:claude_code",
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
    # THE CHAT'S MODEL ON A KEY OR A LOCAL MODEL. The Claude and Codex CLIs have their own
    # model knobs above; every other chat engine reads `chat_model` (model_routing.adapter_for).
    "chat_model": {
        "attr": "chat_model",
        "label": "Model for chat on a key",
        "help": "Which model answers the chat when it runs on an API key or a local model. "
                "Empty means the source's own default model.",
        "kind": "choice", "choices": "models_for_harness:native",
    },
    # WHO ANSWERS THE MULTIPLE-CHOICE QUESTIONS — a workflow's typed_decision step. "typesafe"
    # needs the project's own Jev key (Setup -> Model); without one, Claude decides. Every
    # attempt is reserved, leased and receipted before the call (BOSS-451).
    "model_routing_mode": {
        "attr": "model_routing_mode",
        "label": "Model routing",
        "help": "Fixed routes as configured per job; Smart picks a tier per step from the task "
                "and a source with headroom (docs/product/MODEL_ROUTER.md).",
        "kind": "choice", "choices": ("fixed", "smart"),
    },
    "decision_backend": {
        "attr": "decision_backend",
        "label": "Decision model",
        "help": "Who answers multiple-choice decisions in workflows. claude uses the connected "
                "subscription; typesafe uses TypeSafe's Jev on your own key and asks Claude "
                "again when Jev is unsure. Each call is budgeted and receipted.",
        "kind": "choice", "choices": ("claude", "typesafe"),
    },
    # A THROWAWAY DATABASE PER BUILD (build_services). The same opt-in a repo makes with
    # `services: [postgres]` in boss.verify.yaml, for a project whose repo does not declare it.
    "build_services": {
        "attr": "build_services",
        "label": "Build database",
        "help": "auto (the default) gives a build its own empty PostgreSQL database (DATABASE_URL), "
                "dropped when the build ends, whenever BOSS detects the app needs one: the brief, "
                "a template, the plan or the app's code says PostgreSQL. always gives every build "
                "one; never gives none.",
        "kind": "choice", "choices": ("auto", "always", "never"),
    },
    "decision_model": {
        "attr": "decision_model",
        "label": "Claude model for decisions",
        "help": "Which Claude model decides, and gives the second opinion when Jev is unsure.",
        "kind": "choice", "choices": "models_for_harness:claude_code",
    },
}

#: Each lane's engine knob and the model knob that belongs to it. When the engine changes, a model
#: chosen for the old one is cleared (see PUT /v1/settings) rather than left paired with a
#: harness that cannot run it.
MODEL_FOR_ENGINE = {
    "code_agent_engine": "code_agent_model",
    "code_review_engine": "code_review_model",
    "code_agent_strong_engine": "code_agent_strong_model",
    "code_agent_low_cost_engine": "code_agent_low_cost_model",
}

#: Engines that can hold a CONVERSATION, as opposed to drive a coding CLI: every engine
#: `model_routing.adapter_for` builds a chat adapter for, on that source's own credential.
from .model_routing import CHAT_ROUTABLE as CHAT_ENGINES  # noqa: E402

def _harness_names() -> list[str]:
    try:
        from .cli_harness import BUILTIN_CLI_HARNESSES

        return sorted(BUILTIN_CLI_HARNESSES)
    except Exception:  # noqa: BLE001 — a knob must not break because a registry did
        return []


def _models_of(harness: str) -> tuple[str, ...]:
    """The models this harness's CLI reports for the signed-in account. Never a written list:
    `fable` was offered for Claude after the account's CLI had stopped listing it, and
    gpt-5.2-codex/o3 for Codex while the account had gpt-6-*."""
    try:
        from .cli_harness import offered_models

        return offered_models(harness)
    except Exception:  # noqa: BLE001
        return ()


def model_choice_is_open(spec: dict[str, Any], harness: str) -> bool:
    """Whether this model knob accepts a name outside its offered list. Every model knob does.

    Every CLI BOSS drives takes a full model name as well as what its catalog lists, and a
    model ships before any list here could know about it. The offered list is what the CLI
    reported; typing another name is still allowed.
    """
    return str(spec.get("choices") or "").startswith(("models_for:", "models_for_harness:"))


def model_is_compatible_with_harness(model: Any, harness: str) -> bool:
    """Keep a model on the harness that can run it, decided by what the CLIs report.

    A name this harness's CLI lists is fine. A name ANOTHER harness's CLI lists and this one
    does not (`opus` on codex) is refused — that pairing fails inside a build looking like a
    bad model. Anything neither lists is a name the operator typed, and is allowed through.
    """
    name = str(model or "").strip().lower()
    if not name:
        return True
    try:
        from .cli_harness import BUILTIN_CLI_HARNESSES

        harness = str(harness or "").lower()
        if harness in BUILTIN_CLI_HARNESSES:
            if name in {m.lower() for m in _models_of(harness)}:
                return True
        else:
            # Native/provider harnesses are in the coding registry rather than the CLI model
            # registry. Their model ids are open text, but the harness itself must exist.
            from .harness_registry import get_harness

            get_harness(harness)
        elsewhere = {m.lower() for other in _harness_names() if other != harness
                     for m in _models_of(other)}
        return name not in elsewhere
    except Exception:  # noqa: BLE001 — an unknown pairing must not reach a model call
        logger.exception("Could not verify model and harness compatibility")
        return False


def harness_for_model_knob(spec: dict[str, Any], settings: Any) -> str:
    """Which harness a model knob's list should come from.

    `models_for_harness:codex` names one outright. `models_for:code_review_engine` follows the
    lane's harness setting, falling back to the implementer's when that lane has none — which is
    what an empty reviewer means: the implementer reviews its own work.
    """
    choice = str(spec.get("choices") or "")
    if choice.startswith("models_for_harness:"):
        return choice.split(":", 1)[1]
    if not choice.startswith("models_for:"):
        return ""
    attr = choice.split(":", 1)[1]
    return (str(getattr(settings, attr, "") or "")
            or str(getattr(settings, "code_agent_engine", "") or ""))


def _allowed_choices(spec: dict[str, Any]) -> list[str]:
    """The values a choice knob will accept, asked of the registry rather than hardcoded.

    A list written here would drift the moment a harness was added, and the failure would be a
    provider the operator has connected and cannot select.
    """
    if spec.get("choices") == "chat_engines":
        return list(CHAT_ENGINES)
    # MODELS, AS THE ACCEPT BOUND. Which harness this lane runs is a per-project question and
    # this function has no project — that split is the one the file already draws, so the
    # narrowing happens in `offered_choices` and what is ACCEPTED here is every harness's
    # models. A knob whose harness does not enumerate its models accepts anything: see
    # `model_choice_is_open`.
    if str(spec.get("choices") or "").startswith(("models_for:", "models_for_harness:")):
        every: list[str] = [""]
        for name in _harness_names():
            for model in _models_of(name):
                if model not in every:
                    every.append(model)
        return every
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
    if str(spec.get("choices") or "").startswith(("models_for:", "models_for_harness:")):
        # The models of the harness this lane actually runs — not every harness's, which is the
        # accept bound and the wrong list for a dropdown.
        harness = harness_for_model_knob(spec, settings)
        offered = ["", *_models_of(harness)]
        if current and str(current) not in offered:
            offered.append(str(current))
        return offered
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


#: `Agents at once` is the review panel's parallelism, and the panel never runs narrower than
#: this: values 1-6 all behave like 6 (governed_code_task_service reads it through
#: `effective_review_lanes`). Govern shows the effective value beside the stored one.
REVIEW_LANES_FLOOR = 6
#: What `Agents at once` reads as when nothing is stored and the deployment says 0/None.
REVIEW_LANES_FALLBACK = 3
#: What `Review rounds` reads as when it is 0 or unset: `int(v or 2)` — 0 is falsy, so 0 means 2.
REVIEW_ROUNDS_FALLBACK = 2


def memory_limited_seats(connection) -> Optional[int]:
    """How many review seats the panel may run RIGHT NOW because of memory, or None for no
    limit. Under memory pressure (admission level L1+) the panel sits one seat at a time
    (stability spec §4b.3). The pipeline and Govern both read this, so the board says what the
    panel does. An unreadable level never limits the panel."""
    try:
        from .admission import L1, current_level
        with connection.begin_nested():  # a failed read must not abort the caller's transaction
            level = current_level(connection)
        return 1 if level >= L1 else None
    except Exception:  # noqa: BLE001
        return None


def effective_review_lanes(stored: Any, panel_size: Optional[int] = None) -> int:
    """How many reviewers work at once on a build, from the `Agents at once` value.

    THE ONE PLACE this arithmetic lives: the pipeline sizes its review panel with it and the
    Govern board shows its result, so the screen cannot claim a number the engine does not use.
    `panel_size` caps it at the number of checks the panel actually has."""
    lanes = max(int(stored or REVIEW_LANES_FALLBACK), REVIEW_LANES_FLOOR)
    return min(int(panel_size), lanes) if panel_size is not None else lanes


def effective_review_rounds(stored: Any) -> int:
    """How many times reviewers and the implementer converse in a run (`Review rounds`).
    0 reads as 2 — the same truthiness the pipeline always had; Govern says so."""
    return max(0, int(stored or REVIEW_ROUNDS_FALLBACK))


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
        # A MODEL KNOB ACCEPTS ANY NAME its harness's CLI could run. Validating against a list
        # would reject a model that works the week after it ships — and the operator would read
        # "BOSS cannot drive that" about a model their CLI runs fine. The one refusal is a name
        # another CLI reports and this one does not: `opus` saved for codex fails in a build.
        choice = str(spec.get("choices") or "")
        if choice.startswith(("models_for:", "models_for_harness:")):
            if not name:
                return ""
            if choice.startswith("models_for_harness:"):
                harness = choice.split(":", 1)[1]
                if not model_is_compatible_with_harness(name, harness):
                    offered = ", ".join(_models_of(harness)) or "its default"
                    raise ValueError(
                        f"{spec['label']}: '{name}' is a model for another harness, not "
                        f"{harness}. {harness} offers: {offered}.")
            return name
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


#: What Govern calls each knob — the names on the board AND in the Log's change lines, one
#: table (D14e: a change read "Agents at once" while the board said "Review lanes"). The KNOBS
#: labels stay as they are: they phrase the bounds errors callers already depend on.
GOVERN_NAMES: dict[str, str] = {
    "code_agent_max_concurrency": "Review lanes",
    "code_agent_max_turns": "Turn budget",
    "code_review_inrun_max_rounds": "Review rounds",
    "captain_leads_projects": "Captain leads",
}


def _record_change(connection, project_id: str, key: str, before: Any, after: Any,
                   actor: str) -> None:
    """`settings.changed`, so Govern's changes are read in the Log (`Govern changes`) and Govern
    keeps no change list of its own. Best effort, in a savepoint: a failed write of the record
    must never undo or refuse the change itself."""
    if before == after:
        return
    from .core import emit_event

    spec = KNOBS.get(key) or {}
    try:
        with connection.begin_nested():
            emit_event(connection, project_id, "settings.changed", actor or "operator",
                       "project_setting", key,
                       {"key": key, "label": GOVERN_NAMES.get(key) or spec.get("label") or key,
                        "from": before,
                        "to": after,
                        "source_before": "project" if before is not None else "default",
                        "source_after": "project" if after is not None else "default"})
    except Exception:  # noqa: BLE001 — the record of a change is not the change
        logger.exception("could not record settings.changed for %s", key)


def set_one(connection, project_id: str, key: str, value: Any, actor: str = "operator") -> Any:
    clean = coerce(key, value)
    before = connection.execute(
        text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
        {"p": project_id, "k": key},
    ).scalar()
    connection.execute(
        text("INSERT INTO project_settings (project_id, key, value, updated_by, updated_at) "
             "VALUES (:p, :k, CAST(:v AS JSONB), :by, now()) "
             "ON CONFLICT (project_id, key) DO UPDATE "
             "SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
             "    updated_at = now()"),
        {"p": project_id, "k": key, "v": json.dumps(clean), "by": actor},
    )
    _record_change(connection, project_id, key, before, clean, actor)
    _routing_changed(project_id)
    return clean


def _routing_changed(project_id: str) -> None:
    """A knob the model router reads changed: drop its cached snapshot (model_router.store)."""
    try:
        from .model_router.store import invalidate

        invalidate(project_id)
    except Exception:  # noqa: BLE001
        pass


def clear_one(connection, project_id: str, key: str, actor: str = "operator") -> None:
    """Hand the knob back to the environment. Deleting is how a project says 'use the default'
    — storing the current default instead would freeze it against a later change."""
    before = connection.execute(
        text("DELETE FROM project_settings WHERE project_id = :p AND key = :k RETURNING value"),
        {"p": project_id, "k": key},
    ).scalar()
    if before is not None:
        _record_change(connection, project_id, key, before, None, actor)
    _routing_changed(project_id)


def version_of(connection, project_id: str, key: str) -> tuple[str, Optional[str]]:
    """(version, who changed it last) for one knob — the If-Match a Govern edit sends. A knob
    nobody set is version 'unset'; otherwise its last write time."""
    row = connection.execute(
        text("SELECT updated_at, updated_by FROM project_settings "
             "WHERE project_id = :p AND key = :k"),
        {"p": project_id, "k": key},
    ).mappings().first()
    if not row:
        return "unset", None
    stamp = row["updated_at"]
    return (stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)), row["updated_by"]


def applied(connection, project_id: Optional[str], settings: Any) -> Any:
    """`settings` with this project's choices layered on top.

    One place, rather than a lookup at each use. The alternative is threading `effective(...)`
    through every read of every knob — `code_agent_strong_engine` alone is read in four
    different functions — and the reads that got missed would be the ones that quietly kept
    using the deployment default while the console showed the operator's choice.

    A settings lookup failure falls back to deployment values. The compatibility check still
    runs: even a deployment-wide Codex/Claude-model mismatch must not reach a model call.
    """
    chosen = {}
    if project_id:
        try:
            chosen = get_all(connection, project_id)
        except Exception:  # noqa: BLE001 — a DB outage must not expose an unsafe model pair
            logger.exception("Could not load project settings; using deployment settings")
    overrides = {}
    for key, value in chosen.items():
        spec = KNOBS.get(key)
        if spec and value not in (None, ""):
            overrides[spec["attr"]] = value
    layered = settings.model_copy(update=overrides) if overrides else settings
    compatible = {}
    for model_key in MODEL_FOR_ENGINE.values():
        spec = KNOBS[model_key]
        model_attr = spec["attr"]
        model = getattr(layered, model_attr, "")
        harness = harness_for_model_knob(spec, layered)
        if model and not model_is_compatible_with_harness(model, harness):
            # A project engine override can expose a deployment model chosen for the old
            # engine even after its project model was cleared. Never send that pairing to
            # a run. An explicit compatible project choice remains untouched.
            compatible[model_attr] = ""
    return layered.model_copy(update=compatible) if compatible else layered


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
