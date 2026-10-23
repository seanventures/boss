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
| Workflow agent step | `services/agent_steps.py:run` → `agent_loop.run_agent_loop` | **own path**: routing prompt, then its own tool loop with `WRITE_TOOLS` | **Left to do** |
| Step recovery | `services/step_recovery.py:decide` / `ask_model` | **own path**: its own prompt and multi-turn tool loop over `ACTIONS` | **Left to do** |
| Inbox / Slack / email replies | `services/comms.py:CommsService.decide_reply` | **own decision**: `agent_decision.decide` over approve/reject/revise/… | **Left to do** |
| GitHub issue triage | `services/github_triage_agent.py:GitHubTriageAgent._decide` | **own decision**: `agent_decision.decide` | **Left to do** |
| Alert triage, flap tuning (Beacon) | `services/app_alerts.py:triage_alert`, `propose_flap_tuning` | **own decision**: `agent_decision.decide` | **Left to do** |
| Incident verdict | `services/investigation.py:model_verdict` | **own decision**: `agent_decision.decide` over hypotheses. It records a verdict and acts on nothing | `AgentService.decide`: a turn by the on-call agent (`sre`) on the thread `incident:<id>`, with the hypotheses as choices and the signals and scores as the record. It still records a verdict and acts on nothing |
| Agent-to-agent talk, plan, room | `services/agent_converse.py:take_turn/propose_start` (model through `conversation_spend.py`) | **own path**: persona prompt and its own model call. Plan and room choose a workflow, which a person presses | **Left to do** |
| Focused agents | `services/focused_agents.py:tick_one` | a deterministic duty loop over the captain's validated moves. It makes no model decision | **Left to do**: run as scheduled agent wakeups through `AgentService.chat` |
| Self-fix (Mender) dispatch | `workflow_capability_runner.py:_boss_side_wait` → `self_fix.propose` | triggered by step recovery's `fix_boss`. Confidence is deterministic | Follows step recovery |
| Debug-session investigation | `services/debug_session.py` | a coding engine with `boss-do`. It can run registry write tools | A worker. Its writes go through `registry.invoke`. Revisit with the workflow agent step |

### Workers, and where the boundary is

A worker calls a model to produce an artifact, such as code, a PRD, a review, an edit plan or a drafted workflow. It doesn't choose what BOSS does next. Workers stay outside the pipeline, and so do the coding engines inside a build step (`code_agent.py`, the CLI harnesses), panel seats and PRD synthesis (`meeting_service.py`), the PR reviewers, `meeting_actions.py`, `prd_decomposition.py`, `video_planner.py`, `workflow_drafting.py`, `typed_decisions.py` (it answers a workflow author's own question), the read-only pre-check, `sre_ask.py` (it plans a query and never executes one) and the factory plan artifact. The boundary is choosing a BOSS action. Any module that picks which BOSS action to take, offer or skip belongs in the pipeline.

`engine/tests/test_one_agent_pipeline.py` enforces this. Any module that imports the decision model, builds or calls the router, calls a model or runs its own tool loop must appear in its `ALLOWED` list with a reason. Each "left to do" entry above is marked `MIGRATE` there. The list can only shrink: an entry that stops deciding on its own fails the test until someone removes it. Metatron's router, the captain service, the captain tools, `focused_agents.py` and `metatron_command.py` may never decide on their own.

### Intentional differences

- **Typed captain commands skip the model.** `/overwatch` is the person pressing the agent's `captain_overwatch` action, like a console button. A press is never re-decided.
- **The overwatch playbook stays deterministic.** Inside `captain_overwatch` and `captain_drive`, `validate_move`, the budget and duplicate checks and "decisions and dead letters never apply" still run as code. That logic is how the tool does its job. It doesn't choose what BOSS does next: the agent or the person chooses whether to run the tool.

## Same base for every shipped agent

Every shipped agent holds the same baseline skills (`agent_baseline.BASELINE_SKILLS`): `boss.fundamentals`, `repo.read`, `boss.propose_fix`, `knowledge.read` and `knowledge.propose`. Every agent also gets the same chat registry, whoever is asked. The role changes the lens and the history, not the reach.

- **Removed:** the six-role `kg_propose_fact` grant (`PROPOSE_FACT_ROLES` and the `knowledge.propose` entries in six role specs). `knowledge.propose` is in the baseline. What it writes is always `inferred`, the same governance for every agent.
- **Kept:** each role's specialist skills in its spec's `allowed_skills`. These are data that `sync_role_skills` writes into `role_skills`, and an operator can see and change them in the console like any other grant. Governance is unchanged: approvals, step-up, budget, receipts, Passport ceilings and consent.
- **Enforced:** `engine/tests/test_every_shipped_agent_has_the_same_base.py`. No role list and no comparison against a shipped role key may appear in the pipeline's code. The base is one constant, not a function of the role. No role spec may forbid a baseline skill. The chat registry takes no role.

## Left to do, in risk order

1. Incident verdict (`investigation.py`): ask through the pipeline with the hypotheses as the record.
2. GitHub issue triage and inbox replies (`github_triage_agent.py`, `comms.py`): a turn on the issue's or decision's thread, with its verdict offered as actions.
3. Alert triage and flap tuning (`app_alerts.py`): the same, keyed to the incident.
4. Agent-to-agent talk, plan and room (`agent_converse.py`, `conversation_spend.py`): a role-addressed turn (`role=`) on the conversation's thread.
5. Focused agents: scheduled wakeups through `AgentService.chat`, with the duty as the message.
6. Step recovery and the workflow agent step (`step_recovery.py`, `agent_steps.py`, `agent_loop.py`): the riskiest move, because these run unattended inside builds. Move them onto `route_turn` with a step-scoped thread (`external_ref` = the step) once the battle set's `step_recovery` scenarios pass on the pipeline.

Each step moves its module out of `MIGRATE` in `test_one_agent_pipeline.py` and runs the battle set's scenarios for that surface.
