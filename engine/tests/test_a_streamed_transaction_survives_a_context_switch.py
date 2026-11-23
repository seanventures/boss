"""A transaction opened inside a streamed response survives being resumed in another context.

Starlette iterates a sync streaming body one step per thread-pool call, each in a COPY of the
context. `db.begin` set its open-transactions stack in the first copy and reset it in another, and
ContextVar.reset refused the token ("was created in a different Context"): the activity log's CSV
and NDJSON exports died mid-stream, and the security guard that reads them
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
    assert contextvars.copy_context().run(next, stream) == 2
    finished = contextvars.copy_context().run(lambda: list(stream))
    assert finished == []
    assert db.open_transactions() == ()


def test_the_ordinary_path_still_restores_the_stack():
    with db.begin(_Engine()) as outer:
        with db.begin(_Engine()) as inner:
            assert db.open_transactions() == (outer, inner)
        assert db.open_transactions() == (outer,)
    assert db.open_transactions() == ()
