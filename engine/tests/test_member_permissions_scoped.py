"""A member's permissions carry the scoped equivalents of its legacy capabilities.

The Graph view's read routes (apps/api/app/routers/knowledge_view.py, exercised over HTTP by
apps/api/tests/test_knowledge_view_api.py) withhold decision and precedent nodes from a caller
without ``decision:read``. On the fallback path that reads the stored member row, a seeded owner
holds the legacy capability ``decision_resolve`` rather than the scoped ``decision:read`` string,
so without translation the owner would be wrongly withheld its own decisions. The translation
lives in boss_engine.services.members._scoped_permissions; this pins it.
"""
from __future__ import annotations

from boss_engine.identity.migration import capability_permissions
from boss_engine.services.members import _scoped_permissions


def test_legacy_decision_resolve_gains_scoped_decision_read():
    scoped = _scoped_permissions(["decision_resolve"])
    # The legacy string is kept (checks against the old form still work) ...
    assert "decision_resolve" in scoped
    # ... and its scoped catalogue permissions are folded in, so the owner holds decision:read.
    assert "decision:read" in scoped
    assert "decision:approve" in scoped


def test_an_already_scoped_permission_is_untouched_and_grants_nothing_extra():
    # An agent's narrow read/write view is seeded with the scoped form directly; it must not
    # silently acquire decision:read.
    scoped = _scoped_permissions(["knowledge:read"])
    assert scoped == ["knowledge:read"]
    assert "decision:read" not in scoped


def test_star_capability_expands_to_every_catalogue_permission():
    scoped = set(_scoped_permissions(["*"]))
    assert capability_permissions(["*"]) <= scoped
    assert "decision:read" in scoped


def test_empty_and_unknown_permissions_are_safe():
    assert _scoped_permissions([]) == []
    assert _scoped_permissions(None) == []
    # An unknown string is kept verbatim and maps to no scoped permission.
    assert _scoped_permissions(["not_a_capability"]) == ["not_a_capability"]


def test_the_result_has_no_duplicates_and_keeps_stored_order():
    scoped = _scoped_permissions(["decision_resolve", "decision:read"])
    assert scoped[:2] == ["decision_resolve", "decision:read"]
    assert len(scoped) == len(set(scoped))
