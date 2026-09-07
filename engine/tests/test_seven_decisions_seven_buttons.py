"""Thirteen buttons for seven decisions, under a sentence that said seven.

Measured on the Command Center, answering "what changed, what is blocked, what should I do
next?". The prose was right and well evidenced — it even opened by disclosing a query_db call
that had failed on the previous turn. Then it rendered:

    Approve Lead-Triage Inbox (PR #186)      Approve merge gate — Lead-Triage Inbox PR #186 (base)
    Approve Lead-Triage follow-up (#186)     Approve merge gate — Lead-Triage Inbox PR #186 (follow-up)
    Approve Change Calendar (PR #184)        Approve merge gate — Change Calendar PR #184 (base)
    Approve Change Calendar follow-up        Approve merge gate — Change Calendar PR #184 (follow-up)
    Approve Interview Scorecard (PR #182)    Approve merge gate — Interview Scorecard PR #182 (base)
    Approve Interview Scorecard follow-up    Approve merge gate — Interview Scorecard PR #182 (follow-up)
    Close stale budget-reservation decision  Close stale budget decision (blocks nothing)

Six duplicated pairs plus one, above the words "All seven now have a button above."

The cause is this module's own doing. Proposals are collected from the first pass AND from the
resolution loop's continuation, and three recovery paths additionally read the reply for offers
written out as prose, a tag or a call literal — so an offer made as a real tool call is
frequently also described in the sentence above it. Every one of those appended.

A list that contradicts the sentence above it is worse than a shorter list: it makes the
operator check every row to find which button is the real one, on the screen whose whole
purpose is to let them act without checking.
"""
from __future__ import annotations

import inspect

from boss_engine.services.intent_router import remember_offer


def offer(tool: str, args: dict, label: str) -> dict:
    return {"id": f"act_{label[:6]}", "tool": tool, "args": args, "label": label, "why": ""}


class TestOneDecisionOneButton:
    def test_the_pair_from_the_screenshot_collapses(self):
        proposed: list = []
        remember_offer(proposed, offer("decide", {"decision_id": "dec_2963613f",
                                                  "action": "approve"},
                                       "Approve Change Calendar (PR #184)"))
        remember_offer(proposed, offer("decide", {"decision_id": "dec_2963613f",
                                                  "action": "approve"},
                                       "Approve merge gate — Change Calendar PR #184 (base)"))
        assert len(proposed) == 1

    def test_the_first_label_wins(self):
        """A continuation's phrasing is not better for having come second, and keeping the
        earlier one means a button does not rename itself while it is being read."""
        proposed: list = []
        remember_offer(proposed, offer("decide", {"decision_id": "d"}, "Approve Change Calendar"))
        remember_offer(proposed, offer("decide", {"decision_id": "d"}, "Approve merge gate — …"))
        assert proposed[0]["label"] == "Approve Change Calendar"

    def test_internal_markers_do_not_make_two_offers_look_different(self):
        """`_writes_allowed` and `_thread_id` are stamped by the loop, not chosen by the model,
        so two otherwise identical offers must not survive because one carries them."""
        proposed: list = []
        remember_offer(proposed, offer("decide", {"decision_id": "d", "action": "approve"}, "A"))
        remember_offer(proposed, offer(
            "decide", {"decision_id": "d", "action": "approve", "_writes_allowed": True}, "B"))
        assert len(proposed) == 1

    def test_a_different_decision_is_a_different_button(self):
        proposed: list = []
        remember_offer(proposed, offer("decide", {"decision_id": "dec_1"}, "base"))
        remember_offer(proposed, offer("decide", {"decision_id": "dec_2"}, "follow-up"))
        assert len(proposed) == 2

    def test_and_so_is_a_different_action_on_the_same_decision(self):
        """Approve and reject on one decision are two real choices."""
        proposed: list = []
        remember_offer(proposed, offer("decide", {"decision_id": "d", "action": "approve"}, "A"))
        remember_offer(proposed, offer("decide", {"decision_id": "d", "action": "reject"}, "R"))
        assert len(proposed) == 2

    def test_a_different_tool_is_a_different_button(self):
        proposed: list = []
        remember_offer(proposed, offer("decide", {"run_id": "r"}, "decide"))
        remember_offer(proposed, offer("redeploy", {"run_id": "r"}, "redeploy"))
        assert len(proposed) == 2

    def test_seven_decisions_make_seven_buttons(self):
        """The whole case, in the shape it arrived: each decision offered twice."""
        proposed: list = []
        for i in range(7):
            remember_offer(proposed, offer("decide", {"decision_id": f"dec_{i}",
                                                      "action": "approve"}, f"Approve {i}"))
            remember_offer(proposed, offer("decide", {"decision_id": f"dec_{i}",
                                                      "action": "approve"},
                                           f"Approve merge gate — {i}"))
        assert len(proposed) == 7


class TestEveryPathGoesThroughIt:
    BODY = inspect.getsource(
        __import__("boss_engine.services.intent_router", fromlist=["x"]))

    def test_no_proposal_is_appended_directly_any_more(self):
        # One append remains, inside remember_offer itself — that is the point of it.
        assert self.BODY.count("proposed.append(") == 1
        assert "def remember_offer" in self.BODY[:self.BODY.index("proposed.append(")]

    def test_nor_extended_from_a_recovery_path(self):
        """Three recovery paths read the reply for offers the model wrote as prose, a tag or a
        call literal — and an offer made as a real tool call is often also described in the
        sentence above it."""
        assert "proposed.extend(" not in self.BODY

    def test_both_the_first_pass_and_the_continuation_use_it(self):
        assert self.BODY.count("remember_offer(proposed, {") == 2
        assert self.BODY.count("remember_offer(proposed, _found)") == 3
