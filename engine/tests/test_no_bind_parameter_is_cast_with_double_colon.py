"""A bind parameter is never cast with `::` in SQL text.

SQLAlchemy's `text()` reads `:name` as a bind parameter only when what follows cannot continue a
name, and `:s::text` is left as literal SQL: Postgres then sees `:s` and refuses the statement.
`_mark_stage_done` in the governed pipeline wrote `to_jsonb(:s::text)`, so every stage's
"completed" mark failed (swallowed as bookkeeping) and a resumed attempt could never skip a stage
it had already done; the redelivery route wrote `to_jsonb(:attempt::int)`. Found by running the
real pipeline on the scripted conformance engine. Write `CAST(:name AS type)`.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_BIND_CAST = re.compile(r"(?<![:\w]):[a-z_][a-z0-9_]*::[a-z]", re.IGNORECASE)
#: Prose that QUOTES the mistake to explain it (`:s::text` in a docstring) is not SQL: SQL text
#: never holds a backtick, so a backticked span is dropped before the line is read.
_QUOTED = re.compile(r"`[^`]*`")


def _casts(line: str) -> bool:
    return bool(_BIND_CAST.search(_QUOTED.sub("", line)))


def test_the_guard_reads_sql_and_skips_prose_that_quotes_the_mistake():
    assert _casts("    \"UPDATE t SET x = to_jsonb(:s::text) WHERE id = :id\"")
    assert _casts("text('SELECT :attempt::int')")
    assert not _casts("    The cast is CAST(:s AS text), never `:s::text`: a text clause ...")
    assert not _casts("    \"SELECT CAST(:s AS text), x::jsonb, '{}'::jsonb\"")


def test_no_sql_text_casts_a_bind_parameter_with_double_colon():
    found = []
    for base in ("engine/boss_engine", "apps/api/app", "apps/worker"):
        for path in (ROOT / base).rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore")
                                     .splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if _casts(line):
                    found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert not found, "write CAST(:name AS type), not :name::type:\n" + "\n".join(found)
