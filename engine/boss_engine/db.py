from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings, get_settings

# Column definitions in schema.sql whose leading token is one of these are table-level
# constraints, not columns — skip them during column reconciliation.
_CONSTRAINT_KEYWORDS = {
    "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "EXCLUDE", "LIKE",
}


def _iter_create_tables(sql: str):
    """Yield (table_name, body) for each CREATE TABLE block, balancing parentheses so
    nested types (NUMERIC(10,2)) and DEFAULT '[...]'::jsonb don't truncate the body."""
    pat = re.compile(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?("?\w+"?)\s*\(', re.I)
    for m in pat.finditer(sql):
        name = m.group(1).strip('"')
        depth, start = 0, m.end() - 1
        j = start
        while j < len(sql):
            ch = sql[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    yield name, sql[start + 1 : j]
                    break
            j += 1


def _split_top_level_commas(body: str) -> list[str]:
    parts, cur, depth, instr, strch = [], [], 0, False, ""
    for ch in body:
        if instr:
            cur.append(ch)
            if ch == strch:
                instr = False
            continue
        if ch in ("'", '"'):
            instr, strch = True, ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _safe_add_column_def(definition: str) -> str:
    """Reduce a column definition to a form safe to ADD to an already-populated table:
    keep the type (+ DEFAULT), drop constraints that can't be added retroactively
    (NOT NULL without a default, PRIMARY KEY, UNIQUE, REFERENCES, GENERATED)."""
    d = definition
    d = re.sub(r"\bGENERATED\b.*$", "", d, flags=re.I | re.S)
    d = re.sub(r"\bREFERENCES\b.*$", "", d, flags=re.I | re.S)
    d = re.sub(r"\bPRIMARY\s+KEY\b", "", d, flags=re.I)
    d = re.sub(r"\bUNIQUE\b", "", d, flags=re.I)
    # NOT NULL is only safe with a DEFAULT (Postgres backfills); otherwise add nullable.
    if not re.search(r"\bDEFAULT\b", d, flags=re.I):
        d = re.sub(r"\bNOT\s+NULL\b", "", d, flags=re.I)
    return " ".join(d.split())


def _reconcile_columns(cursor, sql: str) -> tuple[int, list[str]]:
    """Make every column declared in schema.sql's CREATE TABLE blocks exist on the live
    table — `ALTER TABLE … ADD COLUMN IF NOT EXISTS …`, each in its own savepoint so one
    bad statement can't abort the rest. Additive only: never drops or retypes a column."""
    attempted, skipped = 0, []
    # Strip SQL comments first — inline `-- …` comments often contain parens/apostrophes
    # that would corrupt the paren/string depth tracking used to split columns.
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", "", sql)
    for table, body in _iter_create_tables(sql):
        for item in _split_top_level_commas(body):
            item = item.strip()
            if not item:
                continue
            m = re.match(r'"?(\w+)"?\s*(.*)', item, re.S)
            if not m:
                continue
            col, rest = m.group(1), m.group(2).strip()
            # Skip table-level constraints, incl. forms with no space (UNIQUE(a,b)).
            if col.upper() in _CONSTRAINT_KEYWORDS:
                continue
            safe = _safe_add_column_def(rest)
            if not safe:
                continue
            stmt = f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" {safe}'
            attempted += 1
            try:
                cursor.execute("SAVEPOINT _col")
                cursor.execute(stmt)
                cursor.execute("RELEASE SAVEPOINT _col")
            except Exception as exc:  # noqa: BLE001 — isolate + skip a bad column def
                cursor.execute("ROLLBACK TO SAVEPOINT _col")
                skipped.append(f"{table}.{col}: {str(exc)[:80]}")
    return attempted, skipped


#: `CREATE [UNIQUE] INDEX [IF NOT EXISTS] <name> ON [ONLY] <table> <rest>` — every index
#: schema.sql declares has this shape (test_every_declared_index_converges pins it).
_DECLARED_INDEX = re.compile(
    r'^\s*CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?'
    r'"?(?P<name>\w+)"?\s+ON\s+(?:ONLY\s+)?"?(?P<table>\w+)"?\s*(?P<rest>.*)$',
    re.I | re.S,
)
#: What Postgres says an index IS (pg_get_indexdef), minus its own name and table — the part two
#: indexes on different tables can share: uniqueness, method, columns, opclasses, predicate.
_INDEXDEF_SHAPE = re.compile(
    r'^CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+\S+\s+ON\s+(?:ONLY\s+)?\S+\s+(?P<shape>USING\s.*)$',
    re.S,
)
#: Above this many pages (8 KiB each: ~1 GiB) a drifted index is NOT rebuilt during a boot.
#: CREATE INDEX inside a transaction blocks writes to the table for as long as the build takes,
#: and CONCURRENTLY cannot run inside one; on a table this size the repair is reported (with the
#: exact statements to run CONCURRENTLY by hand) instead of stalling every writer at startup.
INDEX_REBUILD_MAX_PAGES = 131072
#: How long a rebuild waits for its table lock before giving up and reporting instead.
INDEX_REBUILD_LOCK_TIMEOUT = "5s"


def declared_indexes(sql: str) -> list[dict[str, str]]:
    """Every `CREATE [UNIQUE] INDEX` statement schema.sql declares, as {name, table, statement}."""
    out = []
    for stmt in split_statements(sql):
        body = re.sub(r"--[^\n]*", "", stmt).strip()
        m = _DECLARED_INDEX.match(body)
        if m:
            out.append({"name": m.group("name").lower(), "table": m.group("table").lower(),
                        "unique": bool(m.group("unique")), "rest": m.group("rest").strip(),
                        "statement": body})
    return out


def _index_shape(indexdef: str) -> Optional[tuple[bool, str]]:
    m = _INDEXDEF_SHAPE.match(" ".join(str(indexdef or "").split()))
    return (bool(m.group("unique")), m.group("shape")) if m else None


def _converge_indexes(raw_connection, sql: str) -> tuple[list[str], list[str]]:
    """INDEX DRIFT. `CREATE INDEX IF NOT EXISTS` never redefines an index that exists, so an
    install whose database predates a change to an index keeps the OLD definition forever.

    Seen on shared-server: schema.sql widened `idx_agent_threads_surface_external` from
    (project_id, surface, external_ref) to (…, agent_role) so each agent keeps its own thread on
    a shared subject; shared-server kept the 3-column index, and the second agent asked about a
    subject hit a unique violation on every turn.

    For each index schema.sql declares that exists under that name: build the DECLARED index on
    an empty probe copy of its table (CREATE TEMP TABLE … (LIKE table) — no rows, no lock beyond
    a read of the catalogue), ask Postgres for its definition, and compare it with the live
    index's. Comparing Postgres's own rendering of both sides means casts, parentheses and
    whitespace in the predicate can never read as drift. A different definition is dropped and
    recreated in its own savepoint and committed at once (so its lock is held for that index
    only), with a lock timeout; on a table over INDEX_REBUILD_MAX_PAGES it is reported, not
    rebuilt. Indexes schema.sql does not declare are never looked at.

    Returns (repairs, failures) — both recorded in schema_apply_runs.
    """
    repairs: list[str] = []
    failures: list[str] = []
    declared = declared_indexes(sql)
    if not declared:
        return repairs, failures
    try:
        with raw_connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexname, tablename, indexdef, pg_relation_size("
                "(quote_ident(schemaname) || '.' || quote_ident(tablename))::regclass) / 8192 "
                "FROM pg_indexes WHERE schemaname = current_schema()")
            live = {str(r[0]).lower(): (str(r[1]).lower(), str(r[2]), int(r[3] or 0))
                    for r in cursor.fetchall()}
        raw_connection.commit()
    except Exception as exc:  # noqa: BLE001 — a convergence that cannot look never blocks a boot
        try:
            raw_connection.rollback()
        except Exception:
            pass
        failures.append(f"index convergence could not read the catalogue -> {str(exc)[:160]}")
        return repairs, failures

    for index in declared:
        name, table = index["name"], index["table"]
        if name not in live:
            continue  # absent: the CREATE … IF NOT EXISTS above already made it (or said why not)
        live_table, live_def, pages = live[name]
        unique = "UNIQUE " if index["unique"] else ""
        try:
            with raw_connection.cursor() as cursor:
                cursor.execute("SAVEPOINT _idx_probe")
                try:
                    cursor.execute(
                        f'CREATE TEMP TABLE "_boss_idx_probe" (LIKE "{table}") ON COMMIT DROP')
                    cursor.execute(f'CREATE {unique}INDEX "_boss_idx_probe_ix" '
                                   f'ON "_boss_idx_probe" {index["rest"]}')
                    cursor.execute("SELECT pg_get_indexdef('_boss_idx_probe_ix'::regclass)")
                    declared_def = str(cursor.fetchone()[0])
                finally:
                    cursor.execute("ROLLBACK TO SAVEPOINT _idx_probe")
                    cursor.execute("RELEASE SAVEPOINT _idx_probe")
        except Exception as exc:  # noqa: BLE001 — cannot judge this index; leave it and say so
            try:
                raw_connection.rollback()
            except Exception:
                pass
            failures.append(f"index {name}: could not compare with schema.sql -> "
                            f"{str(exc).strip()[:160]}")
            continue
        if live_table == table and _index_shape(live_def) == _index_shape(declared_def):
            continue
        was = " ".join(live_def.split())
        if pages > INDEX_REBUILD_MAX_PAGES:
            concurrently = index["statement"].replace("INDEX", "INDEX CONCURRENTLY", 1)
            failures.append(
                f"index {name} differs from schema.sql (live: {was[:160]}) but {table} is too "
                f"large to rebuild during a boot; run outside a transaction: DROP INDEX "
                f"CONCURRENTLY {name}; {concurrently}")
            continue
        try:
            with raw_connection.cursor() as cursor:
                cursor.execute("SAVEPOINT _idx_fix")
                try:
                    cursor.execute(f"SET LOCAL lock_timeout = '{INDEX_REBUILD_LOCK_TIMEOUT}'")
                    cursor.execute(f'DROP INDEX IF EXISTS "{name}"')
                    cursor.execute(index["statement"])
                    cursor.execute("RELEASE SAVEPOINT _idx_fix")
                except Exception:
                    cursor.execute("ROLLBACK TO SAVEPOINT _idx_fix")
                    raise
            raw_connection.commit()
            repairs.append(f"index {name} redefined to match schema.sql (was: {was[:200]})")
        except Exception as exc:  # noqa: BLE001 — the old index stays; name why
            try:
                raw_connection.rollback()
            except Exception:
                pass
            failures.append(f"index {name} differs from schema.sql and could not be rebuilt "
                            f"(kept as is) -> {str(exc).strip()[:160]}")
    try:
        raw_connection.commit()   # close the probes' transaction; they left nothing behind
    except Exception:  # noqa: BLE001
        pass
    return repairs, failures


def get_engine(settings: Optional[Settings] = None, *,
               idle_in_transaction_seconds: Optional[int] = None) -> Engine:
    """The SQLAlchemy engine. `idle_in_transaction_seconds` (the API passes its setting) has
    Postgres end a session that sits inside an open transaction doing nothing for that long:
    an API request must never hold row locks for hours because something between two
    statements hung."""
    resolved = settings or get_settings()
    engine = create_engine(resolved.database_url, future=True, pool_pre_ping=True)
    if idle_in_transaction_seconds:
        from sqlalchemy import event

        milliseconds = int(idle_in_transaction_seconds) * 1000

        @event.listens_for(engine, "connect")
        def _limit_idle_transactions(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"SET idle_in_transaction_session_timeout = {milliseconds}")
            finally:
                cursor.close()
            dbapi_connection.commit()

    return engine


def database_is_reachable(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


def split_statements(sql: str) -> list[str]:
    """Split a schema script into statements, respecting strings and $$ bodies.

    A naive split on ';' cuts `DO $$ BEGIN ... END $$;` in half and turns a working script into
    a pile of syntax errors, so quoting state is tracked: single quotes, double quotes, line
    and block comments, and dollar-quoted bodies with their tags ($$ and $tag$).
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote = ""          # "'", '"', or the active dollar tag like "$$" / "$body$"
    while i < n:
        ch = sql[i]
        if quote:
            if quote in ("'", '"'):
                if ch == quote:
                    quote = ""
                buf.append(ch)
                i += 1
                continue
            if sql.startswith(quote, i):     # closing dollar tag
                buf.append(quote)
                i += len(quote)
                quote = ""
                continue
            buf.append(ch)
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            buf.append(sql[i:end])
            i = end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            i = end
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_]\w*\$|\$\$", sql[i:])
            if m:
                quote = m.group(0)
                buf.append(quote)
                i += len(quote)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _record_schema_apply(raw_connection, total: int, failures: list[str],
                         repairs: Optional[list[str]] = None) -> None:
    """Leave the result of this apply where a person can read it.

    "Did the schema actually migrate?" was answerable only by opening the API container's
    stdout, which nobody on this project can do — so a silent partial apply and a clean one
    looked exactly alike from outside, and a missing table would present as a feature returning
    nothing. One row per boot, queryable through the same console as everything else.

    Created here rather than in schema.sql: the whole point is to survive the script failing.
    """
    try:
        with raw_connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_apply_runs ("
                " id BIGSERIAL PRIMARY KEY,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " statements INT NOT NULL,"
                " failed INT NOT NULL,"
                " failures TEXT[] NOT NULL DEFAULT '{}')"
            )
            # WHAT THE APPLY CHANGED BESIDES RUNNING THE SCRIPT — an index redefined to match
            # schema.sql (_converge_indexes). A repair nobody can see is a change nobody made.
            cursor.execute("ALTER TABLE schema_apply_runs "
                           "ADD COLUMN IF NOT EXISTS repairs TEXT[] NOT NULL DEFAULT '{}'")
            cursor.execute(
                "INSERT INTO schema_apply_runs (statements, failed, failures, repairs) "
                "VALUES (%s, %s, %s, %s)",
                (total, len(failures), failures[:50], (repairs or [])[:50]),
            )
            # Keep the last 50 boots: enough to see when a failure first appeared, not enough
            # to become a table anyone has to think about.
            cursor.execute(
                "DELETE FROM schema_apply_runs WHERE id < "
                "(SELECT COALESCE(MIN(id), 0) FROM (SELECT id FROM schema_apply_runs "
                " ORDER BY id DESC LIMIT 50) recent)"
            )
        raw_connection.commit()
    except Exception as exc:  # noqa: BLE001 — a bookkeeping row must never block a boot
        try:
            raw_connection.rollback()
        except Exception:
            pass
        print(f"[ensure_schema] could not record this apply: {exc}")


def ensure_schema(engine: Engine, settings: Optional[Settings] = None) -> None:
    """Converge the database to schema.sql. Every deploy runs this; nothing else migrates.

    STATEMENT BY STATEMENT, EACH IN ITS OWN SAVEPOINT — not one `cursor.execute(script)`.
    Sending the whole file as a single multi-statement makes it a single transaction, so ONE
    statement failing against live drift discards every statement after it. New tables are
    appended, so the newest work is precisely what would be thrown away — and the caller's
    `except Exception: print(...)` put the reason in a container log nobody on this project can
    open, with the deploy still reporting success.

    That combination is why "did the schema migrate?" was unanswerable without asking an agent
    to query the database. A failing statement is now isolated and NAMED, the ones after it
    still run, and the outcome of every apply is recorded in `schema_apply_runs`.
    """
    resolved = settings or get_settings()
    sql = resolved.schema_path.read_text(encoding="utf-8")
    raw_connection = engine.raw_connection()
    try:
        # Column reconcile MUST run before the full schema script.
        # CREATE TABLE IF NOT EXISTS will not add new columns to an existing
        # table; a later CREATE INDEX on those columns (e.g. kg_nodes.valid_from)
        # would abort the entire apply and skip new tables — live dogfood then
        # 500s on token_budget_policies / worker_instances / autoscaling.
        with raw_connection.cursor() as cursor:
            attempted, skipped = _reconcile_columns(cursor, sql)
        raw_connection.commit()

        failures: list[str] = []
        statements = split_statements(sql)
        with raw_connection.cursor() as cursor:
            for stmt in statements:
                try:
                    cursor.execute("SAVEPOINT _stmt")
                    cursor.execute(stmt)
                    cursor.execute("RELEASE SAVEPOINT _stmt")
                except Exception as exc:  # noqa: BLE001 — isolate, name it, keep going
                    cursor.execute("ROLLBACK TO SAVEPOINT _stmt")
                    head = " ".join(stmt.split())[:120]
                    failures.append(f"{head} -> {str(exc).strip()[:160]}")
        raw_connection.commit()

        # AFTER the script: the tables and columns an index names exist by now.
        repairs, index_failures = _converge_indexes(raw_connection, sql)

        _record_schema_apply(raw_connection, len(statements), failures + index_failures,
                             repairs)

        print(f"[ensure_schema] applied {len(statements) - len(failures)}/{len(statements)} "
              f"statements")
        if skipped:
            print(f"[ensure_schema] column reconcile: {attempted} checked, "
                  f"{len(skipped)} skipped (non-fatal): {skipped[:8]}")
        for repair in repairs:
            print(f"[ensure_schema] REPAIRED: {repair}")
        for failure in failures + index_failures:
            # Loud, one per line, with the statement AND the database's own message. The
            # previous version could not tell you which statement broke because it never knew.
            print(f"[ensure_schema] FAILED: {failure}")
    finally:
        raw_connection.close()


@contextmanager
def begin(engine: Optional[Engine] = None):
    active_engine = engine or get_engine()
    with active_engine.begin() as connection:
        yield connection
