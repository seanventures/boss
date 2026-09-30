"""#111 model-call pacing — rate limiter (concurrency + pacing + AIMD) and the paced agent wrapper."""
import threading
import time
from types import SimpleNamespace

import pytest

from apps.api.app.services.code_agent import PacedCodeAgent
from apps.api.app.services.model_rate_limiter import (
    ModelRateLimiter,
    NullRateLimiter,
    get_rate_limiter,
    is_rate_limit_error,
    provider_of,
)


def test_provider_mapping():
    assert provider_of("claude_code") == "anthropic"
    assert provider_of("claude_cli") == "anthropic"
    assert provider_of("openrouter:deepseek/deepseek-chat") == "openrouter"
    assert provider_of("azure:") == "azure"
    assert provider_of("native") == "openai"
    assert provider_of("") == "default"


def test_rate_limit_detection():
    assert is_rate_limit_error("claude exited 1: api_error_status:429 ...")
    assert is_rate_limit_error(RuntimeError("You've hit your monthly spend limit"))
    assert is_rate_limit_error("Error: 429 Too Many Requests")
    assert is_rate_limit_error("model overloaded, try again")
    assert not is_rate_limit_error("SyntaxError: invalid syntax")
    assert not is_rate_limit_error("connection refused")


def test_aimd_decrease_and_recover():
    lim = ModelRateLimiter(
        max_concurrency=8,
        floor_concurrency=1,
        recover_after=2,
        default_backoff_s=0.0,
    )
    assert lim.limit_for("p") == 8
    lim.report_rate_limited("p")
    assert lim.limit_for("p") == 4  # multiplicative /2
    lim.report_rate_limited("p")
    assert lim.limit_for("p") == 2
    lim.report_rate_limited("p")
    assert lim.limit_for("p") == 1
    lim.report_rate_limited("p")
    assert lim.limit_for("p") == 1  # floor holds
    lim.report_ok("p")
    assert lim.limit_for("p") == 1  # not yet (recover_after=2)
    lim.report_ok("p")
    assert lim.limit_for("p") == 2  # additive +1 after 2 oks


def test_concurrency_is_bounded():
    lim = ModelRateLimiter(max_concurrency=2)
    peak = [0]
    cur = [0]
    lock = threading.Lock()

    def worker():
        with lim.slot("p"):
            with lock:
                cur[0] += 1
                peak[0] = max(peak[0], cur[0])
            time.sleep(0.05)
            with lock:
                cur[0] -= 1

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for thread in ts:
        thread.start()
    for thread in ts:
        thread.join()
    assert peak[0] <= 2  # never more than the cap in flight at once


def test_pacing_spaces_call_starts():
    lim = ModelRateLimiter(max_concurrency=5, min_interval_s=0.05)
    t0 = time.monotonic()
    for _ in range(4):            # 4 starts spaced by >=0.05 -> >=0.15 total
        with lim.slot("p"):
            pass
    assert time.monotonic() - t0 >= 0.14


def test_backoff_blocks_until_clear():
    lim = ModelRateLimiter(max_concurrency=4, default_backoff_s=0.1)
    lim.report_rate_limited("p")
    t0 = time.monotonic()
    with lim.slot("p"):
        pass
    assert time.monotonic() - t0 >= 0.08   # waited out the backoff window


def test_burst_mode_uses_null_limiter():
    paced = SimpleNamespace(model_burst_mode=False, model_max_concurrency=3,
                            model_min_interval_ms=0, model_rate_backoff_seconds=30)
    burst = SimpleNamespace(model_burst_mode=True, model_max_concurrency=3,
                            model_min_interval_ms=0, model_rate_backoff_seconds=30)
    assert isinstance(get_rate_limiter(burst), NullRateLimiter)
    assert isinstance(get_rate_limiter(paced), ModelRateLimiter)


class _FakeLimiter:
    def __init__(self):
        self.ok = []
        self.rl = []
        self.slots = 0

    def slot(self, provider):
        self.slots += 1
        from contextlib import nullcontext

        return nullcontext()

    def report_rate_limited(self, provider, retry_after_s=None):
        self.rl.append(provider)

    def report_ok(self, provider):
        self.ok.append(provider)


class _FakeAgent:
    engine = "claude_code"

    def run_role(self, prompt, cwd, **kw):
        if prompt == "boom":
            raise RuntimeError("claude exited 1: api_error_status:429 rate limit")
        return {"summary": "ok"}


def test_paced_agent_reports_success_and_rate_limit():
    lim = _FakeLimiter()
    agent = PacedCodeAgent(_FakeAgent(), lim)
    # success path: passes result through, reports ok, maps engine -> provider
    assert agent.run_role("hi", "/tmp")["summary"] == "ok"
    assert lim.ok == ["anthropic"] and lim.slots == 1
    # rate-limit path: classifies the 429, reports it, re-raises unchanged
    with pytest.raises(RuntimeError):
        agent.run_role("boom", "/tmp")
    assert lim.rl == ["anthropic"]


def test_paced_agent_delegates_unknown_attrs():
    agent = PacedCodeAgent(_FakeAgent(), _FakeLimiter())
    assert agent.engine == "claude_code"          # exposed on the wrapper
