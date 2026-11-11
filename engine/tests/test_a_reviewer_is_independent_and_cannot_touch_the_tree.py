"""BOSSAPP-960: the reviewer defaults to another model family, and truly cannot write.

Independence was a convention (`BOSS_CODE_REVIEW_ENGINE` empty = the builder reviews itself)
and claude_code's "read-only" review kept Bash with auto-approval in the live build tree.
Here: the default picks a connected engine from a different family when one exists, and any
engine whose read-only mode still hands the model an unconfined shell reviews a throwaway copy.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from boss_engine.services.engine_contract import ContractedAgent, EngineCapabilities
from boss_engine.services.independence import independent_reviewer


class _ShellyReviewer:
    """An engine like claude_code: declares read_only, but its review seat keeps a shell."""

    engine = "shelly"
    capabilities = EngineCapabilities(read_only=True, read_only_shell=True)

    def __init__(self):
        self.ran_in: Path = Path(".")

    def run_role(self, prompt, cwd, *, read_only=False, model=None, resume_session_id=None,
                 max_turns=None, parallel_agents=0):
        self.ran_in = Path(cwd)
        (Path(cwd) / "planted.txt").write_text("a reviewer wrote this")
        return {"summary": "VERDICT: SURVIVED", "usage": {}, "cost_usd_micros": 0, "turns": 1}

    def run(self, spec, repo, sandbox):  # pragma: no cover
        raise NotImplementedError


def test_an_unconfined_shell_reviews_a_copy_and_the_live_tree_stays_untouched(tmp_path):
    live = tmp_path / "repo"
    live.mkdir()
    (live / "app.py").write_text("print('hi')\n")
    agent = _ShellyReviewer()
    result = ContractedAgent(agent).run_role("judge this", live, read_only=True)
    assert agent.ran_in != live, "the review seat ran in the live build tree"
    assert (agent.ran_in / "app.py").name == "app.py"
    assert not (live / "planted.txt").exists(), "a reviewer write reached the live tree"
    assert result["read_only_by"] == "copy"


def test_a_writing_stage_still_runs_in_the_live_tree(tmp_path):
    live = tmp_path / "repo"
    live.mkdir()
    agent = _ShellyReviewer()
    ContractedAgent(agent).run_role("build this", live, read_only=False)
    assert agent.ran_in == live


def test_claude_codes_review_seat_is_declared_shelled():
    """claude_code's read-only mode is an allowed-tools list that still includes Bash with
    auto-approval; the declaration is what routes its review seats onto the copy."""
    from boss_engine.services.code_agent import ClaudeCodeAgent

    assert ClaudeCodeAgent.capabilities.read_only_shell is True
    assert "Bash" in inspect.getsource(ClaudeCodeAgent._invoke)


def test_the_default_reviewer_is_a_connected_engine_from_another_family(monkeypatch):
    from boss_engine.services import setup_service

    monkeypatch.setattr(setup_service, "connected_engines",
                        lambda c, s, p: {"claude_code": {"kind": "subscription"},
                                         "codex": {"kind": "metered"}})

    class S:
        independence_required_level = "family"

    assert independent_reviewer(object(), S(), "proj", "claude_code") == "codex"
    # The builder's own family is never the answer, so one connected engine changes nothing.
    monkeypatch.setattr(setup_service, "connected_engines",
                        lambda c, s, p: {"claude_code": {"kind": "subscription"}})
    assert independent_reviewer(object(), S(), "proj", "claude_code") is None


def test_the_contract_can_be_switched_off_and_fails_safe(monkeypatch):
    from boss_engine.services import setup_service

    class Off:
        independence_required_level = "off"

    assert independent_reviewer(object(), Off(), "proj", "claude_code") is None

    class S:
        independence_required_level = "family"

    def boom(c, s, p):
        raise RuntimeError("settings table is away")

    monkeypatch.setattr(setup_service, "connected_engines", boom)
    assert independent_reviewer(object(), S(), "proj", "claude_code") is None


def test_the_review_lane_consults_the_independence_default_and_evidence_is_measured():
    from boss_engine.services.model_router import stages
    import boss_engine.services.governed_code_task_service as gov

    assert "independent_reviewer" in inspect.getsource(stages.lane_engines)
    source = inspect.getsource(gov)
    # The gate evidence no longer asserts independence because a review ran; it measures it.
    assert '"reviewer_independent": do_review,' not in source
    assert "assess_independence" in source
