"""Shared fixtures for the model router's tests: a policy, sources with headroom, snapshots.

The defaults match docs/product/MODEL_ROUTER.md's "Realistic examples": a Claude subscription
(haiku / sonnet / opus) plus OpenRouter, Smart mode, the `balanced` floor preset.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from boss_engine.services.model_router.headroom import assess
from boss_engine.services.model_router.policy import seed
from boss_engine.services.model_router.router import Snapshot
from boss_engine.services.model_router.selection import SourceState
from boss_engine.services.pipeline_profiles import resolve_profile

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)  # a Thursday


def policy(mode: str = "smart", profile: str = "balanced", **changes: Any):
    base = seed(settings=SimpleNamespace(), connected={"claude_code": {}, "openrouter": {}},
                build_engine="claude_code", profile=resolve_profile(profile), mode=mode)
    return replace(base, **changes) if changes else base


def claude(left: Optional[float] = None, *, status: str = "allowed",
           opus_spent: bool = False, weekly_spent: bool = False, now: datetime = NOW,
           thresholds: Optional[dict] = None) -> SourceState:
    windows: dict[str, dict] = {}
    resets = (now + timedelta(days=4)).isoformat()
    if left is not None:
        windows["seven_day"] = {"used": 1.0 - left, "resets_at": resets, "status": status,
                                "source": "cli"}
    if weekly_spent:
        windows["seven_day"] = {"used": 1.0, "resets_at": resets, "status": "rejected",
                                "source": "limit"}
    if opus_spent:
        windows["seven_day_opus"] = {"used": 1.0, "resets_at": resets, "status": "rejected",
                                     "source": "limit"}
    head = assess("claude_code", {"windows": windows},
                  thresholds=thresholds or {"tight": 0.40, "near_limit": 0.15}, now=now)
    return SourceState("claude_code", "subscription", source_id="anthropic_claude_code",
                       headroom=head)


def metered(engine: str, *, working: bool = True) -> SourceState:
    return SourceState(engine, "metered", working=working, source_id=f"setup-{engine}")


def local(engine: str = "ollama") -> SourceState:
    return SourceState(engine, "local", source_id=f"setup-{engine}")


def snap(*, sources: Optional[dict] = None, mode: str = "smart", budget: str = "green",
         job_pins: Optional[dict] = None, pol=None, **kw: Any) -> Snapshot:
    sources = sources if sources is not None else {
        "claude_code": claude(), "openrouter": metered("openrouter")}
    return Snapshot(project_id="p1", policy=pol or policy(mode), sources=sources,
                    budget_state=budget, job_pins=job_pins or {}, **kw)
