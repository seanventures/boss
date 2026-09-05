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


def get_engine(settings: Optional[Settings] = None) -> Engine:
    resolved = settings or get_settings()
    return create_engine(resolved.database_url, future=True, pool_pre_ping=True)


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


def _record_schema_apply(raw_connection, total: int, failures: list[str]) -> None:
    """Leave the result of this apply where a person can read it.

    "Did the schema actually migrate?" was answerable only by opening the API container's
    stdout, which nobody on this project can do. So it went unanswered, and a missing table
    presented as a feature returning nothing. One row per boot, queryable through the same
    console as everything else.

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
            cursor.execute(
                "INSERT INTO schema_apply_runs (statements, failed, failures) "
                "VALUES (%s, %s, %s)",
                (total, len(failures), failures[:50]),
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
        raw_connection.rollback()
        print(f"[ensure_schema] could not record this apply: {exc}")


def ensure_schema(engine: Engine, settings: Optional[Settings] = None) -> None:
    """Converge the database to schema.sql. Every deploy runs this; nothing else migrates.

    STATEMENT BY STATEMENT, EACH IN ITS OWN SAVEPOINT — not one `cursor.execute(script)`.
    Sending the whole file as a single multi-statement makes it a single transaction, so ONE
    statement that fails against live drift silently discards every statement after it. That is
    not theoretical: a table added at the end of the file was simply absent in production after
    a clean deploy, the API's `except` printed a line into a container log nobody can open, and
    the feature that needed it failed as "no rows" rather than "no table". The apply reported
    success throughout.

    A failing statement is now isolated and NAMED, and the ones after it still run.
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

        _record_schema_apply(raw_connection, len(statements), failures)

        print(f"[ensure_schema] applied {len(statements) - len(failures)}/{len(statements)} "
              f"statements")
        if skipped:
            print(f"[ensure_schema] column reconcile: {attempted} checked, "
                  f"{len(skipped)} skipped (non-fatal): {skipped[:8]}")
        for failure in failures:
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
