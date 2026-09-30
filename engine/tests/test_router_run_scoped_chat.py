"""A chat opened from an app card must CHANGE that app, not rebuild it.

The agent dock is opened from a card, and a card holds a RUN id (crun_...) — that is what makes
the conversation unambiguous when fifty apps exist. But the router's "does this chat already
have an app?" lookup only knew how to search by task_id, so a run-scoped chat found nothing,
`extend` came out False, and "add a currency picker" would have started a fresh build on `main`
instead of a change on the app's own branch.

That is the most expensive possible misreading of a cheap request, and it is silent: the
operator asks for a small change and gets a rebuild that looks like a normal run.
"""

from __future__ import annotations

import json

from boss_engine.services import intent_router as ir


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Connection:
    """Answers whichever lookup the router chooses, and records which one that was."""

    def __init__(self, *, by_run=None, by_task=None):
        self.by_run = by_run
        self.by_task = by_task
        self.queried = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.queried.append(sql)
        if "id = :r" in sql:
            return _Result(self.by_run)
        return _Result(self.by_task)


def _router():
    return ir.IntentRouter.__new__(ir.IntentRouter)


def test_a_run_scoped_chat_finds_the_app_by_its_run_id():
    connection = _Connection(by_run={"id": "crun_app", "finalize_ctx": {}})
    branch, root = _router()._existing_app_for_task(connection, "p1", "crun_app")
    assert (branch, root) == ("boss/code/crun_app", "crun_app")


def test_it_uses_the_lineage_root_so_a_retry_lands_on_the_app_branch():
    """A retry's own id is not the app's branch; the root's is."""
    connection = _Connection(by_run={"id": "crun_retry",
                                     "finalize_ctx": {"root_run_id": "crun_app"}})
    branch, root = _router()._existing_app_for_task(connection, "p1", "crun_retry")
    assert (branch, root) == ("boss/code/crun_app", "crun_app")


def test_finalize_ctx_stored_as_json_text_is_still_read():
    connection = _Connection(by_run={"id": "crun_x",
                                     "finalize_ctx": json.dumps({"root_run_id": "crun_app"})})
    assert _router()._existing_app_for_task(connection, "p1", "crun_x")[1] == "crun_app"


def test_a_task_scoped_chat_still_searches_by_task():
    """The original path must keep working — this is an addition, not a replacement."""
    connection = _Connection(by_task={"id": "crun_from_task", "finalize_ctx": {}})
    branch, _ = _router()._existing_app_for_task(connection, "p1", "tsk_1")
    assert branch == "boss/code/crun_from_task"
    assert not any("id = :r" in q for q in connection.queried)


def test_an_unknown_run_extends_nothing_rather_than_guessing():
    connection = _Connection(by_run=None)
    assert _router()._existing_app_for_task(connection, "p1", "crun_missing") == (None, None)


def test_no_scope_at_all_extends_nothing():
    assert _router()._existing_app_for_task(_Connection(), "p1", None) == (None, None)
