"""Two defects that turned one broken app into an endless, unreadable loop.

Observed live: 49 of 50 previews for a project had FAILED, one created every ~3 minutes,
continuously. Each was a full `npm install` + vite build leaving a ~250MB workdir that the
reclaimer then swept — visible in the container log as `preview_workdirs_reclaimed count=1`
every 3.3 minutes, forever.

  1. NOTHING REMEMBERED THE FAILURES. The captain's `preview_gap` finding fires when no preview
     is running and verify-green apps exist, with `auto_safe: True`. A preview that cannot build
     fails, `running_previews` drops back to 0, and the next tick proposes the identical remedy.
     A self-healing action that cannot heal has to stop and escalate.

  2. THE FAILURE MESSAGE OMITTED THE FAILURE. The log tail kept the last 300 characters, which
     for a Node stack trace is the least informative end of the file. The operator got three
     frames of `rollup/.../parseAst.js` and no error message, 49 times over.
"""

from __future__ import annotations

import pytest

from boss_engine.services.metatron_captain import (
    PREVIEW_FAILURE_GIVE_UP,
    PREVIEW_FAILURE_WINDOW_MINUTES,
)
from boss_engine.services.preview_service import _why_it_failed


# ── Giving up ──────────────────────────────────────────────────────────────────

def _findings(**snapshot):
    from boss_engine.services import metatron_captain as mc

    base = {"running_previews": 0, "completed_verify_green": 1,
            "recent_failed_previews": 0, "budget_state": "green"}
    base.update(snapshot)
    return {f["id"]: f for f in mc.findings_from_snapshot(base, goals=[])}


def test_the_first_failures_still_get_an_automatic_restart():
    """A preview can die for a transient reason. Retrying is right — the loop is not."""
    found = _findings(recent_failed_previews=PREVIEW_FAILURE_GIVE_UP - 1)
    assert "preview_expired" in found
    assert found["preview_expired"]["auto_safe"] is True


def test_after_enough_failures_it_stops_restarting_and_escalates():
    found = _findings(recent_failed_previews=PREVIEW_FAILURE_GIVE_UP)
    assert "preview_expired" not in found, "still proposing the remedy that keeps failing"
    assert "preview_failing" in found
    assert found["preview_failing"]["auto_safe"] is False, "a human must see this, not a loop"


def test_the_escalation_says_how_many_and_over_what_window():
    found = _findings(recent_failed_previews=9)
    title = found["preview_failing"]["title"]
    assert "9" in title and str(PREVIEW_FAILURE_WINDOW_MINUTES) in title


def test_a_healthy_project_gets_no_preview_finding_at_all():
    assert _findings(running_previews=1) == {} or "preview_expired" not in _findings(
        running_previews=1)


def test_the_window_spans_several_ticks_but_still_forgives():
    """Too short and the count never accumulates across ticks; too long and a genuinely fixed
    app stays written off for the rest of the day."""
    assert 10 <= PREVIEW_FAILURE_WINDOW_MINUTES <= 120
    assert 2 <= PREVIEW_FAILURE_GIVE_UP <= 5


# ── Saying what actually broke ─────────────────────────────────────────────────

_ROLLUP_LOG = """
> vite build
transforming...
[vite]: Rollup failed to resolve import "./NotThere" from "src/main.tsx"
    at getRollupError (file:///tmp/x/node_modules/rollup/dist/es/shared/parseAst.js:313:42)
    at ModuleLoader.loadEntryModule (file:///tmp/x/node_modules/rollup/dist/es/shared/node-entry.js:22023:20)
    at async Promise.all (index 0)
"""


def test_the_reason_survives_even_when_the_stack_trace_is_long():
    """THE WHOLE POINT. The old tail returned only the frames below the error."""
    out = _why_it_failed_from_text(_ROLLUP_LOG)
    assert "Rollup failed to resolve import" in out
    assert '"./NotThere"' in out


def test_stack_frames_are_not_mistaken_for_the_reason():
    out = _why_it_failed_from_text(_ROLLUP_LOG)
    reason = out.split("log tail:")[0]
    assert "at getRollupError" not in reason
    assert "at async Promise.all" not in reason


def test_the_tail_is_still_included_for_context():
    assert "log tail:" in _why_it_failed_from_text(_ROLLUP_LOG)


def test_an_empty_or_missing_log_says_so_rather_than_returning_nothing():
    """"" is indistinguishable from "the log said nothing", and the operator has to be able to
    tell those apart."""
    assert "empty" in _why_it_failed_from_text("")
    assert "unavailable" in _why_it_failed("/no/such/path/at/all.log")


def test_a_successful_looking_log_still_returns_the_tail():
    out = _why_it_failed_from_text("building...\nready in 400ms\n")
    assert "ready in 400ms" in out


@pytest.mark.parametrize("marker", ["EADDRINUSE", "Cannot find module 'x'", "npm ERR! code E404"])
def test_common_build_failures_are_recognised(marker):
    assert marker.split("'")[0][:12] in _why_it_failed_from_text(f"noise\n{marker}\nmore noise\n")


def _why_it_failed_from_text(text: str) -> str:
    import tempfile
    from pathlib import Path as _P

    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return _why_it_failed(path)
    finally:
        _P(path).unlink(missing_ok=True)
