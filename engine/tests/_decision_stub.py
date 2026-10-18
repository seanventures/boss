"""A stand-in DECISION MODEL for tests: answers agent_decision's JSON contract as told.

Tests that drive a turn through `route_turn` decide its action here, the way a model would —
never by the words of the message (agent_decision.py)."""
from __future__ import annotations

import json
from typing import Callable, Optional, Union


class DecisionModel:
    """`action` is a key, or a function of the rendered prompt returning one."""

    def __init__(self, action: Union[str, Callable[[str], str]], confidence: float = 0.95,
                 question: str = "", reply: Optional[str] = None):
        self.action, self.confidence, self.question = action, confidence, question
        self.reply = reply
        self.prompts: list[str] = []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.prompts.append(str(prompt))
        if self.reply is not None:
            return "resp", self.reply, {"model": "stub-decider"}
        action = self.action(prompt) if callable(self.action) else self.action
        return "resp", json.dumps({"action": action, "confidence": self.confidence,
                                   "reason": "stub", "question": self.question}), \
            {"model": "stub-decider"}


def decided(router, action, **kw) -> DecisionModel:
    """Give `router` a decision model that answers `action`."""
    model = DecisionModel(action, **kw)
    router._decision_adapter = model
    return model

