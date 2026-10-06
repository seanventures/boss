"""Improve BOSS and the daily heartbeat: the honesty tests (docs/product/RUN_SCORECARDS.md).

  * switch off + heartbeat opted out, BOSS_TELEMETRY=off, DO_NOT_TRACK=1 → ZERO connections
    (counted at the socket layer, so no HTTP client can slip past);
  * heartbeat only → exactly the documented fields;
  * seeded prompts, code, briefs, repo/file/user names, emails and secrets → never in a payload,
    whole or as an 8-character piece;
  * the preview is what is sent, byte for byte;
  * a failed send is not retried before the next day; at most 7 unsent batches;
  * the size cap aggregates instead of truncating;
  * 50,000 scorecards build a batch inside a small time and memory budget.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import socket
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from apps.api.tests.improve_fixtures import (project, reset_state, seed_build, tm_settings, uid)

HEARTBEAT_FIELDS = {"install_id", "version", "edition", "install_method", "os", "arch", "age",
                    "worked_24h"}


@pytest.fixture
def connects(monkeypatch):
    """Every outbound connection attempt made through Python's socket layer, refused."""
    seen: list = []

    def refuse(self, address, *a, **k):
        seen.append(address)
        raise ConnectionRefusedError("test: no network")

    def refuse_create(address, *a, **k):
        seen.append(address)
        raise ConnectionRefusedError("test: no network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, a: (seen.append(a), 111)[1])
    monkeypatch.setattr(socket, "create_connection", refuse_create)
    return seen


def _due(engine, when: datetime) -> None:
    from boss_engine.services.improve_boss import _get, _put

    with engine.begin() as c:
        _put(c, "improve_schedule", {**_get(c, "improve_schedule"),
                                     "next_due_at": when.isoformat()})


def _switch(engine, name: str, on: bool) -> None:
    from boss_engine.services.improve_boss import set_switch

    with engine.begin() as c:
        set_switch(c, name, on, actor="ceo", project_id=project())


def _sends(engine) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT * FROM telemetry_sends ORDER BY cut_at")).mappings().all()]


NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize("env", [
    {},                                   # switch off (default) + heartbeat opted out
    {"telemetry": "off"},                 # BOSS_TELEMETRY=off, both switches on
    {"do_not_track": "1"},                # DO_NOT_TRACK=1, both switches on
])
def test_off_means_zero_network_calls(engine, connects, env):
    from boss_engine.services import improve_boss

    reset_state(engine)
    if env:
        _switch(engine, "improve", True)  # even with everything on, the env wins
    else:
        _switch(engine, "heartbeat", False)
    _due(engine, NOW - timedelta(hours=1))
    for i in range(3):
        improve_boss.tick(engine, tm_settings(**env), now=NOW + timedelta(days=i))
    assert connects == []
    assert _sends(engine) == []  # nothing was even cut
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM telemetry_batches")).scalar() == 0
        assert improve_boss.preview(c, tm_settings(**env))["pending"] is None


def test_the_socket_guard_does_see_a_real_send(engine, connects):
    """The control for the test above: with the heartbeat on, the default client does connect."""
    from boss_engine.services import improve_boss

    reset_state(engine)
    _due(engine, NOW - timedelta(minutes=1))
    got = improve_boss.tick(engine, tm_settings(), now=NOW)
    assert connects, got
    assert got["send"]["sent"] is False
    assert _sends(engine)[-1]["status"] == "failed"


def test_heartbeat_only_carries_exactly_the_documented_fields(engine):
    from boss_engine.services import improve_boss

    reset_state(engine)
    with engine.begin() as c:
        seed_build(c)  # work in the last 24h
        row = improve_boss.cut(c, tm_settings())
        body = improve_boss.decode(row["body_gz"])
    assert set(body) == {"v", "day", "install_id", "secret_hash", "heartbeat"}
    assert set(body["heartbeat"]) == HEARTBEAT_FIELDS
    hb = body["heartbeat"]
    assert hb["install_id"] == body["install_id"] and hb["install_id"].startswith("bi_")
    assert hb["edition"] == "community" and hb["worked_24h"] is True
    assert hb["age"] in ("0-1", "2-7", "8-30", "31-90", "91-180", "181-365", "365+")
    assert len(body["secret_hash"]) == 64


# Seeds that must never travel. Each is distinctive, so any 8-character piece of it in a
# payload can only have come from the seed.
SECRET = "sk-ant-api03-ZqxW9vLmT4rB7nQ2pK5sJ8hF1dG6cY3e"
GITHUB = "ghp_Zqx1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7"
EMAIL = "priya.zqxraman@acmecorp-internal.example"
SEEDS = [SECRET, GITHUB, EMAIL, "Priya Zqxraman", "acmezqx-billing-core",
         "/srv/acmezqx/payroll/ledger.py", "zqxFunctionThatLeaks", "ZqxBriefLaunchPlan",
         "zqx_growth_hacker", "zqx_launch_flow", "zqx-llama-internal", "zqx_billing_skill",
         "ZqxPromptInstruction"]


def _seed_content_everywhere(c, n_builds: int) -> None:
    for i in range(n_builds):
        rid = seed_build(
            c, title=f"ZqxBriefLaunchPlan for acmezqx-billing-core #{i}",
            model="zqx-llama-internal",
            spec={"role_key": "zqx_growth_hacker", "repo": "acmezqx-billing-core",
                  "instructions": f"ZqxPromptInstruction: edit /srv/acmezqx/payroll/ledger.py "
                                  f"def zqxFunctionThatLeaks(): token={SECRET} mail {EMAIL}"},
            state={"build_engine": "claude_code", "model": "zqx-llama-internal",
                   "impl_summary": f"wrote zqxFunctionThatLeaks with {GITHUB}",
                   "tests": [{"command": "pytest /srv/acmezqx/payroll/ledger.py", "ok": i % 2 == 0}]},
            finalize={"failure_class": "zqx_custom_error_by_Priya Zqxraman",
                      "origin": {"repo": "acmezqx-billing-core"}},
            status="failed" if i % 3 == 0 else "completed")
        c.execute(text("""
            INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, role_key, direction,
                                     content)
            VALUES (:id, :p, :r, 'implement', 'zqx_growth_hacker', 'prompt', :t)"""),
            {"id": uid("turn"), "p": project(), "r": rid,
             "t": f"ZqxPromptInstruction {SECRET} {EMAIL} Priya Zqxraman"})
    c.execute(text("""
        INSERT INTO role_skills (id, project_id, role_key, skill_id)
        VALUES (:id, :p, 'zqx_growth_hacker', 'zqx_billing_skill') ON CONFLICT DO NOTHING"""),
        {"id": uid("rs"), "p": project()})
    from boss_engine.services import person_feedback

    person_feedback.record(c, project_id=project(), actor="ceo", subject_kind="build",
                           subject_id=rid, rating="no", chips=["misread_brief"])
    person_feedback.checkin(c, project_id=project(), actor="ceo", answer="very",
                            line=f"fix the {SECRET} thing, mail {EMAIL}", share_line=True)
    from boss_engine.services import scorecards

    scorecards.sweep(c, limit=1000)


def _assert_no_leak(payload_text: str) -> None:
    lower = payload_text.lower()
    for seed in SEEDS:
        s = seed.lower()
        assert s not in lower, f"leaked whole: {seed}"
        for i in range(0, len(s) - 7):
            piece = s[i:i + 8]
            if "zqx" in piece or piece in (SECRET.lower() + GITHUB.lower()):
                assert piece not in lower, f"leaked piece {piece!r} of {seed!r}"


@pytest.mark.parametrize("n_builds", [6, 230])  # per-run rows, then aggregates
def test_no_content_leaves_whole_or_in_pieces(engine, n_builds):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _switch(engine, "improve", True)
    with engine.begin() as c:
        _seed_content_everywhere(c, n_builds)
        row = improve_boss.cut(c, tm_settings())
        body = improve_boss.decode(row["body_gz"])
    raw = gzip.decompress(bytes(row["body_gz"])).decode()
    _assert_no_leak(raw)
    batch = body["batches"][0]
    cards_block = batch["scorecards"]
    if n_builds > 200:
        assert cards_block["rows"] == [] and cards_block["agg"]
        assert batch["aggregated"]["scorecards"]["reason"] == "rows_over_threshold"
        assert {g["role_key"] for g in cards_block["agg"] if "role_key" in g} <= {"custom"}
    else:
        assert cards_block["rows"]
        assert all(r.get("role_key") in (None, "custom") for r in cards_block["rows"])
        assert all(r.get("model") in (None, "other") for r in cards_block["rows"])
        assert all(r.get("error_class") in (None, "other") for r in cards_block["rows"])
    # The one free-text field the person chose to send is masked, not dropped.
    assert batch["feedback"]["lines"] and "[" in batch["feedback"]["lines"][0]
    assert batch["feedback"]["ratings"]["build"]["no"] == 1


def test_fields_outside_the_allow_list_are_dropped():
    from boss_engine.services import improve_allowlist as allow

    clean, dropped = allow.sanitize({
        "v": 1, "day": "2026-10-05", "install_id": "bi_" + "a" * 26, "secret_hash": "0" * 64,
        "hostname": "acme-prod-01", "heartbeat": {"version": "1.0.0", "os": "linux",
                                                  "ip": "10.0.0.7", "age": "0-1"},
        "batches": [{"day": "2026-10-05", "repo": "acme/secret", "scale": {"projects": "1",
                                                                         "names": ["x"]}}]})
    assert "hostname" not in clean and "ip" not in clean["heartbeat"]
    assert clean["batches"][0] == {"day": "2026-10-05", "scale": {"projects": "1"}}
    assert dropped >= 4


def test_the_preview_is_what_is_sent_byte_for_byte(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _switch(engine, "improve", True)
    with engine.begin() as c:
        seed_build(c)
    shown = client.get(f"/v1/telemetry/preview?project_id={project()}")
    assert shown.status_code == 200, shown.text
    pending = shown.json()["pending"]
    with engine.begin() as c:
        seed_build(c)  # arrives after the cut: belongs to tomorrow's batch
    posted: list[bytes] = []
    _due(engine, NOW - timedelta(minutes=1))
    got = improve_boss.tick(engine, tm_settings(),
                            post=lambda url, body: (posted.append(body), (200, None))[1], now=NOW)
    assert got["send"]["sent"] is True and len(posted) == 1
    assert hashlib.sha256(posted[0]).hexdigest() == pending["sha256"]
    assert json.loads(gzip.decompress(posted[0])) == pending["json"]
    with engine.begin() as c:
        assert improve_boss.sent_body(c, pending["id"]) == pending["json"]


def test_a_failed_send_waits_for_the_next_day(engine):
    from boss_engine.services import improve_boss

    reset_state(engine)
    calls: list[str] = []

    def failing(url, body):
        calls.append(url)
        return 503, "http_503"

    _due(engine, NOW - timedelta(minutes=1))
    improve_boss.tick(engine, tm_settings(), post=failing, now=NOW)
    for minutes in (10, 60, 240):  # later the same day: nothing
        improve_boss.tick(engine, tm_settings(), post=failing, now=NOW + timedelta(minutes=minutes))
    assert len(calls) == 1
    with engine.begin() as c:
        sched = improve_boss._schedule(c)
    next_due = datetime.fromisoformat(sched["next_due_at"])
    assert next_due.date() > NOW.date()
    improve_boss.tick(engine, tm_settings(), post=failing, now=next_due + timedelta(minutes=1))
    assert len(calls) == 2
    statuses = [s["status"] for s in _sends(engine)]
    assert statuses == ["failed", "failed"]
    assert {s["error"] for s in _sends(engine)} == {"http_503"}


def test_at_most_seven_unsent_batches_ride_along(engine):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _switch(engine, "improve", True)
    bodies: list[dict] = []

    def failing(url, body):
        bodies.append(json.loads(gzip.decompress(body)))
        return None, "timeout"

    when = NOW
    for _day in range(10):
        _due(engine, when - timedelta(minutes=1))
        improve_boss.tick(engine, tm_settings(), post=failing, now=when)
        when = when + timedelta(days=1)
    assert len(bodies) == 10
    assert all(len(b["batches"]) <= 7 for b in bodies)
    assert len(bodies[-1]["batches"]) == 7 and bodies[-1]["dropped_batches"] == 3
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM telemetry_batches WHERE status = 'dropped'")
                         ).scalar() == 3
    # A delivery marks them sent; the next envelope starts clean.
    _due(engine, when - timedelta(minutes=1))
    improve_boss.tick(engine, tm_settings(), post=lambda u, b: (200, None), now=when)
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM telemetry_batches WHERE status = 'unsent'")
                         ).scalar() == 0


def test_the_size_cap_aggregates_and_says_so(engine, monkeypatch):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _switch(engine, "improve", True)
    with engine.begin() as c:
        for _ in range(40):
            seed_build(c)
        from boss_engine.services import scorecards

        scorecards.sweep(c)
    with engine.begin() as c:
        # The cap, set just under what the per-run rows compress to.
        whole = improve_boss.build_batch(c, tm_settings(), day=NOW.date(), since=None,
                                         until=datetime.now(timezone.utc))
        cap = len(improve_boss.encode({"batches": [whole]})[1]) - 200
    monkeypatch.setattr(improve_boss, "SIZE_CAP_GZ", cap)
    with engine.begin() as c:
        row = improve_boss.cut(c, tm_settings())
        body = improve_boss.decode(row["body_gz"])
    batch = body["batches"][0]
    assert batch["scorecards"]["rows"] == [] and batch["scorecards"]["agg"]
    note = batch["aggregated"]["scorecards"]
    assert note["reason"] == "size_cap" and note["from_rows"] == 40 and note["level"] >= 1
    assert sum(g["n"] for g in batch["scorecards"]["agg"]) == 40  # nothing silently lost
    assert row["aggregated"]  # recorded on the send too (the local log shows it)


def test_revoking_drops_what_was_not_sent_and_the_switch_is_logged(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    r = client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                                   "switch": "improve", "on": True})
    assert r.status_code == 200 and r.json()["switches"]["improve"]["on"] is True
    assert r.json()["switches"]["improve"]["by"] == "ceo"
    with engine.begin() as c:
        assert improve_boss.decode(improve_boss.cut(c, tm_settings())["body_gz"]).get("batches")
    client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                              "switch": "improve", "on": False})
    with engine.begin() as c:
        body = improve_boss.decode(improve_boss.cut(c, tm_settings())["body_gz"])
        assert "batches" not in body
        assert c.execute(text("SELECT COUNT(*) FROM telemetry_batches WHERE status = 'unsent'")
                         ).scalar() == 0
        changes = c.execute(text("SELECT actor, data FROM events WHERE event_type = "
                                 "'settings.changed' AND entity_id = 'improve_boss' "
                                 "ORDER BY created_at")).all()
    assert [(a, d["to"]) for a, d in changes] == [("ceo", "on"), ("ceo", "off")]


def test_rotate_export_and_the_sends_log(engine, client):
    reset_state(engine)
    first = client.get(f"/v1/telemetry/sharing?project_id={project()}").json()["install_id"]
    exported = client.get(f"/v1/telemetry/export?project_id={project()}&actor=ceo")
    assert exported.status_code == 200
    assert json.loads(exported.content)["install_id"] == first
    rotated = client.post("/v1/telemetry/rotate", json={"project_id": project(), "actor": "ceo"})
    assert rotated.status_code == 200 and rotated.json()["install_id"] != first
    view = client.get(f"/v1/telemetry/sharing?project_id={project()}").json()
    assert [s["kind"] for s in view["sends"]] == ["export"]
    assert view["switches"]["heartbeat"]["on"] is True  # on by default
    assert view["switches"]["improve"]["on"] is False   # off by default


def test_building_a_batch_on_fifty_thousand_scorecards_is_cheap(engine):
    """The CPU and memory budget: SQL aggregates, never 50k rows in Python."""
    from boss_engine.services import improve_boss

    reset_state(engine)
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO run_scorecards (unit_kind, unit_id, project_id, role_key, harness, model,
                                        workflow_key, terminal_state, cost_usd, turns, steers,
                                        score, score_version, ended_at, computed_at, pr_merged,
                                        gates_failed)
            SELECT 'build', 'bench_' || g, :p,
                   (ARRAY['software_engineer','backend_engineer','zqx_custom'])[1 + g % 3],
                   (ARRAY['claude_code','codex'])[1 + g % 2],
                   (ARRAY['claude-sonnet-5','gpt-5-codex','zqx-local'])[1 + g % 3],
                   'standard_product_delivery',
                   (ARRAY['completed','failed','cancelled'])[1 + g % 3],
                   (g % 97) / 10.0, g % 60, g % 3, (g % 100) / 100.0, '1',
                   now() - (g || ' seconds')::interval, now() - (g || ' seconds')::interval,
                   g % 4 = 0, g % 5
            FROM generate_series(1, 50000) AS g"""), {"p": project()})
    with engine.begin() as c:
        tracemalloc.start()
        started = time.perf_counter()
        batch = improve_boss.build_batch(c, tm_settings(), day=NOW.date(), since=None,
                                         until=datetime.now(timezone.utc))
        elapsed = time.perf_counter() - started
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        c.execute(text("DELETE FROM run_scorecards WHERE unit_id LIKE 'bench_%'"))
    groups = batch["scorecards"]["agg"]
    assert sum(g["n"] for g in groups) == 50_000
    assert len(gzip.compress(json.dumps(batch).encode())) < improve_boss.SIZE_CAP_GZ
    assert elapsed < 1.5, f"batch took {elapsed:.2f}s"
    assert peak < 16 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"
