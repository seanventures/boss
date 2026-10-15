# Workflow test runs and running again from a step

## Test runs with pinned data

A test run is an ordinary run of a saved workflow version, marked `mode: "test"`. You can try a
workflow without paying for it or changing anything outside BOSS.

- **Pin a step.** You give the step an output (a JSON object). That step does not run. When it
  comes due it is recorded `succeeded` with that output, plus `output.pinned = true` and
  `output.pin` (where the pin came from). A `workflow.step_pinned` event is written. Later steps
  read the pinned output as if the step had produced it.
- **Nothing spends unless you say so.** In a test run, BOSS refuses any step that could spend
  money or reach outside BOSS, unless you pinned it or listed it in `allow_live`. A refused step
  is recorded `failed` with reason `test_run_refused`, a `workflow.step_test_refused` event is
  written, and the run stops at that step. It never stops silently. Try again on a refused step
  is refused for the same reason. To run it, start a new test that pins it or allows it.
- **What counts as spending.** BOSS reads this from each step's own risk declaration in the
  capability registry, the same one that sets the approval floor:

  | Step | In a test run |
  |---|---|
  | A capability at risk tier 0 that does not use a model (for example `report`, `cost_check`) | Runs |
  | An approval gate (for example `spec_review`) | Runs: it asks a person and spends nothing |
  | A capability at risk tier 1 or higher, or one that uses a model | Refused unless pinned or allowed |
  | A `task`, `skill`, `integration` or `agent_group` step, or a capability BOSS cannot classify | Refused unless pinned or allowed |
  | `condition`, `parallel`, `join`, `bounded_loop`, `decision`, `timer` | Run: they decide from the run itself |
  | `subworkflow` | Refused unless allowed: the child run would run for real |

- **Allowed steps get no shortcut.** A step in `allow_live` runs through the ordinary runner, with
  the same approval gates, budget reservations, receipts and dedupe keys as in a normal run. A
  `workflow.step_test_live` event records that it ran for real.
- **Control steps cannot be pinned.** A condition takes the branch the pinned data leads to. A
  decision asks its question again.

Test runs are left out of the run list, the scorecards and the workflow cost history by default.
Turn on **Show test runs** in the Runs list to see them. Each one is labelled **Test run**.

### In the designer

Press **Test run** in the editor bar. Every step is listed with what the test will do with it:
return its pin, run it, or refuse it. Type a JSON object into a step's box to pin it. Tick
**Let it run for real** to allow a step that would otherwise be refused. The test runs the saved
version. Save first if you want unsaved changes included.

## Running again from a step

On a finished or failed run, pick a step and press **Run again from here**. This starts a new
run of the same workflow version:

- Every step before the chosen one that succeeded is pinned to the output it produced in the
  original run. The pin's provenance (`output.pin`) names the original run, step run and
  iteration.
- The chosen step and everything after it run again.
- The new run records `rerun_of: {run_id, from_step}`. The original run gets a
  `workflow.rerun_started` event naming the new one.
- It keeps the original run's mode. Running a test run again from a step keeps that test's own
  pins on later steps, and its `allow_live` list.

BOSS refuses a rerun in two cases. The original run must be finished. And every step the chosen
step depends on, meaning the steps that are certain to have finished before it starts, must have
completed in the original run. Otherwise the refusal names each step that did not complete and
the state it was in.

## API

| Method | Path | Body or query |
|---|---|---|
| `POST` | `/v1/delivery-workflows/test-runs` | `project_id`, `workflow_key` (and optionally `version`) or `workflow_version_id`, `input`, `pins` (`{step_id: output}`), `allow_live` (`[step_id]`), optional `dedupe_key` |
| `GET` | `/v1/delivery-workflows/test-runs` | `project_id`, `limit`: the project's test runs |
| `POST` | `/v1/delivery-workflows/runs/{run_id}/rerun` | `project_id`, `from_step`, optional `dedupe_key` |
| `GET` | `/v1/delivery-workflows/runs` | `include_test=true` to list test runs too |

Each route is checked against the project named in the request. A run or version from another
project is a 404. Every run carries `mode`, `pins`, `allow_live` and `rerun_of`.
