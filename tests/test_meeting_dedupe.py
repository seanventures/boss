"""Convening the same meeting twice must not hold it twice.

An operator pressed "Convene the panel" and nothing on screen changed, so they pressed it
again. Six identical PRD meetings ran — three created inside the SAME SECOND — each staffing a
full agent panel and drafting its own ~12,000-word PRD, leaving six approvals in the inbox
with no way to tell which to approve.

Three in one second is why the UI guard is not sufficient on its own: by the time the button
re-renders, the other clicks are already in flight. The check has to be where every route
converges — the endpoint and the Plane `boss:design` intake both call
`convene_prd_meeting_core`.

    PYTHONPATH=engine .venv/bin/python tests/test_meeting_dedupe.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")
sys.path.insert(0, ".")

from boss_engine.config import Settings                       # noqa: E402
from boss_engine.db import begin, get_engine                  # noqa: E402
from boss_engine.ports import sql                             # noqa: E402
from boss_engine.services.meeting_service import (            # noqa: E402
    active_duplicate_meeting, meeting_fingerprint, start_meeting_run,
)

_failures: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures.append(f"{name}{f' — {why}' if why else ''}")


def main() -> int:
    engine = get_engine(Settings())
    project = "boss-dogfood"
    directive = "Build ApproveIt, an expense approval workflow. (dedupe probe)"
    fp = meeting_fingerprint(project, directive)
    created: list[str] = []

    print("== the fingerprint identifies the SUBJECT, not the keystrokes ==")
    check("whitespace does not make it a different meeting",
          meeting_fingerprint(project, f"  {directive}  \n") == fp)
    check("a different directive is a different meeting",
          meeting_fingerprint(project, directive + " and also BugBox") != fp)
    check("the same directive in another project is separate",
          meeting_fingerprint("other-project", directive) != fp)

    try:
        print("\n== a meeting already running is found ==")
        with begin(engine) as c:
            check("nothing running yet", active_duplicate_meeting(c, project, fp)[0] is None)
            run_id = start_meeting_run(
                c, project_id=project, task_id=None, directive=directive, url="",
                constraints={}, role_keys=[],
                finalize_ctx={"meeting_fingerprint": fp})
        created.append(run_id)
        with begin(engine) as c:
            found, _task = active_duplicate_meeting(c, project, fp)
        check("the running meeting is found by fingerprint", found == run_id,
              f"got {found}")

        print("\n== finishing releases it ==")
        # Convening the same panel again LATER is legitimate — the subject may have moved on.
        # Only a meeting still in progress suppresses a second one.
        with begin(engine) as c:
            c.execute(sql("UPDATE meeting_runs SET status='completed' WHERE id=:i"),
                      {"i": run_id})
            check("a completed meeting no longer blocks a new one",
                  active_duplicate_meeting(c, project, fp)[0] is None)
    finally:
        with begin(engine) as c:
            for rid in created:
                c.execute(sql("DELETE FROM meeting_runs WHERE id=:i"), {"i": rid})

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("a meeting in progress cannot be convened a second time")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
