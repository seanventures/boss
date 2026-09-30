"""The statement passed every local test and 500'd on the first real click.

    ProgrammingError: (psycopg.errors.AmbiguousParameter)
    could not determine data type of parameter $1
    LINE 4:  external_ref = CASE WHEN $1 IS NULL THEN...

This laptop runs **psycopg2**, which infers a bare parameter's type happily. Production runs
**psycopg 3**, which does not. So a driver difference is a class of bug local testing cannot
see, and the only defence is writing the SQL the stricter driver requires.

THE RULE IS NARROW, because a broad one would be noise. Most parameters ARE inferable: compared
against a typed column, or against a literal, psycopg 3 works it out. `:x IS NULL` is the case
where there is genuinely nothing to infer from — NULL has no type and neither does the
parameter — so those, and only those, must be cast.
"""
from __future__ import annotations

import pathlib
import re

#: `:param IS NULL` / `:param IS NOT NULL`, where the parameter is not already cast.
_UNTYPED_NULL_TEST = re.compile(
    r"(?<!AS\s)(?<![\w.)])(:[a-z_][a-z0-9_]*)\s+IS\s+(?:NOT\s+)?NULL",
    re.IGNORECASE,
)


def test_a_parameter_compared_to_null_is_cast():
    """Nothing on either side of `:x IS NULL` carries a type, so psycopg 3 refuses to guess."""
    offenders = []
    for path in sorted(pathlib.Path("engine/boss_engine").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in _UNTYPED_NULL_TEST.finditer(source):
            line = source[:match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to('engine')}:{line}: {match.group(1)} IS NULL")
    assert not offenders, (
        "psycopg 3 cannot infer a parameter's type from a NULL comparison and will fail in "
        "production with AmbiguousParameter, while psycopg2 passes locally. Write "
        "`CAST(:x AS TEXT) IS NULL`:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_would_have_caught_the_bug_it_was_written_for():
    """The exact statement, before the fix."""
    broken = "external_ref = CASE WHEN :ref IS NULL THEN NULL ELSE :ref || 'x' END"
    assert _UNTYPED_NULL_TEST.search(broken)


def test_and_leaves_a_cast_one_alone():
    fixed = "external_ref = CASE WHEN CAST(:ref AS TEXT) IS NULL THEN NULL ELSE 'x' END"
    assert not _UNTYPED_NULL_TEST.search(fixed)


def test_a_column_compared_to_null_is_not_flagged():
    """`external_ref IS NULL` is ordinary SQL and has nothing to do with this."""
    assert not _UNTYPED_NULL_TEST.search("WHERE external_ref IS NULL")
