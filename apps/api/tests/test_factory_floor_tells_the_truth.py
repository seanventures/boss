"""The Floor says only what BOSS knows (factory spec §6.2, §8.1).

`GET /v1/factory/floor` behind the Factory's default tab: what needs you, what is building,
what shipped, what stopped. Pinned here, on the real database:

  (a) no percent, no ETA, no progress — no field of those names anywhere;
  (b) `k of n` never passes n;
  (c) one row per lineage: a retried build shows its newest attempt only;
  (d) BOSS's own housekeeping runs are not builds;
  (e) an unchanged Floor is a 304, and a station changing changes the ETag;
  (f) a build that ran only on a subscription costs $0 and is not `metered`;
  (g) a build stopped for you is one item, the same one the pill counts.
"""
from __future__ import annotations

import uuid

from apps.api.tests import _factory_support as fx


def _floor(client, **headers):
    r = client.get("/v1/factory/floor", params={"project_id": fx.PROJECT}, headers=headers)
    assert r.status_code in (200, 304), r.text
    return r


def _row(body, run_id):
    found = [b for b in body["builds"] if b["run_id"] == run_id]
    return found[0] if found else None


def test_the_floor_never_guesses(client, engine):
    run_id = fx.new_run(engine, title=f"Invoice export {uuid.uuid4().hex[:4]}")
    fx.stage(engine, run_id, "plan")
    fx.stage(engine, run_id, "implement")
    body = _floor(client).json()
    bad = [path for key, path in fx.keys(body) if key.lower() in fx.BANNED_KEYS]
    assert not bad, bad
    row = _row(body, run_id)
    assert row is not None
    assert row["state"] == "building" and row["word"] == "Coding"
    assert 0 <= row["k"] <= row["n"]
    build = next(s for s in row["stations"] if s["key"] == "build")
    assert build["status"] == "working" and build["face"] == "agent"
    assert build["role_key"] == "software_engineer"
    for s in row["stations"]:
        if s["face"] == "hollow":
            assert s["status"] == "pending" and s["role_key"] is None
    for b in body["builds"]:
        assert b["k"] <= b["n"]


def test_one_row_per_lineage_and_no_housekeeping(client, engine):
    first = fx.new_run(engine, status="failed", phase="implement", minutes_ago=30,
                       title="Team inbox")
    second = fx.new_run(engine, title="Team inbox · retry 1",
                        fctx={"root_run_id": first, "parent_run_id": first, "attempt": 2})
    chore = fx.new_run(engine, title="Captain: quality improve pass")
    body = _floor(client).json()
    ids = {b["run_id"] for b in body["builds"]} | {s["run_id"] for s in body["stopped"]}
    assert second in ids and first not in ids
    assert chore not in ids
    assert _row(body, second)["attempt"] == 2
    assert _row(body, second)["root_run_id"] == first


def test_an_unchanged_floor_is_a_304_and_a_station_changes_it(client, engine):
    run_id = fx.new_run(engine, title="Etag app")
    fx.stage(engine, run_id, "write_tests")
    first = _floor(client)
    etag = first.headers["etag"]
    assert _floor(client, **{"If-None-Match": etag}).status_code == 304
    fx.stage(engine, run_id, "implement")
    moved = _floor(client, **{"If-None-Match": etag})
    assert moved.status_code == 200
    assert moved.headers["etag"] != etag


def test_a_subscription_build_costs_nothing(client, engine):
    run_id = fx.new_run(engine, title="Sub app")
    act = fx.stage(engine, run_id, "implement")
    fx.call(engine, run_id, act["act_id"], micros=900_000, included=True)
    row = _row(_floor(client).json(), run_id)
    assert row["cost_usd"] == 0 and row["metered"] is False
    metered = fx.new_run(engine, title="Metered app")
    act = fx.stage(engine, metered, "implement")
    fx.call(engine, metered, act["act_id"], micros=310_000)
    row = _row(_floor(client).json(), metered)
    assert row["metered"] is True and abs(row["cost_usd"] - 0.31) < 1e-9


def test_a_build_stopped_for_you_is_the_pills_item(client, engine):
    run_id = fx.new_run(engine, status="blocked", phase="awaiting_gate_override",
                        title="Gate app", fctx={"failing_gates": ["code_review_passed"]})
    fx.stage(engine, run_id, "implement")
    fx.stage(engine, run_id, "review_code")
    body = _floor(client).json()
    item = next(a for a in body["attention"] if a["id"] == f"build:{run_id}:gates")
    assert item["kind"] == "gate_override" and item["title"] == "1 check failed"
    assert item["role_key"] == "adversarial_reviewer"
    assert _row(body, run_id)["attention_id"] == item["id"]
    assert _row(body, run_id)["word"] == "Checks failed"
    pill = client.get("/v1/agents/live", params={"project_id": fx.PROJECT}).json()["attention"]
    board = client.get("/v1/agents/live-board", params={"project_id": fx.PROJECT}).json()
    assert pill["count"] == board["counts"]["needs_you"]
    assert any(a["id"] == item["id"] for a in board["attention"])
    # ONE NUMBER (agents-home D2): decisions, as the pill says; the stopped build is a card.
    assert body["counts"]["needs_you"] == sum(1 for a in body["attention"] if a["kind"] == "approval")
    assert body["counts"]["needs_you"] == pill["count"]


def test_shipped_and_stopped(client, engine):
    shipped = fx.new_run(engine, status="completed", phase="done", pr=842, title="Shipped app")
    fx.stage(engine, shipped, "implement")
    fx.finish(engine, shipped)
    failed = fx.new_run(engine, status="failed", phase="review_code", title="Broken app",
                        fctx={"failure_class": "recoverable"})
    body = _floor(client).json()
    chip = next(o for o in body["outcomes"] if o["run_id"] == shipped)
    assert chip["text"] == "PR #842 opened" and chip["role_key"] == "software_engineer"
    stop = next(s for s in body["stopped"] if s["run_id"] == failed)
    assert stop["word"] == "Failed · Review" and stop["retryable"] is True


def test_the_apps_tab_groups_by_identity(client, engine):
    a1 = fx.new_run(engine, status="failed", phase="implement", title="Shorty", minutes_ago=50)
    a2 = fx.new_run(engine, title="Shorty · retry 1",
                    fctx={"root_run_id": a1, "parent_run_id": a1, "attempt": 2})
    fx.stage(engine, a2, "implement")
    body = client.get("/v1/factory/apps", params={"project_id": fx.PROJECT}).json()
    row = next(r for r in body["apps"] if a2 in r["run_ids"])
    assert row["run_ids"][0] == a2 and a1 in row["run_ids"]
    assert row["builds"] >= 2 and row["builders"] == ["software_engineer"]
    assert row["app"] == "Shorty"


def test_agents_live_shows_the_same_builds(client, engine):
    """Agents › Live is the agent lens, the Floor the build lens: same rows, same counts."""
    run_id = fx.new_run(engine, title="Both lenses")
    fx.stage(engine, run_id, "implement")
    body = _floor(client).json()
    board = client.get("/v1/agents/live-board", params={"project_id": fx.PROJECT}).json()
    assert board["counts"]["building"] == body["counts"]["building"]
    assert [b["run_id"] for b in board["builds"]] == [b["run_id"] for b in body["builds"]]
    mine = next(b for b in board["builds"] if b["run_id"] == run_id)
    assert mine == _row(body, run_id)
