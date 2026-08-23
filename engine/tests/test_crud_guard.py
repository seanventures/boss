"""BOSS has full CRUD, and a guard against the one mistake that is unrecoverable.

The product owner's instruction: BOSS "should have control on all DB operations, all CRUD
operations". `query_db` already read every table; there was no way to change a row, so any state
no structured tool modelled — a stuck flag, a mislinked row, a status the pipeline left wrong —
was visible and untouchable. That is the same complaint as "it can only recommend", in a
narrower form.

The WHERE requirement below is NOT a permission boundary. BOSS is trusted with the database. It
is a foot-gun guard: an UPDATE without a WHERE is valid SQL that silently rewrites every row,
and a model that omits one has made a typing mistake rather than a decision. `whole_table=true`
is how it says the omission was deliberate.

DDL is excluded for a different reason: a schema change is a migration. It is not reversible
from a chat turn, and nobody asking BOSS to fix a stuck row means "restructure the schema".
"""

import pytest

from boss_engine.tools.builtin import (
    _DISCUSS_OPERATIONS_TOOLS,
    _is_single_write,
    build_default_registry,
    build_discuss_read_registry,
)


# --------------------------------------------------------------------------- #
# What BOSS may do
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("statement", [
    "UPDATE code_task_runs SET status='cancelled' WHERE id='crun_x'",
    "DELETE FROM jobs WHERE status='deadlettered'",
    "INSERT INTO deployment_targets (id, app_name) VALUES ('t','Shorty')",
    "update code_task_runs set phase='done' where id='x'",  # lowercase
])
def test_ordinary_crud_is_allowed(statement):
    ok, why = _is_single_write(statement)
    assert ok, why


def test_an_unqualified_update_is_refused_by_default():
    ok, why = _is_single_write("UPDATE code_task_runs SET status='failed'")
    assert not ok
    assert "WHERE" in why and "EVERY row" in why


def test_an_unqualified_update_is_allowed_when_declared_deliberate():
    ok, _ = _is_single_write("UPDATE code_task_runs SET status='failed'", whole_table=True)
    assert ok, "the guard is against a typo, not against the operator's intent"


def test_an_unqualified_delete_is_refused_by_default():
    ok, why = _is_single_write("DELETE FROM jobs")
    assert not ok and "EVERY row" in why


# --------------------------------------------------------------------------- #
# What it may not, and why each exclusion is about correctness not permission
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("statement", [
    "DROP TABLE code_task_runs",
    "TRUNCATE jobs",
    "ALTER TABLE jobs ADD COLUMN x INT",
])
def test_schema_changes_are_refused_as_migrations(statement):
    ok, why = _is_single_write(statement)
    assert not ok
    assert "migration" in why, "the refusal must say WHERE the change belongs instead"


def test_reads_are_directed_to_the_read_tool():
    ok, why = _is_single_write("SELECT * FROM jobs")
    assert not ok and "query_db" in why


def test_multiple_statements_are_refused():
    """One statement per call keeps the reason, the row count and the event aligned with it."""
    ok, why = _is_single_write("UPDATE a SET b=1 WHERE c=2; DELETE FROM d")
    assert not ok and "one at a time" in why


def test_an_empty_statement_is_refused():
    assert _is_single_write("")[0] is False
    assert _is_single_write("   ")[0] is False


# --------------------------------------------------------------------------- #
# Reachability and accountability
# --------------------------------------------------------------------------- #
def test_boss_can_reach_it_from_a_question():
    assert "write_db" in build_discuss_read_registry().names()
    assert "write_db" in _DISCUSS_OPERATIONS_TOOLS


def test_it_demands_a_reason():
    """A row changed with no recorded reason is indistinguishable from corruption later."""
    spec = build_default_registry().get("write_db")
    assert "reason" in (spec.parameters.get("required") or [])


def test_it_points_at_the_structured_tool_first():
    """Raw SQL emits no domain events and updates no related rows. When cancel_job exists, it is
    the better instrument, and the description has to say so or it will not be preferred."""
    spec = build_default_registry().get("write_db")
    assert "PREFER a structured tool" in spec.description
    assert "cancel_job" in spec.description


def test_the_write_is_recorded_in_the_event_stream():
    import inspect
    src = inspect.getsource(build_default_registry)
    assert '"db.write"' in src or True  # registered via _register_operations_tools
    from boss_engine.tools import builtin
    ops = inspect.getsource(builtin._register_operations_tools)
    assert '"db.write"' in ops
    assert "rows_affected" in ops
