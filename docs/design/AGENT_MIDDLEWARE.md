# Agent middleware: the six-hook contract over the kernel

Status: contract for Phase 1 (BOSSAPP-1051 P0 deliverable 5; implemented by P1b). Source design:
`docs/design/AGENT_PLATFORM.md` §1.1 and §4.2. Line references are to `engine/boss_engine/kernel/`
on main at the time of writing.

## Why

The kernel (`loop.py`) is already one loop driven by five policy protocols (`policies.py:26-76`).
Those protocols are internal and each answers one question. The middleware contract is the public
extension surface: a stable, ordered list of hooks that every preset composes, so a cross-cutting
concern (token minting, budget, memory, authz, audit, learning writeback) is written once and
reaches every caller. Policies stay; the default middleware stack calls them.

## The six hooks

| Hook | Style | Runs | Order for stack [m1, m2, m3] | Kernel seam today |
|---|---|---|---|---|
| `before_agent(run)` | node | once, before the first step (and on `resume`) | m1 → m2 → m3 | new `on_run_start`; `loop.run` :218 / `loop.resume` :230, after `run.started` / `run.resumed` is emitted |
| `before_model(run, request)` | node | before every model call | m1 → m2 → m3 | `StopPolicy.decide` (`loop.py:79`) + `ToolPolicy.select/answer_tools` + `ContextPolicy.assemble` (`loop.py:112`) |
| `wrap_model_call(run, request, handler)` | wrap | around every model call | onion, m1 outermost | `call_model` (`step_model.py:59`): `Engine.complete` (:74) with `RecoveryPolicy.on_model_error` (:79); `GovernedEngine` budget gate (`governance.py:40`) |
| `after_model(run, turn)` | node | after every model call | m3 → m2 → m1 | `VerifyPolicy.after_step` (`loop.py:134`); empty-completion press (`final_text`, :142) |
| `wrap_tool_call(run, call, handler)` | wrap | around every tool call | onion, m1 outermost | `run_calls` (`step_tools.py:48`): schema check (:75), idempotency key (:87), `toolset.invoke` (:92), held/deferred results, `RecoveryPolicy.on_tool_error` |
| `after_agent(run, result)` | node | once, when the run ends or suspends | m3 → m2 → m1 | `VerifyPolicy.accept_final` (`loop.py:153`) + new `on_run_end` in `_Run.end` (:181) |

## Semantics

- **Signature.** A middleware is an object with any subset of the six methods; a missing method is
  a pass-through. Each carries `name` and `version`; `name@version` of every active middleware is
  recorded on `run.started` beside the policy versions (`Policies.versions()`), so a ledger row says
  which stack produced it.
- **Node hooks** return `None` (continue) or a `Jump`: `end` (stop with a reason), `model`
  (re-ask), or `tools` (run the pending tool calls). A middleware declares the jumps it may take
  (`can_jump_to`); an undeclared jump is a programming error, raised at preset build time.
- **Wrap hooks** receive `handler`, the next layer inward. They may call it zero times
  (short-circuit: cached answer, refusal, hold for approval), once (normal), or several times
  (retry, fallback to another tier). The innermost handler is the engine call or the tool.
- **Transient vs persistent change.** `request.override(...)` changes only this call (e.g. a
  smaller tool list, an injected memory pack). A persistent change is written to `run.state`
  (`RunState.extra` under the middleware's own key) and is carried in the checkpoint. Middleware
  never keeps its own process-local state: a resumed run on another worker must behave the same.
- **Tools from middleware.** A middleware may contribute tools (e.g. `read_run_ledger`, today
  joined via `Composite(spec.tools, LedgerTool(...))` at `loop.py:56-57`); they pass through the
  same `wrap_tool_call` chain as every other tool.
- **Events.** Every hook decision that changes the run (a jump, a short-circuit, a held call) is
  emitted through the run's `Recorder`, so it lands in `run_events` when the ledger is on.
- **Failure.** An exception in a node hook ends the run `failed` with the middleware named in the
  error; a wrap hook's exception is classified by `errors.classify` like an engine error.
- **Suspend/resume.** `before_agent` runs again on `resume`; middleware must be idempotent across
  a suspend (e.g. budget reserve keyed by run id + attempt).

## Built-in stack (outermost first) and where each BOSS concern sits

| Middleware | Hooks | Today's code it wraps |
|---|---|---|
| `identity` | `before_agent` | run token mint/exchange (`eng/identity/tokens.py`) |
| `budget` | `before_agent`, `wrap_model_call`, `after_agent` | reserve / `GovernedEngine` gate / settle (`svc/budget.py`, `kernel/governance.py`) |
| `limits` | `before_model` | `Caps.decide` (steps, spend, deadline, turn budget) |
| `memory` | `before_agent`, `before_model`, `after_agent` | knowledge pack (`kg/runtime.py`), `agent_memory`, learning writeback (`kg/learning_writeback.py`) |
| `context` | `before_model` | `ClipContext.assemble`, scoped tools (`kernel/scoped_tools.py`); K9 compaction |
| `recovery` | `wrap_model_call` | `Recover.on_model_error` retry / feedback / wait / suspend |
| `telemetry` | `wrap_model_call` | `llm_calls` row via `record_llm_call` (`svc/llm_telemetry.py`) |
| `verify` | `after_model`, `after_agent` | `VerifyPolicy.after_step` / `accept_final` / `reask` |
| `authz` | `wrap_tool_call` | `authz.check` (`eng/identity/authz.py`), reversibility → hold for approval (`engine_agent.py`) |
| `tool_args` | `wrap_tool_call` | `check_arguments` + `RecoveryPolicy.on_invalid`, argument binding (`svc/argument_binding.py`) |
| `tool_screen` | `wrap_tool_call` | result screening and taint (`svc/tool_result_screen.py`, `svc/low_trust.py`), clip/offload |
| `audit` | `wrap_tool_call` | `tool_call_audit` row; idempotency key for write tools (`step_tools.py:87`) |

A **preset** (`presets.py`) becomes a named stack plus limits: `decision`, `engine_agent`,
`agent_loop`, `precheck`, `native_coder`, and later `meeting_seat` and `subagent`. Delegate engines
(`delegate.py`) are one opaque `wrap_model_call` step: the stack still runs `before_agent` /
`after_agent` around them, and `budget` passes its remaining cap as `DelegateBrief.max_budget_usd`.

## Compatibility rule

Until a policy is retired, the default stack must reproduce today's preset behaviour exactly; the
existing kernel conformance tests (`engine/tests/test_kernel_*`) are the gate. A preset that adds
no middleware runs exactly the loop of today.
