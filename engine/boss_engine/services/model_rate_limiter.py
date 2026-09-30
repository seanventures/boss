"""Rate-limit-aware pacing for model calls (#111) — backpressure on the model lane.

BOSS fans out many concurrent model calls (the review panel, multi-agent stages, bake-offs).
A dense burst trips a provider's *rolling* rate limit (e.g. Anthropic's 5-hour session cap)
even when the weekly/monthly allowance is barely touched — and can silently overflow to paid
usage credits. This module paces those calls so a burst can't happen by default.

Three levers, all per-provider (anthropic / openai / openrouter / azure / …):
  1. Bounded concurrency — a global cap on in-flight calls (agents still run in parallel,
     just bounded, not unbounded).
  2. Pacing — a minimum interval between call *starts* so a run can't spike the rolling window.
  3. Adaptive backoff (AIMD) — on a rate/usage-limit signal, halve the concurrency cap and
     wait (honoring Retry-After); recover one slot at a time on sustained success. BOSS thus
     LEARNS the ceiling and self-throttles instead of hammering.

Default is PACED. Set BOSS_MODEL_BURST_MODE=true to remove the throttle entirely (customers who
don't care about usage limits). The limiter is process-global; cross-process coordination (a
shared/distributed limiter) is a future enhancement — noted in the spec.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional

# Rate/usage-limit signatures a provider surfaces (substring match, case-insensitive). Kept
# broad on purpose: missing a real limit and hammering is worse than an occasional false match.
_RATE_LIMIT_SIGNS = (
    "rate limit", "rate_limit", "ratelimit", "429", "too many requests",
    "usage limit", "spend limit", "quota", "overloaded", "capacity",
    "resource exhausted", "throttl",
)


@dataclass(frozen=True)
class SlotHandle:
    """What acquiring the slot cost. Zero when the lane was free."""

    waited_seconds: float


def is_rate_limit_error(err: object) -> bool:
    """True if an exception/message looks like a provider rate/usage limit."""
    s = str(err).lower()
    return any(sig in s for sig in _RATE_LIMIT_SIGNS)


def provider_of(engine: Optional[str]) -> str:
    """Map a BOSS engine string to a provider/lane key so limits are scoped per provider.
    Engines look like 'claude_code', 'claude_cli', 'native', 'azure:', 'openrouter:model'."""
    e = (engine or "").lower()
    if e.startswith("openrouter"):
        return "openrouter"
    if e.startswith("azure"):
        return "azure"
    if e.startswith("groq"):
        return "groq"
    if e.startswith("deepseek"):
        return "deepseek"
    if "claude" in e or "anthropic" in e:
        return "anthropic"
    if e.startswith(("native", "openai", "gpt")) or "openai" in e:
        return "openai"
    return e or "default"


class RateLimitAcquireTimeout(RuntimeError):
    """Raised when a call slot cannot be acquired within the deadline."""


@dataclass
class _Lane:
    limit: int                      # current AIMD concurrency cap
    in_flight: int = 0
    next_earliest: float = 0.0      # earliest monotonic time the next call may START (pacing)
    backoff_until: float = 0.0      # monotonic time a rate-limit backoff clears
    successes: int = 0              # successes since the last recovery step


class ModelRateLimiter:
    """Thread-safe, per-provider concurrency + pacing + AIMD backoff."""

    def __init__(
        self, *, max_concurrency: int = 3, floor_concurrency: int = 1,
        min_interval_s: float = 0.0, recover_after: int = 5,
        default_backoff_s: float = 30.0,
        max_backoff_s: float = 300.0,
        acquire_timeout_s: float = 600.0,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.max_concurrency = max(1, int(max_concurrency))
        self.floor = max(1, min(int(floor_concurrency), self.max_concurrency))
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.recover_after = max(1, int(recover_after))
        self.default_backoff_s = max(0.0, float(default_backoff_s))
        # A provider Retry-After was previously honoured VERBATIM. When an account hits its
        # quota the provider answers with the time until the quota RESETS — potentially hours —
        # so the limiter slept for hours while holding the job's lease. That is the root cause
        # of the "164 min adversarial review": not review work, a silent backoff.
        self.max_backoff_s = max(1.0, float(max_backoff_s))
        # And acquisition itself was an unbounded `while True`. Past this deadline we raise
        # instead of sleeping forever, so the stage fails FAST with a legible reason.
        self.acquire_timeout_s = max(1.0, float(acquire_timeout_s))
        self._time = time_fn
        self._sleep = sleep_fn
        self._cond = threading.Condition()
        self._lanes: dict[str, _Lane] = {}

    def _lane(self, provider: str) -> _Lane:
        lane = self._lanes.get(provider)
        if lane is None:
            lane = _Lane(limit=self.max_concurrency)
            self._lanes[provider] = lane
        return lane

    def _acquire(self, provider: str) -> None:
        deadline = self._time() + self.acquire_timeout_s
        with self._cond:
            lane = self._lane(provider)
            while True:
                now = self._time()
                if now >= deadline:
                    waited = int(self.acquire_timeout_s)
                    raise RateLimitAcquireTimeout(
                        f"could not acquire a {provider} call slot within {waited}s "
                        f"(in_flight={lane.in_flight}/{lane.limit}, "
                        f"backoff_remaining={max(0, int(lane.backoff_until - now))}s). "
                        "The provider is throttling or its quota is exhausted — failing fast "
                        "instead of holding the job open.")
                # Each wait is SLICED so the acquire deadline is re-checked promptly. A single
                # long wait (a 300s backoff) would otherwise block past the deadline before the
                # loop got a chance to give up.
                slice_s = max(0.001, min(1.0, deadline - now))
                if now < lane.backoff_until:
                    self._cond.wait(timeout=min(slice_s, max(0.001, lane.backoff_until - now)))
                    continue
                if now < lane.next_earliest:
                    self._cond.wait(timeout=min(slice_s, max(0.001, lane.next_earliest - now)))
                    continue
                if lane.in_flight >= lane.limit:
                    self._cond.wait(timeout=slice_s)   # wait for a release; re-check on wake
                    continue
                lane.in_flight += 1
                lane.next_earliest = now + self.min_interval_s
                return

    def _release(self, provider: str) -> None:
        with self._cond:
            lane = self._lane(provider)
            lane.in_flight = max(0, lane.in_flight - 1)
            self._cond.notify_all()

    @contextmanager
    def slot(self, provider: str):
        """Acquire a call slot for ``provider`` (blocks on concurrency/pacing/backoff).

        Yields a handle carrying HOW LONG the acquire blocked. That number was previously
        invisible: a stage that waited four minutes for a slot and then ran for one looked
        identical, in every timing BOSS records, to a stage that ran for five. Which of the
        two it is decides whether the answer is "the model is slow" or "the lane is full",
        and those have opposite fixes.
        """
        started = self._time()
        self._acquire(provider)
        handle = SlotHandle(waited_seconds=max(0.0, self._time() - started))
        try:
            yield handle
        finally:
            self._release(provider)

    def report_rate_limited(self, provider: str, retry_after_s: Optional[float] = None) -> None:
        """A call hit a provider rate/usage limit: multiplicatively DECREASE the cap and back
        off (honoring Retry-After when given). This is the 'D' in AIMD — react hard."""
        with self._cond:
            lane = self._lane(provider)
            lane.limit = max(self.floor, lane.limit // 2)
            lane.successes = 0
            wait = self.default_backoff_s if retry_after_s is None else max(0.0, float(retry_after_s))
            wait = min(wait, self.max_backoff_s)   # never sleep out a quota-reset window
            lane.backoff_until = max(lane.backoff_until, self._time() + wait)
            self._cond.notify_all()

    def report_ok(self, provider: str) -> None:
        """A call succeeded: additively INCREASE the cap after sustained success (the 'AI')."""
        with self._cond:
            lane = self._lane(provider)
            if lane.limit >= self.max_concurrency:
                return
            lane.successes += 1
            if lane.successes >= self.recover_after:
                lane.limit = min(self.max_concurrency, lane.limit + 1)
                lane.successes = 0
                self._cond.notify_all()

    # introspection (tests / telemetry)
    def limit_for(self, provider: str) -> int:
        with self._cond:
            return self._lane(provider).limit

    def lane_state(self, provider: str) -> dict:
        """One lane's live state, cheap enough to read from a watchdog every few seconds.

        `snapshot()` builds every lane and the whole config block; this answers the only
        question a caller blocked in `_acquire` has — WHY is there no slot — and the answer is
        one of three numbers: the lane is full, it is backing off, or it is pacing.
        """
        with self._cond:
            lane = self._lane(provider)
            now = self._time()
            return {
                "in_flight": lane.in_flight,
                "limit": lane.limit,
                "backoff_remaining_seconds": round(max(0.0, lane.backoff_until - now), 1),
                "pacing_wait_seconds": round(max(0.0, lane.next_earliest - now), 1),
            }

    def snapshot(self) -> dict:
        """Process-local limiter state for observability. Lanes appear only after use."""
        with self._cond:
            now = self._time()
            lanes = []
            for provider, lane in sorted(self._lanes.items()):
                lanes.append(
                    {
                        "provider": provider,
                        "limit": lane.limit,
                        "max_concurrency": self.max_concurrency,
                        "floor_concurrency": self.floor,
                        "in_flight": lane.in_flight,
                        "successes": lane.successes,
                        "backoff_remaining_seconds": round(
                            max(0.0, lane.backoff_until - now), 3
                        ),
                        "pacing_wait_seconds": round(
                            max(0.0, lane.next_earliest - now), 3
                        ),
                    }
                )
            return {
                "mode": "paced",
                "scope": "process_local",
                "available": True,
                "note": (
                    "Limiter state is process-local to this process; API and worker "
                    "processes do not share lane counters. Multi-worker coordination "
                    "is not yet implemented."
                ),
                "config": {
                    "max_concurrency": self.max_concurrency,
                    "floor_concurrency": self.floor,
                    "min_interval_ms": int(round(self.min_interval_s * 1000)),
                    "default_backoff_seconds": self.default_backoff_s,
                    "max_backoff_seconds": self.max_backoff_s,
                    "recover_after": self.recover_after,
                    "acquire_timeout_seconds": self.acquire_timeout_s,
                },
                "lanes": lanes,
            }


class NullRateLimiter:
    """Burst mode: no limiting at all (BOSS_MODEL_BURST_MODE=true)."""

    @contextmanager
    def slot(self, provider: str):
        # Same shape as the real one, so a caller reading `.waited_seconds` does not have to
        # know which limiter it got.
        yield SlotHandle(waited_seconds=0.0)

    def report_rate_limited(self, provider: str, retry_after_s: Optional[float] = None) -> None:
        pass

    def report_ok(self, provider: str) -> None:
        pass

    def limit_for(self, provider: str) -> int:
        return 0

    def snapshot(self) -> dict:
        return {
            "mode": "burst",
            "scope": "process_local",
            "available": True,
            "note": (
                "BOSS_MODEL_BURST_MODE is enabled: no concurrency/pacing/backoff is "
                "applied. There is no per-lane state to report."
            ),
            "config": {
                "max_concurrency": 0,
                "floor_concurrency": 0,
                "min_interval_ms": 0,
                "default_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
                "recover_after": 0,
                "acquire_timeout_seconds": 0.0,
            },
            "lanes": [],
        }


_singleton_lock = threading.Lock()
_singleton: Optional[object] = None
_singleton_key: Optional[tuple] = None

_SCOPE_NOTE = (
    "Limiter state is process-local to this process; API and worker processes do not "
    "share lane counters. Multi-worker coordination is not yet implemented."
)


def get_rate_limiter(settings) -> object:
    """Process-global limiter built from settings. NullRateLimiter when burst mode is on."""
    global _singleton, _singleton_key
    burst = bool(getattr(settings, "model_burst_mode", False))
    key = (
        burst,
        int(getattr(settings, "model_max_concurrency", 3) or 3),
        int(getattr(settings, "model_min_interval_ms", 0) or 0),
        int(getattr(settings, "model_rate_backoff_seconds", 30) or 30),
    )
    with _singleton_lock:
        if _singleton is not None and _singleton_key == key:
            return _singleton
        if burst:
            _singleton = NullRateLimiter()
        else:
            _singleton = ModelRateLimiter(
                max_concurrency=key[1],
                min_interval_s=key[2] / 1000.0,
                default_backoff_s=key[3],
            )
        _singleton_key = key
        return _singleton


def rate_limiter_observability(settings, *, project_id: Optional[str] = None) -> dict:
    """API-facing snapshot of the process-local rate limiter (honest empty when unused)."""
    limiter = get_rate_limiter(settings)
    snap = limiter.snapshot() if hasattr(limiter, "snapshot") else {
        "mode": "unknown",
        "scope": "process_local",
        "available": False,
        "note": _SCOPE_NOTE,
        "config": {},
        "lanes": [],
    }
    return {
        "project_id": project_id,
        **snap,
    }


def report_rate_limiter_outcome(
    settings,
    *,
    provider: str,
    outcome: str,
    retry_after_s: Optional[float] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Apply a worker-reported rate-limit or ok signal to the process-local limiter."""
    provider_key = (provider or "").strip().lower() or "default"
    outcome_key = (outcome or "").strip().lower()
    if outcome_key not in {"rate_limited", "ok"}:
        raise ValueError("outcome must be 'rate_limited' or 'ok'")
    limiter = get_rate_limiter(settings)
    if outcome_key == "rate_limited":
        limiter.report_rate_limited(provider_key, retry_after_s=retry_after_s)
    else:
        limiter.report_ok(provider_key)
    snap = rate_limiter_observability(settings, project_id=project_id)
    lane = next(
        (item for item in snap.get("lanes") or [] if item.get("provider") == provider_key),
        None,
    )
    return {
        "accepted": True,
        "project_id": project_id,
        "provider": provider_key,
        "outcome": outcome_key,
        "scope": "process_local",
        "note": _SCOPE_NOTE,
        "lane": lane,
        "mode": snap.get("mode"),
    }


def reset_rate_limiter_for_tests() -> None:
    """Clear the process-global singleton (tests only)."""
    global _singleton, _singleton_key
    with _singleton_lock:
        _singleton = None
        _singleton_key = None
