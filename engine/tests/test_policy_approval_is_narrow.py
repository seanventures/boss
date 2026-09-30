"""A policy can resolve the PRD gate; it must not become a way around governance.

Auto-approval failed the first time it was written, and would have failed silently: the
approval went through DecisionService.transition as actor "auto_approval", and member
validation raised because no such person exists in any org. The caught exception left the
decision pending, so the feature would have looked merely inert -- an operator still clicking
Approve, with nothing saying why.

Requiring membership asks the wrong question of a policy. But the fix has to stay narrow, or
"the system approved it" becomes the way to approve anything.
"""

from __future__ import annotations

import pytest

from boss_engine.services.decisions import SYSTEM_ACTOR, DecisionService


class _Members:
    """Behaves like the real directory: an unknown actor is rejected."""

    def __init__(self):
        self.checked: list[str] = []

    def require_decision_actor(self, connection, project_id, actor, **kwargs):
        self.checked.append(actor)
        if actor != "a-real-person":
            raise PermissionError(f"{actor} is not a member of {project_id}")
        return {"actor": actor}


ROW = {
    "id": "dec_1", "project_id": "boss-dogfood", "task_id": "tsk_1",
    "status": "pending", "risk_tier": 2, "deadline_at": None,
    "recommendation": {"action": "noop"}, "delegated_to": None,
}


@pytest.fixture()
def service(monkeypatch):
    svc = DecisionService.__new__(DecisionService)
    svc.members = _Members()
    svc.settings = object()
    monkeypatch.setattr(DecisionService, "get_row", lambda self, c, i, **k: dict(ROW))
    return svc


def test_a_policy_approval_does_not_need_to_be_a_person(service, monkeypatch):
    """The bug: SYSTEM_ACTOR is not a member, so membership checks disabled the feature."""
    seen: dict = {}
    monkeypatch.setattr(DecisionService, "_apply_transition",
                        lambda self, *a, **k: seen.setdefault("applied", True), raising=False)
    try:
        service.transition(None, "dec_1", action="approve", actor=SYSTEM_ACTOR,
                           note="policy", system_policy=True)
    except PermissionError:
        pytest.fail("a policy approval must not be rejected for not being a member")
    except Exception:
        # Downstream persistence is not under test; reaching past the actor check is.
        pass
    assert service.members.checked == [], "membership must not be demanded of a policy"


def test_the_bypass_does_not_extend_to_other_actors(service):
    """Otherwise system_policy=True is a way to approve as anyone."""
    with pytest.raises(PermissionError):
        service.transition(None, "dec_1", action="approve", actor="someone-else",
                           note="x", system_policy=True)


def test_the_bypass_does_not_extend_to_rejecting_or_delegating(service):
    """Approving under a stated policy is the only case this exists for."""
    for action in ("reject", "revise", "delegate"):
        with pytest.raises(PermissionError):
            service.transition(None, "dec_1", action=action, actor=SYSTEM_ACTOR,
                               note="x", delegate_to="x", system_policy=True)


def test_without_the_flag_the_system_actor_is_just_an_unknown_actor(service):
    """A caller must SAY it is policy; the name alone must not grant anything."""
    with pytest.raises(PermissionError):
        service.transition(None, "dec_1", action="approve", actor=SYSTEM_ACTOR, note="x")


def test_people_are_still_checked(service):
    """The ordinary path must be untouched."""
    with pytest.raises(PermissionError):
        service.transition(None, "dec_1", action="approve", actor="stranger", note="x")
    assert "stranger" in service.members.checked
