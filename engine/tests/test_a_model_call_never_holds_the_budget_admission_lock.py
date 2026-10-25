"""A governed model call never holds the project's budget-admission lock while the provider
answers (brec_042fa4c58dbb40259009; diagnosed by BOSS in self-fixes sfx_3ae11453 and
sfx_be271156, implemented by the shepherd after both were refused).

budget.reserve takes `boss-budget:<project>` with pg_try_advisory_xact_lock — transaction
scoped. GovernedChatAdapter._open reserved on the caller's long-lived connection and the 12-78 s
provider call then ran on that same open transaction, so the lock was held for the whole call
and every other admission in the project — a person's plain chat question — was answered
"Budget admission is busy". The reservation now commits on its own short transaction first.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import model_routing

from .test_workflow_capability_runner import eng, project  # noqa: F401


class _Inner:
    """The provider: while it 'answers', another connection asks for the admission lock."""

    def __init__(self, eng, project_id):
        self.eng, self.project_id = eng, project_id
        self.lock_free_during_call = None
        self.reservation_visible_during_call = None

    def create_text_response(self, *_a, **_k):
        with self.eng.connect() as other:
            with other.begin():
                self.lock_free_during_call = bool(other.execute(text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('boss-budget:' || :p, 0))"),
                    {"p": self.project_id}).scalar_one())
                self.reservation_visible_during_call = other.execute(text(
                    "SELECT count(*) FROM budget_reservations WHERE project_id = :p "
                    "AND category = 'knowledge'"), {"p": self.project_id}).scalar_one()
        return "resp_1", "BOSS_OK", {"model": "m", "prompt_tokens": 1, "completion_tokens": 1}


def _adapter(inner, connection, project_id):
    return model_routing.GovernedChatAdapter(
        inner, connection, get_settings(), project_id, skill_id="test.turn",
        ref=f"t_{uuid.uuid4().hex[:6]}", estimate_usd_micros=1,
        choice=model_routing.ModelChoice("native", "test"), category="knowledge")


def _forget_spend(eng, project_id):
    """The committed reservation and receipt are real rows: removed so the project fixture can
    remove its project."""
    with eng.begin() as c:
        for table in ("cost_receipts", "receipts", "budget_reservations"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project_id})


def test_the_provider_runs_with_the_admission_lock_free(eng, project):
    inner = _Inner(eng, project)
    try:
        with eng.connect() as caller:
            with caller.begin():                  # the caller's long transaction stays open
                _adapter(inner, caller, project).create_text_response("hi")
    finally:
        _forget_spend(eng, project)
    assert inner.lock_free_during_call is True, (
        "model_routing.GovernedChatAdapter._open left the budget admission lock held across "
        "the provider call")
    assert inner.reservation_visible_during_call >= 1, (
        "the reservation was not committed before the provider call")


def test_a_project_the_caller_has_not_committed_reserves_in_place(eng):
    """A project created in the caller's own open transaction (its first turn, a test) is not
    visible to a fresh connection: the reservation stays on the caller's, as before."""
    suffix = uuid.uuid4().hex[:8]
    project_id = f"proj_inplace_{suffix}"
    inner = SimpleNamespace(create_text_response=lambda *_a, **_k: (
        "resp_2", "BOSS_OK", {"model": "m", "prompt_tokens": 1, "completion_tokens": 1}))
    with eng.connect() as caller:
        tx = caller.begin()
        try:
            caller.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'IP')"), {"o": f"org_{suffix}"})
            caller.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'IP')"),
                           {"w": f"ws_{suffix}", "o": f"org_{suffix}"})
            caller.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                                "VALUES (:p, :w, 'IP', :s)"),
                           {"p": project_id, "w": f"ws_{suffix}", "s": f"ip-{suffix}"})
            result = _adapter(inner, caller, project_id).create_text_response("hi")
            assert result[1] == "BOSS_OK"
            assert caller.execute(text(
                "SELECT count(*) FROM budget_reservations WHERE project_id = :p"),
                {"p": project_id}).scalar_one() >= 1
        finally:
            tx.rollback()
