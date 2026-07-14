# Model-call pacing — rate-limit-aware backpressure on the model lane (#111)

> Spec for Plane #111 (Engine & Model Quality module). Implemented first cut on
> `feat/model-call-pacing`. Default **paced**; opt-in **burst**.

## 1. Problem — real incident

BOSS fans out many concurrent model calls: the review panel runs several critics in parallel,
multi-agent stages overlap, and a bake-off multiplies all of it. A **dense burst** trips a
provider's *rolling* rate limit — e.g. Anthropic's **5-hour session cap** — **even when the
weekly/monthly allowance is barely touched**. Worse, with usage credits enabled the overflow is
**silent and paid**: it draws from an overage wallet rather than returning an error.

Observed live: a 12-run VAG bake-off (each run = a multi-turn verify panel + repair loops) spiked
the 5-hour cap and quietly consumed ~$20 of overage credits while the *weekly* meter still read 15%.
BOSS never saw a 429 until the credit wallet itself was exhausted — so it had no chance to react.

The lesson: BOSS must **pace itself** so a burst can't happen by default, and must **react** to a
rate-limit signal when it does arrive — instead of hammering.

## 2. Design — three levers, per provider

All state is scoped **per provider/lane** (anthropic / openai / openrouter / azure / groq / deepseek),
because limits differ by provider.

1. **Bounded concurrency (not serial).** A global cap on *in-flight* model calls per provider.
   Agents still run in parallel — bounded, not unbounded — so multi-agent collaboration survives; the
   burst does not. (Today only the review panel had a *local* `code_agent_max_concurrency`; this makes
   the bound global across every stage and agent.)
2. **Pacing.** A minimum interval between call *starts* per provider, so a run can't spike the rolling
   window. `0` = concurrency-only (the default); set it to enforce a hard calls/min.
3. **Adaptive backoff (AIMD).** On a rate/usage-limit signal, **multiplicatively halve** the concurrency
   cap and back off (honoring `Retry-After` when present); on sustained success, **additively recover**
   one slot at a time. BOSS thus *learns* the ceiling and self-throttles — additive-increase /
   multiplicative-decrease, the same control law that keeps TCP from congesting a link.

**The toggle:** `BOSS_MODEL_BURST_MODE`, **default off (paced)**. Customers who don't care about usage
limits flip it **on** to run wide-open (a `NullRateLimiter` — zero overhead, no limiting).

## 3. Implementation

- **`apps/api/app/services/model_rate_limiter.py`**
  - `ModelRateLimiter` — thread-safe (`threading.Condition`), per-lane `_Lane{limit, in_flight,
    next_earliest, backoff_until, successes}`. `slot(provider)` is a context manager that blocks on
    concurrency / pacing / backoff; `report_rate_limited()` / `report_ok()` drive AIMD.
  - `NullRateLimiter` — burst mode passthrough.
  - `provider_of(engine)` — maps a BOSS engine string to a lane key.
  - `is_rate_limit_error(err)` — broad substring match for 429 / rate / usage / spend / overloaded /
    quota signatures (miss-and-hammer is worse than an occasional false match).
  - `get_rate_limiter(settings)` — process-global singleton, rebuilt on config change.
- **`PacedCodeAgent`** (in `code_agent.py`) — transparent wrapper: every `run_role` / `run_research`
  runs inside `limiter.slot(provider)`; on a rate-limit exception it calls `report_rate_limited` then
  re-raises, on success `report_ok`. Delegates all other attributes to the inner agent.
- **`make_code_agent`** wraps its result via `_wrap_paced()` — so **every** agent (coding, meetings,
  discovery — all model usage) is paced, not just the pipeline. Burst mode returns the raw agent.

## 4. Config reference

| Env | Default | Meaning |
|---|---|---|
| `BOSS_MODEL_BURST_MODE` | `false` | `true` → no limiting at all (run wide-open) |
| `BOSS_MODEL_MAX_CONCURRENCY` | `3` | global in-flight cap per provider (AIMD ceiling) |
| `BOSS_MODEL_MIN_INTERVAL_MS` | `0` | min spacing between call starts per provider (0 = concurrency-only) |
| `BOSS_MODEL_RATE_BACKOFF_SECONDS` | `30` | default backoff after a limit signal (`Retry-After` wins) |

## 5. How it prevents the incident

- The **concurrency cap (3)** stops a bake-off from firing a wide fan of calls at once.
- **Adaptive backoff** means the *first* rate-limit signal halves throughput and pauses — turning a
  runaway into a graceful slow-down instead of hammering (and, with credits off, a clean pause rather
  than silent overage).
- **Burst mode off by default** means a user has to *opt in* to the risky behavior — exactly the
  "default safe, opt-in bursty" ask.

## 6. Limitations & future work

- **Process-global, not distributed.** It bounds bursts *within* a worker process (where the review
  panel's parallelism lives). Multiple worker processes don't yet coordinate — a shared/distributed
  limiter (Redis or a DB token bucket) is the follow-up for multi-worker deployments.
- **Reactive, not predictive of a specific provider quota.** It doesn't know Anthropic's exact 5-hour
  remaining; it paces conservatively and reacts to signals. A future enhancement: a per-window budget
  (calls or tokens per rolling window) fed by provider usage headers for proactive throttling.
- Pairs with the sandbox/Go backpressure in the Runtime & Concurrency spec (#97) — this is the
  **model-lane** sibling of that **execution-lane** backpressure.

## 7. Testing

`apps/api/tests/test_model_rate_limiter.py` (9 tests): provider mapping, limit-error detection, AIMD
decrease+recover, concurrency bound (threaded), pacing spacing, backoff blocking, burst→null selection,
and the `PacedCodeAgent` success/rate-limit reporting + delegation. Full governed + bake-off suites stay
green with pacing on by default.
