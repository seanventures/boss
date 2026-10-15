# Workflow steps: for each

A `for_each` step runs one step once for every item of a list. The list can come from what the
run was started with or from an earlier step's output. Items can run a few at a time, and the
`for_each` step finishes with what each run produced, in item order.

| Field | Shape | Default | What it does |
|---|---|---|---|
| `items` | a path, or a list | required | The list to go through. A path reads what the run knows, the same paths a condition reads: `input.<name>` (what the run was started with), `steps.<id>.output.<field>` (an earlier step's output) or `context.<field>`. A literal JSON list also works. |
| `body` | a step id | required | The step to run once per item. |
| `max_items` | whole number, 1 to 1000 | 100 | The most items this step goes through. A longer list is refused when the run reaches the step, with a sentence that says how long the list was. It is never cut short. |
| `concurrency` | whole number, 1 to 20 | 1 | How many items run at the same time. |
| `next` | step ids | none | Where the run goes once every item is done. |

For example, this workflow visits every URL an earlier step found, three at a time:

```json
{
  "entry_step": "find",
  "steps": [
    {"id": "find", "kind": "capability", "binding": {"capability": "research"},
     "next": ["visit_each"]},
    {"id": "visit_each", "kind": "for_each", "items": "steps.find.output.urls",
     "body": "visit", "max_items": 50, "concurrency": 3, "next": ["summarise"]},
    {"id": "visit", "kind": "capability",
     "binding": {"agent_work": true,
                 "inputs": {"instructions": {"from_input": "instructions"},
                            "url": {"from_item": ""},
                            "position": {"from_item_index": true}}}},
    {"id": "summarise", "kind": "capability",
     "binding": {"agent_work": true,
                 "inputs": {"instructions": {"from_input": "instructions"},
                            "pages": {"from_step": "visit_each", "field": "results"}}}}
  ]
}
```

## The step it runs

The `body` step reads the item it is on from its binding:

- `{"from_item": ""}` is the whole item.
- `{"from_item": "url"}` reads a field of the item. A dotted path reads deeper, for example
  `"owner.name"`, and a number picks from a list, for example `"tags.0"`.
- `{"from_item_index": true}` is the item's position, counting from 0.

Each item runs as its own step run. Its `iteration` is the item's index, and the item is on the
step run's `input` as `item`, `item_index` and `item_count`. The body step keeps its own
`max_attempts`, `retry_backoff` and `timeout_seconds`, and each item gets its own attempts.

## What the for_each produces

When every item is done, the `for_each` step succeeds with this output:

```json
{"for_each": "visit_each", "count": 3, "done": 3,
 "results": [{"...": "item 0's output"}, {"...": "item 1's output"}, {"...": "item 2's output"}]}
```

`results` follows the order of the items, not the order they finished in. A later step reads
it with `{"from_step": "visit_each", "field": "results"}`. While items are running, the step
waits and its output has `count` and `done`, so the run view can say "3 of 10 done".

An empty list is not a failure. The `for_each` step succeeds at once with `results: []`. The
body step is recorded as skipped with reason `no_items`, and the run goes on.

## When an item fails

An item is retried under the body step's own `max_attempts` and `retry_backoff`. When an item
fails for good, the `for_each` step fails with "item 2 of 3 failed: ..." and the run stops at the
`for_each` step. Items already running finish, but no new items start.

- **Retry** on the `for_each` step runs only the failed items again, with the same list it
  resolved the first time. Items that succeeded keep their results.
- **Skip** on the `for_each` step carries the run on without it.

## What the run history shows

- `workflow.for_each_started` gives `count`, `concurrency`, `max_items` and `body`, plus
  `retried: true` on a retry.
- `workflow.for_each_item_started` is written once per item, on the body step run, with
  `for_each`, `item_index` and `item_count`.
- `workflow.for_each_finished` gives `count`.
- `workflow.for_each_failed` gives the `reason`. When an item caused it, it also gives
  `item_index` and `body_step_id`.

## What a workflow is refused for

Publishing refuses a workflow, naming the step and field, when:

- `items` is missing, is not a path into `input`, `steps` or `context`, or is a literal list
  longer than `max_items`.
- `items` reads a step that is not in the workflow, or one that does not always run before the
  `for_each` step.
- `body` is missing, is the `for_each` step itself, or is a control step (a condition, parallel,
  join, loop, gate, timer, sub-workflow or another `for_each`). It must be a step that does work.
- The body step has `next` steps or an `on_error` path. It goes back to its `for_each` after each
  item, so put what comes after on the `for_each` step.
- Another step leads to the body step, or sends its error path there. Only its `for_each` step
  may start it.
- A `bounded_loop` repeats a `for_each` step. That is not supported yet.
- `max_items` or `concurrency` is outside its range.
- A step that is not a `for_each` body reads `from_item` or `from_item_index`.

## In the designer

**For each** is in the palette's Flow group. It is drawn as a stack of cards, with an arrow into
the one step it runs. Placing it adds an agent task that is given the item as its `item` input.
In the step's settings:

- **For each item in** picks the list from the start form and earlier steps' outputs, or
  **Other** lets you type a path.
- **At most** sets the item limit.
- **At a time** sets how many items run together.

You can drag the arrow into its step onto another step to run that step instead. The workflow
check then says whether that step can be a body.
