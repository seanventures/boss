# The end-to-end harness

One command proves — or disproves — that every bundled workflow runs and every console screen
works, against a BOSS that runs the way an installed one does.

```sh
scripts/e2e.sh                # boot a disposable stack, drive everything, walk the console, tear down
make e2e                      # the same (private checkout)
scripts/e2e.sh selftest       # the harness's own fast tests: no Docker, no browser, under a minute
```

It exits non-zero when anything fails — including anything the stack left running afterwards —
and writes `workflows.json`, `console.json` and a screenshot per screen to
`artifacts/e2e/<timestamp>/`, next to every service's log. `workflows.json` is rewritten after
every run, so an interrupted run keeps what it collected.

## Why it exists

Before this, nothing started a workflow and watched it finish. The examples test published specs
to a fake server and asserted that **no** run started; the 214 web test files rendered single
components or grepped source; and every in-process API suite ran with transport auth **off** — so
the 403s boss-1 actually shows (the agent wizard posting as `vp_agents`, "Advance now" sending the
console key to a worker-only route) were invisible to all of them.

## What it boots (`up`)

| Piece | How | Why that way |
|---|---|---|
| Postgres | `postgres:16-alpine` in its own container, database `boss_e2e` | never the operator's `boss` database |
| API | `python -m apps.api.serve` (uvicorn, forwarded-header trust from `BOSS_TRUSTED_PROXIES`) — the image's own entry point | |
| Worker | `python -m apps.worker.boss_worker.main` | runs advance on their own, as on an install |
| Console | `next build` + `next start` (`--web-mode dev` for `next dev`) | |
| People | the first Owner is claimed through the console's origin with a setup link minted on the host shell; then `seed-test-accounts` makes one person per role, a custom `developer` role and a service account, credentials only in `<run dir>/seed-accounts.json` (0600) | the console has no shared password any more (IDENTITY.md §4.11); the walk signs in as `BOSS_E2E_ROLE` (default `owner`) |
| Auth | `BOSS_REQUIRE_API_AUTH=true`, owner key → `ceo`, worker key → `system` | exactly `scripts/install/community-compose.yml` |
| Edition | `community` (`--edition` to change) | what boss-1 runs |

The API and worker run with a **built** environment, not an inherited one: a scratch `HOME`, a
`PATH` whose first entry is `tests/e2e/stubs/bin`, and none of the developer's credentials. An
`OPENAI_API_KEY` in your shell cannot turn a scripted run into a paid one.

## The stubs (`tests/e2e/stubs/`)

- **`model_stub.py`** — an OpenAI-compatible server (`/v1/chat/completions`, `/v1/responses`,
  `/v1/embeddings`). Replies come from `model_script.json`: the first rule whose markers all
  appear in the prompt wins. A rule with `files` writes them — as `write_file` tool calls for the
  native code agent, directly for the CLI. Unmatched prompts get the `default` rule and are
  logged whole at `/_log`, which is where to look when writing a new rule. `/_override` makes one
  test answer differently (e.g. garbage for the routing prompt); `/_reset` undoes it.
- **`bin/claude`** — a `claude` CLI that asks the stub (`/_cli`) and prints Claude Code's own
  `json` / `stream-json` output, so `ClaudeCodeAgent` and `ClaudeCliAdapter` run unchanged.
  `codex`, `grok` and `cursor-agent` are present only to refuse.
- **`bin/docker`** — the host's real docker, except that every `run`, `create` and `build`
  gains `--label boss-e2e.run=<run id>`. The product's preview step builds and runs containers on
  the host; this label is how teardown finds them (see "Teardown").
- **`bin/ssh`** — plays a healthy deployment host: answers the host probe and the deploy's
  health poll, records every command at the fakes' `/deploy/ssh`.
- **`fakes.py`** — one process: an SMTP sink (`/_mail`), a Plane-style tracker (`/plane`), the
  apps a deploy serves (`*.e2e.test`), and an **intercepting HTTP(S) proxy** (`egress.py`).
  The services run with `HTTPS_PROXY` pointed at it and trust only this run's CA, so:
  - `api.github.com` is answered by `github_fake.py` — repos, Git Data API, pulls, merges —
    backed by real bare repositories, and `github.com` clone/push by `git http-backend` on the
    same repositories. A PR the build opens is what the preview and deploy then clone.
  - YouTube upload, Drive and Google OAuth are answered by small fakes.
  - **Any other host is refused with a 502 and logged** as blocked egress. A scripted run that
    reached a real provider would be the worst kind of green.

## What it checks

**`workflows`** — every template in `GET /v1/delivery-workflows/catalogue` and every
`examples/*/setup.py` (installed by running that script, as its README says). Each run must end
`completed` with every step `succeeded` or `skipped`. Every gate it stops at is recorded and then
answered as an operator would: approved, and a deploy step first given a host through
`POST /v1/delivery-workflows/runs/{id}/deploy-target`, which is what the release gate's host picker
calls. A gate the product already answered (the panel's PRD, auto-approved) is recorded as such.
For each failure the report names the step, its own error, the steps never reached, any prompt the
script did not cover, and any blocked egress.

Each run gets a verdict:

| Verdict | Meaning | Passes? |
|---|---|---|
| `pass` | completed, every step green | yes |
| `at an expected human gate` | gates are left open on this target, and the run waits at one with every earlier step green | yes |
| `unproven` | green, but some prompt was answered by the script's generic default reply, so the step it fed proves nothing | no |
| `fail` | anything else | no |

Prompts the script did not cover are printed for every run, not only failing ones. Only a run's
OWN prompts make it `unproven`: every input a run is given carries its marker
(`E2E <key> <stamp>`), and an unmatched prompt that is positively somebody else's — it carries
a background loop's words (`drive.BACKGROUND_PROMPTS`) or another run's marker and not this
one's — is reported as a side effect instead. Only the marker and the run's id are a run's own
words (`drive.attribute_prompts` refuses anything else): its step ids are ordinary words like
`plan`, which other runs' prompts contain too. A prompt that carries no marker at all still
counts against the run: attribution must never turn the default reply into a pass. PRs and
workflow runs that appear while a run is followed, beyond the run itself, are side effects too
(the Captain's "QUALITY IMPROVE PASS" PRs were found this way).

Nothing is abandoned silently: every run the harness stops following before it ends — at a
gate, on a timer, timed out or unreadable — is listed at the end. Timed-out and unreadable runs
are cancelled; on a live install, runs held at a gate are left for you to look at and listed
with the `curl` that cancels each. A fixture that cannot be set up is a failed precondition in
the report (and fails the suite), never a traceback; a transient 5xx while polling is retried.

**`console`** — signs in through `/login` as a person (email, password, then the authenticator
code; `harness/identity.py`), chooses the org on the org gate, then visits every entry of `AREAS` in `apps/web/app/ConsoleShell.tsx` at
`/?org=<org>&area=<id>`, presses every sidebar entry (`[data-area-nav=<id>]`) and visits every
static page route under `apps/web/app`. Routes with a dynamic segment (`/v2/missions/[id]`) are
listed in the report as not walked.

In each area it presses the **primary actions declared for it** in
`harness/console_contract.py`, by `data-testid`, and waits for the outcome declared with them
(a form or dialog that opens, a result that appears, a list that grows; or at least the screen
changing). A press that shows nothing fails with "pressing X showed nothing". Every area must
have an entry: a press, a journey, or `NothingToPress` with the reason (kept rare; the self-test
caps them).

Then the **journeys** (`harness/journeys.py`), each asserting the result an operator would look
for:

| Req. | Journey | Looks for |
|---|---|---|
| R1 | `skill_create`: Skills → write one by hand | "Saved <key>", and the key in Custom skills |
| R1 | `mcp_register`: Capabilities → register the fakes' MCP server (`/mcp`, one tool `e2e_echo`) | the server in the Capabilities list and under Skills → MCP servers |
| R2 | `agent_with_skill`: Agent work → Create agent → that skill and that MCP server → publish | the finished screen, the agent on the roster, and both grants read back through the API the runtime uses: the skill in `GET /v1/role-skills` (not `requested_skills`), the server in `GET /v1/role-mcp-servers` (a ticked box is not evidence) |
| R2 | `agent_update`: open that agent → give it another skill, and an MCP server it does not hold ("Give this agent an MCP server" → pick → confirm) | each listed after a reload — the server's name in `agent-mcp-held` even when the page would show `agent-mcp-empty` — and each read back as a grant; a refusal is reported in the page's own `agent-mcp-failed` sentence |
| R3 | `step_bindings`: Your steps → a step bound to the R1 skill, one bound to `e2e_echo` → both in a new workflow → save → reload → **run it** | "Save — done.", the steps after a reload and in the saved spec; in the run, the skill step's output names the skill and the MCP fake recorded the call |
| R4 | `step_agents`: two "Let an agent work it out" steps, one naming the R2 agent, one naming nobody → save → reload → **run it** | the pickers after a reload and the saved spec; in the run, the named step ran as that agent and was offered its R1 skill, the other was given an agent with `chosen_because` |
| R6 | `workflow_build` (smoke): two palette steps → save | "Save — done.", then "2 steps" after reloading and reopening it |
| R6 | `advance_now` (smoke): Workflow → Running → Advance now | a sentence that is not an error (run `workflows` first so there is a run) |

Each requirement names the controls that ARE it (`journeys.REQUIRES`), and a self-test fails
when no journey labelled with that requirement uses every one, so a smoke check cannot be
relabelled as proof again. Controls the milestone needs and no screen has yet go in
`journeys.PENDING_SELECTORS`: the journeys fail saying so, and the self-test fails the day one
appears so it moves into `JOURNEY_SELECTORS`. The agents stream's MCP grant controls
(`aw-mcp-${server.mcp_server_id}`, `agent-mcp-*`) arrived at integration and moved there; the
list is empty now. MCP servers are registered through the one shared panel
(app/mcp-servers-ui.tsx: `mcp-register-open`, `mcp-name`, `mcp-url`, `mcp-save`, `mcp-list`). A template id arrives by its fixed start
(`aw-mcp-`), whatever the page calls its variable. The requirement check reads each labelled
journey's code too (with the helpers it calls), so a table row alone cannot make a smoke check
proof. The
R3 and R4 runs go through the same driver as the templates (gates answered, leftovers
cancelled); their agent steps are answered by `agent.e2e-journey-step` in `model_script.json`.

A screen, action or journey fails on a console error, an uncaught page error, a same-origin
`/v1`, `/api` or document response of 400 or more, a visible `[role=alert]` / `.error-banner`,
or content that never renders. Disabled buttons with no stated reason and host warnings
(`host-advice`) are soft findings.

Responses are recorded as the browser saw them. `--capture-bodies` re-fetches each `/api` and
`/v1` call through Playwright to record failure bodies; findings made that way are marked
"(under interception)", because the walk then sits in the request path: the intermittent
"Response.clone: Body has already been consumed" 502s were only ever seen in that mode.

Nothing is listed by hand: templates, examples, areas and routes are read from the code
(`harness/discover.py`), and the self-tests enumerate each source to prove the readers miss
nothing. Every selector the walk depends on (`SOURCE_CONTRACT`, `PRIMARY_ACTIONS` and
`JOURNEY_SELECTORS`) is checked against the console source by a self-test that names the file
and what breaks.

## Teardown

`down` (and the end of `all`) removes everything the stack started on this machine, found by
the mark it was given at the source rather than by what the harness remembers starting:

- containers and images labelled `boss-e2e.run=<run id>`: Postgres, and every preview the
  product built through `stubs/bin/docker`;
- every process carrying `BOSS_E2E_RUN_ID=<run id>` or the stack's scratch `HOME` in its
  environment, wherever it was re-parented (git, the code agent, a preview server);
- the recorded API, worker, console and stub process groups, but a recorded PID is only
  signalled while its start time still matches, so a stale `down` never kills a stranger.

It then looks again and lists anything still alive with the command that removes it; `all`
fails if that list is not empty. After `down`, `status` and `workflows` say "No local stack is
up" instead of calling a dead API. `scripts/e2e.sh down --all` does the same for every stack
recorded under `artifacts/e2e/`: what a killed or crashed run left behind.

## Running pieces

```sh
scripts/e2e.sh up --no-web            # stack without the console (faster)
scripts/e2e.sh web                    # add the console to that stack
scripts/e2e.sh workflows --only fast_fix --only weekly-newsletter
scripts/e2e.sh console --only skills --only /v2/factory --headed
scripts/e2e.sh status                 # URLs of the running stack
scripts/e2e.sh perf --seed            # the performance walk (below)
scripts/e2e.sh down
```

**`perf`** (`harness/perf.py`) seeds realistic volume (`--seed`: 35 apps / 156 builds, 80
decisions, 40 workflow runs, 3000 events), restarts the API with a per-request log
(`BOSS_METRICS_LOG`) and `Server-Timing`, signs in as the Owner, then measures each API GET the
Factory leans on (p50/p95, statements, identity's share) and each page's render — time to first
and last byte, size, how many API requests ONE render made and how many were duplicates. Page
loads are 6 s apart by default (`--gap`), past the feed cache's reuse window: a cold load. It
fails when a page or endpoint breaks `PAGE_BUDGETS` / `API_P95_MS` (`--no-budget` only reports).
`--root <checkout>` (with `--restart-api --restart-web`) measures another version on the same
data; `--legacy` signs in the rc5 way (console password + API key).

`BOSS_E2E_RUN_TIMEOUT` (seconds, default 900) bounds each run. `scripts/e2e.sh down --all` stops
every stack ever recorded here, and everything each one started.

## Against a live install (boss-1), with real models

```sh
# on boss-1, from the checkout the install runs
set -a; . ~/boss/.env; set +a
export BOSS_E2E_API_URL="http://127.0.0.1:${BOSS_API_PORT:-8000}" \
       BOSS_E2E_OWNER_KEY="$BOSS_API_KEY" BOSS_E2E_WORKER_KEY="$BOSS_WORKER_API_KEY" \
       BOSS_E2E_CONSOLE_URL="http://127.0.0.1:${BOSS_WEB_PORT:-3000}"
# the console walk signs in as a seeded person: seed them once (an Owner's email), copy the file out
docker compose -f scripts/install/community-compose.yml --env-file ~/boss/.env exec -T -u boss api \
  python -m boss_engine.identity.cli seed-test-accounts --org org_boss --owner-email you@example.com \
  --out /tmp/seed.json --i-know      # --i-know: only if the install looks like production
docker compose -f scripts/install/community-compose.yml --env-file ~/boss/.env exec -T -u boss api \
  sh -c 'cat /tmp/seed.json && rm -f /tmp/seed.json' > ~/seed.json && chmod 600 ~/seed.json
export BOSS_E2E_ACCOUNTS_FILE=~/seed.json      # BOSS_E2E_ROLE=viewer walks the Viewer's console
scripts/e2e.sh all --target live --real-models
```

- It **spends**: without `--real-models` it refuses to start.
- **What it writes to the live install**, exactly:
  - `POST /v1/delivery-workflows/catalogue/install` once (skip with `--no-install`);
  - each `examples/*/setup.py`, which publishes its workflow and grants skills to roles (skip
    with `--no-examples`);
  - one workflow run per template and example it drives, each spending real model budget;
  - `POST /v1/delivery-workflows/run-capabilities` once as the owner and once as the worker, to
    report who may advance runs (this advances any run that is due);
  - approvals, only with `BOSS_E2E_APPROVE_GATES=1`;
  - cancellation of the runs it timed out on;
  - console writes (presses marked `write`, and the journeys, which create a skill, an MCP
    server pointing at `BOSS_E2E_FAKES_URL` or an unreachable address, an agent, a second skill
    grant on it, a second MCP server registered through the API when the agent already holds
    the first (named `<first>-update`) and an MCP grant on it, two custom steps, three workflows, and start two runs of those workflows,
    approving their gates), only with `--allow-writes`.

  It creates no hosts or deployment targets and never touches the database directly. A missing
  deployment host is reported, not made.
- Gates are left open unless `BOSS_E2E_APPROVE_GATES=1`. A run waiting at one with everything
  before it green is reported `at an expected human gate` and passes; those runs stay on the
  install and are listed at the end with the command that cancels each.
- It never touches the database directly and never needs the worker key to read; with the worker
  key it can say so when a worker-only route refuses the owner key.

## Adding to it

- A new template or example is driven automatically. If it fails on a prompt the script does not
  cover, the report lists the prompt's opening; `curl <model_stub>/_log` has it whole. Add a rule
  with markers taken from that prompt's fixed wording (never from the user's directive), keep
  narrow rules first, and run `scripts/e2e.sh selftest` — it proves every rule is still reachable.
- A new console area or route is walked automatically, and the self-test fails until the area
  has an entry in `PRIMARY_ACTIONS`: give its primary action a `data-testid` and declare what an
  operator should see after pressing it.
- A new external service gets a route in `egress.host_route` and a handler in `fakes.py`, with a
  self-test that shows what the fake keeps.
