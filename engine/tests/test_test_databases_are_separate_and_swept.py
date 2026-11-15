"""Test databases live on their own server, carry their birthday, and are swept — nothing else is.

BOSSAPP-1152 (a separate test Postgres, scratch names with a 24 h TTL, a guard against the live
server) and BOSSAPP-1162 (146 leftover `boss_test_*` schemas, 4.8 GB, and "out of shared
memory" on the bench database). The rules live in engine/boss_engine/dev/test_databases.py and
import nothing from the application, so most of this runs without a database. The sweeper is
proven on a throwaway database this file creates on BOSS_TEST_DATABASE_URL's server and drops.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from boss_engine.dev import test_databases as td

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
LIVE = "postgresql+psycopg://boss:s3cret@localhost:5432/boss"


# --------------------------------------------------------------------------------------------- #
# names and the TTL
# --------------------------------------------------------------------------------------------- #

class TestAThrowawayCarriesItsBirthday:
    def test_a_scratch_database_name_ends_in_the_utc_stamp(self):
        assert td.scratch_database_name("st7", NOW) == "boss_scratch_st7_20261114t120000"
        assert td.test_schema_name("api1a2b3c4d", NOW) == "boss_test_api1a2b3c4d_20261114t120000"

    def test_the_name_parses_back_to_its_kind_purpose_and_time(self):
        parsed = td.parse_stamped("boss_scratch_st7_20261114t120000")
        assert parsed is not None
        assert (parsed.kind, parsed.purpose, parsed.created_at) == ("scratch", "st7", NOW)
        parsed = td.parse_stamped("boss_test_api_deadbeef_20261113t090000")
        assert parsed is not None and parsed.purpose == "api_deadbeef"

    @pytest.mark.parametrize("name", [
        "boss", "boss_lf_dev", "boss_px_dev", "postgres", "infisical",
        "boss_test", "boss_test_bench", "boss_test_t1", "boss_test_826", "boss_selffix_test",
        "boss_test_1990c19478fc",            # the API suite's legacy schema: no stamp
        "boss_test_x_20261399t999999",       # looks like a stamp, is not a time
        "boss_test_20261114t120000",         # no purpose
        "boss_test_X_20261114t120000",       # upper case
        "boss_run_ab12cd",
    ])
    def test_everything_else_is_not_a_throwaway(self, name):
        assert td.parse_stamped(name) is None

    def test_the_ttl_is_24_hours_by_the_stamp(self):
        young = td.parse_stamped(td.test_schema_name("x", NOW - timedelta(hours=23, minutes=59)))
        old = td.parse_stamped(td.test_schema_name("x", NOW - timedelta(hours=24, minutes=1)))
        assert young is not None and not young.expired(now=NOW)
        assert old is not None and old.expired(now=NOW)
        assert old.expired(ttl_hours=1, now=NOW) and not young.expired(ttl_hours=48, now=NOW)

    def test_a_purpose_is_lower_case_letters_digits_and_underscores(self):
        with pytest.raises(ValueError):
            td.scratch_database_name("Bad-Name", NOW)
        with pytest.raises(ValueError):
            td.scratch_database_name("x" * 60, NOW)   # over Postgres's 63-byte identifier


# --------------------------------------------------------------------------------------------- #
# the guard
# --------------------------------------------------------------------------------------------- #

class TestTheLiveServerIsNotATestServer:
    def test_a_non_test_database_on_the_live_server_is_refused_in_plain_words(self):
        with pytest.raises(td.LiveServerRefused) as caught:
            td.refuse_live_server("postgresql+psycopg://boss:s3cret@127.0.0.1:5432/boss_lf_dev", [LIVE])
        message = str(caught.value)
        assert "BOSS_TEST_DATABASE_URL" in message and "localhost:5432" in message
        assert "boss_lf_dev" in message
        assert "--profile dev up -d db-test" in message and "55700" in message
        assert "s3cret" not in message, "the message must never carry a password"

    def test_the_live_database_itself_is_refused(self):
        with pytest.raises(td.LiveServerRefused):
            td.refuse_live_server(LIVE, [LIVE])

    @pytest.mark.parametrize("url", [
        "postgresql+psycopg://boss:s3cret@localhost:5432/boss_test_bench",
        "postgresql+psycopg://boss:s3cret@localhost:5432/boss_test",
        "postgresql+psycopg://boss:s3cret@127.0.0.1:5432/boss_scratch_st7_20261114t120000",
    ])
    def test_a_test_named_database_on_the_live_server_passes(self, url):
        assert td.refuse_live_server(url, [LIVE]) == url

    def test_another_server_passes_whatever_the_name(self):
        url = "postgresql+psycopg://boss:s3cret@localhost:55700/boss_lf_dev"
        assert td.refuse_live_server(url, [LIVE]) == url
        url = "postgresql+psycopg://boss:s3cret@db-test:5432/boss_dev"
        assert td.refuse_live_server(url, [LIVE]) == url

    def test_loopback_spellings_and_the_default_port_are_one_server(self):
        assert td.server_of("postgresql://u:p@localhost/boss") == td.server_of("postgresql+psycopg://u:p@127.0.0.1:5432/x")
        assert td.server_of("postgresql://u:p@[::1]:5432/boss") == ("localhost", 5432)
        assert td.server_of("postgresql://u:p@db:5432/boss") == ("db", 5432)

    def test_the_live_server_is_read_from_the_environment(self):
        environ = {"BOSS_TEST_DATABASE_URL": "postgresql+psycopg://boss:s3cret@localhost:5432/boss_dev",
                   "DATABASE_URL": LIVE}
        with pytest.raises(td.LiveServerRefused):
            td.guard_test_url(environ)
        environ["BOSS_TEST_DATABASE_URL"] = "postgresql+psycopg://boss:s3cret@localhost:5432/boss_test_bench"
        assert td.guard_test_url(environ)

    def test_a_disposable_database_url_is_not_mistaken_for_the_live_one(self):
        """The suites set DATABASE_URL to their own test database (bench-env does; self-fix does);
        that is not the install's server. BOSS_DATABASE_URL always is."""
        environ = {"BOSS_TEST_DATABASE_URL": "postgresql+psycopg://boss:p@db:5432/boss_selffix_test",
                   "DATABASE_URL": "postgresql://boss:p@db:5432/boss_selffix_test"}
        assert td.guard_test_url(environ)
        environ["BOSS_DATABASE_URL"] = "postgresql+psycopg://boss:p@db:5432/boss"
        with pytest.raises(td.LiveServerRefused):
            td.guard_test_url(environ)

    def test_the_laptops_live_postgres_is_refused_with_no_variable_naming_it(self, tmp_path):
        """The developer's shell sets neither BOSS_DATABASE_URL nor a live DATABASE_URL; the
        install's published loopback port is what makes localhost:5432 the live server."""
        environ = {"BOSS_INSTALL_DIR": str(tmp_path),
                   "BOSS_TEST_DATABASE_URL": "postgresql+psycopg://boss:s3cret@localhost:5432/boss_lf_dev"}
        with pytest.raises(td.LiveServerRefused) as caught:
            td.guard_test_url(environ)
        assert "s3cret" not in str(caught.value)
        environ["BOSS_TEST_DATABASE_URL"] = "postgresql+psycopg://boss:s3cret@127.0.0.1:5432/boss_test_bench"
        assert td.guard_test_url(environ)
        environ["BOSS_TEST_DATABASE_URL"] = "postgresql+psycopg://boss:s3cret@localhost:55700/boss_lf_dev"
        assert td.guard_test_url(environ), "the test server on 55700 is not the install"

    def test_the_install_port_comes_from_its_env_file(self, tmp_path):
        (tmp_path / ".env").write_text("BOSS_POSTGRES_PORT=5544\n")
        environ = {"BOSS_INSTALL_DIR": str(tmp_path),
                   "BOSS_TEST_DATABASE_URL": "postgresql+psycopg://boss:p@localhost:5544/boss_dev"}
        with pytest.raises(td.LiveServerRefused):
            td.guard_test_url(environ)
        environ["BOSS_TEST_DATABASE_URL"] = "postgresql+psycopg://boss:p@localhost:5432/boss_dev"
        assert td.guard_test_url(environ)
        environ["BOSS_POSTGRES_PORT"] = "5432"
        with pytest.raises(td.LiveServerRefused):
            td.guard_test_url(environ)

    def test_a_bare_ci_postgres_can_say_it_is_not_an_install(self, tmp_path):
        environ = {"BOSS_INSTALL_DIR": str(tmp_path), td.NOT_AN_INSTALL: "1",
                   "BOSS_TEST_DATABASE_URL": "postgresql+psycopg://boss:p@localhost:5432/boss_dev"}
        assert td.guard_test_url(environ)

    def test_the_api_suites_guard_is_reachable(self, tmp_path):
        """db_guard lets `ci_x` through; this guard must not, on the install's loopback port."""
        from apps.api.tests.db_guard import assert_safe_to_wipe

        url = "postgresql+psycopg://boss:p@localhost:5432/ci_x"
        assert assert_safe_to_wipe(url) == url
        with pytest.raises(td.LiveServerRefused):
            td.guard_test_url({"BOSS_INSTALL_DIR": str(tmp_path), "DATABASE_URL": url}, variable="DATABASE_URL")

    def test_no_test_url_is_nothing_to_guard(self):
        assert td.guard_test_url({"DATABASE_URL": LIVE}) is None

    def test_both_conftests_run_the_guard_at_import(self):
        engine = Path("engine/tests/conftest.py").read_text()
        api = Path("apps/api/tests/conftest.py").read_text()
        assert '_guard_test_url(variable="BOSS_TEST_DATABASE_URL")' in engine
        assert 'guard_test_url(variable="DATABASE_URL")' in api
        assert api.index("guard_test_url(") < api.index("from apps.api.app.main import app")
        assert "pytest_sessionstart" in engine and "pytest_sessionstart" in api


# --------------------------------------------------------------------------------------------- #
# the lock table
# --------------------------------------------------------------------------------------------- #

class TestTheLockPreflight:
    def test_the_bench_servers_numbers_are_too_small_and_the_message_says_the_fix(self):
        """64 x 100 = 6400 slots; a schema of 2117 relations x 3 shards = 6351 asked for, plus
        every other session's locks: the exact 'out of shared memory' of BOSSAPP-1162."""
        head = td.LockHeadroom(64, 100, 0, 2117, 3)
        assert head.capacity == 6400 and head.needed == 6351
        assert not head.enough, "6400 slots for 6351 wanted leaves nothing for any other session"
        assert td.LockHeadroom(64, 100, 0, 2117, 1).enough, "one run alone fits, which is why it only failed under load"
        message = head.message()
        assert message and "max_locks_per_transaction=64" in message
        assert "-c max_locks_per_transaction=1024" in message
        assert "db-test" in message

    def test_the_dev_servers_setting_is_enough(self):
        assert td.LockHeadroom(1024, 100, 0, 2117, 6).enough
        assert td.LockHeadroom(1024, 100, 0, 2117, 6).message() is None

    def test_the_recommendation_grows_with_the_schema(self):
        assert td.LockHeadroom(64, 100, 0, 2117, 3).recommended == 1024
        assert td.LockHeadroom(64, 100, 0, 60000, 3).recommended == 4096   # 180000 x 5/4 over 100 sessions

    def test_the_test_postgres_service_ships_the_setting(self):
        compose = Path("scripts/install/community-compose.yml").read_text()
        service = compose.split("\n  db-test:\n", 1)[1].split("\n  # ---", 1)[0]
        assert 'profiles: ["dev"]' in service
        assert "max_locks_per_transaction=${BOSS_TEST_PG_MAX_LOCKS_PER_TRANSACTION:-1024}" in service
        assert "temp_file_limit=${BOSS_TEST_PG_TEMP_FILE_LIMIT:-1GB}" in service
        assert "statement_timeout=${BOSS_TEST_PG_STATEMENT_TIMEOUT:-60s}" in service
        assert "${BOSS_TEST_POSTGRES_PORT:-55700}:5432" in service
        assert "boss_community_pg_test:/var/lib/postgresql/data" in service
        for label in ("owner: boss", "kind: test", "ttl: 24h"):
            assert label in service
        volume = compose.split("\n  boss_community_pg_test:\n", 1)[1].split("\n  boss_community_", 1)[0]
        for label in ("owner: boss", "kind: test", "ttl: 24h"):
            assert label in volume

    def test_the_api_suites_schema_is_stamped_and_dropped_in_batches(self):
        confinement = Path("apps/api/tests/schema_confinement.py").read_text()
        assert "test_schema_name(" in confinement
        assert "drop_schema_in_batches(" in confinement
        assert "uuid.uuid4().hex[:12]" not in confinement


# --------------------------------------------------------------------------------------------- #
# the sweeper's plan (pure)
# --------------------------------------------------------------------------------------------- #

NAMES = [
    "boss", "boss_lf_dev", "boss_test", "boss_test_bench", "boss_test_t1", "public",
    "boss_test_1990c19478fc",                        # legacy API schema
    "boss_test_old_20261113t110000",                 # 25 h: expired
    "boss_test_new_20261114t110000",                 # 1 h: kept
    "boss_scratch_st7_20261112t120000",              # 48 h: expired
    "boss_scratch_fresh_20261114t115959",            # kept
]


class TestTheSweeperPlansOnlyStampedNames:
    def test_only_expired_stamped_names_are_dropped(self):
        drop, kept = td.plan_sweep(NAMES, now=NOW)
        assert drop == ["boss_scratch_st7_20261112t120000", "boss_test_old_20261113t110000"]
        kept_names = {name for name, _ in kept}
        assert {"boss_test_1990c19478fc", "boss_test_new_20261114t110000", "boss_scratch_fresh_20261114t115959"} <= kept_names
        # Names outside the boss_test_/boss_scratch_ families are never in question, so no line;
        # an unstamped boss_test_* is listed as kept, with the reason, so a reader sees why.
        assert not {"boss", "boss_lf_dev", "public", "boss_test"} & kept_names
        assert all("no stamp" in why for name, why in kept if name in ("boss_test_bench", "boss_test_t1"))

    def test_databases_are_only_ever_scratch_ones(self):
        drop, _ = td.plan_sweep(NAMES, now=NOW, kinds=("scratch",))
        assert drop == ["boss_scratch_st7_20261112t120000"]

    def test_the_legacy_api_schema_goes_only_on_request(self):
        drop, _ = td.plan_sweep(NAMES, now=NOW, include_unstamped=True)
        assert "boss_test_1990c19478fc" in drop
        assert "boss_test_t1" not in drop and "boss_test_bench" not in drop and "boss_test" not in drop

    def test_a_longer_ttl_keeps_more(self):
        drop, _ = td.plan_sweep(NAMES, now=NOW, ttl_hours=30)
        assert drop == ["boss_scratch_st7_20261112t120000"]


# --------------------------------------------------------------------------------------------- #
# the sweeper on a real server, inside a throwaway database of its own
# --------------------------------------------------------------------------------------------- #

def _server_url() -> str:
    url = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    if not url or not td.is_test_database_name(td.database_name(url)):
        pytest.skip("needs BOSS_TEST_DATABASE_URL naming a boss_test*/boss_scratch* database")
    return url


@pytest.fixture()
def scratch():
    """A boss_scratch_st7_<rand>_<stamp> database on the test server, dropped after."""
    url = _server_url()
    try:
        with td.scratch_database(url, "st7") as scratch_url:
            yield scratch_url
    except Exception as exc:  # noqa: BLE001
        if "psycopg" in type(exc).__module__ or "Operational" in type(exc).__name__:
            pytest.skip(f"test postgres unreachable: {type(exc).__name__}")
        raise


def _run(url: str, sql: str):
    with td._connect(td.psycopg_dsn(url)) as c:
        cur = c.execute(sql)
        return cur.fetchall() if cur.description else None


def _schemas(url: str) -> set:
    return {r[0] for r in _run(url, "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'boss%'")}


class TestTheSweeperOnAServer:
    def test_dry_run_and_the_real_run_name_the_same_set_and_nothing_else_moves(self, scratch):
        now = datetime.now(timezone.utc)
        expired_schema = td.test_schema_name("old", now - timedelta(hours=25))
        fresh_schema = td.test_schema_name("new", now - timedelta(hours=1))
        legacy_schema = "boss_test_0123456789ab"
        for schema in (expired_schema, fresh_schema, legacy_schema, "boss_test_bench", "boss_lf_dev", "boss"):
            _run(scratch, f'CREATE SCHEMA "{schema}"')
            _run(scratch, f'CREATE TABLE "{schema}".t (id int)')
        expired_db = td.scratch_database_name("st7old", now - timedelta(hours=25))
        _run(td.with_database(scratch, "postgres"), f'CREATE DATABASE "{expired_db}"')
        try:
            dry = td.sweep(scratch, dry_run=True)
            assert dry.schemas == [expired_schema]
            assert dry.databases == [expired_db]
            assert _schemas(scratch) >= {expired_schema, fresh_schema, legacy_schema}, "a dry run drops nothing"

            real = td.sweep(scratch, dry_run=False)
            assert (set(real.schemas), set(real.databases)) == (set(dry.schemas), set(dry.databases))
            assert not real.failures

            left = _schemas(scratch)
            assert expired_schema not in left
            assert {fresh_schema, legacy_schema, "boss_test_bench", "boss_lf_dev", "boss"} <= left
            databases = {r[0] for r in _run(scratch, "SELECT datname FROM pg_database")}
            assert expired_db not in databases
            assert td.database_name(scratch) in databases, "never the database it is connected to"

            # The legacy shape goes only on request, and the real run again matches its dry run.
            dry_legacy = td.sweep(scratch, dry_run=True, include_unstamped=True)
            assert dry_legacy.schemas == [legacy_schema]
            real_legacy = td.sweep(scratch, dry_run=False, include_unstamped=True)
            assert real_legacy.schemas == [legacy_schema]
            assert legacy_schema not in _schemas(scratch)
            assert {"boss_test_bench", "boss_lf_dev", "boss", fresh_schema} <= _schemas(scratch)
        finally:
            _run(td.with_database(scratch, "postgres"), f'DROP DATABASE IF EXISTS "{expired_db}" WITH (FORCE)')

    def test_a_schema_another_session_is_using_is_kept(self, scratch):
        import psycopg

        busy = td.test_schema_name("busy", datetime.now(timezone.utc) - timedelta(hours=30))
        _run(scratch, f'CREATE SCHEMA "{busy}"')
        _run(scratch, f'CREATE TABLE "{busy}".t (id int)')
        holder = psycopg.connect(td.psycopg_dsn(scratch))
        try:
            holder.execute(f'SELECT 1 FROM "{busy}".t')   # a lock held by an open transaction
            report = td.sweep(scratch, dry_run=False)
            assert busy not in report.schemas
            assert any(name == busy and "lock" in why for name, why in report.kept)
            assert busy in _schemas(scratch)
        finally:
            holder.close()

    def test_a_database_someone_is_connected_to_is_kept(self, scratch):
        import psycopg

        busy = td.scratch_database_name("st7busy", datetime.now(timezone.utc) - timedelta(hours=30))
        admin = td.with_database(scratch, "postgres")
        _run(admin, f'CREATE DATABASE "{busy}"')
        holder = psycopg.connect(td.psycopg_dsn(td.with_database(scratch, busy)))
        try:
            dry = td.sweep(scratch, dry_run=True)
            real = td.sweep(scratch, dry_run=False)
            assert busy not in dry.databases and busy not in real.databases
            assert any(name == busy and "connected" in why for name, why in real.kept)
            assert not holder.closed and holder.execute("SELECT 1").fetchone() == (1,), "never killed"
            assert busy in {r[0] for r in _run(admin, "SELECT datname FROM pg_database")}
        finally:
            holder.close()
            _run(admin, f'DROP DATABASE IF EXISTS "{busy}" WITH (FORCE)')

    def test_the_sweeper_never_forces_a_drop(self):
        import inspect

        assert "WITH (FORCE)" not in inspect.getsource(td.sweep)

    def test_the_session_start_waits_two_seconds_at_most(self):
        import time

        start = time.monotonic()
        assert td.session_start("postgresql+psycopg://u:p@10.255.255.1:5432/boss_test") == []
        assert time.monotonic() - start < 8   # sweep + preflight, 2 s each, plus slack

    def test_a_drop_in_batches_takes_the_whole_schema_a_few_tables_at_a_time(self, scratch):
        schema = td.test_schema_name("batched")
        _run(scratch, f'CREATE SCHEMA "{schema}"')
        _run(scratch, f'CREATE TABLE "{schema}".parent (id int PRIMARY KEY)')
        for i in range(5):
            _run(scratch, f'CREATE TABLE "{schema}".child{i} (id int PRIMARY KEY, p int REFERENCES "{schema}".parent(id))')
            _run(scratch, f'CREATE INDEX ON "{schema}".child{i} (p)')
        _run(scratch, f'CREATE VIEW "{schema}".v AS SELECT * FROM "{schema}".parent')
        statements = []

        def execute(sql: str):
            statements.append(sql)
            return _run(scratch, sql)

        dropped = td.drop_schema_in_batches(execute, schema, batch=2)
        assert dropped == 6
        assert sum(1 for s in statements if s.startswith("DROP TABLE")) == 3, "6 tables, 2 per statement"
        assert schema not in _schemas(scratch)

    def test_the_scratch_database_helper_drops_what_it_made(self):
        url = _server_url()
        with td.scratch_database(url, "helper") as scratch_url:
            name = td.database_name(scratch_url)
            assert re.fullmatch(r"boss_scratch_helper_[0-9a-f]{6}_\d{8}t\d{6}", name)
            assert name in {r[0] for r in _run(url, "SELECT datname FROM pg_database")}
        assert name not in {r[0] for r in _run(url, "SELECT datname FROM pg_database")}

    def test_the_preflight_reads_the_servers_real_numbers(self, scratch):
        with td._connect(td.psycopg_dsn(scratch)) as connection:
            head = td.lock_preflight(td._executor(connection), concurrent_runs=1)
        assert head.max_locks_per_transaction >= 64 and head.max_connections > 0
        assert head.capacity == head.max_locks_per_transaction * (head.max_connections + head.max_prepared_transactions)

    def test_the_cli_dry_run_prints_the_plan_without_the_password(self, scratch, capsys):
        from boss_engine.__main__ import main

        expired = td.test_schema_name("cli", datetime.now(timezone.utc) - timedelta(hours=25))
        _run(scratch, f'CREATE SCHEMA "{expired}"')
        assert main(["dev", "sweep-test-db", "--url", scratch, "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "would drop" in out and expired in out
        password = td.psycopg_dsn(scratch).split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
        if password:
            assert password not in out
        assert expired in _schemas(scratch), "a dry run drops nothing"
        assert main(["dev", "test-db-preflight", "--url", scratch, "--runs", "1"]) in (0, 1)
