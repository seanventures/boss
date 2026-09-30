"""The step said what happened and never why, and the why was two tables away.

An operator's panel step read:

    Convene a panel — failed — "the meeting it started finished as 'cancelled'"

That sentence contains no cause. Tracing it by hand took four queries:

    1. POST /v1/meetings/step raised IntegrationConfigurationError: OPENAI_API_KEY is not
       configured, and returned 500.
    2. The meeting.step job retried and DEADLETTERED, keeping that message in jobs.last_error.
    3. The shepherd saw a run with no job in ('queued','running','leased') and closed it as
       cancelled — "abandoned: no job remains that could advance this run".
    4. The workflow step reported the STATUS of that run and nothing else.

The real cause — a missing credential the operator can fix in a minute — survived step 2 and
was discarded by steps 3 and 4. The console then offered "Try Convene a panel again": a button
guaranteed to fail identically forever, with nothing on screen explaining why.

This is the walk back down.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.workflow_capability_runner import _why_it_ended

REAL_CAUSE = "OPENAI_API_KEY is not configured."
SWEEPER_TAUTOLOGY = "abandoned: no job remains that could advance this run"


def _meeting(engine, project_id, run_id, error):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO meeting_runs (id, project_id, status, phase, error, directive) "
            "VALUES (:i, :p, 'cancelled', 'round1', :e, 'build a thing') "
            "ON CONFLICT (id) DO UPDATE SET error = EXCLUDED.error"),
            {"i": run_id, "p": project_id, "e": error})


def _job(engine, project_id, run_id, last_error):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload, last_error) "
            "VALUES (:i, :p, 'meeting.step', 'deadlettered', CAST(:pl AS JSONB), "
            "        CAST(:e AS JSONB)) ON CONFLICT (id) DO NOTHING"),
            {"i": f"job_{run_id}", "p": project_id,
             "pl": json.dumps({"run_id": run_id}), "e": json.dumps(last_error)})


def test_the_real_cause_is_recovered_from_the_job(engine):
    """The case that was reported. The run's error is the sweeper's tautology."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why1", SWEEPER_TAUTOLOGY)
    _job(engine, pid, "mrun_why1", {"type": "HTTPStatusError", "message": REAL_CAUSE})

    with engine.begin() as c:
        why = _why_it_ended(c, "meeting_runs", "mrun_why1")
    assert REAL_CAUSE in why
    # And it is no longer the sentence that explains nothing.
    assert SWEEPER_TAUTOLOGY not in why


def test_the_runs_own_error_is_preferred_when_it_is_a_real_one(engine):
    """Only the tautology triggers the extra hop — a genuine error is the better answer."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why2", "the panel could not reach a quorum")
    _job(engine, pid, "mrun_why2", {"message": "something less specific"})

    with engine.begin() as c:
        why = _why_it_ended(c, "meeting_runs", "mrun_why2")
    assert why == "the panel could not reach a quorum"


def test_an_empty_error_still_reaches_for_the_job(engine):
    """Three of the four cancelled meetings on the reported instance had no error at all."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why3", None)
    _job(engine, pid, "mrun_why3", {"type": "HTTPStatusError", "message": REAL_CAUSE})

    with engine.begin() as c:
        why = _why_it_ended(c, "meeting_runs", "mrun_why3")
    assert REAL_CAUSE in why


def test_it_reports_the_exception_type_when_there_is_one(engine):
    """"HTTPStatusError: 500" tells an operator where to look; a bare message may not."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why4", SWEEPER_TAUTOLOGY)
    _job(engine, pid, "mrun_why4", {"type": "IntegrationConfigurationError", "message": REAL_CAUSE})

    with engine.begin() as c:
        why = _why_it_ended(c, "meeting_runs", "mrun_why4")
    assert why.startswith("IntegrationConfigurationError:")


def test_no_job_and_no_error_degrades_quietly(engine):
    """The status is still reportable without a reason; this must never raise."""
    pid = get_settings().default_project_id
    _meeting(engine, pid, "mrun_why5", None)
    with engine.begin() as c:
        assert _why_it_ended(c, "meeting_runs", "mrun_why5") == ""


def test_a_missing_table_does_not_raise(engine):
    """Optional tables differ by edition; a diagnostic must not become the failure."""
    with engine.begin() as c:
        assert _why_it_ended(c, "table_that_does_not_exist", "x") == ""


class TestARetriedPanelFollowsTheRightOne:
    """Both guards were missing, and both produce a SILENT WRONG ANSWER.

    Found by an independent review of the first version, which ordered the whole
    fingerprint class by recency and took the first live row. Two consequences:

      * when the real retry was itself dead, it adopted a run that PRE-DATED the failure;
      * the fingerprint is (project, directive, url, type), so it could adopt a panel that a
        DIFFERENT workflow run commissioned.

    Either way the step force-completes on a stale panel's minutes, which is worse than the
    stuck step it replaced: it looks like success.
    """

    def _meeting(self, engine, mid, *, status, fp, wfr=None, age_days=0):
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO meeting_runs (id, project_id, status, phase, directive, "
                "        finalize_ctx, created_at) "
                "VALUES (:i, :p, :s, 'round1', 'd', CAST(:f AS JSONB), "
                "        now() - (:a || ' days')::interval) "
                "ON CONFLICT (id) DO NOTHING"),
                {"i": mid, "p": get_settings().default_project_id, "s": status,
                 "a": age_days,
                 "f": json.dumps({"meeting_fingerprint": fp,
                                  **({"workflow_run_id": wfr} if wfr else {})})})

    def test_it_never_adopts_a_panel_older_than_the_failure(self, engine):
        from boss_engine.services.workflow_capability_runner import _meeting_successor

        pid = get_settings().default_project_id
        self._meeting(engine, "mrun_old", status="completed", fp="fp1", age_days=2)
        self._meeting(engine, "mrun_dead", status="cancelled", fp="fp1", age_days=1)
        self._meeting(engine, "mrun_retry_failed", status="failed", fp="fp1", age_days=0)
        with engine.begin() as c:
            got = _meeting_successor(c, pid, "mrun_dead")
        assert got != "mrun_old", "adopted a panel that pre-dates the failure"
        assert got is None, f"nothing live is newer, so there is no successor (got {got})"

    def test_it_follows_a_genuine_later_retry(self, engine):
        from boss_engine.services.workflow_capability_runner import _meeting_successor

        pid = get_settings().default_project_id
        self._meeting(engine, "mrun_dead2", status="cancelled", fp="fp2", age_days=1)
        self._meeting(engine, "mrun_new2", status="completed", fp="fp2", age_days=0)
        with engine.begin() as c:
            assert _meeting_successor(c, pid, "mrun_dead2") == "mrun_new2"

    def test_it_does_not_adopt_another_workflow_runs_panel(self, engine):
        """Four real rows shared one fingerprint across four days and four runs."""
        from boss_engine.services.workflow_capability_runner import _meeting_successor

        pid = get_settings().default_project_id
        self._meeting(engine, "mrun_mine", status="cancelled", fp="fp3",
                      wfr="wfr_mine", age_days=1)
        self._meeting(engine, "mrun_theirs", status="completed", fp="fp3",
                      wfr="wfr_theirs", age_days=0)
        with engine.begin() as c:
            got = _meeting_successor(c, pid, "mrun_mine", "wfr_mine")
        assert got != "mrun_theirs", "adopted a panel commissioned by a different run"

    def test_it_still_works_when_the_awaited_run_records_no_workflow(self, engine):
        """Older rows carry no workflow_run_id; lineage alone must still function."""
        from boss_engine.services.workflow_capability_runner import _meeting_successor

        pid = get_settings().default_project_id
        self._meeting(engine, "mrun_nowfr", status="cancelled", fp="fp4", age_days=1)
        self._meeting(engine, "mrun_nowfr_new", status="running", fp="fp4", age_days=0)
        with engine.begin() as c:
            assert _meeting_successor(c, pid, "mrun_nowfr") == "mrun_nowfr_new"


class TestDeclaredInputsReadsBothShapes:
    """21 of 45 real workflow versions use JSON Schema, and this read only knew the other one.

    Every failed run of those reported `missing_inputs: ["required"]` and told the operator to
    supply an input called "required" — and because that list was truthy it SUPPRESSED the
    step-level diagnosis that would have named the real cause.
    """

    def test_json_schema_required_list(self):
        from boss_engine.tools.builtin import _declared_inputs

        assert _declared_inputs({"required": ["project_id", "task_ref"]}) == [
            "project_id", "task_ref"]

    def test_json_schema_properties_only(self):
        from boss_engine.tools.builtin import _declared_inputs

        assert sorted(_declared_inputs({"properties": {"a": {}, "b": {}}})) == ["a", "b"]

    def test_the_name_to_type_map_draft_workflow_emits(self):
        from boss_engine.tools.builtin import _declared_inputs

        assert sorted(_declared_inputs(
            {"media_artifact_id": "string", "title": "string"})) == [
            "media_artifact_id", "title"]

    def test_nothing_declared(self):
        from boss_engine.tools.builtin import _declared_inputs

        assert _declared_inputs({}) == [] and _declared_inputs(None) == []

    def test_a_step_error_is_never_suppressed_by_a_missing_input_list(self, engine):
        """The `elif` that hid the real answer.

        Asserted on BEHAVIOUR: when a run has both missing inputs and a step that recorded
        why it died, the reply must carry both. Whichever leads, neither may vanish.
        """
        import hashlib

        from boss_engine.context import EngineContext
        from boss_engine.tools import build_default_registry

        pid = get_settings().default_project_id
        spec = {"entry_step": "go", "input_schema": {"required": ["media_artifact_id"]},
                "steps": [{"id": "go", "kind": "capability", "next": [],
                           "binding": {"capability": "video_edit"}}]}
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                "  created_by) VALUES ('wfd_both', :p, 'both_demo', 'Both', 'ceo') "
                "ON CONFLICT (id) DO NOTHING"), {"p": pid})
            c.execute(text(
                "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                "  version, status, spec, content_hash, created_by) "
                "VALUES ('wfv_both', :p, 'wfd_both', 1, 'published', CAST(:s AS JSONB), "
                "  :h, 'ceo') ON CONFLICT (id) DO NOTHING"),
                {"p": pid, "s": json.dumps(spec),
                 "h": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()})
            c.execute(text(
                "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                "  workflow_version_id, actor, status, input) "
                "VALUES ('wfr_both', :p, 'wfd_both', 'wfv_both', 'ceo', 'deadlettered', "
                "  CAST('{}' AS JSONB)) ON CONFLICT (id) DO NOTHING"), {"p": pid})
            c.execute(text(
                "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                "  step_kind, status, attempt, max_attempts, last_error) "
                "VALUES ('wsr_both', :p, 'wfr_both', 'go', 'capability', 'deadlettered', "
                "  3, 3, 'ffmpeg exited 1: no such codec') ON CONFLICT (id) DO NOTHING"),
                {"p": pid})

        ctx = EngineContext(settings=get_settings(), engine=engine)
        out = build_default_registry().invoke(
            "describe_workflow_run", ctx, project_id=pid, actor="ceo",
            args={"run_id": "wfr_both", "project_id": pid})

        # The JSON-Schema form parsed correctly — not a phantom input called "required".
        assert out["declared_inputs"] == ["media_artifact_id"]
        remedy = out["remedy"]
        assert "media_artifact_id" in remedy["diagnosis"]
        # And the step's own error survives alongside it.
        assert "ffmpeg exited 1" in remedy["step_error"]
