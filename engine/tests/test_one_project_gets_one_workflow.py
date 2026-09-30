"""One app, two workflow runs, and the operator was shown two projects.

Observed on shared-server:

    waiting  Interview Scorecard App                        Standard Product Delivery v1
             3 of 8   started 5 Sep 23:40Z   Research skipped, spec approved in 0s
             build -> AWAIT crun_33a0a77e6ccf4d17b36e   ADOPTED true

    waiting  Build an interview scorecard app. Who it is for: a hiring manager tir…
                                                          Standard Product Delivery v2
             3 of 10  started 5 Sep 23:35Z   Research 3m35s, panel 40s, spec succeeded
             build -> RUN ID crun_33a0a77e6ccf4d17b36e

The second one is not a project. Its "directive" is BOSS's own scaffold prompt — "A buildable
FULL-STACK scaffold ALREADY EXISTS on this branch" — and it adopted the code task that the
FIRST run had just produced.

The chain, confirmed against the live rows:

  1. wfr_de0ff (v2) started 23:35:36 with the operator's brief and ran research, panel, spec.
  2. Its spec_review gate opened dec_7cfbf7c776e149fe9dc5 — `workflow_step_runs.decision_id`
     names both.
  3. Approving it fired `_launch_build`, which calls `start_web_app_build` directly.
  4. `start_code_task` checks `_already_governed = finalize_ctx["workflow_run_id"]`, found
     nothing, concluded this was a brand new project, and opened wfr_666b — resolving the
     project's MAPPING, which points at v1 — to adopt the task it had just created.

The guard was right and the path never fed it. `create_code_task` sets `workflow_run_id`
because a workflow STEP calls it; the panel path, which is how projects are actually started,
had nothing to set it from. The link needed was already in the database.
"""
from __future__ import annotations

import inspect

from boss_engine.services import code_task_service, decisions


class TestTheApprovalKnowsWhichRunItBelongsTo:
    """A gate's decision is recorded on its step. That is the join nobody was making."""

    def test_the_run_is_resolved_from_the_decision(self):
        body = inspect.getsource(decisions)
        assert "FROM workflow_step_runs" in body
        assert "WHERE decision_id = :d" in body

    def test_and_travels_with_the_build_it_launches(self):
        body = inspect.getsource(decisions)
        assert 'origin_ctx = {**(origin_ctx or {}), "workflow_run_id": str(governing_run_id)}' \
            in body

    def test_a_decision_that_belongs_to_no_workflow_still_builds(self):
        """Most decisions are not workflow gates. Approving one must not start failing because
        a lookup came back empty — the behaviour there is exactly what it was."""
        body = inspect.getsource(decisions)
        block = body[body.index("governing_run_id = None"):]
        assert "except Exception" in block[:1600]
        assert "if governing_run_id:" in block[:2400]

    def test_the_meeting_is_tried_when_the_decision_link_is_not_written_yet(self):
        """Measured on the duplicate that got through after the first version of this fix:

            05:53:02  decision approved      <- the lookup runs, finds nothing
            05:53:23  code task created      <- opens a SECOND workflow run
            05:53:26  step.decision_id set   <- the link appears, three seconds too late

        A spec_review gate ADOPTS a decision the meeting already made, so the workflow has not
        claimed it at approval time. The meeting has been claimed since the run's own meeting
        step produced it."""
        body = inspect.getsource(decisions)
        assert "s.output->>'meeting_run_id' = :m" in body
        assert "meeting_id = (origin_ctx or {}).get(\"meeting_run_id\")" in body

    def test_only_a_live_run_can_claim_the_build(self):
        """A finished run is not governing anything; attaching a build to it would hide the
        build rather than place it."""
        body = inspect.getsource(decisions)
        assert "r.status IN ('running', 'waiting')" in body

    def test_the_exact_decision_link_is_still_preferred(self):
        """It is the precise answer when it exists; the meeting is the earlier-available one."""
        body = inspect.getsource(decisions)
        assert body.index("WHERE decision_id = :d") < body.index("meeting_run_id' = :m")


class TestTheGuardSeesIt:
    def test_the_run_id_is_read_from_origin_as_well_as_the_top_level(self):
        body = inspect.getsource(code_task_service)
        assert '_from_origin = (run_finalize.get("origin") or {}).get("workflow_run_id")' in body

    def test_and_is_lifted_to_where_everything_else_looks(self):
        """Downstream code — the adopt bookkeeping, the console's "which run governs this" —
        has always read the top level. Leaving it nested would fix the duplicate and break the
        attribution."""
        body = inspect.getsource(code_task_service)
        assert 'run_finalize["workflow_run_id"] = str(_from_origin)' in body

    def test_the_guard_still_decides_whether_a_run_is_opened(self):
        body = inspect.getsource(code_task_service)
        assert '_already_governed = bool(run_finalize.get("workflow_run_id"))' in body
        assert "_is_new_project = (not _already_governed)" in body

    def test_an_ungoverned_project_still_gets_its_outer_run(self):
        """The opposite failure, and the reason this code exists at all: a project born from a
        panel used to get NO outer run, so the Workflows page sat empty while it ran. The fix
        for the duplicate must not bring that back."""
        body = inspect.getsource(code_task_service)
        assert "if _is_new_project:" in body
        assert "start_project_run" in body


class TestTheOrderOfPrecedence:
    """Exercised as data, because the resolution is what decides whether a run is duplicated."""

    @staticmethod
    def _resolve(finalize: dict) -> bool:
        """The service's own rule, in the shape the service applies it."""
        if not finalize.get("workflow_run_id"):
            from_origin = (finalize.get("origin") or {}).get("workflow_run_id")
            if from_origin:
                finalize["workflow_run_id"] = str(from_origin)
        return bool(finalize.get("workflow_run_id"))

    def test_a_task_made_by_a_workflow_step_is_governed(self):
        assert self._resolve({"workflow_run_id": "wfr_de0ff"}) is True

    def test_a_task_made_by_approving_a_gate_is_governed(self):
        """The case that produced two runs for the Interview Scorecard App."""
        finalize = {"origin": {"kind": "meeting", "workflow_run_id": "wfr_de0ff"}}
        assert self._resolve(finalize) is True
        assert finalize["workflow_run_id"] == "wfr_de0ff"

    def test_a_task_started_from_nothing_is_not(self):
        """A directive typed into a chat is a new project and SHOULD open a run."""
        assert self._resolve({"origin": {"kind": "meeting"}}) is False
        assert self._resolve({}) is False

    def test_the_top_level_wins_over_origin(self):
        """`origin` is the beginning of the lineage and can be carried forward by retries and
        follow-ups; the top level is this task's own governance. A retry that inherited an old
        origin must not be re-attributed to the run that started the lineage."""
        finalize = {"workflow_run_id": "wfr_current",
                    "origin": {"workflow_run_id": "wfr_original"}}
        assert self._resolve(finalize) is True
        assert finalize["workflow_run_id"] == "wfr_current"
