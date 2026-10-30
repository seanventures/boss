"""The Graph view's permission withholding, pinned under the gated engine suite.

``view.withheld_for`` is the pure form of the route's ``_withheld``
(``apps/api/app/routers/knowledge_view.py``), exercised end to end over the wire in
``apps/api/tests/test_knowledge_view_api.py``. That apps/api suite cannot run under the
code-task gate (its conftest's db_guard refuses the build-services database and apps/api is
not a verify suite — boss.verify.yaml says so on purpose), so these assertions live here, in the
``engine`` suite the gate DOES run, so a one-line change to the decision/precedent gate is caught.
"""
from __future__ import annotations

from boss_engine.services.knowledge import view


def test_without_decision_read_both_decision_and_precedent_are_withheld():
    # A caller holding only knowledge:read sees neither decisions nor precedents.
    assert view.withheld_for(["knowledge:read"]) == frozenset({"decision", "precedent"})
    # And a caller holding nothing at all is treated the same (knowledge:read is the route's gate).
    assert view.withheld_for([]) == frozenset({"decision", "precedent"})


def test_decision_read_reveals_exactly_the_decision_kinds():
    assert view.withheld_for(["decision:read"]) == frozenset()
    assert view.withheld_for(["knowledge:read", "decision:read"]) == frozenset()


def test_each_kind_is_gated_by_its_own_permission_only():
    # Every withheld kind is gated by its mapped permission, nothing else widens it.
    for kind, permission in view.KIND_PERMISSIONS.items():
        assert kind in view.withheld_for(["knowledge:read"]), kind
        assert kind not in view.withheld_for([permission]), kind
    # An unrelated permission never reveals a gated kind (a mutation from `not in` to `in`,
    # or dropping the permission check, would fail here).
    assert view.withheld_for(["project:admin", "some:other"]) == frozenset(
        view.KIND_PERMISSIONS
    )


def test_result_is_a_frozenset_and_accepts_any_iterable():
    held = (p for p in ("decision:read",))  # a generator, consumed once
    assert view.withheld_for(held) == frozenset()
    assert isinstance(view.withheld_for(set()), frozenset)
    # Duplicates and ordering do not matter.
    assert view.withheld_for(["knowledge:read", "knowledge:read"]) == view.withheld_for(
        ["knowledge:read"]
    )
