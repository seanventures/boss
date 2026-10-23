# One agent pipeline

Plane BOSSAPP-759. Operator decisions, 2026-10-23:

- "Metatron is just the CLI that interacts with the core BOSS agents… if two have different code path for AI agents then we have to make duplicate fixes every time — avoid that."
- "Why do we even have the captain autopilot? Retire it. Put its functionality into the BOSS agent. There must be ONE agent pipeline, and all agents go through it. Some agents may have extra skills (customised by the user), but when BOSS ships, all agent communication goes through the same code path: console, API, CLI, and agent-to-agent too."
- "ALL BOSS default (shipped) agents have the SAME expertise and the SAME full action and tool access through the one pipeline. They differ only by skills and MCP servers an operator assigns in the console, or when a user customizes an agent."

## The pipeline

| Part | Where | What it does |
|---|---|---|
| The turn | `apps/api/app/main.py:agent_turn` | Every conversational HTTP surface sends a turn here. It handles rate limiting, the project check and error wording, then calls `AgentService.chat` |
| The conversation | `engine/boss_engine/services/agent_service.py:AgentService.chat` | Thread model (`agent_threads`, `agent_messages`), signed acts, the pending plan |
| The decision | `engine/boss_engine/services/intent_router.py:IntentRouter.route_turn` → `agent_decision.decide` | A model picks the turn's action from the action catalogue. Words are never a rule |
| A domain agent's turn | `agent_service.py:AgentService.decide` → `IntentRouter.decide_among` → `agent_decision.decide` | The same decision, for an agent whose actions are its domain's (an alert, a reply, an issue, a root cause). The caller passes the candidates, the record and its pattern guesses as hints. The turn is kept on the record's own thread (surface `agent`, `external_ref` = the record), whose earlier turns are the decision's history. What the chosen action does stays the caller's, under its own governance |
| The answer | `engine/boss_engine/agent/engine_agent.py:EngineAgent`, with `tools.build_discuss_read_registry` | The tool loop. Reads run, and writes run or are offered according to `ToolSpec.self_executable` |
| The press | `apps/api/app/main.py:agent_action` (`POST /v1/engine/agent/action`) | Runs exactly the offer the person accepted, with no model in between. It checks the tool's permission, the identity gate and consent, and records the outcome in the transcript |

Governance is the same for every caller: permission and identity gate, consent (`propose_action` and `operator_only`), step-up, budget, receipts and Passport ceilings.

## Entry points

"Before" means the entry point had its own decision path. "Now" names the path it takes today.

| Entry point | file:function | Before | Now |
|---|---|---|---|
| Console Ask BOSS | `main.py:engine_agent_chat` (+ deprecated `agent_chat`) | the pipeline | `agent_turn` (unchanged) |
| Metatron, plain text | `routers/metatron_captain.py:captain_session_turn` | **own path**: `MetatronCaptain.session_turn`, `interpret_turn` keywords, `decide_free_text` | A thin adapter onto `agent_turn`. It takes the console's request and returns the console's reply. The old decision code is deleted |
| Metatron, numbered choice | `scripts/metatron_tui.py:MetatronClient.pick` | — (new) | `POST /v1/engine/agent/action`, the console's button path |
| Metatron `/watch` `/overwatch` `/drive` `/go`, theater, goals | `routers/metatron_captain.py:_press` | **own engine**: `MetatronCaptain.watch_once/overwatch_once/drive/command_once` called directly | Each command presses the BOSS agent's own action (`captain_*` in `tools/captain_tools.py`) through `agent_action` |
| Captain autopilot (worker) | `apps/worker/boss_worker/main.py:_tick_metatron_watch` | **own loop**: an always-on overwatch that applied moves | **Retired.** The worker runs `captain_watch` on a schedule through the press path. It only observes and scores goals. Applying moves happens only when a person asks |
| Captain powers inside chat | `tools/captain_tools.py` | not reachable from chat | Agent actions. The agent may run `captain_watch`, `captain_goals` and `captain_set_goal` itself. It only offers `captain_drive`, `captain_overwatch` and `captain_theater` (`operator_only`), so plain text never runs them |
| Scheduled agent wakeups | `services/agent_wakeups.py:run_due` | the pipeline | `AgentService.chat` (unchanged) |
| Router door without a thread | `main.py:intent_route` (`POST /v1/intent/route`) | the router | `route_turn` itself, with the same decision code and no thread. No duplicate fix is possible |
| MCP tools | `engine/boss_engine/mcp/server.py:_tools_call` | registry dispatch | The external client agent decides. BOSS dispatches registry tools under the same `invoke` gates, including `operator_only` and `confirmation_required` |
| Engine CLI `ask` / `chat` | `engine/boss_engine/__main__.py:_cmd_ask/_cmd_chat` | the pipeline's tool loop (`EngineAgent`) without `route_turn` or a thread | With a database, `AgentService.chat` (`_pipeline_turn`). `chat` presses an offer you answer `y` to, as shown, and records it in the thread. Without a database there is no thread or decision step, so it falls back to `EngineAgent`, the same loop the router answers with |
| Workflow agent step | `services/agent_steps.py:run` → `agent_loop.run_agent_loop` | **own path**: routing prompt, then its own tool loop with `WRITE_TOOLS` | **The decision moved; the execution is a worker.** Which agent runs a step that names none is `AgentService.decide` (`choose_agent`), with the pool (what each agent is for and holds) as the choices. The routing prompt and the word-overlap fallback are gone: with no model the step says to name its agent or connect one. `agent_loop` then carries out the instruction with only that agent's granted skills and servers. It is worker execution inside a step, and the runner decides what the run does next |
| Step recovery | `services/step_recovery.py:decide` / `ask_model` | **own path**: its own prompt and multi-turn tool loop over `ACTIONS` | **Left, with a reason** (see "Not moved yet"): its decision carries per-action arguments, follows a read-tool phase and is checked against the evidence it cites, which the pipeline's decision cannot express yet |
| Inbox / Slack / email replies | `services/comms.py:CommsService.decide_reply` | **own decision**: `agent_decision.decide` over approve/reject/revise/… | `AgentService.decide`: a turn by BOSS's inbox agent on the thread `decision:<id>`, with the decision (its question and options) as the record and the first decision word as a hint. Below the bar, or with no model, nothing is transitioned (unchanged) |
| GitHub issue triage | `services/github_triage_agent.py:GitHubTriageAgent._decide` | **own decision**: `agent_decision.decide` | `AgentService.decide`: a turn by the GitHub triage agent on the thread `github-issue:<repo>#<n>`, with the keyword triage as a hint. Unsure or no model: maintainer review (unchanged). Its keyword hint reads the pipeline's own router (`classify_message`) |
| Alert triage, flap tuning (Beacon) | `services/app_alerts.py:triage_alert`, `propose_flap_tuning` | **own decision**: `agent_decision.decide` | `AgentService.decide`: a turn by the on-call agent (`sre`) on its incident's thread (`incident:<id>`, the one the verdict is decided on), else the alert's (`alert:<id>`). Candidates, context, the person's answer path (`answer_triage`) and the outcomes are unchanged. A flap tuning is still only proposed |
| Incident verdict | `services/investigation.py:model_verdict` | **own decision**: `agent_decision.decide` over hypotheses. It records a verdict and acts on nothing | `AgentService.decide`: a turn by the on-call agent (`sre`) on the thread `incident:<id>`, with the hypotheses as choices and the signals and scores as the record. It still records a verdict and acts on nothing |
| Agent-to-agent talk, plan, room | `services/agent_converse.py:take_turn/propose_start` (model through `conversation_spend.py`) | **own path**: persona prompt and its own model call. Plan and room choose a workflow, which a person presses | **Left, with a reason** (see "Not moved yet"): every seat's reply is one streamed call under the conversation's own spend envelope, which the pipeline's turn cannot carry yet |
| Focused agents | `services/focused_agents.py:tick_one` | a deterministic duty loop over the captain's validated moves. It makes no model decision | `AgentService.decide` on the thread `focused_agent:<id>`: each tick the agent chooses which ONE of the playbook's validated moves to apply, or none, with its duty, the snapshot and the moves as input. The playbook still proposes, `validate_move` still checks, and decisions and dead letters still never apply. No model, or unsure: nothing is applied and the tick says why. One move per tick (it was every validated one). Not `AgentService.chat`: a tick runs unattended every few minutes, and its choices must be exactly the moves BOSS validated, not whatever a full tool loop would write |
| Self-fix (Mender) dispatch | `workflow_capability_runner.py:_boss_side_wait` → `self_fix.propose` | triggered by step recovery's `fix_boss`. Confidence is deterministic | Follows step recovery |
| Debug-session investigation | `services/debug_session.py` | a coding engine with `boss-do`. It can run registry write tools | A worker. Its writes go through `registry.invoke`. Revisit with the workflow agent step |

### Workers, and where the boundary is

A worker calls a model to produce an artifact, such as code, a PRD, a review, an edit plan or a drafted workflow. It doesn't choose what BOSS does next. Workers stay outside the pipeline, and so do the coding engines inside a build step (`code_agent.py`, the CLI harnesses), panel seats and PRD synthesis (`meeting_service.py`), the PR reviewers, `meeting_actions.py`, `prd_decomposition.py`, `video_planner.py`, `workflow_drafting.py`, `typed_decisions.py` (it answers a workflow author's own question), the read-only pre-check, `sre_ask.py` (it plans a query and never executes one), the factory plan artifact and `agent_loop.py`, which carries out a workflow agent step's instruction with only its agent's granted skills (each call re-checked by `SkillRuntimeService.invoke`, tier-3 skills still suspending the run). The boundary is choosing a BOSS action. Any module that picks which BOSS action to take, offer or skip belongs in the pipeline.

`engine/tests/test_one_agent_pipeline.py` enforces this. Any module that imports the decision model, builds or calls the router, calls a model or runs its own tool loop must appear in its `ALLOWED` list with a reason. Each entry not moved yet is marked `MIGRATE` there. The list can only shrink: an entry that stops deciding on its own fails the test until someone removes it. Metatron's router, the captain service, the captain tools, `focused_agents.py` and `metatron_command.py` may never decide on their own.

### Intentional differences

- **Typed captain commands skip the model.** `/overwatch` is the person pressing the agent's `captain_overwatch` action, like a console button. A press is never re-decided.
- **The overwatch playbook stays deterministic.** Inside `captain_overwatch` and `captain_drive`, `validate_move`, the budget and duplicate checks and "decisions and dead letters never apply" still run as code. That logic is how the tool does its job. It doesn't choose what BOSS does next: the agent or the person chooses whether to run the tool.

## Same base for every shipped agent

Every shipped agent holds the same baseline skills (`agent_baseline.BASELINE_SKILLS`): `boss.fundamentals`, `repo.read`, `boss.propose_fix`, `knowledge.read` and `knowledge.propose`. Every agent also gets the same chat registry, whoever is asked. The role changes the lens and the history, not the reach.

- **Removed:** the six-role `kg_propose_fact` grant (`PROPOSE_FACT_ROLES` and the `knowledge.propose` entries in six role specs). `knowledge.propose` is in the baseline. What it writes is always `inferred`, the same governance for every agent.
- **Kept:** each role's specialist skills in its spec's `allowed_skills`. These are data that `sync_role_skills` writes into `role_skills`, and an operator can see and change them in the console like any other grant. Governance is unchanged: approvals, step-up, budget, receipts, Passport ceilings and consent.
- **Enforced:** `engine/tests/test_every_shipped_agent_has_the_same_base.py`. No role list and no comparison against a shipped role key may appear in the pipeline's code. The base is one constant, not a function of the role. No role spec may forbid a baseline skill. The chat registry takes no role.

## The migration, in risk order

1. Incident verdict (`investigation.py`): **moved** (`AgentService.decide`).
2. GitHub issue triage and inbox replies (`github_triage_agent.py`, `comms.py`): **moved**.
3. Alert triage and flap tuning (`app_alerts.py`): **moved**, keyed to the incident.
4. Agent-to-agent talk, plan and room (`agent_converse.py`, `conversation_spend.py`): **not moved**. See "Not moved yet".
5. Focused agents (`focused_agents.py`): **moved** (`AgentService.decide` over the validated moves, not `chat`; see the table).
6. Step recovery and the workflow agent step (`step_recovery.py`, `agent_steps.py`, `agent_loop.py`): the agent step's choice of agent is **moved**, and its loop is a worker. Step recovery is **not moved**. See "Not moved yet".

Each step moves its module out of `MIGRATE` in `test_one_agent_pipeline.py` and runs the battle set's scenarios for that surface. A moved module is listed in that test's `MIGRATED`, which checks that it calls `AgentService.decide` and nothing on the decision signs.

## Not moved yet

These stay on the allow-list as `MIGRATE`, each with its reason there. Moving them needs a change to the pipeline itself, not to the entry point.

### Agent-to-agent talk, plan and room

**Why it cannot move as it is.** A talk, a plan and a room are conversations between a person and one to five seated agents. Each seat's reply is one model call, streamed token by token to the console, under the conversation's own spend envelope (`ConversationCallSpend`): one pinned provider attempt, a reservation and a model lease held across the live stream, a settled receipt, and the floor claim that keeps two streams from speaking at once. The pipeline's turn (`AgentService.chat` → `route_turn` → `EngineAgent`) answers in one piece and governs its own calls, so it has no way to stream a seat's reply or to run under a caller's envelope. Moving the seats onto it today would drop streaming and the conversation's settlement, which the console and `test_a_talk_streams_as_the_model_writes` rely on. The plan's start card and the room's summary are one structured call inside the same envelope, so they cannot move separately without splitting one governed call into two.

**Design note.**

1. Give the pipeline's answer a streaming form: `EngineAgent` yields tokens through the adapter's `stream_text_response`, and `route_turn` takes an optional spend envelope that its model calls run under, in place of its own governance.
2. A seat then speaks as `AgentService.chat(role=<seat>, mode="discuss", external_ref="conversation:<id>")`, a role-addressed turn on the conversation's thread. `discuss` already writes nothing, which is the talk's rule (`TALK_CANNOT_ACT`). Round two, one agent answering another, is the same turn with the earlier seat's reply as the message. That is agent-to-agent communication on the one pipeline.
3. The plan's start card becomes `AgentService.decide` over the catalogue's workflows, with the transcript as the record. The card's directive, inputs and acceptance are then a worker's draft for the person to press, as they are today.
4. Move it when `test_a_talk_streams_as_the_model_writes`, `test_a_talk_never_claims_an_action` and the talking-with-agents API tests pass on the pipeline.

### Step recovery

**Why it cannot move as it is.** A failed step's recovery agent runs unattended inside a build, and its decision is more than a choice. The model (1) reads more evidence with BOSS's read tools for up to `MAX_TURNS` turns, inside the step's recovery budget; (2) answers with the action and that action's arguments: a precise note for `fix_app`, the projects to take down for `clean_and_retry`, a `boss_fix` with an area and reasons for `wait_for_boss` and `fix_boss`, one question and what happens after for `ask_person`, and a backoff for `retry`; and (3) is checked by `validate` before anything runs. The check covers the step kind's allowed actions, the same action for the same cause twice, projects that are not this app's own, and cited evidence that appears nowhere in what the agent was shown. A refused answer gets one re-ask. The pipeline's decision (`agent_decision.decide`) returns only `{action, confidence, reason, question}`, has no tool phase and no caller check with a re-ask. Choosing the action through it and the arguments in a second call would split one governed, validated decision into two unvalidated halves, in the one place that runs without a person watching.

**Design note.**

1. Give `Choice` an optional argument schema, and give `agent_decision.decide` an optional `check` (the caller's validator, which returns the refusal reason) with one re-ask on refusal. The JSON contract gains `"arguments": {…}` for the chosen action.
2. Give `IntentRouter.decide_among` an optional read phase: the read-only registry every agent holds (`build_discuss_read_registry`, as the answer path uses), bounded in turns and cost by the caller. Its tool results join the corpus that `check` cites against.
3. `step_recovery.decide` then calls `AgentService.decide(role=<the step's agent>, external_ref="step:<run>:<step>")`, with `ACTIONS` (narrowed to `KINDS[kind]`) as the choices, the evidence pack as the context, the coded triage as a hint and `validate` as the check. Its budget window, `no_model_ask`, `unsure_ask` and execution stay as they are.
4. Move it when `test_a_failed_step_is_recovered_by_its_agent`, the recovery API tests (`test_a_failed_build_recovers_on_its_own`, `test_a_failed_deploy_recovers_on_its_own`, `test_a_deploy_that_fails_for_the_app_is_fixed_and_redeployed`) and the battle set's `step_recovery` scenarios pass on the pipeline.
