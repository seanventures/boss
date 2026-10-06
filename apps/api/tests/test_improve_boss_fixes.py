"""The independent test's FIX FIRST list (2026-10-05): five privacy blockers and the defects,
each fixed as a class, each with a test that fails without its fix."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import socket
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.tests.improve_fixtures import (cards, project, reset_state, seed_build,
                                             seed_meeting, seed_workflow_run, tm_settings, uid)

ROOT = Path(__file__).resolve().parents[3]
PRIVATE_MODELS = ["claude-nightingale-ft", "llama3-acmewidgets-internal", "qwen-acme-payroll-7b",
                  "gpt-4o-acmecorp", "gemini-priyaraman"]


def _switch(engine, name, on):
    from boss_engine.services.improve_boss import set_switch

    with engine.begin() as c:
        set_switch(c, name, on, actor="ceo", project_id=project())


@pytest.fixture
def connects(monkeypatch):
    seen: list = []

    def refuse(*a, **k):
        seen.append(a[-1] if a else k)
        raise ConnectionRefusedError("test: no network")

    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: refuse(addr))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, addr: (seen.append(addr), 111)[1])
    monkeypatch.setattr(socket, "create_connection", lambda addr, *a, **k: refuse(addr))
    return seen


# ------------------------------------------------------------------ P1: model names
@pytest.mark.parametrize("n_builds", [5, 230])
def test_private_model_names_never_travel_in_rows_or_aggregates(engine, n_builds):
    from boss_engine.services import improve_boss, scorecards

    reset_state(engine)
    _switch(engine, "improve", True)
    with engine.begin() as c:
        c.execute(text("UPDATE system_state SET value = jsonb_set(value, '{at}', "
                       "to_jsonb((now() - interval '1 day')::text)) WHERE key = 'improve_boss'"))
        for i in range(n_builds):
            seed_build(c, model=PRIVATE_MODELS[i % len(PRIVATE_MODELS)])
        scorecards.sweep(c, limit=1000)
        body = gzip.decompress(bytes(improve_boss.cut(c, tm_settings())["body_gz"])).decode().lower()
    for name in PRIVATE_MODELS:
        assert name not in body
        for piece in re.findall(r"[a-z]{5,}", name):
            if piece not in ("claude", "llama", "gemini"):
                assert piece not in body, f"{piece!r} of {name!r} leaked"
    assert '"other"' in body


def test_public_ids_are_exact():
    from boss_engine.services.improve_allowlist import public_model

    assert [public_model(m) for m in PRIVATE_MODELS] == ["other"] * 5
    assert public_model("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"
    assert public_model("openai/gpt-5.1-codex") == "gpt-5.1-codex"
    assert public_model("qwen2.5-coder:7b") == "qwen2.5-coder:7b"


# ------------------------------------------------------------------ P2: one choke point
@pytest.mark.parametrize("env", [{"telemetry": "off"}, {"do_not_track": "1"},
                                 {"telemetry": "disable"}, {"telemetry_url": ""}])
def test_no_path_sends_when_off_or_without_a_collector(engine, connects, env):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _switch(engine, "improve", True)
    settings = tm_settings(**env)
    with engine.begin() as c:
        rid = seed_build(c, status="failed")
        improve_boss._put(c, "improve_identity", {**improve_boss.identity(c), "sent": True})
        improve_boss._put(c, "improve_schedule", {"next_due_at": "2020-01-01T00:00:00+00:00"})
    improve_boss.tick(engine, settings)
    improve_boss.share_send(engine, settings, bundle="x", note="y")
    improve_boss.forget(engine, settings, actor="ceo", project_id=project())
    improve_boss.rotate(engine, settings, actor="ceo", project_id=project())
    assert connects == []
    with engine.begin() as c:
        statuses = c.execute(text("SELECT kind, status FROM telemetry_sends WHERE status <> 'pending'")).all()
        # The old id was KEPT (its delete could not go), never silently forgotten.
        assert len(improve_boss._retired(c)) >= 1
    assert all(s == "failed" for _k, s in statuses)


def test_the_routes_say_why_and_send_nothing(engine, client, connects, monkeypatch):
    from apps.api.app import main as api_main

    reset_state(engine)
    monkeypatch.setattr(api_main.settings, "telemetry", "off")
    for path, body in (("/v1/telemetry/forget", {}), ("/v1/telemetry/share-run", {"bundle": "x"})):
        r = client.post(path, json={"project_id": project(), "actor": "ceo", **body})
        assert r.status_code == 409 and "BOSS_TELEMETRY=off" in r.text
    monkeypatch.setattr(api_main.settings, "telemetry", "")
    monkeypatch.setattr(api_main.settings, "telemetry_url", "")
    view = client.get(f"/v1/telemetry/sharing?project_id={project()}").json()
    assert view["blocked"] == "No collector set" and view["url_source"] == "none"
    r = client.post("/v1/telemetry/share-run", json={"project_id": project(), "actor": "ceo", "bundle": "x"})
    assert r.status_code == 409 and "No collector set" in r.text
    assert connects == []


def test_the_collector_url_is_set_in_govern_and_validated(engine, client, monkeypatch):
    from apps.api.app import main as api_main

    reset_state(engine)
    monkeypatch.setattr(api_main.settings, "telemetry_url", "")
    put = lambda url: client.put("/v1/telemetry/sharing", json={"project_id": project(),  # noqa: E731
                                                                "actor": "ceo", "url": url})
    assert put("http://collector.example.com/v1/telemetry/ingest").status_code == 422
    assert put("https://user:pw@collector.example.com/v1/telemetry/ingest").status_code == 422
    ok = put("https://collector.example.com/v1/telemetry/ingest")
    assert ok.status_code == 200 and ok.json()["url_source"] == "govern" and ok.json()["blocked"] is None
    monkeypatch.setattr(api_main.settings, "telemetry_url", "https://env.example.com/v1/telemetry/ingest")
    assert put("https://other.example.com/v1/telemetry/ingest").status_code == 409


def test_the_off_words_are_one_list():
    from boss_engine.services.improve_boss import DNT_ON_WORDS, TELEMETRY_OFF_WORDS

    sh = (ROOT / "scripts/install/checkup.sh").read_text()
    tel = re.search(r'case "\$_tel" in ([^)]+)\)', sh).group(1).split("|")
    dnt = re.search(r'case "\$_dnt" in ([^)]+)\)', sh).group(1).split("|")
    assert tuple(tel) == TELEMETRY_OFF_WORDS and tuple(dnt) == DNT_ON_WORDS


def test_no_private_hostname_ships():
    for f in ("README.md", ".env.example", "scripts/install/community-compose.yml",
              "engine/boss_engine/config.py", "docs/product/RUN_SCORECARDS.md"):
        assert "sean.network/v1/telemetry" not in (ROOT / f).read_text(), f


# ------------------------------------------------------------------ P3: nothing before consent
def test_nothing_from_before_consent_or_from_an_off_period(engine):
    from boss_engine.services import improve_boss, person_feedback, scorecards

    reset_state(engine)
    with engine.begin() as c:
        before = seed_build(c)
        scorecards.sweep(c)
        person_feedback.record(c, project_id=project(), actor="ceo", subject_kind="build",
                               subject_id=before, rating="no", chips=["too_slow"])
        person_feedback.checkin(c, project_id=project(), actor="ceo", answer="very",
                                line="written while off", share_line=True)
    time.sleep(0.05)
    _switch(engine, "improve", True)
    with engine.begin() as c:
        row = improve_boss.cut(c, tm_settings())
        body = improve_boss.decode(row["body_gz"])
        consent = improve_boss.consent_moment(c)
    batch = body["batches"][0]
    assert batch["since"] == consent.date().isoformat()
    assert batch["scorecards"] == {"rows": [], "agg": []}
    assert batch["feedback"]["ratings"] == {} and batch["feedback"]["lines"] == []
    # Off, work, on again: the off period's work is never sent.
    _switch(engine, "improve", False)
    with engine.begin() as c:
        during_off = seed_build(c)
        scorecards.sweep(c)
    time.sleep(0.05)
    _switch(engine, "improve", True)
    with engine.begin() as c:
        again = improve_boss.decode(improve_boss.cut(c, tm_settings())["body_gz"])
        ref = improve_boss._ref(improve_boss.identity(c)["secret"], during_off)
    assert ref not in json.dumps(again)
    assert again["batches"][0]["scorecards"]["rows"] == []


# ------------------------------------------------------------------ P4: rotate deletes first
def test_rotate_deletes_the_old_id_or_keeps_it_and_says_so(engine):
    from boss_engine.services import improve_boss

    reset_state(engine)
    posts: list[str] = []

    def ok(url, body):
        posts.append(json.loads(gzip.decompress(body))["install_id"])
        return 200, None

    with engine.begin() as c:
        first = improve_boss.identity(c)
        improve_boss._put(c, "improve_identity", {**first, "sent": True})
    down = lambda url, body: (None, "timeout")  # noqa: E731
    kept = improve_boss.rotate(engine, tm_settings(), actor="ceo", project_id=project(), post=down)
    assert kept["old"] == "kept" and kept["waiting"] == 1
    with engine.begin() as c:
        second = improve_boss.identity(c)
        improve_boss._put(c, "improve_identity", {**second, "sent": True})
    gone = improve_boss.forget(engine, tm_settings(), actor="ceo", project_id=project(), post=ok)
    assert gone["deleted"] is True and gone["ids_deleted"] == 2
    assert set(posts) == {first["id"], second["id"]}  # every id the install used
    with engine.begin() as c:
        assert improve_boss._retired(c) == []


# ------------------------------------------------------------------ P5: no client address logged
def test_the_access_log_names_no_client_for_the_collector():
    from apps.api.app.routers.improve import install_access_log_filter

    install_access_log_filter()
    log = logging.getLogger("uvicorn.access")
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("203.0.113.9:5555", "POST", "/v1/telemetry/ingest", "1.1", 200), None)
    for f in log.filters:
        f.filter(rec)
    assert "203.0.113" not in rec.getMessage() and rec.getMessage().startswith("- ")
    other = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                              ("203.0.113.9:5555", "GET", "/v1/govern", "1.1", 200), None)
    for f in log.filters:
        f.filter(other)
    assert "203.0.113.9" in other.getMessage()


# ------------------------------------------------------------------ share scrub
def test_a_shared_run_masks_hosts_urls_paths_repos_and_names_but_keeps_times(engine):
    from boss_engine.services import improve_boss

    reset_state(engine)
    with engine.begin() as c:
        rid = seed_build(c, status="failed", spec={"role_key": "software_engineer",
                                                    "repo": "acmezqx-billing", "owner": "acmezqx"})
        c.execute(text("UPDATE code_task_runs SET error = :e WHERE id = :id"), {"id": rid, "e": (
            "boom at https://git.acmezqx.io/x/y on db-01.acmezqx.internal in /srv/acmezqx/pay/ledger.py "
            "and acmezqx-billing for priya@acmezqx.com, project boss-dogfood")})
        b = improve_boss.share_bundle(c, project(), "build", rid)["bundle"]
    for leak in ("acmezqx", "db-01", "/srv/", "ledger.py", "priya@", "boss-dogfood"):
        assert leak not in b, leak
    assert "[PHONE]" not in b and re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", b)


# ------------------------------------------------------------------ D1: streamed cap
def test_the_collector_never_reads_more_than_its_cap():
    from apps.api.app.routers.improve import read_capped
    from boss_engine.services.improve_collector import CollectorError

    served = {"bytes": 0}

    class Req:
        headers = {}

        async def stream(self):
            chunk = b"x" * 65536
            for _ in range(150 * 16):  # 150 MB, lazily
                served["bytes"] += len(chunk)
                yield chunk

    tracemalloc.start()
    with pytest.raises(CollectorError) as got:
        asyncio.run(read_capped(Req(), 256 * 1024))
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert got.value.status == 413
    assert served["bytes"] <= 256 * 1024 + 65536
    assert peak < 4 * 1024 * 1024


def test_a_chunked_oversize_post_is_refused(engine, client):
    from boss_engine.services.improve_boss import set_switch

    reset_state(engine)
    with engine.begin() as c:
        set_switch(c, "collector", True, actor="ceo", project_id=project())

    def body():
        for _ in range(40):
            yield b"x" * 65536

    r = client.post("/v1/telemetry/ingest", content=body(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


# ------------------------------------------------------------------ D2 + D12: coarsen, never cut
def test_9720_groups_coarsen_until_they_fit_and_cover_every_unit(engine):
    from boss_engine.services import improve_boss

    reset_state(engine)
    roles = ["software_engineer", "backend_engineer", "frontend_engineer", "qa_engineer",
             "devops_engineer", "product_manager", "designer", "security_engineer",
             "data_engineer", "analytics_engineer", "customer_support", "cfo"]
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO run_scorecards (unit_kind, unit_id, project_id, role_key, harness, model,
                                        workflow_key, terminal_state, experiment_arm, cost_usd,
                                        turns, steers, score, score_version, ended_at, computed_at)
            SELECT 'build', 'grp_' || g, :p, (CAST(:roles AS TEXT[]))[1 + g % 12],
                   (ARRAY['claude_code','codex','native'])[1 + (g / 12) % 3],
                   (ARRAY['claude-sonnet-5','gpt-5-codex','claude-opus-4-1','gpt-4o','o3','gemini-2.5-pro'])[1 + (g / 36) % 6],
                   (ARRAY['standard_product_delivery','minor_feature','major_feature','fast_fix','research_led_discovery'])[1 + (g / 216) % 5],
                   (ARRAY['completed','failed','cancelled'])[1 + (g / 1080) % 3],
                   (ARRAY[NULL,'a','b'])[1 + (g / 3240) % 3],
                   1, 10, 0, (g % 100) / 100.0, '1', now(), now()
            FROM generate_series(0, 49999) AS g"""), {"p": project(), "roles": roles})
        distinct = c.execute(text("""SELECT COUNT(*) FROM (SELECT DISTINCT role_key, harness, model,
            workflow_key, terminal_state, experiment_arm FROM run_scorecards WHERE unit_id LIKE 'grp_%') x""")).scalar()
        assert distinct == 9720
        tracemalloc.start()
        t0 = time.perf_counter()
        batch = improve_boss.build_batch(c, tm_settings(), day=datetime.now(timezone.utc).date(),
                                         since=None, until=datetime.now(timezone.utc))
        took = time.perf_counter() - t0
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        c.execute(text("DELETE FROM run_scorecards WHERE unit_id LIKE 'grp_%'"))
    note = batch["aggregated"]["scorecards"]
    agg = batch["scorecards"]["agg"]
    assert note["level"] >= 2 and len(agg) <= 400 and note["groups"] == len(agg)
    assert note["covered"] == note["from_rows"] == 50000 == sum(g["n"] for g in agg)
    assert took < 1.5, f"{took:.2f}s"
    assert peak < 16 * 1024 * 1024, f"{peak / 1e6:.1f} MB"


# ------------------------------------------------------------------ D4, D5, D7, D8, D11
def test_the_view_never_goes_blank_on_a_weights_change(engine, client):
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        scorecards.record_scorecard(c, "build", seed_build(c))
        # A row scored under an older version only (as after an interrupted rescore):
        c.execute(text("UPDATE run_scorecards SET score_version = '0.9'"))
    v = client.get(f"/v1/scorecards?project_id={project()}").json()
    assert v["total"]["n"] == 1 and v["score_version"] == "0.9"


def test_a_panel_rated_from_its_room_lands_on_the_meeting_run(engine, client):
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        mrun, mid = seed_meeting(c)
        scorecards.record_scorecard(c, "panel", mrun)
    r = client.post("/v1/feedback", json={"project_id": project(), "actor": "ceo",
                                          "subject_kind": "panel", "subject_id": mid, "rating": "wrong"})
    assert r.status_code == 200
    with engine.begin() as c:
        assert cards(c, mrun)[0]["rating"] == "wrong"
        assert c.execute(text("SELECT subject_id FROM person_feedback")).scalar() == mrun
    mine = client.get(f"/v1/feedback/prompts?project_id={project()}&actor=ceo&subjects=panel:{mid}").json()
    assert mine["mine"][f"panel:{mid}"]["rating"] == "wrong"


def test_a_step_cancelled_before_it_started_has_no_scorecard_live_or_backfilled(engine):
    from boss_engine.services import scorecards
    from boss_engine.services.workflows import cancel_run

    reset_state(engine)
    with engine.begin() as c:
        wf, steps = seed_workflow_run(c, status="running")
        c.execute(text("UPDATE workflow_step_runs SET started_at = NULL WHERE id = :id"), {"id": steps[1]})
        cancel_run(c, project(), wf, "ceo", reason="no", compensate=False)
        c.execute(text("UPDATE workflow_step_runs SET status = 'cancelled' WHERE workflow_run_id = :r"), {"r": wf})
        assert scorecards.record_scorecard(c, "step", steps[1]) is None
        scorecards.sweep(c)
        assert cards(c, steps[1]) == []
        assert len(cards(c, steps[0])) == 1  # it had started: it is scored


def test_a_build_inside_a_workflow_carries_its_workflow_and_agent(engine):
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        wf, _ = seed_workflow_run(c, status="running")
        rid = seed_build(c, spec={"repo": "x"}, finalize={"workflow_run_id": wf})
        c.execute(text("INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, role_key, direction, content) "
                       "VALUES (:i, :p, :r, 'implement', 'backend_engineer', 'prompt', 'x')"),
                  {"i": uid("turn"), "p": project(), "r": rid})
        card = scorecards.record_scorecard(c, "build", rid)
    assert card["workflow_key"].startswith("standard_product_delivery") and card["role_key"] == "backend_engineer"


def test_whats_costing_you_lists_only_low_scores(engine, client):
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        for _ in range(3):
            scorecards.record_scorecard(c, "build", seed_build(c))  # good builds
    v = client.get(f"/v1/scorecards?project_id={project()}").json()
    assert [x for x in v["costing"] if x["why"] == "low"] == []
    assert v["rows"][0]["label"] != v["rows"][0]["key"] or "_" not in v["rows"][0]["label"]


# ------------------------------------------------------------------ D10, D17, D18
def test_the_rate_limit_counts_the_real_peer_and_new_ids_are_capped(engine, client):
    from apps.api.app.routers.improve import client_ip
    from boss_engine.services import improve_collector
    from boss_engine.services.improve_boss import set_switch

    trusted = "127.0.0.0/8,10.0.0.0/8"
    assert client_ip("203.0.113.7", "1.1.1.1", trusted) == "203.0.113.7"  # spoof ignored
    assert client_ip("10.0.0.5", "9.9.9.9, 203.0.113.8", trusted) == "203.0.113.8"
    reset_state(engine)
    with engine.begin() as c:
        set_switch(c, "collector", True, actor="ceo", project_id=project())
    day = datetime.now(timezone.utc).date().isoformat()
    for i in range(6):
        iid = "bi_" + "abcdefghij"[i] * 26
        env = {"v": 1, "day": day, "install_id": iid,
               "heartbeat": {"install_id": iid, "version": "1.0.0", "os": "linux"}}
        r = client.post("/v1/telemetry/ingest", content=gzip.compress(json.dumps(env).encode()),
                        headers={"Content-Encoding": "gzip", "X-Forwarded-For": f"198.51.100.{i}"})
        assert r.status_code == 200
    with engine.begin() as c:
        sus = c.execute(text("SELECT COUNT(*) FROM tm_installs WHERE suspicious")).scalar()
        view = improve_collector.dashboard(c)
    assert sus == 3  # the forged X-Forwarded-For did not make six addresses out of one
    assert view["active"]["daily"] == 3 and view["held_back"] == 3


def test_the_check_in_api_refuses_a_second_answer_in_a_month(engine, client):
    reset_state(engine)
    body = {"project_id": project(), "actor": "ceo", "answer": "very"}
    assert client.post("/v1/feedback/checkin", json=body).status_code == 200
    assert client.post("/v1/feedback/checkin", json=body).status_code == 409


def test_after_delete_re_enabling_the_same_day_sends_from_that_moment(engine):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _switch(engine, "improve", True)
    sent: list = []
    ok = lambda url, body: (sent.append(json.loads(gzip.decompress(body))), (200, None))[1]  # noqa: E731
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        improve_boss._put(c, "improve_schedule", {"next_due_at": (now - timedelta(minutes=5)).isoformat()})
    improve_boss.tick(engine, tm_settings(), post=ok, now=now)
    assert len(sent) == 1
    improve_boss.forget(engine, tm_settings(), actor="ceo", project_id=project(), post=ok)
    _switch(engine, "improve", False)
    _switch(engine, "improve", True)
    with engine.begin() as c:
        improve_boss._put(c, "improve_schedule", {**improve_boss._schedule(c),
                                                  "next_due_at": (now - timedelta(minutes=1)).isoformat()})
    got = improve_boss.tick(engine, tm_settings(), post=ok, now=now + timedelta(minutes=10))
    daily = [s for s in sent if s.get("v") == 1]
    assert got["send"]["sent"] is True and len(daily) == 2
    with engine.begin() as c:
        consent = improve_boss.consent_moment(c)
    assert daily[-1]["batches"][0]["since"] == consent.date().isoformat()
    assert daily[-1]["install_id"] != daily[0]["install_id"]
