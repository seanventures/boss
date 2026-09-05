"""A deploy said it migrated, and a table added at the end of the file was simply not there.

`engineering_standards` was appended to `db/schema.sql`, the API was deployed, the boot log said
nothing, and the table did not exist. The feature that needed it degraded into "no rows" —
indistinguishable, from the console, from "nobody has mandated a standard yet".

The cause was one line: the whole script went to `cursor.execute(sql)` as a single
multi-statement, which Postgres runs as ONE transaction. Any statement that failed against live
drift — a constraint that already exists in a different shape, an index on a column somebody
renamed — aborted the transaction, and every statement AFTER it was discarded. Newest tables
sit at the end of the file, so the newest work is exactly what gets thrown away, and the failure
surfaced as `except Exception: print(...)` into a container log nobody on this project can open.

So: each statement in its own savepoint, failures named individually, and the result of every
apply written to a table, because "did the schema migrate?" must be answerable without SSH.
"""
from __future__ import annotations

import re

import pytest

from boss_engine.db import ensure_schema, split_statements


class TestSplittingTheScript:
    """A naive split on ';' cuts `DO $$ ... END $$;` in half and turns a working script into a
    pile of syntax errors — so the splitter has to understand quoting, or the cure is worse."""

    def test_ordinary_statements_split(self):
        assert split_statements("CREATE TABLE a (i int); CREATE TABLE b (i int);") == [
            "CREATE TABLE a (i int)", "CREATE TABLE b (i int)"]

    def test_a_dollar_quoted_body_stays_whole(self):
        sql = "DO $$ BEGIN IF true THEN CREATE TABLE a (i int); END IF; END $$;"
        assert len(split_statements(sql)) == 1

    def test_a_tagged_dollar_body_stays_whole(self):
        sql = "CREATE FUNCTION f() RETURNS int AS $body$ BEGIN; RETURN 1; END $body$ LANGUAGE x;"
        assert len(split_statements(sql)) == 1

    def test_a_semicolon_inside_a_string_is_not_a_boundary(self):
        assert len(split_statements("INSERT INTO a VALUES ('x;y');")) == 1

    def test_a_semicolon_inside_a_comment_is_not_a_boundary(self):
        assert len(split_statements("CREATE TABLE a (i int) -- one; two\n;")) == 1

    def test_the_real_schema_survives_the_split(self):
        """The file this actually has to work on. Unbalanced $$ would mean the splitter cut a
        DO block, which is the failure mode that would take the whole schema down."""
        import pathlib

        sql = pathlib.Path("db/schema.sql").read_text(encoding="utf-8")
        statements = split_statements(sql)
        assert len(statements) > 300
        assert all(s.count("$$") % 2 == 0 for s in statements)

    def test_every_do_block_is_one_statement(self):
        import pathlib

        sql = pathlib.Path("db/schema.sql").read_text(encoding="utf-8")
        do_blocks = len(re.findall(r"(?m)^DO \$\$", sql))
        # Matched anywhere in the statement, not at its start: a leading `-- why this exists`
        # comment stays attached to the statement it explains, which is correct — Postgres
        # ignores it and the next reader of a failure message gets the reason with it.
        kept = [s for s in split_statements(sql) if re.search(r"(?m)^DO \$\$", s)]
        assert len(kept) == do_blocks
        assert all(s.rstrip().endswith("$$") for s in kept), "a DO block was cut in half"


class FakeCursor:
    """A cursor that fails one specific statement, the way live drift does."""

    def __init__(self, poison: str, store: dict):
        self.poison = poison
        self.store = store
        self.aborted = False

    def execute(self, sql, params=None):
        text = str(sql)
        if text.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO")):
            if text.startswith("ROLLBACK TO"):
                self.aborted = False
            return
        if self.aborted:
            raise RuntimeError("current transaction is aborted")
        if self.poison and self.poison in text:
            self.aborted = True
            raise RuntimeError('column "gone" does not exist')
        self.store.setdefault("ran", []).append(" ".join(text.split())[:60])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class FakeRawConnection:
    def __init__(self, poison=""):
        self.poison = poison
        self.store: dict = {}

    def cursor(self):
        return FakeCursor(self.poison, self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class FakeEngine:
    def __init__(self, raw):
        self._raw = raw

    def raw_connection(self):
        return self._raw


class FakeSettings:
    def __init__(self, path):
        self.schema_path = path


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "schema.sql"
    path.write_text(
        "CREATE TABLE early (i int);\n"
        "CREATE INDEX idx_bad ON early(gone);\n"       # the statement that fails
        "CREATE TABLE engineering_standards (i int);\n"  # everything after used to vanish
    )
    return path


class TestOneBadStatementNoLongerDiscardsTheRest:
    def test_the_statements_after_the_failure_still_run(self, script, capsys):
        raw = FakeRawConnection(poison="idx_bad")
        ensure_schema(FakeEngine(raw), FakeSettings(script))
        ran = " | ".join(raw.store.get("ran", []))
        assert "engineering_standards" in ran, (
            "the table after the failing statement was skipped — this is the exact bug")

    def test_the_failure_is_named_with_the_statement_and_the_database_message(
            self, script, capsys):
        """The old version could not tell you which statement broke, because it never knew:
        it sent the file as one string and got back one error."""
        ensure_schema(FakeEngine(FakeRawConnection(poison="idx_bad")), FakeSettings(script))
        out = capsys.readouterr().out
        assert "FAILED" in out and "idx_bad" in out and "does not exist" in out

    def test_a_clean_apply_reports_the_count(self, script, capsys):
        ensure_schema(FakeEngine(FakeRawConnection()), FakeSettings(script))
        assert "applied 3/3" in capsys.readouterr().out

    def test_the_result_is_written_somewhere_a_person_can_read(self, script):
        """'Did the schema migrate?' had exactly one answer path — the container's stdout, which
        nobody on this project can open. Hence a missing table that looked like empty data."""
        raw = FakeRawConnection()
        ensure_schema(FakeEngine(raw), FakeSettings(script))
        ran = " | ".join(raw.store.get("ran", []))
        assert "schema_apply_runs" in ran

    def test_recording_the_apply_cannot_block_a_boot(self, script, capsys):
        """It is bookkeeping. An API that will not start because it could not write a log row
        is a worse outcome than not having the row."""
        class Hostile(FakeRawConnection):
            def cursor(self):
                cursor = super().cursor()
                original = cursor.execute

                def execute(sql, params=None):
                    if "schema_apply_runs" in str(sql):
                        raise RuntimeError("no permission")
                    return original(sql, params)

                cursor.execute = execute
                return cursor

        ensure_schema(FakeEngine(Hostile()), FakeSettings(script))   # must not raise
        assert "applied 3/3" in capsys.readouterr().out
