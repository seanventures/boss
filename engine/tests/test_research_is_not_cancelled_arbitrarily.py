"""Research that is working must not be swept away for looking idle.

A discovery has no JOB. `start_discovery` inserts the row and drives the pipeline on a daemon
thread, so there is nothing in `jobs` to point at. The Task Shepherd's abandonment rule asks
exactly that question — "is there a job carrying this?" — and for a discovery the answer is
always no. All that stands between a discovery and the reaper is `updated_at`, and one research
phase can legitimately run past the twenty-minute window without reaching the next `_phase`
call. Measured: drun_c341f12b7dfc43e5b500 finished `cancelled`, which failed the research step,
which failed the whole run (wfr_9782313e30cf4826a0) before a line of code was written.

Two failure modes, two fixes: a pipeline that is alive says so, and a pipeline whose process
died is restarted rather than left to be reaped.
"""

from __future__ import annotations

import inspect

from boss_engine.services import discovery_service as ds


def test_a_running_pipeline_touches_its_row():
    """The shepherd's only liveness signal for a discovery is `updated_at`."""
    assert hasattr(ds, "_still_working")
    body = inspect.getsource(ds._still_working)
    assert "UPDATE discovery_runs SET updated_at = now()" in body
    assert "stop.wait(60)" in body


def test_it_beats_faster_than_the_sweep_gives_up():
    """Twenty minutes is the sweep's window; a beat a minute leaves nineteen spare."""
    from boss_engine.services.shepherd_service import ORPHAN_AFTER_MINUTES

    assert 60 * 4 < ORPHAN_AFTER_MINUTES * 60, "the heartbeat is not comfortably inside the window"


def test_a_finished_run_keeps_its_end_time():
    """Moving a finished run's timestamp makes "when did this end" unanswerable."""
    assert "AND status = 'running'" in inspect.getsource(ds._still_working)


def test_the_beat_stops_with_the_pipeline():
    """A heartbeat outliving its pipeline keeps a dead run looking alive — exactly the state
    the shepherd exists to catch."""
    body = inspect.getsource(ds._run_discovery)
    assert "finally:" in body
    assert "_hb_stop.set()" in body


def test_research_interrupted_by_a_restart_is_resumed():
    """The pipeline is a daemon thread, so it dies with its process. Every API restart — a
    deploy, a crash — orphaned every discovery in flight."""
    assert hasattr(ds, "resume_interrupted")
    body = inspect.getsource(ds.resume_interrupted)
    assert "WHERE status = 'running'" in body
    assert "_run_discovery" in body


def test_the_resume_is_recorded_where_somebody_can_see_it():
    """Restarting research silently would make a run's duration inexplicable."""
    assert '"discovery.resumed"' in inspect.getsource(ds.resume_interrupted)


def test_one_unresumable_run_does_not_block_the_others():
    body = inspect.getsource(ds.resume_interrupted)
    assert "except Exception" in body and "continue" in body


def test_the_dossier_sections_are_the_ones_a_dossier_actually_has():
    """Guessed section names hand the panel an empty document.

    The first version of this list read `brief`, `findings`, `analysis`, `outcomes` — none of
    which exist. A completed run (drun_e615b07aa693491a8aba) stores `research_brief`,
    `signal_findings` (twelve of them), `market_analysis`, `critique`, `candidate_outcomes` and
    `gate`. Reading the wrong names is silent: every section comes back empty, the handoff
    "works", and the panel argues from nothing.
    """
    from boss_engine.services.workflow_capability_runner import _DOSSIER_SECTIONS

    keys = {k for k, _ in _DOSSIER_SECTIONS}
    for real in ("research_brief", "signal_findings", "market_analysis",
                 "critique", "candidate_outcomes", "gate"):
        assert real in keys, f"a dossier stores {real} and the handoff ignores it"
    for invented in ("brief", "findings", "analysis", "outcomes"):
        assert invented not in keys, f"{invented} is not a key any dossier has"


def test_a_dossier_of_the_real_shape_produces_readable_prose():
    """The panel reads this inside a prompt, so it has to be text — not JSON with a heading."""
    from boss_engine.services.workflow_capability_runner import _dossier_brief

    out = _dossier_brief("Dependency freshness", {
        "research_brief": {"question": "how do teams keep deps current"},
        "signal_findings": ["renovate is the default", "most teams batch monthly"],
        "candidate_outcomes": [],
    })
    assert "Dependency freshness" in out
    assert "What the research found" in out
    assert "renovate is the default" in out
    # An empty section is omitted rather than printed as a heading over nothing.
    assert "Outcomes worth considering" not in out
