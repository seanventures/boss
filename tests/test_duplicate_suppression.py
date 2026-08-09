"""Starting the same work twice while it is still running must not start it twice.

Seven identical QA-benchmark runs sat queued on the production box — four HookWatch, two
BugBox, one ApproveIt — because the Launch button was a bare form submit that changed nothing
on screen, so it was pressed again. Each of those is a full budgeted governed pipeline that
could produce nothing the first was not already producing.

The Task Shepherd did not catch it and could not have:

  * `sweep()` selects `status IN ('failed','blocked')` — seven HEALTHY runs are invisible to
    a component that only triages failures;
  * its `DUPLICATE_EXECUTION` class matches "lease reassigned" / "runfenced" — one run being
    executed twice by two workers, which is a different problem that happens to share a name.

So the guard belongs at creation, where it costs nothing. Detection after the fact only
reports what has already been spent.

    PYTHONPATH=engine .venv/bin/python tests/test_duplicate_suppression.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")
sys.path.insert(0, ".")

from boss_engine.config import Settings                       # noqa: E402
from boss_engine.db import begin, get_engine                  # noqa: E402
from boss_engine.ports import sql                             # noqa: E402
from boss_engine.services.code_task_service import (          # noqa: E402
    active_duplicate, start_code_task, work_fingerprint,
)

_failures: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures.append(f"{name}{f' — {why}' if why else ''}")


class _Jobs:
    """Enqueues nothing. This suite is about the row that gets created, not the worker."""

    def __init__(self, settings):
        self.settings = settings

    def create_job(self, *_a, **_k):
        return "job_test"


WORK = dict(
    title="dup-guard probe", instructions="Do the thing exactly once.",
    acceptance_criteria=["it happened once"], test_commands=[],
    owner="paramg", repo="boss-sandbox", base_branch="main", finalize_ctx=None,
)


def main() -> int:
    settings = Settings()
    engine = get_engine(settings)
    jobs = _Jobs(settings)
    project = "boss-dogfood"
    fp = work_fingerprint(project_id=project, owner=WORK["owner"], repo=WORK["repo"],
                          base_branch=WORK["base_branch"], title=WORK["title"],
                          instructions=WORK["instructions"])
    created: list[str] = []
    try:
        print("== the fingerprint identifies the WORK, not the request ==")
        same = work_fingerprint(project_id=project, owner=WORK["owner"], repo=WORK["repo"],
                                base_branch=WORK["base_branch"], title=f'  {WORK["title"]}  ',
                                instructions=WORK["instructions"] + "\n")
        check("whitespace does not make it a different task", same == fp)
        other = work_fingerprint(project_id=project, owner=WORK["owner"], repo=WORK["repo"],
                                 base_branch="release", title=WORK["title"],
                                 instructions=WORK["instructions"])
        check("a different branch IS different work", other != fp)

        print("== the second start returns the first run ==")
        with begin(engine) as c:
            first = start_code_task(c, project_id=project, task_id=None, jobs=jobs, **WORK)
        created.append(first)
        with begin(engine) as c:
            second = start_code_task(c, project_id=project, task_id=None, jobs=jobs, **WORK)
        check("pressing Launch twice yields ONE run", second == first,
              f"got a second run {second}")

        with begin(engine) as c:
            n = c.execute(sql("SELECT count(*) FROM code_task_runs "
                              "WHERE project_id=:p AND spec->>'work_fingerprint'=:f"),
                          {"p": project, "f": fp}).scalar()
        check("exactly one row exists in the database", n == 1, f"found {n}")

        print("== an explicit second run is still possible ==")
        with begin(engine) as c:
            forced = start_code_task(c, project_id=project, task_id=None, jobs=jobs,
                                     allow_duplicate=True, **WORK)
        created.append(forced)
        check("allow_duplicate starts a genuinely separate run", forced != first)

        print("== relaunching a stored spec is the SAME work ==")
        # The button sends back the instructions as stored, which already carry the engine's
        # deployability note. Appending it again grew the prompt by 460 characters per press
        # (1987 -> 2447 -> 2907, the same boilerplate three times over) and moved the
        # fingerprint each time — so three presses of one button produced three identical runs
        # and the guard never saw a duplicate. Hash the work, not the boilerplate; append the
        # note once.
        from boss_engine.services.code_task_service import _DEPLOYABILITY_NOTE

        with begin(engine) as c:
            stored = c.execute(
                sql("SELECT spec->>'instructions' FROM code_task_runs WHERE id=:i"),
                {"i": first}).scalar()
        copies = stored.count(_DEPLOYABILITY_NOTE.strip())
        check("the note is present exactly once", copies == 1, f"found {copies} copies")

        with begin(engine) as c:
            again = start_code_task(c, project_id=project, task_id=None, jobs=jobs,
                                    **dict(WORK, instructions=stored))
        check("relaunching the stored spec hits the same run", again == first,
              f"got {again}; the fingerprint moved")

        with begin(engine) as c:
            after = c.execute(
                sql("SELECT spec->>'instructions' FROM code_task_runs WHERE id=:i"),
                {"i": first}).scalar()
        check("and the instructions did not grow", len(after) == len(stored),
              f"{len(stored)} -> {len(after)}")

        print("== finishing releases the guard ==")
        # Relaunching a COMPLETED task is legitimate — that is what the Launch button on a
        # finished card is for. Only in-flight work is suppressed.
        with begin(engine) as c:
            c.execute(sql("UPDATE code_task_runs SET status='completed' "
                          "WHERE id IN (:a, :b)"), {"a": first, "b": forced})
            check("no in-flight duplicate remains", active_duplicate(c, project, fp) is None)
        with begin(engine) as c:
            third = start_code_task(c, project_id=project, task_id=None, jobs=jobs, **WORK)
        created.append(third)
        check("a finished task can be launched again", third not in (first, forced))
    finally:
        with begin(engine) as c:
            for run_id in created:
                c.execute(sql("DELETE FROM code_task_runs WHERE id=:i"), {"i": run_id})

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("duplicate work cannot be started twice while it is running")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
