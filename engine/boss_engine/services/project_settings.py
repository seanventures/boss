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
    # THE KNOWLEDGE GRAPH SWITCH (BOSSAPP-622). Off: agents get no knowledge pack, precedent,
    # citations or kg tool from this project's graph. Keyword: full-text keyword search only, no
    # graph traversal, vectors, learned facts or precedent (knowledge/runtime.knowledge_mode, read
    # by every delivery path). Indexing and capture go on, so turning it back on loses nothing.
    # The case study (scripts/case-study/knowledge) runs arm A off, A′ keyword and B on.
    "knowledge_graph": {
        "attr": "knowledge_graph",
        "label": "Knowledge graph",
        "help": "On: agents are given what this project's knowledge graph knows (packs, precedent, "
                "the facts a change is based on). Keyword: plain keyword search over its records "
                "only, no graph, meaning search, learned facts or precedent. Off: they work "
                "without it. The graph keeps indexing either way.",
        "kind": "choice", "choices": ["on", "keyword", "off"],
    },
    # THE PROMPT-INJECTION SCREEN ON TOOL RESULTS (BOSSAPP-822, services/tool_result_screen.py).
    # Read at call_tool_with_receipt and the agent loop's outside-origin results.
    "tool_result_screen": {
        "attr": "tool_result_screen",
        "label": "Screen tool results",
        "help": "Patterns: BOSS checks every tool result for prompt injection with fixed rules; a "
                "hit is marked untrusted for the agent and the run's next write waits for you. "
                "Model: the same, plus a small model check when the rules find nothing (spends a "
                "little budget). Off: results reach agents unscreened.",
        "kind": "choice", "choices": ["patterns", "model", "off"],
    },
    "run_supervisor_stall_minutes": {
        "attr": "run_supervisor_stall_minutes",
        "label": "Run supervisor stall minutes",
        "help": "How long a run may sit with no caused-failure progress before the run "
                "supervisor (L2a) treats it as stalled.",
        "kind": "int", "min": 5, "max": 180,
    },
    "run_supervisor_signal": {
        "attr": "run_supervisor_signal",
        "label": "Run supervisor: compute signal",
        "help": "Whether the run supervisor (L2a) even computes its stalled-progress signal "
                "for a repair round. 1 on, 0 off.",
        "kind": "int", "min": 0, "max": 1,
    },
    "run_supervisor_judgement": {
        "attr": "run_supervisor_judgement",
        "label": "Run supervisor: call judgement",
        "help": "Whether a tripped/forced run supervisor signal calls the governed judgement "
                "seam. Off degrades to a deterministic stop_report with no judgement spend. "
                "1 on, 0 off.",
        "kind": "int", "min": 0, "max": 1,
    },
    "run_supervisor_action": {
        "attr": "run_supervisor_action",
        "label": "Run supervisor: enforce action",
        "help": "Whether the run supervisor's decision changes what the repair loop does next. "
                "Off still records the decision and lesson, but never changes the loop's "
                "control flow. 1 on, 0 off.",
        "kind": "int", "min": 0, "max": 1,
    },
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
    # OPT-IN, OFF ON A NEW INSTALL (Settings.captain_auto_improve). An operator's own "drive"
    # with a directive and a focused agent someone created are asked for; this governs only the
    # passes the Captain decides to start itself (metatron_captain.captain_launch_gate).
    "captain_auto_improve": {
        "attr": "captain_auto_improve",
        "label": "Captain improves on its own",
        "help": "Lets the Captain start its own quality-improve builds when a build's scorecard "
                "shows gaps. Each one spends model budget. Off: it only proposes them.",
        "kind": "int", "min": 0, "max": 1,
    },
    # FIX AND REDEPLOY (deploy_repair.py): a deploy that failed for the app's own reason is
    # repaired by a build and shipped again, at most this many times per deploy step.
    "deploy_fix_attempts": {
        "attr": "deploy_fix_attempts",
        "label": "Deploy fixes",
        "help": "When a workflow's deploy fails because of the app (a runtime the machine lacks, "
                "the app's own install or health error), how many times BOSS fixes the app and "
                "deploys again by itself. 0: never, a person decides.",
        "kind": "int", "min": 0, "max": 10,
    },
    # THE RECOVERY AGENT (step_recovery.py): a failed deploy or build step is recovered by the
    # step's own agent until it succeeds or the window's budget is spent.
    "step_recovery": {
        "attr": "step_recovery",
        "label": "Agents recover failed steps",
        "help": "When a deploy or build step fails, its agent reads the evidence and decides: "
                "retry, fix the app, clean this app's earlier release, wait for a BOSS fix, or "
                "ask you one question. 1 on, 0 to send every failure to a person.",
        "kind": "int", "min": 0, "max": 1,
    },
    "recovery_budget_cents": {
        "attr": "recovery_budget_cents",
        "label": "Recovery budget (cents)",
        "help": "The most one failed step's recovery may spend before it asks you: the agent's "
                "model turns plus the repair builds it starts. A Retry starts a new budget.",
        "kind": "int", "min": 0, "max": 10000,
    },
    "recovery_max_minutes": {
        "attr": "recovery_max_minutes",
        "label": "Recovery time (minutes)",
        "help": "How long one failed step's recovery may go on before it asks you.",
        "kind": "int", "min": 5, "max": 1440,
    },
    # OPEN THE PR WHEN CHECKS FAIL (checks_failing.py): after its repair rounds a build whose
    # checks still fail opens a draft PR labelled `checks failing` instead of waiting for a waiver.
    "open_pr_when_checks_fail": {
        "attr": "open_pr_when_checks_fail",
        "label": "Open the PR when checks fail",
        "help": "When a build's checks still fail after its repair rounds, open its pull request "
                "anyway: a draft labelled checks failing, listing every failing check and finding "
                "(security first), with a follow-up build to fix them. You decide the merge. "
                "1 on, 0 to stop the build until a person waives the checks.",
        "kind": "int", "min": 0, "max": 1,
    },
    # WHEN A SPEND LIMIT IS REACHED (BOSSAPP-807, spend_limits.at_limit): read by
    # BudgetGovernor.reserve and build_budget.cap_reached at every reservation and stage boundary.
    "budget_at_limit": {
        "attr": "budget_at_limit",
        "label": "When a limit is reached",
        "help": "pause: new work waits and asks you. cheaper: work goes on with low-cost "
                "models. stop: nothing runs until the period resets.",
        "kind": "choice", "choices": ["pause", "cheaper", "stop"],
    },
    # SPEND BOSS CANNOT PRICE (BOSSAPP-869, services/unpriced_policy.py): read by
    # BudgetGovernor.reserve. estimate is today's behaviour; block holds paid work once today's
    # unpriced money is past the allowance below and opens the increase-or-defer decision.
    "budget_unpriced_policy": {
        "attr": "budget_unpriced_policy",
        "label": "Unpriced usage",
        "help": "estimate: a model BOSS has no price for is charged at the dearest known rate and "
                "labelled. block: once today's unpriced money passes the allowance, paid work "
                "waits and asks you.",
        "kind": "choice", "choices": ["estimate", "block"],
    },
    "budget_unpriced_allowance_usd_micros": {
        "attr": "budget_unpriced_allowance_usd_micros",
        "label": "Unpriced allowance per day (micro-dollars)",
        "help": "With Unpriced usage set to block: how much unpriced money a day is tolerated "
                "before paid work waits. 0 = none.",
        "kind": "int", "min": 0, "max": 1_000_000_000_000,
    },
    # PER-CLASS CALL GOVERNANCE (BOSSAPP-999, services/call_classes.py): deny a class of model
    # calls outright, or cap it by calls-per-day / tokens-per-day — VOLUME caps, because a
    # $0-settling subscription call cannot be money-capped. Enforced where calls are authorized
    # (governed(), the gateway lease, the chat adapter seam). Unset = everything allowed.
    "call_classes": {
        "attr": "call_classes",
        "label": "Model call classes",
        "help": "Per-class rules over every model call, as JSON: "
                '{"<class>": {"allow": true|false, "calls_per_day": n, "tokens_per_day": n}}. '
                "Classes: build_stage, chat, panel, decision, proof, kg, bench, gateway, other. "
                "A denied or over-cap call is refused in words before anything reaches a model. "
                "Unset: everything allowed, no caps.",
        "kind": "call_classes",
    },
    # THE MODEL PRE-CHECK at the start door (workflow_precheck_agent.py). OFF unless turned on:
    # it is a model call, so it costs money and time on every start it runs at.
    "workflow_precheck_agent": {
        "attr": "workflow_precheck_agent",
        "label": "Model pre-check at start",
        "help": "When a workflow starts, an agent also reads the pre-check's evidence and "
                "predicts failures the coded checks miss. Spends a little model budget per "
                "start. 1 to run it, 0 to leave it to the CLI's --agent.",
        "kind": "int", "min": 0, "max": 1,
    },
    "workflow_precheck_agent_max_cents": {
        "attr": "workflow_precheck_agent_max_cents",
        "label": "Model pre-check cost cap (cents)",
        "help": "The most one model pre-check may spend; it stops when it reaches this.",
        "kind": "int", "min": 1, "max": 500,
    },
    "workflow_precheck_agent_max_turns": {
        "attr": "workflow_precheck_agent_max_turns",
        "label": "Model pre-check turns",
        "help": "The most model turns one model pre-check may take.",
        "kind": "int", "min": 1, "max": 30,
    },
    "workflow_precheck_agent_max_wait_seconds": {
        "attr": "workflow_precheck_agent_max_wait_seconds",
        "label": "Model pre-check wait (seconds)",
        "help": "How long, in all, one model pre-check waits for the per-minute budget to "
                "free before it gives up. 0 never waits.",
        "kind": "int", "min": 0, "max": 900,
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
    # WHICH ANTHROPIC CREDENTIAL PAYS (claude_credential, BOSSAPP-530). Builds, agents and
    # meetings run `claude` on the subscription's login or on the Anthropic API key stored in
    # Repo and models; disconnecting the subscription falls through to the key either way.
    "anthropic_credential_order": {
        "attr": "anthropic_credential_order",
        "label": "Anthropic credential",
        "help": "Which Anthropic credential runs Claude: the subscription first (the API key "
                "when it is disconnected), or the API key first. The key is billed to your "
                "Anthropic API account.",
        "kind": "choice", "choices": ("subscription_first", "api_key_first"),
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
    # BUILD SPEED (build_parallelism). How many sub-agents a build's writing stages may run in
    # parallel. Speed is bought with more tokens at once; the model and every check stay as
    # they are.
    "build_speed": {
        "attr": "build_speed",
        "label": "Build speed",
        "help": "Kept for older installs. A build writes its tests and code in one session unless "
                "Most parallel streams (build_streams_max) opts into a split; same model, same "
                "checks either way.",
        "kind": "choice", "choices": ("balanced", "fastest"),
    },
    "build_parallelism": {
        "attr": "build_parallelism",
        "label": "Parallel sub-agents",
        "help": "Exact number of sub-agents a build may run at once (0 = follow Build speed; "
                "1 = no fan-out).",
        "kind": "int", "min": 0, "max": 8,
    },
    # BUILD EFFORT (code_agent.stage_effort): how hard a build's writing stages think per step.
    "build_effort_writing": {
        "attr": "build_effort_writing",
        "label": "Writing effort",
        "help": "The reasoning effort for writing tests and code (and fix rounds): low (the "
                "default) writes the planned change without re-deliberating every step; design, "
                "plan review and reviewers always run at high.",
        "kind": "choice", "choices": ("low", "medium", "high", "xhigh", "max"),
    },
    "build_test_budget": {
        "attr": "build_test_budget",
        "label": "Tests per build",
        "help": "The most test cases a build writes (default 20): one per acceptance criterion "
                "first, then the riskiest ones. BOSS counts them and trims a suite that is over. "
                "More coverage comes from a later More tests pass.",
        "kind": "int", "min": 5, "max": 200,
    },
    "build_implement_budget_minutes": {
        "attr": "build_implement_budget_minutes",
        "label": "Implement time limit",
        "help": "Minutes the implement stage may take, its fix round included (default 25; 0 = "
                "no limit). Past it the build stops and fails with its failing tests listed.",
        "kind": "int", "min": 0, "max": 240,
    },
    # BUILD STREAMS (build_streams): the bounds each writing stage sizes its parallel streams
    # within, from the host's free cores, memory and load at the moment the stage starts.
    "build_streams_min": {
        "attr": "build_streams_min",
        "label": "Fewest parallel streams",
        "help": "The fewest parallel streams a build stage runs, even on a busy host (default 1).",
        "kind": "int", "min": 1, "max": 8,
    },
    "build_streams_max": {
        "attr": "build_streams_max",
        "label": "Most parallel streams",
        "help": "The most parallel streams a build stage may run when the host has room "
                "(0 = 1: the split is off; 2 or more opts in).",
        "kind": "int", "min": 0, "max": 8,
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


def allowed_values_said(spec: dict[str, Any]) -> str:
    """The values a knob accepts, in a few words, from its own spec (no registry calls): what
    an agent reads before it sets one, so it never guesses a name or a value."""
    kind = spec.get("kind")
    if kind == "int":
        return f"{spec.get('min')}..{spec.get('max')}"
    choices = spec.get("choices")
    if isinstance(choices, (list, tuple)):
        return "|".join(str(c) for c in choices)
    if choices == "harnesses":
        return "a connected coding harness"
    if choices == "chat_engines":
        return "a chat engine"
    if str(choices or "").startswith(("models_for:", "models_for_harness:")):
        return "a model name"
    return "a value"


def catalogue_said() -> str:
    """Every key `set_project_setting` accepts with its allowed values, ONE line, generated from
    KNOBS. Asked to "set the knowledge setting", the agent guessed the key `knowledge`; the real
    one (`knowledge_graph`, on|keyword|off) was in this table and nowhere it could read."""
    return "; ".join(f"{key}={allowed_values_said(spec)}" for key, spec in KNOBS.items())


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
    if spec["kind"] == "call_classes":
        from .call_classes import coerce_policy

        return coerce_policy(value)
    return value


def get_own(connection, project_id: str) -> dict[str, Any]:
    """Every knob THIS PROJECT has set itself (for a model knob: its override of the org's)."""
    rows = connection.execute(
        text("SELECT key, value FROM project_settings WHERE project_id = :p"),
        {"p": project_id},
    ).mappings().all()
    return {r["key"]: r["value"] for r in rows}


def get_all(connection, project_id: str) -> dict[str, Any]:
    """Every knob chosen for this project: the org's model choices (org_models.MODEL_KNOBS),
    with this project's own rows over them. Absent keys are not defaults — they are silence."""
    from . import org_models

    own = get_own(connection, project_id)
    org = org_models.org_values(connection, project_id)
    return {**org, **own} if org else own


def source_of(connection, project_id: str, key: str) -> str:
    """Where this knob's value comes from: 'project', 'org' or 'default'."""
    from . import org_models

    if key in get_own(connection, project_id):
        return "project"
    if org_models.is_org_knob(key) and key in org_models.org_values(connection, project_id):
        return "org"
    return "default"


#: What Govern calls each knob — the names on the board AND in the Log's change lines, one
#: table (D14e: a change read "Agents at once" while the board said "Review lanes"). The KNOBS
#: labels stay as they are: they phrase the bounds errors callers already depend on.
GOVERN_NAMES: dict[str, str] = {
    "code_agent_max_concurrency": "Review lanes",
    "code_agent_max_turns": "Turn budget",
    "code_review_inrun_max_rounds": "Review rounds",
    "captain_leads_projects": "Captain leads",
    "captain_auto_improve": "Captain self-improves",
}


def _record_change(connection, project_id: str, key: str, before: Any, after: Any,
                   actor: str, scope: str = "project") -> None:
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
                        "scope": scope,
                        "source_before": scope if before is not None else "default",
                        "source_after": scope if after is not None else "default"})
    except Exception:  # noqa: BLE001 — the record of a change is not the change
        logger.exception("could not record settings.changed for %s", key)


def _org_scoped(key: str, scope: Optional[str]) -> bool:
    from . import org_models

    if scope not in (None, "", "org", "project"):
        raise ValueError(f"scope is 'org' (every project of the organisation) or 'project', "
                         f"not {scope!r}.")
    return org_models.is_org_knob(key) and scope != "project"


def set_one(connection, project_id: str, key: str, value: Any, actor: str = "operator",
            scope: Optional[str] = None) -> Any:
    """Set one knob. A model knob (org_models.MODEL_KNOBS) is the ORG's unless scope='project':
    every project of the org uses it, and this project's own override of it goes, so the
    project that set it follows it. scope='project' pins it for this project only."""
    clean = coerce(key, value)
    if _org_scoped(key, scope):
        from . import org_models

        org_id = org_models.org_of(connection, project_id)
        if org_id and org_models.resolve_scope(connection, org_id, scope) == "org":
            row = org_models.org_row(connection, org_id, key)
            own = connection.execute(
                text("DELETE FROM project_settings WHERE project_id = :p AND key = :k "
                     "RETURNING value"), {"p": project_id, "k": key}).scalar()
            before = own if own is not None else (row or {}).get("value")
            org_models.write_org(connection, org_id, key, clean, actor)
            _record_change(connection, project_id, key, before, clean, actor, scope="org")
            org_models.invalidate_org(connection, org_id)
            return clean
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


def clear_one(connection, project_id: str, key: str, actor: str = "operator",
              scope: Optional[str] = None) -> None:
    """Hand the knob back to the environment. Deleting is how a project says 'use the default'
    — storing the current default instead would freeze it against a later change.

    A model knob: scope='project' removes only this project's override (it follows the org
    again); otherwise the org's value goes too (every project back to Auto / the install)."""
    if _org_scoped(key, scope):
        from . import org_models

        org_id = org_models.org_of(connection, project_id)
        if org_id and org_models.resolve_scope(connection, org_id, scope) == "org":
            own = connection.execute(
                text("DELETE FROM project_settings WHERE project_id = :p AND key = :k "
                     "RETURNING value"), {"p": project_id, "k": key}).scalar()
            org = org_models.delete_org(connection, org_id, key)
            before = own if own is not None else org
            if before is not None:
                _record_change(connection, project_id, key, before, None, actor, scope="org")
            org_models.invalidate_org(connection, org_id)
            return
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
        from . import org_models

        org_id = org_models.org_of(connection, project_id) if org_models.is_org_knob(key) else None
        row = org_models.org_row(connection, org_id, key) if org_id else None
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
    if row is None:
        from . import org_models

        if org_models.is_org_knob(key):
            row = org_models.org_values(connection, project_id).get(key)
    return fallback if row is None else row


#: Sub-agents per Build speed. Both are one session now (build_streams.DEFAULT_MAX: the split is
#: off unless build_streams_max opts in), so the console never promises parallel agents a build
#: will not run.
BUILD_SPEED_PARALLELISM = {"balanced": 1, "fastest": 1}


def build_parallelism(connection, project_id: Optional[str], settings: Any) -> int:
    """How many parallel streams a build's writing stage would run on this host NOW. Never
    raises; 1 = none. build_parallelism, when set, is the explicit override."""
    from . import build_streams

    try:
        lo, hi, explicit = build_streams.bounds(connection, project_id, settings)
    except Exception:  # noqa: BLE001 — a sizing that cannot read its settings builds with one
        return 1
    if explicit > 0:
        return min(explicit, build_streams.HARD_MAX)
    # The host's capacity right now (free cores after load, free memory), within min..max.
    # A stage refines this at its start with its part count and the model lane
    # (build_streams.for_stage), and records the reasons.
    return max(lo, min(hi, machine_parallelism()))


#: What one stream reserves in memory (build_streams.MEMORY_PER_STREAM_MB).
_MEMORY_PER_SUB_AGENT_MB = 768


def machine_parallelism() -> int:
    """How many streams THIS host could carry right now with no other bound: its free cores
    (after load) and free memory, read inside the container (build_streams.read_host)."""
    from . import build_streams

    host = build_streams.read_host()
    return build_streams.plan_streams(host, min_n=1, max_n=build_streams.HARD_MAX).n
