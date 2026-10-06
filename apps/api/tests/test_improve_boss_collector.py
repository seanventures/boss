"""The collector (collector mode): validated, bounded, rate-limited, idempotent, deletable, and
end to end with a client on the same stack. docs/product/RUN_SCORECARDS.md, "Collector"."""
from __future__ import annotations

import gzip
import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from apps.api.tests.improve_fixtures import project, reset_state, seed_build, tm_settings

INGEST = "/v1/telemetry/ingest"
IID = "bi_" + "abcdefghijklmnopqrstuvwxyz"


def _collector(engine, on: bool = True) -> None:
    from boss_engine.services.improve_boss import set_switch

    with engine.begin() as c:
        set_switch(c, "collector", on, actor="ceo", project_id=project())


def _envelope(**over) -> dict:
    body = {"v": 1, "day": date.today().isoformat(), "install_id": IID,
            "secret_hash": "0" * 64,
            "heartbeat": {"install_id": IID, "version": "1.0.0-rc.4", "edition": "community",
                          "install_method": "compose", "os": "linux", "arch": "arm64",
                          "age": "2-7", "worked_24h": True}}
    body.update(over)
    return body


def _post(client, body, *, path=INGEST, raw: bytes | None = None, headers=None):
    data = raw if raw is not None else gzip.compress(json.dumps(body).encode())
    return client.post(path, content=data, headers={"Content-Type": "application/json",
                                                    "Content-Encoding": "gzip", **(headers or {})})


def test_the_collector_is_off_unless_collector_mode_is_on(engine, client):
    reset_state(engine)
    assert _post(client, _envelope()).status_code == 404
    assert client.get(f"/v1/telemetry/collector?project_id={project()}").status_code == 404
    _collector(engine)
    assert _post(client, _envelope()).status_code == 200


def test_ingest_validates_and_is_idempotent_per_install_and_day(engine, client):
    reset_state(engine)
    _collector(engine)
    first = _post(client, _envelope(hostname="acme-prod", batches=[{
        "day": date.today().isoformat(), "scale": {"projects": "2-5"},
        "repo": "acme/secret"}]))
    assert first.status_code == 200, first.text
    assert first.json()["duplicate"] is False and first.json()["dropped_fields"] >= 2
    again = _post(client, _envelope())
    assert again.json()["duplicate"] is True
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tm_heartbeats")).scalar() == 1
        body = c.execute(text("SELECT body FROM tm_batches")).scalar()
        inst = c.execute(text("SELECT * FROM tm_installs")).mappings().one()
    assert body == {"day": date.today().isoformat(), "scale": {"projects": "2-5"}}
    # The newest envelope says whether the switch is on: the second carried no batch.
    assert inst["improve_on"] is False and inst["secret_hash"] == "0" * 64
    assert _post(client, _envelope(v=2)).status_code == 422
    assert _post(client, _envelope(install_id="host-name-1")).status_code == 422
    old = (date.today() - timedelta(days=30)).isoformat()
    assert _post(client, _envelope(day=old)).status_code == 422
    assert _post(client, None, raw=b"\x1f\x8bnot gzip").status_code == 400


def test_ingest_bounds_size_and_inflation(engine, client):
    reset_state(engine)
    _collector(engine)
    bomb = gzip.compress(b"{" + b" " * (3 * 1024 * 1024) + b"}")  # small on the wire, 3 MB inside
    assert len(bomb) < 64 * 1024
    assert _post(client, None, raw=bomb).status_code == 413
    huge = b"\x1f\x8b" + b"x" * (300 * 1024)
    assert _post(client, None, raw=huge).status_code == 413


def test_ingest_rate_limits_per_install_and_per_ip(engine, client):
    from boss_engine.services import improve_collector

    reset_state(engine)
    _collector(engine)
    codes = [_post(client, _envelope()).status_code for _ in range(7)]
    assert codes[:6] == [200] * 6 and codes[6] == 429
    improve_collector.INSTALL_LIMIT.reset()
    improve_collector.IP_LIMIT.reset()
    codes = []
    for i in range(31):
        iid = "bi_" + f"{i:02d}".translate(str.maketrans("0123456789", "abcdefghij")) + "a" * 24
        codes.append(_post(client, _envelope(install_id=iid,
                                             heartbeat=None)).status_code)
    assert codes[:30].count(429) == 0 and codes[-1] == 429


def test_no_ip_address_is_stored(engine, client):
    reset_state(engine)
    _collector(engine)
    _post(client, _envelope(), headers={"X-Forwarded-For": "203.0.113.77"})
    with engine.begin() as c:
        dump = json.dumps([dict(r) for t in ("tm_installs", "tm_heartbeats", "tm_batches")
                           for r in c.execute(text(f"SELECT * FROM {t}")).mappings().all()],
                          default=str)
        cols = c.execute(text("SELECT column_name FROM information_schema.columns "
                              "WHERE table_name LIKE 'tm_%'")).scalars().all()
    assert "203.0.113" not in dump
    assert not any("ip" == col or col.endswith("_ip") or "addr" in col for col in cols)


def test_signed_delete_removes_everything_for_that_install(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    _collector(engine)
    with engine.begin() as c:
        ident = improve_boss.identity(c)
    env = _envelope(install_id=ident["id"], secret_hash=improve_boss.secret_hash(ident["secret"]))
    env["heartbeat"]["install_id"] = ident["id"]
    assert _post(client, env).status_code == 200
    forged = improve_boss.delete_request({"id": ident["id"], "secret": "f" * 64})
    assert _post(client, forged, path="/v1/telemetry/delete").status_code == 401
    stale = improve_boss.delete_request(ident, ts=int(time.time()) - 3600)
    assert _post(client, stale, path="/v1/telemetry/delete").status_code == 401
    good = _post(client, improve_boss.delete_request(ident), path="/v1/telemetry/delete")
    assert good.status_code == 200 and good.json()["deleted"] >= 2
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tm_installs")).scalar() == 0
        assert c.execute(text("SELECT COUNT(*) FROM tm_heartbeats")).scalar() == 0


def test_end_to_end_client_to_collector_on_one_stack(engine, client):
    """The install sends its real daily payload; this same BOSS, in collector mode, stores it;
    "Delete what was sent" removes it and rotates the id; the dashboard and CSV read it."""
    from boss_engine.services import improve_boss

    reset_state(engine)
    _collector(engine)
    from boss_engine.services.improve_boss import set_switch

    with engine.begin() as c:
        set_switch(c, "improve", True, actor="ceo", project_id=project())
    # Work AFTER consent (P3: a unit that ended before the switch went on never travels).
    with engine.begin() as c:
        seed_build(c)
        from boss_engine.services import scorecards

        scorecards.sweep(c)

    def via_api(url, body):
        path = url.split("://", 1)[1].split("/", 1)[1]
        r = client.post("/" + path, content=body,
                        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
        return r.status_code, None if r.status_code < 300 else f"http_{r.status_code}"

    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        improve_boss._put(c, "improve_schedule", {"next_due_at": (now - timedelta(minutes=1)).isoformat()})
    got = improve_boss.tick(engine, tm_settings(), post=via_api, now=now)
    assert got["send"]["sent"] is True, got
    view = client.get(f"/v1/telemetry/collector?project_id={project()}")
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["active"]["daily"] == 1 and body["opted_in"] == 1
    assert body["proof"].startswith("1 active install this month")
    assert body["by"]["harness"][0]["key"] == "claude_code"
    assert body["scale"]["builds"]["dist"]
    csv = client.get(f"/v1/telemetry/collector.csv?project_id={project()}")
    assert csv.status_code == 200 and csv.text.startswith("section,key,value")
    gone = improve_boss.forget(engine, tm_settings(), actor="ceo", project_id=project(),
                               post=via_api)
    assert gone["deleted"] is True
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tm_installs")).scalar() == 0


def test_share_this_run_is_redacted_and_sent_only_on_the_press(engine, client):
    reset_state(engine)
    _collector(engine)
    with engine.begin() as c:
        rid = seed_build(c, status="failed")
        c.execute(text("UPDATE code_task_runs SET error = :e WHERE id = :id"),
                  {"e": "boom with sk-ant-api03-ZqxW9vLmT4rB7nQ2pK5sJ8hF1dG6cY3e", "id": rid})
    b = client.get(f"/v1/telemetry/share-bundle?project_id={project()}&unit_kind=build"
                   f"&unit_id={rid}&actor=ceo")
    assert b.status_code == 200, b.text
    assert "sk-ant" not in b.json()["bundle"] and "boom" in b.json()["bundle"]
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tm_shares")).scalar() == 0  # not on preview


def test_retention_rolls_up_and_deletes_after_eighteen_months(engine):
    from boss_engine.services import improve_collector

    reset_state(engine)
    old = date.today() - timedelta(days=600)
    with engine.begin() as c:
        c.execute(text("INSERT INTO tm_installs (install_id, first_seen_day, last_seen_day, edition, version) "
                       "VALUES (:i, :d, :d, 'community', '1.0.0')"), {"i": IID, "d": old})
        c.execute(text("INSERT INTO tm_heartbeats (install_id, day, edition, version) "
                       "VALUES (:i, :d, 'community', '1.0.0')"), {"i": IID, "d": old})
        out = improve_collector.retention(c)
        rolled = c.execute(text("SELECT metric, value FROM tm_rollups")).all()
    assert out["tm_heartbeats"] == 1 and out["tm_installs"] == 1
    assert ("active_installs", 1) in [(m, int(v)) for m, v in rolled]


def test_machine_routes_need_no_api_key_but_everything_else_still_does(engine, client, monkeypatch):
    from apps.api.app import main as api_main

    reset_state(engine)
    _collector(engine)
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    assert _post(client, _envelope()).status_code == 200
    # No key: refused (401, or 503 when this test app has no keys configured at all).
    assert client.get(f"/v1/telemetry/sharing?project_id={project()}").status_code in (401, 503)
    assert client.put("/v1/telemetry/sharing", json={"project_id": project(), "switch": "improve",
                                                      "on": True}).status_code in (401, 503)
