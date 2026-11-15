"""Foreign-key lookups for test helpers, read straight from pg_catalog (BOSSAPP-1144).

Nothing here is collected (no test_ names). The information_schema views behind the obvious
`table_constraints JOIN key_column_usage JOIN constraint_column_usage` query are built from
several catalog joins with per-row privilege checks; on a database holding many schemas (the
shared bench DB) the join ran for minutes and spilled 6-11 GB of temp files. pg_constraint
already carries both ends of every foreign key, so these are single index-backed lookups.

`test_no_information_schema_fk_joins_in_tests.py` keeps new tests on these helpers.
"""
from __future__ import annotations

from sqlalchemy import text


def tables_referencing(c, table: str, *, column: str | None = None) -> list[str]:
    """Every table with a foreign key onto `table` (resolved on the search path, as the DELETEs
    that use the answer are), optionally only keys that include `column`. `table` itself is left
    out. Names come back as `regclass` text: bare when on the search path, else schema-qualified."""
    sql = ("SELECT DISTINCT con.conrelid::regclass::text FROM pg_constraint con "
           "WHERE con.contype = 'f' AND con.confrelid = CAST(:t AS regclass) "
           "AND con.conrelid <> con.confrelid")
    params = {"t": table}
    if column is not None:
        sql += (" AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = con.conrelid "
                "AND a.attnum = ANY(con.conkey) AND a.attname = :col)")
        params["col"] = column
    return [str(name) for name in c.execute(text(sql), params).scalars().all()]


def foreign_key_parents(c, table: str) -> dict[str, str]:
    """{column: referenced table} for each foreign key of `table` in the current schema whose
    parent is in the same schema."""
    rows = c.execute(text(
        "SELECT a.attname, parent.relname FROM pg_constraint con "
        "JOIN pg_class child ON child.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = child.relnamespace "
        "JOIN pg_class parent ON parent.oid = con.confrelid "
        "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey) "
        "WHERE con.contype = 'f' AND child.relname = :t AND n.nspname = current_schema() "
        "AND parent.relnamespace = child.relnamespace"), {"t": table}).all()
    return {str(column): str(parent) for column, parent in rows}
