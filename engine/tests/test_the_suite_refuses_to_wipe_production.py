"""The API test suite must refuse a database it has no business deleting from.

On 2026-09-02 a BOSS code-task agent ran that suite inside the deployed container. DATABASE_URL
there points at production, and its session-scoped autouse fixture DELETEs from roughly twenty
tables before the first test runs. It destroyed every workflow run, meeting, decision and code
task in the live system. Only `discovery_runs` survived, because that table is not in the
delete list.

The agent was doing its job. The defect was that a suite carrying an unconditional DELETE would
run against whatever it was handed and never ask a question.

These live under engine/tests deliberately: the guard must be verifiable WITHOUT a database, and
the API suite's own autouse fixture demands one before any of its tests can run.
"""

from __future__ import annotations

import pytest

from apps.api.tests.db_guard import (
    OVERRIDE,
    ProductionDatabaseRefused,
    assert_safe_to_wipe,
)

PROD = "postgresql+psycopg://boss:boss@10.0.0.5:5432/boss"


class TestItRefusesAnythingNotObviouslyDisposable:
    @pytest.mark.parametrize("url", [
        PROD,
        "postgresql+psycopg://boss:boss@127.0.0.1:5432/boss",
        "postgresql+psycopg://u:p@db.internal:5432/production",
        "postgresql+psycopg://u:p@localhost:5432/boss_prod",
        # The real shape from the incident: looks like a dev URL, points at prod.
        "postgresql+psycopg://boss:boss@postgres:5432/boss",
    ])
    def test_a_database_that_does_not_say_it_is_disposable_is_refused(self, url):
        with pytest.raises(ProductionDatabaseRefused):
            assert_safe_to_wipe(url)

    def test_the_message_says_what_to_do_about_it(self):
        with pytest.raises(ProductionDatabaseRefused) as caught:
            assert_safe_to_wipe(PROD)
        message = str(caught.value)
        # Whoever hits this needs the fix in the error, not in a wiki.
        assert "test" in message
        assert OVERRIDE in message

    @pytest.mark.parametrize("url", [
        "postgresql+psycopg://u:p@localhost:5432/",
        "postgresql+psycopg://u:p@localhost:5432",
    ])
    def test_it_fails_closed_when_it_cannot_tell(self, url):
        """No name to inspect means no permission. Defaulting to "probably fine" here is
        exactly the reasoning that lost the data."""
        with pytest.raises(ProductionDatabaseRefused):
            assert_safe_to_wipe(url)


class TestItStillAllowsRealTestDatabases:
    @pytest.mark.parametrize("url", [
        "postgresql+psycopg://boss:boss@127.0.0.1:5432/boss_test",
        "postgresql+psycopg://boss:boss@127.0.0.1:5432/test_boss",
        "postgresql+psycopg://u:p@localhost:5432/boss_testing",
        "postgresql+psycopg://u:p@ci-db:5432/ci_boss",
        "postgresql+psycopg://u:p@localhost:5432/scratch",
        "postgresql+psycopg://u:p@localhost:5432/boss_test?sslmode=disable",
    ])
    def test_a_database_named_like_a_test_database_passes(self, url):
        assert assert_safe_to_wipe(url) == url

    def test_the_name_is_matched_case_insensitively(self):
        url = "postgresql+psycopg://u:p@localhost:5432/BOSS_TEST"
        assert assert_safe_to_wipe(url) == url


class TestTheOverride:
    """A deliberate escape hatch, and only a deliberate one."""

    def test_the_override_lets_an_oddly_named_database_through(self, monkeypatch):
        monkeypatch.setenv(OVERRIDE, "1")
        assert assert_safe_to_wipe(PROD) == PROD

    @pytest.mark.parametrize("value", ["", "0", "no", "false", "maybe", " "])
    def test_anything_short_of_a_clear_yes_does_not_unlock_it(self, monkeypatch, value):
        monkeypatch.setenv(OVERRIDE, value)
        with pytest.raises(ProductionDatabaseRefused):
            assert_safe_to_wipe(PROD)

    def test_the_override_cannot_be_set_by_accident(self):
        # Named so it cannot be mistaken for routine configuration: anyone typing this has
        # been told exactly what it does.
        assert "MAY_WIPE" in OVERRIDE


def test_the_wiping_fixture_goes_through_the_guard():
    """The guard is worth nothing if the destructive path can skip it."""
    import inspect

    from apps.api.tests import conftest

    source = inspect.getsource(conftest.database_url)
    assert "assert_safe_to_wipe" in source, (
        "database_url is what the engine fixture — and therefore the DELETE loop — is built "
        "from. If it stops calling the guard, the suite can wipe production again."
    )
    assert "database_url" in inspect.getsource(conftest.engine), (
        "the engine must be built from the guarded url, not from os.environ directly"
    )
