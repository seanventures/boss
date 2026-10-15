# The engine contract and the coupling audit

BOSS is a harness independent of any model. Its **behaviour** (stages, gates, fix rounds, the PR,
events, the build profile, failure words) must be identical on every engine; only **quality and
speed** may vary. Audited 2026-10-13 on `release/rc10.5`, plus the assumptions `integrate/premortem`
adds. G = `engine/boss_engine/services/governed_code_task_service.py`.

## The contract (`services/engine_contract.py`)

```
run_role(prompt, cwd, *, read_only, model, resume_session_id, max_turns, parallel_agents)
  -> {summary, usage{model, prompt/completion/cache tokens, cost}, cost_usd_micros,
      cost_unreported, turns, tool_calls, model, session_id, stop_reason, engine}
capabilities: EngineCapabilities(resume, read_only, effort, sub_agents, turn_limit,
                                 reports_cost, streams_turns)
```

`make_code_agent` wraps every engine in `ContractedAgent`. BOSS-side fallbacks:

| Missing capability | Fallback |
|---|---|
| resume | a BOSS session id (`boss-sess-…`) for every session; continuing it starts a new session seeded with what the chain was first asked and what it reported (`seeded_prompt`) |
| read_only | the reviewer runs on a throwaway copy of the tree; nothing reaches the build |
| effort | per-engine mapping where one exists (Codex `model_reasoning_effort`), else none |
| sub_agents | none: one session (BOSS's own split, `parallel_build`, is engine-independent) |
| turn_limit | the stage's wall-time deadline still bounds the session |
| reports_cost | `cost_unreported: true`, never an invented number |
| streams_turns | a prompt row and a completion row per session |

Always, for every engine: tool names normalised to BOSS's vocabulary at the one transcript writer
(`agent_turns.record_turns`); a turn cap carries the one partial-attempt phrase the pipeline reads;
a failed session raises; an engine's name is never passed back as a model; the working rules and
sandbox facts reach every writing session.

## Audit

B = behaviour coupling (must fix), Q = quality/speed only, R = reporting/profile. Engines: N =
NativeCodeAgent via OpenRouter/Groq, C = Codex, A = Aider.

| # | Coupling | Where | Class | What happened on N / C / A | Fixed? |
|---|---|---|---|---|---|
| 1 | Warm fix round, implement continuation, test-budget trim, install loop need `session_id` | G:5137, 5218-5283, 5416, 5460-5486, 6659-6666; premortem `_can_warm_fix` (G:5791) + 360 s `WARM_FIX_RESERVE_SECONDS` | B | N/C/A: no session id: trim skipped, continuation skipped, install loop never asks, fix rounds cold with a context-free prompt; premortem: warm-fix slice reserved but never usable, stage fails `budget_exhausted` | **Yes**: contract issues a session for every engine + seeded fallback; Codex resumes natively (`codex exec resume`) |
| 2 | Turn cap recognised only in Claude's words | G:156-160 `_INCONCLUSIVE_PHRASES`; code_agent native cap text | B | N: capped implement judged as finished, no continuation | **Yes**: `PARTIAL_MARKER` from every engine (`stop_reason`) |
| 3 | Transcript only from Claude | code_agent `record_agent_turns` | R/B (profile, live view, cockpit) | N/C/A: no agent_turns; profile empty, `think_share`≈1.0 on N | **Yes**: native per-turn rows, Codex JSONL → turns live, generic CLI prompt+completion |
| 4 | Tool names hard-coded to Claude's | agent_turns.py:49,53; build_profile.py:46,239,250; build_detail.py:36 | R | N `write_file/edit_file/bash/read_file`, C `apply_patch/shell` invisible | **Yes**: normalised at `record_turns` (+ arg keys `path→file_path`, `cmd→command`) |
| 5 | `max_turns` honoured only by Claude | G:4910, 6963-6979 (escalated re-check) | B | N: ignored, escalation re-runs identically | **Yes** for N; C/A have no turn flag (deadline bounds them) |
| 6 | Stage deadline only bounds Claude | code_agent `_cap_to_deadline` | B | N/C/A run past the stage deadline / into the premortem warm-fix reserve | **Yes**: N checks per turn, CLI timeouts capped |
| 7 | Reviewer read-only mode | cli_harness aider/cursor `read_only_args=()`; G:5797-5850, 6571, 6749-6771, 4755 design | B | A: every seat raises `CliHarnessUnavailable` → `not_run`, `security_clean` fails; design stage fails the run | **Yes**: copy-of-tree fallback |
| 8 | A failed generic CLI session passed on as an answer | cli_harness `fail_on_nonzero=False` | B | A/cursor/codex: an exit 1's stderr judged as the agent's work | **Yes**: contract raises on `stop_reason=error`; Codex adapter raises |
| 9 | Harness name returned as the model | cli_harness `model or spec.name` → G:5360 (premortem QA static fix passes it back) | B | C: `--model codex` breaks the turn | **Yes** |
| 10 | `PacedCodeAgent` kept attribute writes on itself | code_agent PacedCodeAgent; G:3423 `a.redact=True`, `a.command_runner=` | B (data boundary!) | every engine: restricted-task PII redaction never applied; N `bash` ran outside the sandbox runner | **Yes**: writes forwarded |
| 11 | Working rules + sandbox facts only in Claude's prompt | code_agent.py:1111 | Q (affects pass rate) | N/C/A built without "no C compiler", "clean npm install" facts | **Yes**: contract appends them |
| 12 | `--effort` / MAX_THINKING_TOKENS | code_agent `stage_effort`, `thinking_token_cap` | Q | event `effort_chosen` recorded but not applied | Codex: mapped to `model_reasoning_effort`; N/A: none (plan: map to `reasoning_effort` for o-series/DeepSeek-R models) |
| 13 | Sub-agents (`Agent`/Task tool, 4756ce68) | code_agent `sub_agent_tools`, G:5431,5448 | Q | ignored; one session | contract zeroes `parallel_agents` unless capable |
| 14 | Shared prompt prefix / CLI caching (096e73e7) | code_agent `split_shared_context`, `--append-system-prompt` | Q | N/C: context stays at the head of the prompt (provider prefix cache) | no change needed |
| 15 | Billing: `UNMETERED={claude_code,codex,ollama,vllm}` | model_routing.py:566; llm_telemetry.py:180 | B (cost caps) | the ledger already follows the adapter's `billing` stamp first: claude_code and claude_agent_sdk stamp `metered` on an API key (claude_credential), Codex by its login kind; only the pre-call reservation (`turn_estimate_usd_micros`) still reads the engine name | Ledger **yes**; reservation: **plan** — decide it from the source's entitlement kind |
| 16 | MicroVM guest engine mapping by name | G:2905-2939 | — | micro-VM execution is frozen for the community release (`BOSS_MICROVM_CLAIMS_FROZEN=true`) and BOSS deploys to VMs as containers | **Dropped** with BOSSAPP-516 |
| 17 | Chat adapter by engine name, `CHAT_ROUTABLE` | model_routing.py:66, 385-407 | B (chat) | grok/cursor/aider fall to platform OpenAIAdapter | plan: chat routes only to chat-capable engines, refuse in words otherwise |
| 18 | Smart-mode failover drops the session | model_router/failover.py:169 | B | context-free resume prompts run cold on the new source | covered by #1 (seeded session) once failover passes the BOSS session id; plan: keep the id |
| 19 | Stray-reviewer reaper matches only `claude` | G:308-360, 5980 | Q | N seat threads leak past the panel deadline | plan: cancellable native loop (deadline flag) |
| 20 | `OFFICIAL_ENGINES={openai,native,claude_code}` | code_task_service.py:1074, spend_cap_failover.py:18 | B (latent) | other engines refused in `start_followup_task(engine=)` | plan: ask the registry |
| 21 | Discovery hard-locked to claude_code | discovery_service.py:268, 798 | B (outside build) | fails | plan: research via any engine with web tools |
| 22 | `routing-row.tsx` HARD_JOBS build/review/chat | apps/web/app/setup/routing-row.tsx:378 | none | engine-agnostic | — |
| 23 | SQL `to_jsonb(:s::text)` in `_mark_stage_done` (found by the scripted engine) | G:4082; apps/api/app/main.py:8438 | B (all engines) | completed stages never recorded; redelivery route 500 | **Yes** + enforcing test |

## Since the audit (merged onto main, 2026-10-15)

- `main` gained two safeguards after this audit was written: the model-key proxy
  (`model_proxy.scoped`, BOSSAPP key proxy) and fail-fast on an engine that cannot run
  (`harness_failures`). Both live in `GenericCliCodeAgent._preflight` / `_raise_harness_verdict`,
  which every CLI driver calls — `CodexCodeAgent` included, so a purpose-written driver cannot
  quietly skip them (`engine/tests/test_the_codex_driver_keeps_every_cli_safeguard.py`).
- Codex builds its environment once per session: each `_env()` issues a proxy run token, and the
  `billing` stamp reads the environment the child actually ran with.
- A Codex session Codex no longer has is continued as a new session seeded from the ledger
  (`seeded_prompt`), never with a cold prompt.
- `claude_agent_sdk` (BOSSAPP-537) subclasses `ClaudeCodeAgent`, so it keeps Claude Code's
  capabilities and adds the working rules itself.
- The engines gate runs on the `balanced` profile at tier 1: with no project default the community
  edition picks `lean`, which seats no reviewer, so there was nothing to compare.

## Conformance

- `engine/tests/test_every_engine_keeps_the_same_contract.py`: one set of assertions over
  claude_code, codex, aider, openrouter (native) and scripted, with stubbed I/O.
- `scripts/e2e-gate.sh engines` (`tests/e2e/gate/engines.py`): the real governed pipeline end to end
  on the scripted engine (poorest vs richest capabilities) over clean / failing_test / blocker /
  crash_on_start / engine_crash; P0 release step.

## A live per-engine matrix needs

claude_code: a subscription token or ANTHROPIC_API_KEY. codex: `CODEX_AUTH_JSON` (ChatGPT login,
`openai_codex_chatgpt`), and one recorded `codex exec --json` stream to replace the documented-schema
fixture. OpenRouter/Groq: `OPENROUTER_API_KEY`/`GROQ_API_KEY` and a tool-calling model
(e.g. `qwen/qwen3-coder`, `deepseek/deepseek-chat`). aider: `aider` installed plus a provider key.
Run `scripts/e2e-gate.sh agent --brief <b>` with `model_config.default=<engine>` per engine and
compare with the same `engines.py` expectations.
