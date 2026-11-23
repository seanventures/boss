"""A transaction opened inside a streamed response closes cleanly when resumed in another context.

Starlette iterates a sync streaming body one step per thread-pool call, each in a COPY of the
request's context. `db.begin` pushed its connection on the open-transactions stack in one copy and
`ContextVar.reset(token)` in another refused the token ("was created in a different Context"): the
activity log's CSV and NDJSON exports died mid-stream, and the security guard that reads them
(apps/api/tests/test_no_secret_leaves_the_log.py::test_exports_are_clean) was red on main.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

from boss_engine import db


class _Engine:
    @contextmanager
    def begin(self):
        yield object()


def test_a_generator_resumed_in_another_context_closes_its_transaction_cleanly():
    def rows():
        with db.begin(_Engine()) as connection:
            assert connection in db.open_transactions()
            yield 1
            yield 2

    stream = rows()
    # Each step in a fresh copy of the context, as starlette.concurrency.iterate_in_threadpool does.
    assert contextvars.copy_context().run(next, stream) == 1
    # The known limit: a later step's context copy never saw the push.
    assert contextvars.copy_context().run(
        lambda: (next(stream), db.open_transactions())) == (2, ())
    assert contextvars.copy_context().run(lambda: list(stream)) == []
    assert db.open_transactions() == ()


def test_nested_transactions_still_unwind_in_order():
    with db.begin(_Engine()) as outer:
        with db.begin(_Engine()) as inner:
            assert db.open_transactions() == (outer, inner)
            assert db.open_transaction() is inner
        assert db.open_transactions() == (outer,)
    assert db.open_transactions() == ()


def test_a_failing_body_still_leaves_the_stack_empty():
    try:
        with db.begin(_Engine()):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.open_transactions() == ()
