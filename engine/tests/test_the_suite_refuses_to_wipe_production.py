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


class TestTheSuiteCannotReachAnotherSchema:
    """Two independent defences, asserted separately because either alone is a single point.

    1. The guard refuses a database that is not named like a test database.
    2. Confinement gives the run its own schema, so even an allowed database is only ever
       touched inside a namespace created for this run and dropped afterwards.

    The reset statements themselves are unchanged and still delete unconditionally. That is
    fine — and only fine — because of (2). If confinement is ever removed, the 2026-09-02
    failure mode returns exactly as it was.
    """

    def _conftest_source(self) -> str:
        from pathlib import Path

        return Path("apps/api/tests/conftest.py").read_text()

    def test_the_guard_runs_before_anything_reads_the_database(self):
        source = self._conftest_source()
        assert "_assert_safe_to_wipe(_BASE_DATABASE_URL)" in source

    def test_the_run_is_confined_to_its_own_schema(self):
        source = self._conftest_source()
        assert "confine_to_private_schema" in source

    def test_confinement_happens_before_the_application_is_imported(self):
        """The app builds its own engine from DATABASE_URL at import. Confining after that
        leaves the app in `public` and the tests in their own schema — which is not just
        unsafe, it fails 18 tests, which is how this ordering was found."""
        source = self._conftest_source()
        assert source.index("confine_to_private_schema(") < source.index(
            "from apps.api.app.main import app"
        )

    def test_the_schema_is_dropped_when_the_run_ends(self):
        from pathlib import Path

        confinement = Path("apps/api/tests/schema_confinement.py").read_text()
        assert "DROP SCHEMA IF EXISTS" in confinement
        assert "atexit.register" in confinement, (
            "the schema is created before any fixture exists and must go away even if "
            "collection fails"
        )

    def test_the_search_path_travels_on_the_url(self):
        """So that engines built later by the application inherit the confinement instead of
        quietly falling back to public."""
        from pathlib import Path

        confinement = Path("apps/api/tests/schema_confinement.py").read_text()
        assert "search_path" in confinement
        assert 'os.environ["DATABASE_URL"]' in confinement
