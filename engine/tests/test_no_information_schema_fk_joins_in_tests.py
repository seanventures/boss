"""Tests never join the information_schema constraint views (BOSSAPP-1144).

`information_schema.table_constraints JOIN key_column_usage JOIN constraint_column_usage` ran for
minutes on the shared bench DB (many schemas, many tables) and spilled 6-11 GB of Postgres temp
files twice in one day. Foreign keys come from pg_catalog instead: engine/tests/_pg_foreign_keys.py
(`tables_referencing`, `foreign_key_parents`). Cheap single-view lookups such as
`information_schema.columns WHERE table_name = ...` stay allowed.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: The constraint views whose joins are the slow class.
_SLOW_VIEW = re.compile(
    r"information_schema\s*\.\s*(table_constraints|key_column_usage|constraint_column_usage"
    r"|referential_constraints|constraint_table_usage)\b", re.IGNORECASE)

#: "path/relative/to/repo.py" for a reviewed, cheap single-view lookup. Empty today.
ALLOWED: frozenset[str] = frozenset()


def _test_sources():
    for root in sorted(REPO.glob("**/tests")):
        if any(part in {"node_modules", "node_modules.off", ".venv", "venv", ".git"}
               or part.startswith("boss-wt-") for part in root.relative_to(REPO).parts):
            continue
        yield from sorted(root.rglob("*.py"))


def test_no_test_joins_the_information_schema_constraint_views():
    here = Path(__file__).resolve()
    offenders = []
    for path in _test_sources():
        if path.resolve() == here:
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore")
                                      .splitlines(), 1):
            if _SLOW_VIEW.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "information_schema constraint views are slow on a many-schema database (BOSSAPP-1144); "
        "use engine/tests/_pg_foreign_keys.py (pg_catalog) instead:\n" + "\n".join(offenders))


def test_the_guard_sees_the_test_trees():
    roots = {p.relative_to(REPO).parts[0] for p in _test_sources()}
    assert "engine" in roots and "apps" in roots
