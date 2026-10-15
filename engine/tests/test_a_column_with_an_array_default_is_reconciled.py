"""ensure_schema's column reconcile adds a column whose DEFAULT is an array literal.

`_split_top_level_commas` tracked parentheses and quotes but not square brackets, so
`modes TEXT[] NOT NULL DEFAULT ARRAY['work', 'chat', 'panel']` (agent_instructions) was cut at the
commas inside the brackets and the reconcile sent `... DEFAULT ARRAY['work'` to Postgres: a
syntax error, logged as non-fatal and skipped. An install upgraded past that column never got it.
"""
from __future__ import annotations

from boss_engine import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_instructions (
  id          TEXT PRIMARY KEY,
  modes       TEXT[] NOT NULL DEFAULT ARRAY['work', 'chat', 'panel'],
  weights     NUMERIC(10,2) DEFAULT 0,
  tags        TEXT[] DEFAULT '{a,b}',
  UNIQUE (id, modes)
);
"""


class _Cursor:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, stmt):
        self.statements.append(stmt)


def test_an_array_literal_default_stays_one_column():
    parts = [p.strip() for p in db._split_top_level_commas(  # noqa: SLF001
        "a TEXT, modes TEXT[] DEFAULT ARRAY['work', 'chat'], b INT")]
    assert parts == ["a TEXT", "modes TEXT[] DEFAULT ARRAY['work', 'chat']", "b INT"]


def test_the_reconcile_adds_the_whole_column_definition():
    cursor = _Cursor()
    attempted, skipped = db._reconcile_columns(cursor, SCHEMA)  # noqa: SLF001
    adds = [s for s in cursor.statements if s.startswith("ALTER TABLE")]
    assert attempted == 4 and skipped == []
    assert ('ALTER TABLE agent_instructions ADD COLUMN IF NOT EXISTS "modes" TEXT[] NOT NULL '
            "DEFAULT ARRAY['work', 'chat', 'panel']") in adds
    assert not any(s.rstrip().endswith("ARRAY['work'") for s in adds)
    assert ('ALTER TABLE agent_instructions ADD COLUMN IF NOT EXISTS "weights" NUMERIC(10,2) '
            "DEFAULT 0") in adds


def test_the_real_schema_reconciles_every_column_it_declares():
    """No column of schema.sql is cut by the splitter: each one parses to a definition that
    names a type, and the agent_instructions.modes default survives whole."""
    from pathlib import Path

    cursor = _Cursor()
    schema = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    db._reconcile_columns(cursor, schema.read_text(encoding="utf-8"))  # noqa: SLF001
    adds = [s for s in cursor.statements if s.startswith("ALTER TABLE")]
    modes = [s for s in adds if s.startswith("ALTER TABLE agent_instructions ") and '"modes"' in s]
    assert modes and modes[0].endswith("DEFAULT ARRAY['work', 'chat', 'panel']"), modes
    # A fragment of a cut array default would add a column named after a quoted word.
    assert not any("ADD COLUMN IF NOT EXISTS \"'" in s or s.count("[") != s.count("]")
                   for s in adds)
