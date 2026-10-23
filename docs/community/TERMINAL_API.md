# Terminal and API

The Metatron terminal is a command-line client for the BOSS HTTP API. It uses the same control plane, credentials, and server-side enforcement as any other API client, and adds project selection, operation discovery, and one-shot JSON output for scripts.

Metatron is a client of the same BOSS agent as the console's **Ask BOSS**. When you type a sentence, it goes through the same agent turn as a message typed in the console. The thread, the decision, the actions, consent and step-up are all the same, and so is the reply. A conversation you start in the terminal continues in the console, and the other way round, as long as you use the same actor. Captain powers (`/watch`, `/overwatch`, `/drive`) run only when you type them as commands. A sentence never sets them off.

New to BOSS? The [quickstart](QUICKSTART.md) gets you to a console on `127.0.0.1`, and [Features](FEATURES.md) lists what Community includes. To connect an agent client instead, see [MCP](MCP.md).

## Start the terminal

### On the install machine

```sh
boss metatron
```

This runs Metatron natively on the host, so colours, keys and the terminal size behave as they do for any other program. It talks to the local API with the host tools' key (`BOSS_HOST_API_KEY`, issued for you if missing), which it passes through the environment and never prints. The first run installs Rich and prompt_toolkit into `~/.local/share/boss/metatron-venv` (set `BOSS_METATRON_VENV` to put it elsewhere). If they cannot be installed, Metatron still runs, as plain text. It needs Python 3.9 or newer.

`make metatron`, from the install directory, still opens the same terminal inside the API container, with no host Python needed.

### From a checkout

Set these environment variables, then run `python3 scripts/metatron_tui.py`:

| Variable | Required | Purpose |
|---|---|---|
| `BOSS_API_URL` | Yes | API origin |
| `BOSS_API_KEY` | Yes | Your key: a personal key from **My account › API keys**, or on the install machine the host tools' `BOSS_HOST_API_KEY`. Sent in the `X-Boss-Api-Key` header; never printed |
| `BOSS_PROJECT_ID` | Yes | Initial project |
| `BOSS_ACTOR` | No | Actor the terminal sends with requests |

Examples on this page use `boss-dogfood`, the default project seeded on your machine.

## What it looks like

Metatron opens with the BOSS logo and a welcome panel. The panel shows what's new in the installed release and a connection checklist, with a one-line fix for anything marked ✗:

```text
╭──────────────────────────────────────────────────────────────────────────────────────────────────╮
│ Welcome. Type to talk to BOSS — the same        Connection                                       │
│ agent, thread and actions as the console's      ✓ API reachable   http://127.0.0.1:8000          │
│ Ask BOSS. / for commands.                       ✓ Signed in       Host tools (service account)   │
│                                                 ✓ Project         BOSS dogfood (boss-dogfood)    │
│ What's new                                      ✓ Model connected Claude Sonnet (subscription)   │
│ BOSS Community 1.0.0-rc.10.9 candidate          ✓ Embedding model Ollama nomic-embed-text        │
│ • Monitoring and alerting                       ✓ GitHub access   example-org/helpdesk           │
│ • Agents in operations                                                                           │
│ • Safety                                                                                         │
│ docs/community/RELEASE_NOTES_v1.0-rc10.9.md                                                      │
╰─────────────────────────────────────────────────────────── boss-dogfood · http://127.0.0.1:8000 ─╯
❯
 ⬡ boss-dogfood · BOSS · Claude Sonnet (subscription) · $1.84 of $25.00 today
 Enter send  Esc cancel  ↑↓ history  Tab complete  / commands  Ctrl-D quit
```

The transcript scrolls in your terminal as usual. Only the input line and the status bar under it stay at the bottom. The bar shows the project, the agent you are talking to, the chat model and today's budget.

- **While BOSS works**, the bar shows a spinner, the elapsed time and what BOSS reports doing. You can keep typing: each line you send waits its turn and runs in order. **Esc** stops waiting. It does not stop the turn on the server, and `/thread` shows the reply when it lands.
- **Type `/`** for every command, each with a one-line description. **Tab** completes commands, and also completes agent keys, project IDs and model names.
- **`/agent`, `/project` and `/model`** with no argument open a picker. Type to filter, move with ↑↓, and press Enter to choose. `/agent` lists the agents from Agent Studio. `/model` writes the chat job's model through Setup, the same call the console's picker makes. `/agent auto` and `/model auto` hand the choice back to BOSS.
- **Replies render as Markdown**, block by block, with syntax-highlighted code. A diff is shown with old and new line numbers, additions in green and removals in red. What the agent read and did shows as compact `● tool — ok` lines. `/expand` or **Ctrl-O** shows their arguments.
- **What waits on you is amber.** Offers come in a **You decide** card, numbered. Type the number, or `/pick` to choose with the arrows. A plan comes in a **Waiting for your go-ahead** card, with its phases as a checklist; `/confirm` dispatches it. If an action needs you to confirm your identity again (step-up), the card says so, and pressing **Enter** after you sign in again runs the same command again.
- **Runs and lists:** `/run ID` shows a run's steps as one progress line (`✓ spec › ● build › ○ deploy  1/3`), and `/follow ID` keeps watching it. Tasks, decisions, runs and alerts are tables.
- **Errors are red panels** that end with the fix.

A recorded session (scripted API, no model) is in [`assets/metatron/`](assets/metatron/): `metatron-demo.cast` plays in any asciinema player, and `session.txt`, `welcome.txt`, `working.txt`, `completion.txt` and `agent-picker.txt` are the same session as plain text.

### Modes

| How it runs | What you get |
|---|---|
| A terminal, Rich and prompt_toolkit installed | The interactive terminal described above |
| `--plain`, `TERM=dumb`, or Rich missing | The plain prompt (`metatron>`) and plain text |
| Output not a terminal (a pipe, a file, CI) | Plain text, unchanged from earlier releases |
| `NO_COLOR` set | The same layout with no colour and no escape sequences |
| `--print 'COMMAND'` (or `--exec`, `-p`) | One command, then exit. A spinner with the elapsed time goes to stderr only when stderr is a terminal |
| `--print 'COMMAND' --json` | The API's JSON, exactly as before. Scripts and the case-study harness rely on it |

### Slow turns

An agent turn can take minutes. Metatron waits up to 15 minutes for a turn's reply (`BOSS_METATRON_TURN_TIMEOUT`, in seconds). Other requests time out after 30 seconds. If the connection drops after the turn was sent, Metatron does not report BOSS as unreachable. It reads the conversation from the server (`/v1/agent/threads/{id}/messages`) until the reply lands, for up to 10 minutes (`BOSS_METATRON_RECOVER_SECONDS`), and then shows it, `--json` included. If the reply still has not landed, it says the turn is still running and gives the thread to check with `/thread ID`. Only a connection that never reached BOSS is reported as unreachable.

## Command reference

| Command | What it does |
|---|---|
| *(plain text)* | One Ask BOSS turn. Sent to `/v1/metatron/session`, which hands it to the same agent turn as the console's `/v1/engine/agent/chat` |
| `/pick N`, or just `N` | Runs one of the numbered choices from the last reply, through the console's own button path (`/v1/engine/agent/action`) |
| `/confirm` | Confirms the plan the last reply is waiting on (`confirm_plan_id`) |
| `/pick` | Opens the last reply's choices in a picker (interactive terminal) |
| `/expand` | Shows the arguments of the last reply's tool and action calls |
| `/thread [ID]` | Reads the conversation from the server, including a slow turn's reply |
| <code>/agent [KEY&#124;auto]</code> | Talks to one agent from Agent Studio (the turn's `role`), or back to BOSS. A new agent starts a new conversation |
| <code>/model [ENGINE[:MODEL]&#124;auto]</code> | Shows or sets the chat job's model through Setup (`PUT /v1/setup/jobs/chat`) |
| `/status` | The connection checklist: API, sign-in, project, chat model, embedding model, GitHub |
| `/runs [STATUS]`, `/run ID`, `/follow ID` | Workflow runs, one run's steps, or one run followed live |
| <code>/alerts [firing&#124;resolved&#124;all]</code> | Alerts across apps |
| `/help`, `/clear`, `/quit` | Commands and keys, clear the screen, leave |
| `/goals`, `/watch`, `/overwatch`, <code>/drive [auto&#124;go]</code>, `/go` | Captain commands. They run only when you type them |
| `/projects` | Lists projects returned by the API |
| `/project ID` | Selects a project and clears the prior Metatron conversation thread |
| `/tasks [PAGE]` | Lists 25 tasks at a time |
| `/task ID` | Shows one task |
| `/task create DEDUPE_KEY TITLE` | Creates a task |
| <code>/decisions [open&#124;all] [PAGE]</code> | Reads the decision inbox |
| `/decision ID` | Shows one decision card |
| `/decision approve ID NOTE`, `/decision reject ID NOTE` | Approves or rejects a decision with a note |
| `/ops [TERM] [--page N]` | Lists API domains or searches operations |
| `/describe OPERATION_ID` | Shows an operation's parameters and request body |
| `/op OPERATION_ID [JSON]` | Invokes an operation |
| `/api METHOD /v1/path [JSON]` | Calls a local `/v1/` route directly |
| `/coverage` | Compares the running OpenAPI with the mounted route catalog |

## Talking to the BOSS agent

Type a sentence and Metatron prints the agent's reply. Below the reply it lists what the agent did, any plan waiting for your confirmation, and the choices it offers, numbered. Nothing in that list has run yet:

```text
metatron> is anything stuck right now?
One build is waiting on a failed step: Order API, step Build (GitHub refused the push, 403)…

Choices (nothing has run yet — /pick N, or type N):
  1. Retry the Build step [retry_workflow_step] — the saved work is pushed; no rebuild
  2. Investigate further
metatron> 1
retry_workflow_step: done.
```

That is the plain text form. In the interactive terminal the same reply looks like this (from the recorded session in `assets/metatron/session.txt`):

```text
● BOSS
  One build is waiting on a failed step: HelpDesk AI, step build. GitHub refused the push (HTTP 403)
  because the App's install lost write access to example-org/helpdesk.

  The saved work is intact, so a retry pushes it without rebuilding:

              @@ -12,4 +12,4 @@ steps:
     12   12     - id: build
     13       -    push: token
          13  +    push: github_app
     14   14       retries: 2
  ● list_running_work — ok
  ● describe_workflow_run — ok
  ● test_github_access — failed  403: Resource not accessible by integration
    /expand or Ctrl-O shows the arguments
╭─ You decide ─────────────────────────────────────────────────────────────────────────────────────╮
│ 1 Retry the build step  retry_workflow_step                                                      │
│   the saved work is pushed; no rebuild                                                           │
│ 2 Investigate further                                                                            │
│                                                                                                  │
│ Nothing has run yet. Type a number, or /pick to choose with the arrows.                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
❯ 1
● retry_workflow_step — done
  Retried build. It pushed the saved work.
  ✓ spec › ● build › ○ test › ○ deploy  ▰▰▱▱▱▱▱▱ 1/4
```

When you pick an offer, Metatron sends the console's button request. It goes to `/v1/engine/agent/action` with exactly the tool and arguments you were shown, and no model reinterprets the choice. The server checks permission, consent, budget and receipts there, just as it does for the button. If an action needs you to confirm your identity again (step-up), Metatron tells you to sign in again in the console and then run the command again.

`--exec '…' --json` prints the console API's reply structure (`thread_id`, `reply`, `actions`, `pending_confirmation`, `proposed_actions`, `next_steps`, `tool_calls`, `messages`), field for field.

## Tasks

`/tasks [PAGE]` lists 25 tasks at a time, `/task ID` shows one task, and `/task create DEDUPE_KEY TITLE` creates one. Choose a stable dedupe key for each logical task and reuse it when retrying a request. The terminal sends the selected project and configured actor to the task API; the server enforces permissions and records the task and its audit event.

```text
metatron> /project boss-dogfood
metatron> /tasks
metatron> /task create launch-plan-1 Plan the community launch
metatron> /task task-123
metatron> /tasks 2
```

## Decisions

| Command | Shows |
|---|---|
| `/decisions` | Pending and delegated items |
| `/decisions all` | Every status |
| `/decisions open 2` | The next page |
| `/decision ID` | Question, risk, reason, recommendation, options, and cost summary |

Approve or reject with an explicit note:

```text
metatron> /decisions
metatron> /decision dec-launch-1
metatron> /decision approve dec-launch-1 Release gates reviewed
metatron> /decision reject dec-launch-2 Keep the spending guardrail
```

These commands use the standard decision API. The terminal includes the selected project on inbox and detail reads, and checks the detail in that project before posting an action. The API binds the action to the authenticated reviewer and enforces role, risk, confirmation, and transition rules.

!!! note
    A service key that requires a separate human confirmation still receives an HTTP 409; the terminal cannot grant that confirmation.

Use `--exec '/decision ID' --json` for the full decision card. Other decision transitions and fields are available through `/op`.

## Discover and invoke any operation

| Command | Use |
|---|---|
| `/ops` | Product domains exposed by the running API |
| `/ops TASK` | Search operations |
| `/ops * --page N` | Browse every operation in pages of 25; domain searches also accept `--page N` |
| `/describe OPERATION_ID` | Inspect parameters and request body |
| `/op OPERATION_ID [JSON]` | Invoke the operation |

Interactive output shows concise rows; one-shot `--json` returns the same page with counts and stable operation IDs.

Operation IDs are derived from the HTTP method and path, so they stay unique even when the API framework assigns duplicate IDs to a multi-method route, and they match the generated Community OpenAPI contract. The terminal fetches the live `/openapi.json` and capability catalog on each run rather than relying on a bundled route list.

### `/op` arguments

The JSON argument has three optional keys: `path`, `query`, and `body`. `path` and `query` are JSON objects.

- The terminal checks path placeholders and declared query names before sending a request.
- If an operation declares a `project_id` query parameter, the active project is inserted when omitted.
- If a typed JSON request body declares `project_id` and your body omits it, the selected project is inserted. An explicit `project_id` is preserved, and the API authorizes access to that project.
- All other required body fields must be supplied. `/describe` lists typed fields, choices, and nested required keys where the API schema provides them.

The API checks access and policy on every call.

```text
metatron> /ops
metatron> /projects
metatron> /ops tasks
metatron> /ops * --page 2
metatron> /describe get_v1_tasks_339c8c60
metatron> /op get_v1_tasks_339c8c60
metatron> /op post_v1_tasks_54d40d0b {"body":{"title":"Plan launch","dedupe_key":"launch-plan-1"}}
metatron> /project another-project
```

### Direct routes with `/api`

For a catalog-only route or direct path control, use `/api METHOD /v1/path [JSON]`. The method may be `GET`, `POST`, `PUT`, `PATCH`, or `DELETE`; the optional JSON body applies to non-GET requests. The path must be a local `/v1/` route.

!!! warning
    Unlike `/op`, `/api` does not infer `project_id`. Include it wherever the route requires it.

```text
metatron> /api GET /v1/tasks?project_id=boss-dogfood
metatron> /api POST /v1/metatron/watch {"project_id":"boss-dogfood","actor":"executive_console"}
```

## Scripting with one-shot mode

Run one command and receive one JSON result:

```sh
boss metatron --print 'is anything stuck right now?' --json
python3 scripts/metatron_tui.py --exec '/api GET /v1/tasks?project_id=boss-dogfood' --json
python3 scripts/metatron_tui.py --exec '/op get_v1_tasks_339c8c60' --json
python3 scripts/metatron_tui.py --exec '/coverage' --json > tui-coverage.json
```

| Exit code | Meaning |
|---|---|
| `0` | Successful HTTP response |
| `1` | HTTP or connection failure |
| `2` | Invalid local input or missing configuration |

HTTP failures return a JSON object with `__http_error__`, `status`, and `body`.

`/coverage` compares the running `/openapi.json` with the mounted route catalog. Its JSON output includes each route's stable operation ID (if in OpenAPI), terminal addressability, raw `/api` addressability, and an explicit `unverified_server_enforced` access state.

!!! note "Discovery is not authorization"
    Discovery does not prove the caller has permission or that a paid edition feature is enabled. The API remains the authority for edition, permissions, policies, approvals, budget gates, receipts, and audit records; the terminal has no separate execution path.

## Credential handling

If an API response echoes a submitted credential, the terminal redacts values from nested secret, token, password, API-key, and credential fields (including `value` on credential routes) before showing or returning the response.

!!! warning
    Never paste API keys into an issue. Report vulnerabilities by email only, as described in [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md).

## Engine tooling

In the stock Community install, run the engine's Python module inside the API container. From the install directory:

```sh
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T -u boss api python -m boss_engine --project boss-dogfood tools
```

Use the same `python -m boss_engine` entry point for `call`, `code`, and `serve-mcp`. The Community API image does not install a `boss-engine` executable. If your user needs sudo for Docker, run these commands through a working Docker permission path. [MCP](MCP.md) shows the stdio command; its optional HTTP transport is read-only by default.

## Next

- [Terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md) for command examples by workflow.
- [Guide index](README.md) for install, models, feedback, and security.
- [Console access](CONSOLE_AUTH.md) if you want the browser instead of this terminal.
- [Control surfaces](CONTROL_SURFACES.md) for how the API, this terminal, and MCP line up.
