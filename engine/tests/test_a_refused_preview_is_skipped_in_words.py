"""A preview the step's agent may not start is skipped in words, not deadlettered raw.

D1 (2026-10-09): the Captain's improve-pass workflow deadlettered with "'start_preview' needs
preview:manage" — a tool id and a permission key — on every pass.
"""
from __future__ import annotations

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_outcomes
from boss_engine.tools.registry import permission_refusal_words

REFUSED = {"ok": False, "tool": "start_preview", "status": "forbidden", "error": "forbidden",
           "permission": "preview:manage",
           "message": permission_refusal_words("start_preview", "preview:manage")}


def test_the_refusal_is_words():
    words = REFUSED["message"]
    assert "Manage previews" in words and not words.startswith("'start_preview'"), words
    assert "never an agent" in permission_refusal_words("x", "preview:manage", never_delegable=True)


def test_a_refused_preview_is_skipped_with_a_reason():
    out = runner._preview_not_permitted("preview", dict(REFUSED), "qa")
    assert out and out["status"] == "skipped" and out["ok"] is True, out
    assert "qa is not allowed to start previews" in out["reason"]
    # The verdict the runner applies afterwards does not fail it.
    assert workflow_outcomes._refused(out) is None


def test_other_refusals_and_other_steps_still_fail():
    assert runner._preview_not_permitted("deploy", dict(REFUSED), "qa") is None
    assert runner._preview_not_permitted(
        "preview", {"error": "preview_unavailable_no_pr", "preview": None}, "qa") is None
