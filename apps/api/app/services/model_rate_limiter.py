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
from dataclasses import dataclass, field
from typing import Callable, Optional

# Rate/usage-limit signatures a provider surfaces (substring match, case-insensitive). Kept
# broad on purpose: missing a real limit and hammering is worse than an occasional false match.
_RATE_LIMIT_SIGNS = (
    "rate limit", "rate_limit", "ratelimit", "429", "too many requests",
    "usage limit", "spend limit", "quota", "overloaded", "capacity",
    "resource exhausted", "throttl",
)


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
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.max_concurrency = max(1, int(max_concurrency))
        self.floor = max(1, min(int(floor_concurrency), self.max_concurrency))
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.recover_after = max(1, int(recover_after))
        self.default_backoff_s = max(0.0, float(default_backoff_s))
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
        with self._cond:
            lane = self._lane(provider)
            while True:
                now = self._time()
                if now < lane.backoff_until:
                    self._cond.wait(timeout=max(0.001, lane.backoff_until - now))
                    continue
                if now < lane.next_earliest:
                    self._cond.wait(timeout=max(0.001, lane.next_earliest - now))
                    continue
                if lane.in_flight >= lane.limit:
                    self._cond.wait(timeout=1.0)   # wait for a release; re-check on wake
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
        """Acquire a call slot for ``provider`` (blocks on concurrency/pacing/backoff)."""
        self._acquire(provider)
        try:
            yield
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


class NullRateLimiter:
    """Burst mode: no limiting at all (BOSS_MODEL_BURST_MODE=true)."""

    @contextmanager
    def slot(self, provider: str):
        yield

    def report_rate_limited(self, provider: str, retry_after_s: Optional[float] = None) -> None:
        pass

    def report_ok(self, provider: str) -> None:
        pass


_singleton_lock = threading.Lock()
_singleton: Optional[object] = None
_singleton_key: Optional[tuple] = None


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
