"""A catalogue improvement must reach projects that already exist.

workflow_mappings pins a VERSION, not a definition. `_seed_default_mappings` returned early
whenever a mapping existed, so the default seeded on a project's first boot stayed pointed at
version 1 for the life of that project. Every catalogue change after that was published, became
the definition's active version, and was never reached by a single run.

Found while pre-flighting a run: the PRD-to-builder binding was published as v2 and active, and
`project.new` still resolved to v1 — the fix would have been inert and reported as deployed.

The rule it protects stays: an operator's mapping is a decision and is never overwritten. Only
BOSS's own default — the seeded priority, pointing at this same catalogue workflow — moves.
"""

from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

from boss_engine.services import workflow_catalogue as wc


class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k) if isinstance(k, str) else list(self.values())[k]


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        if self._row is None:
            return None
        return self._row if isinstance(self._row, dict) else self._row


class _FakeConn:
    """Answers the three queries _repoint_default makes, and records the UPDATE."""

    def __init__(self, mapping_row, newest_version_id):
        self.mapping_row, self.newest = mapping_row, newest_version_id
        self.updated_to = None

    def execute(self, statement, params=None):
        sql = str(statement)
        if "UPDATE workflow_mappings" in sql:
            self.updated_to = (params or {}).get("v")
            return _FakeResult(None)
        if "FROM workflow_mappings m" in sql:
            return _FakeResult(self.mapping_row)
        if "SELECT v.id FROM workflow_versions" in sql:
            return _FakeResult((self.newest,) if self.newest else None)
        return _FakeResult(None)


DEFAULT = wc._DEFAULT_MAPPING_PRIORITY


def _ours(version_id="wfv_1", key="standard_product_delivery", priority=DEFAULT):
    return {"priority": priority, "workflow_version_id": version_id,
            "workflow_key": key, "is_catalogue": True}


def test_bosss_own_default_follows_the_newest_published_version():
    conn = _FakeConn(_ours("wfv_1"), "wfv_2")
    moved = wc._repoint_default(conn, "map_1", "proj", "standard_product_delivery")
    assert moved is True
    assert conn.updated_to == "wfv_2"


def test_an_operators_mapping_is_never_moved():
    """A different priority means a person chose it. Leave it alone."""
    conn = _FakeConn(_ours("wfv_1", priority=100), "wfv_2")
    assert wc._repoint_default(conn, "map_1", "proj", "standard_product_delivery") is False
    assert conn.updated_to is None


def test_a_mapping_onto_a_different_workflow_is_never_moved():
    conn = _FakeConn(_ours("wfv_1", key="fast_fix"), "wfv_2")
    assert wc._repoint_default(conn, "map_1", "proj", "standard_product_delivery") is False
    assert conn.updated_to is None


def test_a_non_catalogue_workflow_is_never_moved():
    row = _ours("wfv_1")
    row["is_catalogue"] = False
    conn = _FakeConn(row, "wfv_2")
    assert wc._repoint_default(conn, "map_1", "proj", "standard_product_delivery") is False
    assert conn.updated_to is None


def test_an_already_current_mapping_is_left_untouched():
    """Idempotent: re-seeding an unchanged catalogue must not write on every boot."""
    conn = _FakeConn(_ours("wfv_2"), "wfv_2")
    assert wc._repoint_default(conn, "map_1", "proj", "standard_product_delivery") is False
    assert conn.updated_to is None


def test_a_missing_mapping_is_not_an_error():
    conn = _FakeConn(None, "wfv_2")
    assert wc._repoint_default(conn, "map_gone", "proj", "standard_product_delivery") is False
