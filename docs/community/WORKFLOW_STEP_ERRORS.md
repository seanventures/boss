# Workflow steps: retries, timeouts and error paths

A step that does work (a capability, skill, integration, agent group or task step) can say what
happens when it fails. Every field below is optional. A step without them behaves exactly as
before: up to `max_attempts` attempts, retried at once, and the run stops at the step when the
last attempt fails.

| Field | Shape | Default | What it does |
|---|---|---|---|
| `max_attempts` | whole number, 1 to 25 | 3 | How many automatic attempts the step gets. |
| `retry_backoff` | `{"initial_seconds": 0..3600, "multiplier": 1..10, "max_seconds": 0..86400}` | `{0, 2, 3600}` | How long to wait between automatic attempts. After attempt *n* fails, the step is not started again before `min(initial_seconds × multiplier^(n−1), max_seconds)` seconds have passed. With `initial_seconds` 0 there is no wait. |
| `timeout_seconds` | whole number, 1 to 86400 | none | How long one attempt may run. The clock starts when the attempt is leased to a worker. An attempt that runs longer is ended as failed with reason `step_timeout`. It counts as an attempt, and then backs off and retries, or fails for good. |
| `on_error` | a step id | none | Where the run goes when this step fails for good (out of attempts, or a failure no retry can fix). The step stays recorded as failed, and the run carries on at the `on_error` step instead of stopping. |

For example, this step tries a deploy up to four times. It waits 30, 60 and then 120 seconds
between attempts and gives each attempt ten minutes. If it still fails, the run goes on to
`rollback`:

```json
{
  "id": "deploy",
  "kind": "capability",
  "binding": {"capability": "deploy"},
  "next": ["announce"],
  "max_attempts": 4,
  "retry_backoff": {"initial_seconds": 30, "multiplier": 2, "max_seconds": 600},
  "timeout_seconds": 600,
  "on_error": "rollback"
}
```

## What the run history shows

Every retry, timeout and error path is recorded as an event on the run:

- `workflow.step_retry_scheduled` gives the attempt, `backoff_seconds`, the time the step is next
  due (`run_after`) and, for a timeout, `reason: step_timeout`.
- `workflow.step_timed_out` gives `timeout_seconds`, the attempt and the worker that held it.
- `workflow.error_path_taken` gives `step_id`, `error_step_id`, the `reason`
  (`attempts_exhausted`, `not_retryable` or `step_timeout`) and `attempts`. It is written after a
  `workflow.step_failed` event for the step. Nothing is dead-lettered, and the run does not stop
  and wait for an operator, because the workflow already says what to do.
- When the step **succeeds**, its `on_error` step is skipped with reason `error_path_not_taken`.
  If another live path can still reach that step, it is kept instead
  (`workflow.step_not_skipped`). This follows the same rule as a condition's path not taken.
  If the run finishes and no path reached the step, it is recorded as skipped with reason
  `path_not_reached`.

## What a workflow is refused for

Publishing refuses a workflow, naming the step, when:

- `on_error` names a step that does not exist, the step itself, or the workflow's start step.
- `on_error` leads back to a step that reaches the failing step. That would make a loop with
  no bound. Use `max_attempts` to retry, or a `bounded_loop` to repeat.
- `on_error` points at a join or a loop body, or it is set on a loop body. The same holds for
  the step a `for_each` runs (see [Workflow for each](WORKFLOW_FOR_EACH.md)).
- `retry_backoff`, `timeout_seconds` or `on_error` is set on a condition, parallel, join, loop,
  approval gate, timer or sub-workflow step. Those steps have no attempts of their own.
- A value is outside its range, or `retry_backoff` has a field it does not know.

A step reached only through an error path can bind only the outputs of steps that finished
before the failing step. It cannot bind the failing step's output, because that step did not
produce one.
