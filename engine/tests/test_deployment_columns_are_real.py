"""A query against a column that does not exist fails every time, and said nothing.

BOSS found this itself. Asked to retry a failed deployment it called `redeploy`, hit the error,
and reported: "the internal redeploy tool is querying a non-existent column `target_id` in
deployment_runs (the correct column is `deployment_target_id`). This is a bug in BOSS's
deployment-retry code, not in your application." It was right.

Two things made it invisible for as long as it was:
  * the only caller was the shepherd's DEPLOY_FAILED remedy, which runs under autopilot — off
    by default since I18 — so nobody exercised it;
  * `_act_on_deployment` swallows the exception into "no_action: no deployment run for this
    task", which reads like an ABSENCE of data rather than a broken query.

So the guard is not "spell this column right". It is: every column a deployment query names
must exist in db/schema.sql — checked mechanically, because a typo here degrades to silence.
"""

import pathlib
import re

SCHEMA = pathlib.Path("db/schema.sql").read_text()


def _columns_of(table: str) -> set[str]:
    block = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?{table} \((.*?)\n\);", SCHEMA, re.S)
    assert block, f"{table} is not in db/schema.sql"
    cols = set()
    for line in block.group(1).splitlines():
        m = re.match(r"\s*([a-z_][a-z0-9_]*)\s+[A-Za-z]", line)
        if m and m.group(1).upper() not in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
            cols.add(m.group(1))
    return cols


def test_deployment_runs_has_the_column_the_code_now_uses():
    assert "deployment_target_id" in _columns_of("deployment_runs")


def test_the_column_the_code_used_to_use_really_does_not_exist():
    """If a future schema adds `target_id`, this test becomes wrong in a useful way: it says
    the two names now coexist and someone must pick one."""
    assert "target_id" not in _columns_of("deployment_runs")


def test_no_engine_query_selects_a_deployment_runs_column_that_is_missing():
    """Mechanical, because a typo here fails silently rather than loudly."""
    # Matched WITHIN ONE LINE, deliberately. The first version used re.S and the `.+?` ran
    # across the whole file, so every English word between an unrelated SELECT and the next
    # mention of deployment_runs was reported as a missing column. A guard that cries wolf on
    # prose gets deleted by the next person, which is worse than the narrower check.
    #
    # Cost of the narrowing, stated plainly: a SELECT whose column list is split across lines
    # is not checked. The bug this exists for — `SELECT id, target_id FROM deployment_runs` —
    # is on one line, as most of them are.
    cols = _columns_of("deployment_runs")
    offenders = []
    for path in list(pathlib.Path("engine/boss_engine").rglob("*.py")) + \
            list(pathlib.Path("apps/api/app").rglob("*.py")):
        for line in path.read_text().splitlines():
            m = re.search(r"SELECT\s+(.+?)\s+FROM deployment_runs", line, re.I)
            if not m:
                continue
            for token in m.group(1).split(","):
                token = token.strip().split()[0] if token.strip() else ""
                token = token.split(".")[-1]
                if not re.fullmatch(r"[a-z_][a-z0-9_]*", token or ""):
                    continue          # *, count(*), an expression — not a bare column
                if token in ("distinct",):
                    continue
                if token not in cols:
                    offenders.append(f"{path}: {token}")
    assert not offenders, (
        "these select columns deployment_runs does not have:\n  " + "\n  ".join(sorted(set(offenders))))


def test_the_failure_no_longer_reads_as_missing_data():
    """`no_action: no deployment run for this task` was returned for a BROKEN QUERY, which is
    why nobody looked. An error and an absence must not share a message."""
    import inspect
    from boss_engine.services import shepherd_service

    src = inspect.getsource(shepherd_service._act_on_deployment)
    assert "deployment_target_id" in src
