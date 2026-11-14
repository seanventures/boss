"""WHICH SURFACES RUN ON THE KERNEL (AP-P1b; design decision D3: per surface, each after parity).

One table, one reading of the environment. A flag is ON when its variable is unset or empty and
its default is on, and OFF only when set to an off word ("0", "false", "no", "off") — so the
escape hatch is explicit and a blank line in an .env never turns a surface back to its old loop.
The community compose forwards each variable with the same default (scripts/install/
community-compose.yml), so an install that sets nothing runs what this table says.

    BOSS_KERNEL_DECISIONS  K2: agent_decision.decide's read phase. ON.
                           Evidence: test_kernel_decisions, test_kernel_conformance.
    BOSS_KERNEL_CHAT       K6: EngineAgent.chat / IntentRouter.route_turn on the OpenAI-compatible
                           and Anthropic-native lanes (CLI text brains keep their loop until
                           K8/K10). ON. Evidence: test_kernel_chat, the chat suites flag-on.
    BOSS_KERNEL_STEPS      K4: agent_loop.run_agent_loop (workflow agent steps, the factory
                           plan). ON. Evidence: test_kernel_agent_steps (shadow comparison), the
                           agent-step suites flag-on.
    BOSS_KERNEL_MEETINGS   Meeting seats, the synthesis and the PRD draft. ON.
                           Evidence: test_kernel_meetings, the meeting suites flag-on.

    BOSS_CONTEXT_POLICY    AP-P6: the context policy (kernel/context_policy.py) on chat and agent
                           steps — the run filesystem, offload instead of the CUT clip,
                           summarisation at 85%, tool-result clearing, skills on demand. ON.
                           Evidence: test_ap_p6_context_engineering.

(K3 the pre-check and K11 the native coder are not flipped here.)
"""
from __future__ import annotations

import os

OFF_WORDS = ("0", "false", "no", "off")

KERNEL_DECISIONS = "BOSS_KERNEL_DECISIONS"
KERNEL_CHAT = "BOSS_KERNEL_CHAT"
KERNEL_STEPS = "BOSS_KERNEL_STEPS"
KERNEL_MEETINGS = "BOSS_KERNEL_MEETINGS"
CONTEXT_POLICY = "BOSS_CONTEXT_POLICY"

#: Each surface's default when its variable is unset. Flipped per surface, each after parity.
DEFAULTS: dict = {
    KERNEL_DECISIONS: True,
    KERNEL_CHAT: True,
    KERNEL_STEPS: True,
    KERNEL_MEETINGS: True,
    CONTEXT_POLICY: True,
}


def on(name: str) -> bool:
    """Whether surface flag `name` is on: unset/empty takes its default; an off word is off;
    anything else set is on."""
    said = (os.environ.get(name) or "").strip().lower()
    if not said:
        return bool(DEFAULTS.get(name, False))
    return said not in OFF_WORDS


__all__ = ["CONTEXT_POLICY", "DEFAULTS", "KERNEL_CHAT", "KERNEL_DECISIONS", "KERNEL_MEETINGS", "KERNEL_STEPS",
           "OFF_WORDS", "on"]
