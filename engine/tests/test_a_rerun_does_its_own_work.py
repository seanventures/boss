"""A caller cannot assert that work has already happened.

`adopt_*` tells the runtime "this already happened, go and watch it rather than doing it". It
is true by construction in exactly one place: start_project_run sets it when a code task
creates the run that governs it.

Accepting it from a client let "Run this workflow again" hand back the previous run's input
whole. Measured on wfr_b83bce5dbb6b424987, a re-run of Dependency Freshness Report:

    input: adopt_code_task_run_id=crun_d9f30e31  adopt_meeting_run_id=mrun_76f07c8b
    research     not run
    meeting      adopted, 20s
    spec_review  adopted, 0s

— a re-observation of yesterday's build, not a re-run. The console was fixed first, and a
browser still holding the previous bundle did it again ten minutes later. Which is the point:
a console is a client, and the next client would do the same.
"""

from __future__ import annotations

import inspect


def _endpoint_source() -> str:
    from apps.api.app import main as api  # noqa: PLC0415

    return inspect.getsource(api.start_delivery_workflow)


def test_the_endpoint_refuses_adoption_from_a_caller():
    body = _endpoint_source()
    assert 'k.startswith("adopt_")' in body
    assert "run_input = {k: v for k, v in run_input.items()" in body


def test_it_says_what_it_refused():
    """Dropped in silence, a caller wonders why its run behaved differently from the one it
    copied."""
    body = _endpoint_source()
    assert "dropped = sorted" in body
    assert "ignored_input" in body


def test_the_console_strips_them_too():
    """Both halves, deliberately. The endpoint is the guarantee; the console not sending them
    keeps the request honest about what it is asking for."""
    from pathlib import Path

    ui = Path("apps/web/app/delivery-workflows-ui.tsx").read_text()
    assert "export function inputForRerun" in ui
    assert 'if (key.startsWith("adopt_")) continue;' in ui
    assert "input: inputForRerun(run.input)" in ui
