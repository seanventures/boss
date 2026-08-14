"""Focused always-on agents — templates, planning, and fail-closed validation."""

from boss_engine.services.focused_agents import (
    plan_moves,
    slugify_agent_key,
    template_for,
    validate_spec,
)
from boss_engine.services.metatron_captain import validate_move


def test_templates_cover_the_builder_kinds():
    for kind in ("preview_keeper", "queue_helper", "factory_driver", "reviewer", "custom"):
        template = template_for(kind)
        assert template["name"]
        assert 60 <= int(template["interval_seconds"]) <= 3600


def test_slugify_and_spec_validation():
    assert slugify_agent_key("Preview Keeper!") == "preview-keeper"
    ok = validate_spec(
        name="Preview keeper",
        focus="Keep views up",
        kind="preview_keeper",
        mode="always_on",
        interval_seconds=90,
        allowed_actions=["restart_preview"],
    )
    assert ok["ok"] is True

    empty = validate_spec(
        name="",
        focus="",
        kind="nope",
        mode="forever",
        interval_seconds=3,
        allowed_actions=["explode"],
    )
    assert empty["ok"] is False
    assert "nonempty_name" in empty["failed"]
    assert "known_kind" in empty["failed"]
    assert "known_actions" in empty["failed"]


def test_preview_keeper_plans_restart_not_factory():
    snapshot = {
        "running_previews": 0,
        "completed_verify_green": 1,
        "deadletters": 0,
        "pending_decisions": 0,
        "budget_state": "green",
        "in_flight": 0,
    }
    moves = plan_moves(
        kind="preview_keeper",
        focus="Keep views up",
        allowed_actions=["restart_preview"],
        snapshot=snapshot,
        auto=True,
    )
    kinds = {move["kind"] for move in moves}
    assert "restart_preview" in kinds
    assert "launch_factory_job" not in kinds
    restart = next(move for move in moves if move["kind"] == "restart_preview")
    assert restart["apply"] is True
    verdict = validate_move(
        restart, snapshot, auto=True, in_flight=0, budget_state="green",
        verify_green_run_id="crun_ok",
    )
    assert verdict["ok"] is True


def test_factory_driver_does_not_apply_decisions():
    snapshot = {
        "running_previews": 1,
        "completed_verify_green": 0,
        "deadletters": 2,
        "pending_decisions": 3,
        "budget_state": "green",
        "in_flight": 0,
    }
    moves = plan_moves(
        kind="factory_driver",
        focus="Ship HookWatch settings",
        allowed_actions=["launch_factory_job"],
        snapshot=snapshot,
        auto=True,
    )
    human = [move for move in moves if move.get("kind") in {"escalate_decision", "propose_deadletter_review"}]
    assert human
    assert all(move.get("status") != "applied" for move in human)
    factory = [move for move in moves if move.get("kind") == "launch_factory_job"]
    assert factory
    blocked = validate_move(
        {"kind": "escalate_decision", "status": "needs_human"},
        snapshot, auto=True, budget_state="green",
    )
    assert blocked["ok"] is False


def test_reviewer_never_applies():
    moves = plan_moves(
        kind="reviewer",
        focus="Review HookWatch",
        allowed_actions=["propose_review"],
        snapshot={"completed_verify_green": 2, "running_previews": 1, "deadletters": 0, "pending_decisions": 0},
        auto=True,
    )
    assert any(move["kind"] == "propose_review" for move in moves)
    assert all(move.get("apply") is not True for move in moves)
