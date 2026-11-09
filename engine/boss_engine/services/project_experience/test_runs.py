"""The project's test baseline: the last FULL test run BOSS saw, said with where it came from.

Every build keeps the tail of its verify output (`code_task_runs.state.last_verify_output`). Not
every run is the whole suite: a follow-up's verify often runs only the tests it touched (13 of a
190-test suite), so "the newest run" would say "13 / 13 pass" about a repository whose suite is
red. The baseline is therefore the NEWEST run among those that ran the most tests (the full
suite), and it always says where its numbers came from ("the last full run · PR #3 · 9h ago",
or "a blocked build" when that build never opened a PR).

When most failures share one setup error, the failures are not 119 broken features. When that
error is ENVIRONMENTAL by its structured data (suite_environment: the SQLSTATE/errno/exception
class says the suite could not reach its database or service), the suite did not run at all:
the baseline carries `env` ("BOSS couldn't run the full suite: the test database refused the
login BOSS gave it") and counts those tests as not run (`not_run`), never as failing — the
Tests ring, the onboarding checklist and the backlog say what happened instead of proposing
"Fix the 119 failing tests" (crun_de96bf12, helpdesk-ai-demo). Otherwise the shared error is
the app's and is named in one line (`cause`). Read only from BOSS's own records; nothing here
runs a test.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import age, href, iso

#: How many of the newest builds are read for the fullest run.
LOOK_BACK = 12
#: A run counts as the full suite when it ran at least this share of the most tests seen.
FULL_SHARE = 0.9
#: One setup error explains the failures when it is behind at least this share of those seen.
ONE_CAUSE_SHARE = 0.6

_TAP = (re.compile(r"^# tests (\d+)", re.M), re.compile(r"^# pass (\d+)", re.M))
_PYTEST = (re.compile(r"(\d+) passed"), re.compile(r"(\d+) failed"))
_JEST = re.compile(r"Tests:\s+(?:(\d+) failed, )?(?:\d+ skipped, )?(\d+) passed, (\d+) total")
#: node:test / TAP failure details ("failureType: 'hookFailed'", "error: '…'").
_FAILURE_TYPE = re.compile(r"failureType:\s*'([A-Za-z]+)'")
_ERROR = re.compile(r"^\s*error:\s*['\"](.+?)['\"]\s*$", re.M)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'|\b[0-9a-f]{8,}\b|\b\d+\b")


def parse_counts(output: Any) -> Optional[dict[str, int]]:
    """{passed, total} from a test run's output (jest/vitest, node:test TAP, pytest), or None."""
    out = str(output or "")
    if not out:
        return None
    jest = list(_JEST.finditer(out))
    if jest:
        m = jest[-1]
        return {"passed": int(m.group(2)), "total": int(m.group(3))}
    tests, passed = _TAP[0].findall(out), _TAP[1].findall(out)
    if tests and passed:
        return {"passed": int(passed[-1]), "total": int(tests[-1])}
    ok = _PYTEST[0].findall(out)
    if ok:
        bad = _PYTEST[1].findall(out)
        p_, f_ = int(ok[-1]), int(bad[-1]) if bad else 0
        return {"passed": p_, "total": p_ + f_}
    return None


def setup_cause(output: Any) -> Optional[str]:
    """One line when the failures in this output share one setup error, else None. Identifiers,
    numbers and quoted values are dropped from the line (a generated user name is not news)."""
    out = str(output or "")
    kinds = Counter(_FAILURE_TYPE.findall(out))
    errors = [(_QUOTED.sub("", e).strip(" :,.") or e)[:120] for e in _ERROR.findall(out)]
    if not errors:
        return None
    top, n = Counter(errors).most_common(1)[0]
    hooks = kinds.get("hookFailed", 0)
    if n / len(errors) < ONE_CAUSE_SHARE or hooks < max(1, sum(kinds.values()) // 2):
        return None
    return " ".join(top.split())


def _source(row: Any) -> str:
    from .common import pr_number

    n = row["pull_request_number"] or pr_number(row["pull_request_url"])
    if n:
        return f"PR #{n}"
    status = str(row["status"] or "")
    return {"blocked": "a blocked build", "failed": "a failed build",
            "running": "a running build"}.get(status, "a build")


def last_full_run(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    """{passed, total, failing, from, at, ago, label, cause?, env?, not_run?, build_id, href}
    or None. With `env`, `failing` is 0 and `not_run` counts the tests the suite never ran."""
    rows = connection.execute(text(
        "SELECT id, status, title, pull_request_number, pull_request_url, updated_at, "
        "right(state->>'last_verify_output', 20000) AS output, "
        "COALESCE(state->'build_database'->>'postgres', '') = 'true' AS boss_db "
        "FROM code_task_runs "
        "WHERE project_id = :p AND state ? 'last_verify_output' "
        "ORDER BY updated_at DESC LIMIT :n"), {"p": project_id, "n": LOOK_BACK}).mappings().all()
    runs = []
    for r in rows:
        got = parse_counts(r["output"])
        if got and got["total"]:
            runs.append((r, got))
    if not runs:
        return None
    most = max(g["total"] for _, g in runs)
    row, got = next((r, g) for r, g in runs if g["total"] >= FULL_SHARE * most)
    failing = got["total"] - got["passed"]
    at = row["updated_at"]
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    ago = f"{age(at, datetime.now(timezone.utc))} ago" if at else ""
    source = _source(row)
    full = len({g["total"] for _, g in runs}) > 1 or len(runs) > 1
    label = ", ".join(w for w in (("the last full run" if full else "the last run"), source, ago)
                      if w)
    out = {**got, "failing": failing, "from": source, "at": iso(row["updated_at"]),
           "ago": ago, "label": label, "build_id": row["id"],
           "href": href(project_id, area="code_tasks", view="apps", build=row["id"])}
    if failing:
        from ..suite_environment import classify

        env = classify(row["output"], boss_service=bool(row.get("boss_db")))
        if env:
            # Not the app's failures: the suite could not run them (BOSS's environment).
            out.update({"failing": 0, "not_run": failing, "env": env})
            return out
    cause = setup_cause(row["output"]) if failing else None
    if cause:
        out["cause"] = cause
    return out


def suite_words(run: Optional[Mapping[str, Any]], test_files: int) -> dict[str, str]:
    """THE ONE STATEMENT about a project's tests, said the same everywhere (Onboarding's live
    diagram, Discovered, the Ready checklist, the dossier): `label` names it in ≤ 3 words for a
    node ("40 test files", "142/148 tests"), `line` is the whole truth in one line.

    A suite BOSS could not run (`env`, suite_environment) has no pass rate: its counts are
    BOSS's environment, not the app ("71 / 190 pass" after the sandbox fix was a false claim),
    so it says what the repo holds and when the full run comes — "40 test files · full suite on
    next build"."""
    files = max(0, int(test_files or 0))
    named = f"{files} test file{'s' if files != 1 else ''}" if files else ""
    if run and run.get("env"):
        label = named or "Suite not run"
        return {"label": label, "line": f"{label} · full suite on next build", "state": "env"}
    if run and run.get("total"):
        failing = int(run.get("failing") or 0)
        return {"label": f"{run['passed']}/{run['total']} tests",
                "line": (f"{failing} failing → backlog" if failing
                         else f"{run['passed']} / {run['total']} pass"),
                "state": "failing" if failing else "passing"}
    if named:
        return {"label": named, "line": f"{named} · first build runs them", "state": "unrun"}
    return {"label": "No tests yet", "line": "No tests", "state": "none"}


__all__ = ["last_full_run", "parse_counts", "setup_cause", "suite_words"]
