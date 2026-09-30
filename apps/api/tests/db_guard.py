"""Refuse to run a destructive test suite against a database that is not disposable.

THIS EXISTS BECAUSE IT ALREADY HAPPENED. On 2026-09-02 a BOSS code-task agent ran the API test
suite inside the deployed container. DATABASE_URL there points at production, and conftest's
session-scoped autouse fixture DELETEs from roughly twenty tables before the first test runs.
It destroyed every workflow run, meeting, decision and code task in the live system.
`discovery_runs` survived only because that table is not in the delete list.

The agent was doing its job — running the tests is the job. The defect was that a suite carrying
an unconditional DELETE would run against whatever it was handed and never ask a question.

Kept in its own module, deliberately: it imports nothing from the application, so it can be
tested without a database, and so nothing in the import graph can make the guard itself fail to
load and take the safety catch with it.
"""

from __future__ import annotations

import os


class ProductionDatabaseRefused(RuntimeError):
    """The test suite was pointed at a database it is not allowed to destroy."""


#: A database this suite may wipe has to SAY it is disposable. Matched on the database NAME
#: rather than the host, because a test database and production routinely live on the same
#: host — the name is the only part that reliably tells them apart.
DISPOSABLE_NAMES = ("test", "testing", "ci", "scratch", "tmp")

#: Set this to run against a database whose name does not look disposable. It exists for the
#: one legitimate case — a differently-named local database — and it must be typed on purpose,
#: which is exactly what nobody does by accident inside a container.
OVERRIDE = "BOSS_TESTS_MAY_WIPE_THIS_DATABASE"

_YES = {"1", "true", "yes"}


def database_name(url: str) -> str:
    tail = url.rsplit("/", 1)[-1] if "/" in url else ""
    return tail.split("?", 1)[0].strip().lower()


def assert_safe_to_wipe(url: str) -> str:
    """Return the url, or raise if these tests have no business deleting from it.

    Fails CLOSED: anything this cannot positively identify as disposable is refused. Defaulting
    to "probably fine" is the reasoning that lost the data.
    """
    if os.environ.get(OVERRIDE, "").strip().lower() in _YES:
        return url
    name = database_name(url)
    if not name:
        raise ProductionDatabaseRefused(
            "DATABASE_URL has no database name, so these tests cannot tell whether wiping it "
            f"is safe. Point them at a test database, or set {OVERRIDE}=1 if you are certain."
        )
    if not any(marker in name for marker in DISPOSABLE_NAMES):
        raise ProductionDatabaseRefused(
            f"Refusing to run: database {name!r} is not named like a test database, and this "
            "suite DELETEs from ~20 tables before every session. This is the guard that was "
            "missing when an agent ran these tests against production on 2026-09-02 and "
            "destroyed the run history. If this really is disposable, rename it to include "
            f"'test' or set {OVERRIDE}=1."
        )
    return url
