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


def ensure_schema(engine: Engine, settings: Optional[Settings] = None) -> None:
    resolved = settings or get_settings()
    sql = resolved.schema_path.read_text(encoding="utf-8")
    raw_connection = engine.raw_connection()
    try:
        with raw_connection.cursor() as cursor:
            cursor.execute(sql)  # new tables/indexes (IF NOT EXISTS) + any explicit ALTERs
            # Drift guard: CREATE TABLE IF NOT EXISTS won't add NEW columns to a table that
            # already exists, so reconcile every column declared in schema.sql onto the live
            # table. Additive + savepoint-isolated — safe to run on every deploy.
            attempted, skipped = _reconcile_columns(cursor, sql)
        raw_connection.commit()
        if skipped:
            print(f"[ensure_schema] column reconcile: {attempted} checked, "
                  f"{len(skipped)} skipped (non-fatal): {skipped[:8]}")
    finally:
        raw_connection.close()


@contextmanager
def begin(engine: Optional[Engine] = None):
    active_engine = engine or get_engine()
    with active_engine.begin() as connection:
        yield connection
