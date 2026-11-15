"""Test databases: where they may live, what they are called, and who cleans them up.

Storage hygiene, layer 1 (BOSSAPP-1152, BOSSAPP-1162). Three rules, one module, no application
imports — like apps/api/tests/db_guard.py it must be testable without a database and nothing in
the import graph may stop the safety catch from loading.

1. THE LIVE SERVER IS NOT A TEST SERVER. On 2026-11-14 the operator's install held ~9 GB of
   test, bench, dev and scratch databases inside the same Postgres as the live BOSS, and one slow
   test query spilled 11 GB of temp files next to the real data. `refuse_live_server` turns a
   test URL on the install's server away unless the database says it is a test one by name
   (`boss_test*`, `boss_scratch*`). The separate test server is the `db-test` compose service
   (profile `dev`, port 55700).

2. A THROWAWAY CARRIES ITS BIRTHDAY IN ITS NAME. `boss_scratch_<purpose>_<stamp>` databases and
   `boss_test_<purpose>_<stamp>` schemas end in a UTC `YYYYmmddtHHMMSS` stamp. Postgres records
   no creation time for a schema or a database, so the name is the only place a sweeper can read
   an age from — and the only reason it can promise to touch nothing else. `boss`, `boss_lf_dev`,
   `boss_test`, `boss_test_bench`, `boss_test_t1`: no stamp, never a candidate.

3. A DROP TAKES LOCKS IN BATCHES. The API suite's private schema holds ~2100 relations (578
   tables, 675 indexes, their toast tables and sequences). `DROP SCHEMA ... CASCADE` takes an
   AccessExclusive lock on every one of them in ONE transaction, and the server's lock table has
   only max_locks_per_transaction (64) x max_connections (100) = 6400 slots for EVERY session
   together. Three API shards ending at once asked for ~6350: "out of shared memory — You might
   need to increase max_locks_per_transaction", the drop failed, the schema stayed. 146 leftover
   schemas (298,000 catalog rows, 4.8 GB) later, every information_schema query the suites make
   crawled. `drop_schema_in_batches` drops the tables a hundred at a time, each in its own
   transaction, so one drop needs a few hundred slots instead of two thousand; `lock_preflight`
   warns at session start when the server's lock table is too small for the schema anyway.
"""

from __future__ import annotations

import atexit
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

#: The UTC stamp a throwaway's name ends in. Second precision: two throwaways made in the same
#: second by the same purpose would collide, so a caller that needs more adds a random purpose
#: suffix (as the API suite does).
STAMP_FORMAT = "%Y%m%dt%H%M%S"

#: Names a sweeper may consider at all. A purpose is lower-case letters, digits and underscores.
STAMPED = re.compile(r"^boss_(?P<kind>test|scratch)_(?P<purpose>[a-z0-9][a-z0-9_]*?)_(?P<stamp>\d{8}t\d{6})$")

#: The one legacy shape the sweeper may remove on request: the API suite's private schema as
#: schema_confinement named it before the stamp (`boss_test_` + 12 hex digits, nothing else).
LEGACY_API_SCHEMA = re.compile(r"^boss_test_[0-9a-f]{12}$")

#: Database names that say "a test may create and drop things here" (the install-server rule).
TEST_NAME_PREFIXES = ("boss_test", "boss_scratch")

#: Default time-to-live for a stamped throwaway, in hours (the ST-6 `ttl=24h` label).
DEFAULT_TTL_HOURS = 24.0

#: Tables per DROP statement. ~4 locks per table (table, toast, toast index, an index or two)
#: keeps one batch around 400-500 slots: far under the default lock table even with other
#: sessions busy.
DROP_BATCH = 100

#: Postgres identifiers are 63 bytes.
MAX_IDENTIFIER = 63

#: How many test runs the lock preflight assumes can end at the same moment. The test tier runs
#: up to 6 API shards; three is the number that actually ran out on the bench server.
DEFAULT_CONCURRENT_RUNS = 3


# ---------------------------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StampedName:
    name: str
    kind: str          # "test" | "scratch"
    purpose: str
    created_at: datetime

    def age(self, now: Optional[datetime] = None) -> timedelta:
        return _utcnow(now) - self.created_at

    def expired(self, ttl_hours: float = DEFAULT_TTL_HOURS, now: Optional[datetime] = None) -> bool:
        return self.age(now) > timedelta(hours=ttl_hours)


def _utcnow(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def stamp(now: Optional[datetime] = None) -> str:
    return _utcnow(now).strftime(STAMP_FORMAT)


def _check_purpose(purpose: str, prefix: str, now: Optional[datetime]) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", purpose):
        raise ValueError(f"a throwaway's purpose is lower-case letters, digits and underscores, not {purpose!r}")
    name = f"{prefix}_{purpose}_{stamp(now)}"
    if len(name) > MAX_IDENTIFIER:
        raise ValueError(f"{name!r} is longer than Postgres's {MAX_IDENTIFIER}-byte identifier limit; shorten the purpose")
    return name


def scratch_database_name(purpose: str, now: Optional[datetime] = None) -> str:
    """`boss_scratch_<purpose>_<stamp>`: a database a test makes and drops."""
    return _check_purpose(purpose, "boss_scratch", now)


def test_schema_name(purpose: str, now: Optional[datetime] = None) -> str:
    """`boss_test_<purpose>_<stamp>`: a schema a test run makes and drops."""
    return _check_purpose(purpose, "boss_test", now)


def parse_stamped(name: str) -> Optional[StampedName]:
    """The stamped throwaway `name` describes, or None for anything else — including every
    permanent database (`boss`, `boss_lf_dev`), the shared test databases (`boss_test`,
    `boss_test_bench`, `boss_test_t1`) and an unstamped legacy schema."""
    m = STAMPED.match(name)
    if not m:
        return None
    try:
        created = datetime.strptime(m.group("stamp"), STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:  # 20261399T999999 matches the regex and is not a time
        return None
    return StampedName(name=name, kind=m.group("kind"), purpose=m.group("purpose"), created_at=created)


def is_test_database_name(name: str) -> bool:
    return name.lower().startswith(TEST_NAME_PREFIXES)


# ---------------------------------------------------------------------------------------------
# URLs (no SQLAlchemy: this module loads before the suites decide whether they have a database)
# ---------------------------------------------------------------------------------------------

def database_name(url: str) -> str:
    path = urlsplit(url).path if "://" in url else ""
    return path.rsplit("/", 1)[-1].strip().lower() if path else ""


def server_of(url: str) -> Tuple[str, int]:
    """(host, port) with loopback spellings folded together and the default port filled in, so
    `localhost`, `127.0.0.1` and `::1` on 5432 compare equal."""
    parts = urlsplit(url)
    host = (parts.hostname or "localhost").lower()
    if host in ("127.0.0.1", "::1", "localhost", ""):
        host = "localhost"
    try:
        port = parts.port or 5432
    except ValueError:
        port = 5432
    return host, port


def redacted(url: str) -> str:
    """The URL without its password, for messages. Never print a URL any other way."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    userinfo = parts.username or ""
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{userinfo}:***@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


def psycopg_dsn(url: str) -> str:
    """A SQLAlchemy `postgresql+psycopg://` URL as libpq wants it."""
    scheme, rest = url.split("://", 1)
    return "postgresql://" + rest


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + name))


# ---------------------------------------------------------------------------------------------
# rule 1: the live server is not a test server
# ---------------------------------------------------------------------------------------------

class LiveServerRefused(RuntimeError):
    """A test database URL points into the live install's Postgres."""


#: The variables that name the live install's database. BOSS_DATABASE_URL always counts;
#: DATABASE_URL counts only when it is not itself disposable by name (apps/api/tests/db_guard's
#: markers), because the suites routinely set DATABASE_URL to their own test database.
LIVE_URL_VARIABLES = ("BOSS_DATABASE_URL", "DATABASE_URL")
_DISPOSABLE_MARKERS = ("test", "testing", "ci", "scratch", "tmp", "boss_run")

#: How to point tests at the separate server. One place, so every message says the same thing.
HOW_TO_FIX = (
    "Start the test Postgres and point the tests at it:\n"
    "  docker compose -f scripts/install/community-compose.yml --env-file .env --profile dev up -d db-test\n"
    "  export BOSS_TEST_DATABASE_URL=postgresql+psycopg://boss:$BOSS_DB_PASSWORD@localhost:55700/boss_test\n"
    "If this database really is disposable, name it boss_test_* or boss_scratch_* "
    "(the server then knows too)."
)


def live_server_urls(environ=os.environ) -> List[str]:
    found: List[str] = []
    for var in LIVE_URL_VARIABLES:
        url = environ.get(var, "").strip()
        if not url or "://" not in url:
            continue
        if var != "BOSS_DATABASE_URL":
            name = database_name(url)
            if any(marker in name for marker in _DISPOSABLE_MARKERS):
                continue
        found.append(url)
    return found


def refuse_live_server(test_url: str, live_urls: Sequence[str], *, variable: str = "BOSS_TEST_DATABASE_URL") -> str:
    """Return `test_url`, or raise LiveServerRefused when it names a database on the live
    install's server that is not called boss_test* / boss_scratch*."""
    if not test_url or "://" not in test_url:
        return test_url
    name = database_name(test_url)
    if is_test_database_name(name):
        return test_url
    server = server_of(test_url)
    for live in live_urls:
        if not live or "://" not in live:
            continue
        if server_of(live) == server:
            host, port = server
            raise LiveServerRefused(
                f"{variable} points at the live install's Postgres ({host}:{port}, the server "
                f"{redacted(live)} uses) with a database called {name or '(none)'!r}. Tests create "
                "and drop databases and schemas and one runaway query there has already filled the "
                "disk next to the real data, so tests run on the separate test server.\n" + HOW_TO_FIX)
    return test_url


def guard_test_url(environ=os.environ, *, variable: str = "BOSS_TEST_DATABASE_URL") -> Optional[str]:
    """The conftest entry point: refuse `environ[variable]` when it lands in the live server."""
    url = environ.get(variable, "").strip()
    if not url:
        return None
    return refuse_live_server(url, live_server_urls(environ), variable=variable)


# ---------------------------------------------------------------------------------------------
# rule 3: drops in batches, and a preflight for the lock table
# ---------------------------------------------------------------------------------------------

def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def drop_schema_in_batches(execute: Callable[[str], Optional[list]], schema: str, *,
                           batch: int = DROP_BATCH, lock_timeout: str = "20s") -> int:
    """Drop `schema` without ever locking all of it at once.

    `execute(sql)` runs one statement in AUTOCOMMIT and returns its rows (or None). Tables go
    `batch` at a time with CASCADE (their indexes, toast tables, owned sequences and the
    foreign keys that point at them go with each batch), then DROP SCHEMA ... CASCADE takes
    what is left: views, functions, types. Returns the number of tables dropped.
    """
    execute(f"SET lock_timeout = '{lock_timeout}'")
    rows = execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {_literal(schema)} AND c.relkind IN ('r', 'p') ORDER BY c.relname") or []
    tables = [r[0] for r in rows]
    for start in range(0, len(tables), batch):
        names = ", ".join(f"{_quote(schema)}.{_quote(t)}" for t in tables[start:start + batch])
        execute(f"DROP TABLE IF EXISTS {names} CASCADE")
    execute(f"DROP SCHEMA IF EXISTS {_quote(schema)} CASCADE")
    return len(tables)


@dataclass(frozen=True)
class LockHeadroom:
    max_locks_per_transaction: int
    max_connections: int
    max_prepared_transactions: int
    relations: int
    concurrent_runs: int

    @property
    def capacity(self) -> int:
        """Slots in the shared lock table: the figure Postgres documents."""
        return self.max_locks_per_transaction * (self.max_connections + self.max_prepared_transactions)

    @property
    def needed(self) -> int:
        return self.relations * self.concurrent_runs

    @property
    def enough(self) -> bool:
        """With a quarter to spare: the lock table also holds every other session's locks (the
        API's pool, the worker, autovacuum), and 6400 slots against 6351 asked for is how the
        bench server ran out."""
        return self.capacity >= self.needed * 5 // 4

    @property
    def recommended(self) -> int:
        """The smallest power-of-two max_locks_per_transaction that fits, at least 1024."""
        value = 1024
        sessions = max(self.max_connections + self.max_prepared_transactions, 1)
        while value * sessions < self.needed * 5 // 4:
            value *= 2
        return value

    def message(self) -> Optional[str]:
        if self.enough:
            return None
        return (
            f"Postgres lock table is too small for this schema: max_locks_per_transaction="
            f"{self.max_locks_per_transaction} x (max_connections={self.max_connections} + "
            f"max_prepared_transactions={self.max_prepared_transactions}) = {self.capacity} slots "
            f"shared by every session, and one test schema holds {self.relations} relations; "
            f"{self.concurrent_runs} runs ending together ask for {self.needed}. That is the "
            "'out of shared memory — increase max_locks_per_transaction' failure that left test "
            f"schemas behind. Start the server with -c max_locks_per_transaction={self.recommended} "
            "(the db-test compose service does), or use it: " + HOW_TO_FIX.splitlines()[1].strip())


def lock_preflight(execute: Callable[[str], Optional[list]], *,
                   concurrent_runs: int = DEFAULT_CONCURRENT_RUNS) -> LockHeadroom:
    """Read the server's lock settings and the relation count of the schema the tests use.

    `execute(sql)` returns rows. The schema measured is current_schema() when it holds relations,
    else public (a fresh private schema is empty before ensure_schema fills it; public on the
    same database shows what it is about to hold)."""
    def one(setting: str) -> int:
        rows = execute(f"SHOW {setting}") or [[0]]
        return int(str(rows[0][0]).strip() or 0)

    rows = execute(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema()") or [[0]]
    relations = int(rows[0][0] or 0)
    if relations == 0:
        rows = execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public'") or [[0]]
        relations = int(rows[0][0] or 0)
    return LockHeadroom(
        max_locks_per_transaction=one("max_locks_per_transaction"),
        max_connections=one("max_connections"),
        max_prepared_transactions=one("max_prepared_transactions"),
        relations=relations,
        concurrent_runs=max(int(concurrent_runs), 1),
    )


# ---------------------------------------------------------------------------------------------
# rule 2: throwaways carry their birthday; the sweeper reads it
# ---------------------------------------------------------------------------------------------

@dataclass
class SweepReport:
    """What a sweep found. `dry_run` reports the same sets a real run would drop: that equality
    is a test (test_test_databases_are_separate_and_swept.py)."""
    databases: List[str] = field(default_factory=list)
    schemas: List[str] = field(default_factory=list)
    kept: List[Tuple[str, str]] = field(default_factory=list)   # (name, reason)
    dry_run: bool = True
    failures: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.databases and not self.schemas

    def lines(self) -> List[str]:
        verb = "would drop" if self.dry_run else "dropped"
        out = [f"{verb} {len(self.databases)} database(s) and {len(self.schemas)} schema(s)"]
        out += [f"  database {n}" for n in self.databases]
        out += [f"  schema   {n}" for n in self.schemas]
        out += [f"  kept     {n}: {why}" for n, why in self.kept]
        out += [f"  FAILED   {n}: {why}" for n, why in self.failures]
        return out


def _connect(dsn: str):
    import psycopg  # the one import that needs a database; late, so the rules above load anywhere

    return psycopg.connect(dsn, autocommit=True, connect_timeout=10)


def _executor(connection) -> Callable[[str], Optional[list]]:
    def execute(sql: str) -> Optional[list]:
        cur = connection.execute(sql)
        return cur.fetchall() if cur.description else None
    return execute


def plan_sweep(names: Iterable[str], *, ttl_hours: float = DEFAULT_TTL_HOURS,
               now: Optional[datetime] = None, include_unstamped: bool = False,
               kinds: Tuple[str, ...] = ("test", "scratch")) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Pure: (to_drop, kept-with-reason) over `names`. Nothing without a stamp of the right kind
    is ever in to_drop, except the legacy API schema shape when `include_unstamped` is set."""
    drop: List[str] = []
    kept: List[Tuple[str, str]] = []
    for name in sorted(names):
        parsed = parse_stamped(name)
        if parsed is None:
            if include_unstamped and LEGACY_API_SCHEMA.match(name):
                drop.append(name)
            elif name.startswith("boss_test_") or name.startswith("boss_scratch_"):
                kept.append((name, "no stamp in the name; not a throwaway this sweeper made"))
            continue
        if parsed.kind not in kinds:
            kept.append((name, f"a boss_{parsed.kind} name is not swept here"))
            continue
        if not parsed.expired(ttl_hours, now):
            hours = parsed.age(now).total_seconds() / 3600
            kept.append((name, f"{hours:.1f} h old, under the {ttl_hours:g} h TTL"))
            continue
        drop.append(name)
    return drop, kept


def _schema_in_use(execute: Callable[[str], Optional[list]], schema: str) -> bool:
    """True when another backend holds a lock on anything in `schema` (a suite still running)."""
    rows = execute(
        "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {_literal(schema)} AND l.pid <> pg_backend_pid()") or [[0]]
    return int(rows[0][0] or 0) > 0


def sweep(url: str, *, ttl_hours: float = DEFAULT_TTL_HOURS, dry_run: bool = True,
          include_unstamped: bool = False, now: Optional[datetime] = None) -> SweepReport:
    """Drop expired `boss_scratch_*_<stamp>` databases on `url`'s server and expired
    `boss_test_*_<stamp>` schemas inside `url`'s database. Nothing else, ever.

    `include_unstamped` adds the legacy `boss_test_<12 hex>` schemas the API suite made before
    names carried a stamp — only ones no other session holds a lock in. Use it once, when no
    suite from before the change is running.
    """
    report = SweepReport(dry_run=dry_run)
    dsn = psycopg_dsn(url)
    own = database_name(url)
    with _connect(dsn) as connection:
        execute = _executor(connection)
        execute("SET lock_timeout = '20s'")
        schema_rows = execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'boss\\_%'") or []
        schemas, kept = plan_sweep([r[0] for r in schema_rows], ttl_hours=ttl_hours, now=now,
                                   include_unstamped=include_unstamped)
        report.kept.extend(kept)
        for schema in schemas:
            if _schema_in_use(execute, schema):
                report.kept.append((schema, "another session holds a lock in it (a suite still running)"))
                continue
            report.schemas.append(schema)
            if not dry_run:
                try:
                    drop_schema_in_batches(execute, schema)
                except Exception as exc:  # noqa: BLE001 — report, keep sweeping
                    report.failures.append((schema, f"{type(exc).__name__}: {str(exc).strip()[:160]}"))
        db_rows = execute("SELECT datname FROM pg_database WHERE datname LIKE 'boss\\_scratch\\_%'") or []
        databases, kept = plan_sweep([r[0] for r in db_rows], ttl_hours=ttl_hours, now=now, kinds=("scratch",))
        report.kept.extend(kept)
        for name in databases:
            if name == own:
                report.kept.append((name, "the database this sweep is connected to"))
                continue
            report.databases.append(name)
            if not dry_run:
                try:
                    execute(f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)")
                except Exception as exc:  # noqa: BLE001
                    report.failures.append((name, f"{type(exc).__name__}: {str(exc).strip()[:160]}"))
    return report


def sweep_quietly(url: str, *, ttl_hours: float = DEFAULT_TTL_HOURS) -> Optional[SweepReport]:
    """The test-session-start sweep: best effort, never raises, never touches an unstamped name."""
    if not url or "://" not in url:
        return None
    try:
        return sweep(url, ttl_hours=ttl_hours, dry_run=False, include_unstamped=False)
    except Exception:  # noqa: BLE001 — an unreachable server is the suite's own skip, not ours
        return None


def session_start(url: str, *, concurrent_runs: Optional[int] = None) -> List[str]:
    """What a test session does first on its database (both conftests call this): sweep expired
    stamped throwaways, then the lock preflight. Returns the lines worth printing; never raises
    — a missing or unreachable server is the suite's own skip."""
    lines: List[str] = []
    if not url or "://" not in url:
        return lines
    report = sweep_quietly(url)
    if report is not None and not report.empty:
        lines.append("[test-db sweep] " + "; ".join(report.lines()[:4]))
    runs = concurrent_runs or int(os.environ.get("TEST_TIER_SHARDS") or DEFAULT_CONCURRENT_RUNS)
    try:
        with _connect(psycopg_dsn(url)) as connection:
            headroom = lock_preflight(_executor(connection), concurrent_runs=runs)
    except Exception:  # noqa: BLE001
        return lines
    message = headroom.message()
    if message:
        lines.append("[test-db preflight] WARNING: " + message)
    return lines


# ---------------------------------------------------------------------------------------------
# the fixture side: a scratch database that is always dropped
# ---------------------------------------------------------------------------------------------

@contextmanager
def scratch_database(server_url: str, purpose: str, *, unique: bool = True) -> Iterator[str]:
    """Create `boss_scratch_<purpose>[_<rand>]_<stamp>` on `server_url`'s server and yield its
    URL (same scheme and credentials). Dropped WITH (FORCE) on exit — and by atexit if the
    process dies before exit runs. Should both fail, the stamp lets `boss dev sweep-test-db`
    finish the job after the TTL."""
    tag = f"{purpose}_{secrets.token_hex(3)}" if unique else purpose
    name = scratch_database_name(tag)
    admin = psycopg_dsn(with_database(server_url, "postgres"))

    def drop() -> None:
        try:
            with _connect(admin) as connection:
                connection.execute(f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)")
        except Exception:  # noqa: BLE001 — the sweeper is the backstop
            pass

    with _connect(admin) as connection:
        connection.execute(f"CREATE DATABASE {_quote(name)}")
    atexit.register(drop)
    try:
        yield with_database(server_url, name)
    finally:
        drop()
        try:
            atexit.unregister(drop)
        except Exception:  # noqa: BLE001
            pass
