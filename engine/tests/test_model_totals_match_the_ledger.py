"""Observability › Models says what llm_calls says (infra-observability spec §9.1).

Totals, every group's row, p95 and spend (metered only: never a call that ran on a
subscription) equal SQL over the seeded ledger; `estimated` is true iff some call in the
window had an estimated price; the 24-point sparks add up to the totals; a call outside the
window is not counted.
"""
from __future__ import annotations

import random
from datetime import timedelta

import pytest
from sqlalchemy import text

from ._obs_seed import llm_call, pg_engine, project
from boss_engine.config import get_settings
from boss_engine.services import obs_view

MODELS = ["claude-sonnet-4-5", "gpt-5-mini", "qwen-local", "claude-haiku", "o4", "m6", "m7"]


@pytest.fixture(scope="module")
def seeded():
    eng = pg_engine()
    rnd = random.Random(11)
    with eng.begin() as c:
        pid = project(c)
        now = c.execute(text("SELECT now()")).scalar()
        for i in range(400):
            at = now - timedelta(seconds=rnd.uniform(0, 23 * 3600))
            llm_call(c, pid, at, model=rnd.choice(MODELS),
                     stage=rnd.choice(["implement", "review_code", "plan"]),
                     role=rnd.choice(["software_engineer", "adversarial_reviewer", None]),
                     engine=rnd.choice(["claude_code", "guest:codex", "native"]),
                     tin=rnd.randint(10, 9000), tout=rnd.randint(1, 900),
                     cost=rnd.randint(0, 40000), est=(i % 97 == 0),
                     latency=rnd.choice([0, rnd.randint(100, 60000)]),
                     ok=rnd.random() > 0.1, included=rnd.random() < 0.3,
                     retries=rnd.randint(0, 3), error="rate limit exceeded (429)")
        # Outside the window: never counted.
        llm_call(c, pid, now - timedelta(hours=30), model="claude-sonnet-4-5", cost=999999)
        clean = project(c)
        llm_call(c, clean, now - timedelta(hours=1), est=False)
    return eng, pid, clean


def _sql_totals(c, pid, group=None):
    w = obs_view.window_bounds("24h", 24)
    col = {"model": "COALESCE(model, '?')", "stage": "COALESCE(stage, '?')",
           "role": "COALESCE(role, '?')", "engine": "COALESCE(engine, '?')"}.get(group, "'all'")
    return {r["g"]: dict(r) for r in c.execute(text(
        f"SELECT {col} AS g, count(*) AS calls, sum(prompt_tokens) AS tin, "
        "  sum(completion_tokens) AS tout, "
        "  COALESCE(sum(cost_usd_micros) FILTER (WHERE NOT included_in_subscription), 0) AS spend,"
        "  count(*) FILTER (WHERE included_in_subscription) AS included, "
        "  bool_or(price_estimated) AS est, count(*) FILTER (WHERE NOT ok) AS failed, "
        "  sum(retries) AS retries, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) "
        "    FILTER (WHERE latency_ms > 0) AS p95 "
        "FROM llm_calls WHERE project_id = :p AND created_at >= :a AND created_at < :z "
        "GROUP BY 1"), {"p": pid, "a": w["start"], "z": w["end"]}).mappings()}


def test_totals_equal_the_ledger(seeded):
    eng, pid, _ = seeded
    with eng.begin() as c:
        body = obs_view.models(c, get_settings(), pid, win="24h")
        want = _sql_totals(c, pid)["all"]
    t = body["totals"]
    assert t["calls"] == want["calls"] == 400
    assert (t["tokens_in"], t["tokens_out"]) == (want["tin"], want["tout"])
    assert t["spend_micros"] == want["spend"]
    assert t["included_calls"] == want["included"]
    assert t["failed"] == want["failed"]
    assert t["p95_ms"] == int(round(want["p95"]))
    assert t["estimated"] is True
    assert sum(body["spark"]["calls"]) == t["calls"]
    assert sum(body["spark"]["spend"]) == t["spend_micros"]
    assert sum(body["spark"]["errors"]) == t["failed"]
    assert len(body["spark"]["calls"]) == obs_view.SPARK_POINTS


@pytest.mark.parametrize("group", ["model", "stage", "role", "engine"])
def test_every_group_equals_the_ledger(seeded, group):
    eng, pid, _ = seeded
    with eng.begin() as c:
        body = obs_view.models(c, get_settings(), pid, win="24h", group=group, metric="calls")
        want = _sql_totals(c, pid, group)
    assert {g["key"] for g in body["groups"]} == set(want)
    for g in body["groups"]:
        w = want[g["key"]]
        assert (g["calls"], g["tokens_in"], g["tokens_out"]) == (w["calls"], w["tin"], w["tout"])
        assert g["spend_micros"] == w["spend"]
        assert g["failed"] == w["failed"] and g["retries"] == w["retries"]
        assert g["p95_ms"] == (None if w["p95"] is None else int(round(w["p95"])))
        assert g["estimated"] == bool(w["est"])
        assert sum(g["trend"]) == g["calls"]
    # Stacked series: top 5 + Other add up to every bucket's calls.
    bands = body["series"]["bands"]
    assert len(bands) <= 6
    for i, total in enumerate(body["spark"]["calls"]):
        assert sum(b["values"][i] for b in bands) == total
    if group == "engine":
        guest = next(g for g in body["groups"] if g["key"] == "guest:codex")
        assert guest["guest"] is True and guest["label"] == "codex"


def test_estimated_is_false_when_no_price_was_estimated(seeded):
    eng, _, clean = seeded
    with eng.begin() as c:
        body = obs_view.models(c, get_settings(), clean)
    assert body["totals"]["calls"] == 1 and body["totals"]["estimated"] is False


def test_failed_calls_are_the_five_newest_with_a_short_word(seeded):
    eng, pid, _ = seeded
    with eng.begin() as c:
        body = obs_view.models(c, get_settings(), pid)
        newest = [r[0] for r in c.execute(text(
            "SELECT id FROM llm_calls WHERE project_id = :p AND NOT ok "
            "ORDER BY created_at DESC, id LIMIT 5"), {"p": pid})]
    assert [f["id"] for f in body["failed"]] == newest
    assert all(f["error_word"] == "Rate limited" for f in body["failed"])
    assert all(len(f["error_word"].split()) <= 4 for f in body["failed"])
