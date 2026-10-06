"""The independent re-check of tm/fixes (2026-10-05): a delete that sticks, shared runs that can
be deleted and a screen that reports what was really removed, masking of any host / IPv6 / spec
name / chosen word, loopback-only trusted proxies. Each test reproduces its finding."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.tests.improve_fixtures import project, reset_state, seed_build, tm_settings


def _collector_on(engine):
    from boss_engine.services.improve_boss import set_switch

    with engine.begin() as c:
        set_switch(c, "collector", True, actor="ceo", project_id=project())


def _via(client):
    """A poster that delivers to THIS app's collector routes and returns its JSON answer."""
    def post(url, body):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        r = client.post(path, content=body, headers={"Content-Type": "application/json",
                                                      "Content-Encoding": "gzip"})
        try:
            answer = r.json()
        except ValueError:
            answer = None
        return r.status_code, None if r.status_code < 300 else f"http_{r.status_code}", answer
    return post


def _due(engine, when):
    from boss_engine.services import improve_boss

    with engine.begin() as c:
        improve_boss._put(c, "improve_schedule", {**improve_boss._schedule(c),
                                                  "next_due_at": (when - timedelta(minutes=1)).isoformat()})


# ------------------------------------------------------------------ 1: a delete sticks
def test_delete_is_never_undone_by_the_next_daily_send(engine, client):
    from boss_engine.services import improve_boss, scorecards
    from boss_engine.services.improve_boss import set_switch

    reset_state(engine)
    _collector_on(engine)
    post = _via(client)
    with engine.begin() as c:
        set_switch(c, "improve", True, actor="ceo", project_id=project())
    with engine.begin() as c:
        before = seed_build(c)
        scorecards.sweep(c)
    now = datetime.now(timezone.utc)
    _due(engine, now)
    assert improve_boss.tick(engine, tm_settings(), post=post, now=now)["send"]["sent"] is True
    with engine.begin() as c:
        old_ref = improve_boss._ref(improve_boss.identity(c)["secret"], before)
    gone = improve_boss.forget(engine, tm_settings(), actor="ceo", project_id=project(), post=post)
    assert gone["deleted"] is True and gone["removed"] >= 2
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tm_batches")).scalar() == 0
        delete_at = improve_boss.consent_moment(c)
    # The next send, same day: nothing from before the delete may arrive again.
    later = now + timedelta(minutes=30)
    _due(engine, later)
    sent: list[dict] = []

    def capture(url, body):
        sent.append(json.loads(gzip.decompress(body)))
        return post(url, body)

    assert improve_boss.tick(engine, tm_settings(), post=capture, now=later)["send"]["sent"] is True
    batch = sent[-1]["batches"][0]
    assert batch["scorecards"] == {"rows": [], "agg": []}
    assert batch["since"] >= delete_at.date().isoformat()
    assert old_ref not in json.dumps(sent[-1])
    with engine.begin() as c:
        stored = c.execute(text("SELECT body FROM tm_batches")).scalars().all()
    assert all(not b["scorecards"]["rows"] for b in stored)


# ------------------------------------------------------------------ 2: shared runs are deletable
def _share(engine, client):
    from boss_engine.services import improve_boss

    got = improve_boss.share_send(engine, tm_settings(), bundle="build failed: tests", note="n",
                                  post=_via(client))
    assert got["sent"] is True
    with engine.begin() as c:
        return improve_boss.identity(c)["id"]


def test_a_shared_run_is_removed_by_rotate(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _collector_on(engine)
    old = _share(engine, client)
    with engine.begin() as c:
        assert improve_boss.identity(c).get("sent") is True  # a share marks the id as sent
        assert c.execute(text("SELECT COUNT(*) FROM tm_installs WHERE install_id = :i"), {"i": old}).scalar() == 0
    got = improve_boss.rotate(engine, tm_settings(), actor="ceo", project_id=project(), post=_via(client))
    assert got["old"] == "deleted" and got["removed"] == 1
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tm_shares WHERE install_id = :i"), {"i": old}).scalar() == 0


def test_a_shared_run_is_removed_by_delete(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _collector_on(engine)
    old = _share(engine, client)
    got = improve_boss.forget(engine, tm_settings(), actor="ceo", project_id=project(), post=_via(client))
    assert got["deleted"] is True and got["removed"] == 1
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tm_shares WHERE install_id = :i"), {"i": old}).scalar() == 0


def test_the_screen_never_says_deleted_for_nothing(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _collector_on(engine)
    # Never sent, collector reachable: the delete is still tried, and the answer is the truth.
    got = improve_boss.rotate(engine, tm_settings(), actor="ceo", project_id=project(), post=_via(client))
    assert got["old"] == "nothing_held" and got["removed"] == 0
    gone = improve_boss.forget(engine, tm_settings(), actor="ceo", project_id=project(), post=_via(client))
    assert gone["deleted"] is True and gone["removed"] == 0


def test_a_share_cannot_swap_the_hash_that_deletes_an_id(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _collector_on(engine)
    old = _share(engine, client)
    with engine.begin() as c:
        first = c.execute(text("SELECT secret_hash FROM tm_shares WHERE install_id = :i"), {"i": old}).scalar()
    forged = {"v": 1, "install_id": old, "secret_hash": "f" * 64, "bundle": "x"}
    client.post("/v1/telemetry/share", content=gzip.compress(json.dumps(forged).encode()),
                headers={"Content-Encoding": "gzip"})
    with engine.begin() as c:
        hashes = set(c.execute(text("SELECT secret_hash FROM tm_shares WHERE install_id = :i"), {"i": old}).scalars())
    assert hashes == {first}


# ------------------------------------------------------------------ 4: masking
def test_share_masks_any_host_ipv6_spec_names_and_chosen_words(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    r = client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                                  "mask_words": "Nightjar, Larkspur"})
    assert r.status_code == 200 and r.json()["mask_words"] == ["Nightjar", "Larkspur"]
    with engine.begin() as c:
        rid = seed_build(c, status="failed", spec={"role_key": "software_engineer", "repo": "ledgerly",
                                                    "base_branch": "release-larkspur-q4"},
                         finalize={"app_name": "PayrollPilot", "branch": "boss/fix-ledger-77"})
        c.execute(text("UPDATE code_task_runs SET error = :e WHERE id = :id"), {"id": rid, "e": (
            "boom on build-07.intranet and db.acme.corp-net via fe80::1ff:fe23:4567:890a for "
            "ledgerly (PayrollPilot) on boss/fix-ledger-77 from release-larkspur-q4, codename Nightjar")})
        b = improve_boss.share_bundle(c, project(), "build", rid)["bundle"]
    for leak in ("build-07", "intranet", "acme", "fe80", "ledgerly", "PayrollPilot", "fix-ledger",
                 "larkspur", "Nightjar"):
        assert leak.lower() not in b.lower(), leak
    assert client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                                     "mask_words": "ab"}).status_code == 422


# ------------------------------------------------------------------ 6, nits
def test_proxies_are_trusted_only_when_listed_and_malformed_hops_are_skipped():
    from apps.api.app.routers.improve import client_ip
    from boss_engine.config import Settings

    default = Settings.model_fields["trusted_proxies"].default
    assert default == "127.0.0.0/8,::1/128"
    assert client_ip("10.0.0.5", "203.0.113.9", default) == "10.0.0.5"  # private is not a proxy
    assert client_ip("127.0.0.1", "unknown, 203.0.113.9, garbage", default) == "203.0.113.9"
    assert client_ip("127.0.0.1", "not-an-ip", default) == "127.0.0.1"


def test_quoted_off_words_are_off(engine):
    from boss_engine.services.improve_boss import env_disabled

    assert env_disabled(tm_settings(telemetry='"off"')) == "BOSS_TELEMETRY=off"
    assert env_disabled(tm_settings(do_not_track="'1'")) == "DO_NOT_TRACK=1"
    assert env_disabled(tm_settings()) is None
