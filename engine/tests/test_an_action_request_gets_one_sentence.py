"""An action request gets one short sentence and a button (operator, 2026-11-19).

Asked in the captain drawer "add the bench name label to the map bubble — simple change, please
update quickly", the captain replied with `public/app.js` openBench/renderPanel, `server.js`, a
`crun_…` build id, branch and PR mechanics, and closed by asking who should review PR #2.

The rule lives once (services/reply_style.py) and is in the system prompt of the one agent path:
the chat answer the router builds (intent_router) and the engine agent's default prompt. The
console folds whatever follows a `Details:` line (apps/web/lib/answer-details.ts).
"""
from __future__ import annotations

import inspect
from pathlib import Path

from boss_engine.agent.engine_agent import _SYSTEM_PROMPT
from boss_engine.services import intent_router
from boss_engine.services.reply_style import ACTION_REPLY_STYLE, DETAILS_MARKER

ROOT = Path(__file__).resolve().parents[2]


def test_the_rule_says_one_sentence_and_a_button_with_its_example():
    assert "ONE short plain sentence" in ACTION_REPLY_STYLE
    assert "button" in ACTION_REPLY_STYLE
    # The example is the shape, in the person's words, with the change by its number.
    assert "press Start to run it" in ACTION_REPLY_STYLE
    assert "change #2" in ACTION_REPLY_STYLE


def test_it_names_what_must_stay_out_of_the_reply():
    for kept_out in ("file names", "function names", "crun_", "branch names", "PR mechanics"):
        assert kept_out in ACTION_REPLY_STYLE, kept_out
    assert "NEVER END WITH AN UNNECESSARY QUESTION" in ACTION_REPLY_STYLE
    assert "Who reviews" in ACTION_REPLY_STYLE


def test_details_go_after_the_marker_the_console_folds():
    assert DETAILS_MARKER == "Details:"
    assert f"`{DETAILS_MARKER}`" in ACTION_REPLY_STYLE
    web = (ROOT / "apps/web/lib/answer-details.ts").read_text()
    assert f'DETAILS_MARKER = "{DETAILS_MARKER}"' in web


def test_it_overrides_the_evidence_rules_only_for_action_requests():
    assert "overrides the evidence rules" in ACTION_REPLY_STYLE
    assert "diagnoses and questions" in ACTION_REPLY_STYLE


def test_the_router_chat_prompt_carries_it():
    """The captain, Ask BOSS and every role chat answer through the router's system prompt."""
    src = inspect.getsource(intent_router)
    assert "from .reply_style import ACTION_REPLY_STYLE" in src
    assert "+ ACTION_REPLY_STYLE +" in src
    # Placed beside the answer-first rule, in the static part of the prompt (cached prefix).
    assert src.index("+ ACTION_REPLY_STYLE +") > src.index('"ANSWER THE QUESTION THAT WAS ASKED.\\n"')
    assert src.index("+ ACTION_REPLY_STYLE +") < src.index("_baseline_primer()\n")


def test_the_engine_agent_default_prompt_carries_it_and_stops_asking_for_run_ids():
    assert ACTION_REPLY_STYLE in _SYSTEM_PROMPT
    assert "Give run ids and URLs when you have them" not in _SYSTEM_PROMPT
