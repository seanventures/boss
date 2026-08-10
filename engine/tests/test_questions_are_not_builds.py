"""Asking BOSS a question must not start work.

An operator asked "Why this failed - can you please investigate the issue - Web app — BugBox".
BOSS convened a five-agent PRD panel ON THE QUESTION, published the resulting document,
auto-approved it, and queued a build. The recorded directive is the question itself.

The router's instruction was "Route exactly one user ask to one enabled BOSS initiator." There
was no option to answer. Every input had to become code, meeting, PRD, discovery or a command,
so a diagnostic question became a product spec -- the router was doing exactly as told.

It stayed survivable while a human approved every PRD: the misroute appeared in the queue and
was declined. Auto-approval removed that reviewer, so the same misroute now spends money and
starts a build unattended. Two changes that are individually reasonable and jointly harmful.
"""

from __future__ import annotations

import inspect
import re

from boss_engine.config import Settings
from boss_engine.services import intent_router


SOURCE = inspect.getsource(intent_router)
#: Source with comment lines removed. Twice now an assertion has matched the COMMENT that
#: explains a defect rather than the code, and reported the defect as still present.
CODE = "\n".join(ln for ln in SOURCE.splitlines() if not ln.lstrip().startswith("#"))


def prompt_text() -> str:
    """The routing instruction as the MODEL receives it, not as the source is laid out.

    Adjacent string literals are joined, so a phrase that happens to straddle a line break in
    the source is still one phrase in the prompt. Asserting against the raw source instead
    tests where the author pressed enter.
    """
    start = CODE.index('"Decide whether this ask should START WORK')
    end = CODE.index("manifest.render_table()", start)
    literal = CODE[start:end]
    joined = re.sub(r'"\s*\n\s*"', "", literal)          # concatenated literals
    return joined.strip().strip('"').replace("\\n", " ")


def test_the_router_is_allowed_to_start_nothing():
    """The missing option. Without it, a question MUST become a build."""
    text = prompt_text()
    assert "SELECT NO TOOL" in text
    for phrase in ("why something failed", "status", "explain"):
        assert phrase in text.lower(), phrase


def test_ambiguity_resolves_towards_not_spending_money():
    """The two mistakes are not symmetric: one wastes a reply, the other runs a pipeline."""
    text = prompt_text().lower()
    assert "unclear" in text
    assert "select nothing" in text


def test_the_prompt_no_longer_demands_that_every_ask_start_work():
    assert "Route exactly one user ask to one enabled BOSS initiator" not in CODE


def test_the_reply_describes_what_will_actually_happen():
    """It said "Publishing remains human-gated" while auto-approval published it 30s later."""
    assert "Publishing remains human-gated" not in CODE
    assert "meeting_prd_auto_approve" in CODE


def test_the_reply_tracks_the_setting_rather_than_hardcoding_it():
    """A sentence about a gate must be read from the gate, or it goes stale exactly like this."""
    convene = CODE[CODE.index("Convened a governed PRD panel") - 900:]
    convene = convene[:1200]
    assert "gated" in convene
    # Both outcomes must be expressible.
    assert "human-gated" in convene
    assert "automatically" in convene


def test_auto_approval_is_the_configured_default_this_guards():
    """If this flips off, the stale-message risk returns in the other direction."""
    assert Settings().meeting_prd_auto_approve is True
