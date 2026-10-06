"""Final re-check of tm/fixes (2026-10-05): an id cannot be taken over by a forged heartbeat (D),
the API trusts X-Forwarded-For only from the console it can name (C), checkup.sh reads .env the way
Compose does, and a refused collector URL reads as a rule."""
from __future__ import annotations

import gzip
import json
import subprocess
from datetime import date
from pathlib import Path

from sqlalchemy import text

from apps.api.tests.improve_fixtures import project, reset_state, tm_settings

ROOT = Path(__file__).resolve().parents[3]


def _collector_on(engine):
    from boss_engine.services.improve_boss import set_switch

    with engine.begin() as c:
        set_switch(c, "collector", True, actor="ceo", project_id=project())


def _post(client, path, body):
    return client.post(path, content=gzip.compress(json.dumps(body).encode()),
                       headers={"Content-Encoding": "gzip"})


# ------------------------------------------------------------------ D
def test_a_forged_heartbeat_cannot_take_over_a_share_only_id(engine, client):
    from boss_engine.services import improve_boss
    from boss_engine.services.improve_collector import registered_hash

    reset_state(engine)
    _collector_on(engine)

    def via(url, body):
        r = client.post("/" + url.split("://", 1)[1].split("/", 1)[1], content=body,
                        headers={"Content-Encoding": "gzip"})
        return r.status_code, None if r.status_code < 300 else f"http_{r.status_code}", r.json()

    assert improve_boss.share_send(engine, tm_settings(), bundle="x", note="", post=via)["sent"]
    with engine.begin() as c:
        ident = improve_boss.identity(c)
    real = improve_boss.secret_hash(ident["secret"])
    forged = {"v": 1, "day": date.today().isoformat(), "install_id": ident["id"], "secret_hash": "f" * 64,
              "heartbeat": {"install_id": ident["id"], "version": "1.0.0", "os": "linux"}}
    assert _post(client, "/v1/telemetry/ingest", forged).status_code == 409
    with engine.begin() as c:
        assert registered_hash(c, ident["id"]) == real
        assert c.execute(text("SELECT COUNT(*) FROM tm_installs WHERE install_id = :i"),
                         {"i": ident["id"]}).scalar() == 0
    # The owner's own heartbeat (same hash) is accepted; its delete removes everything.
    honest = {**forged, "secret_hash": real}
    assert _post(client, "/v1/telemetry/ingest", honest).status_code == 200
    gone = _post(client, "/v1/telemetry/delete", improve_boss.delete_request(ident))
    assert gone.status_code == 200 and gone.json()["deleted"] >= 2
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tm_shares WHERE install_id = :i"), {"i": ident["id"]}).scalar() == 0


def test_the_first_hash_wins_whichever_table_holds_it(engine, client):
    from boss_engine.services.improve_collector import registered_hash

    reset_state(engine)
    iid = "bi_" + "q" * 26
    with engine.begin() as c:
        c.execute(text("INSERT INTO tm_shares (id, install_id, body, secret_hash, received_at) "
                       "VALUES ('s1', :i, '{}', :h, now() - interval '2 days')"), {"i": iid, "h": "a" * 64})
        c.execute(text("INSERT INTO tm_installs (install_id, secret_hash, first_seen_day, last_seen_day) "
                       "VALUES (:i, :h, CURRENT_DATE, CURRENT_DATE)"), {"i": iid, "h": "b" * 64})
        assert registered_hash(c, iid) == "a" * 64


# ------------------------------------------------------------------ C
def test_xff_is_believed_only_from_the_console_by_name(monkeypatch):
    from apps.api.app.routers import improve

    improve._RESOLVED.clear()
    monkeypatch.setattr(improve, "_resolve", lambda name: ["172.18.0.5"] if name == "web" else [])
    compose = "127.0.0.0/8,::1/128,web"
    # Compose: through the console (peer = web) the hop it forwarded is the client.
    assert improve.client_ip("172.18.0.5", "203.0.113.5", compose) == "203.0.113.5"
    # Straight at the published API port (peer = the docker gateway): a forged header is ignored.
    assert improve.client_ip("172.18.0.1", "198.51.100.9", compose) == "172.18.0.1"
    # Source install, default trust: loopback console with no forwarded hop → one console bucket.
    assert improve.client_ip("127.0.0.1", "", "127.0.0.0/8,::1/128") == "127.0.0.1"
    assert improve.client_ip("203.0.113.7", "1.1.1.1", "127.0.0.0/8,::1/128") == "203.0.113.7"


def test_compose_wires_the_trust_and_the_hops():
    compose = (ROOT / "scripts/install/community-compose.yml").read_text()
    assert "BOSS_TRUSTED_PROXIES: ${BOSS_TRUSTED_PROXIES:-127.0.0.0/8,::1/128,web}" in compose
    assert "BOSS_CONSOLE_PROXY_HOPS: ${BOSS_CONSOLE_PROXY_HOPS:-0}" in compose


# ------------------------------------------------------------------ nits
def _env_value(tmp_path, content, key):
    env = tmp_path / ".env"
    env.write_text(content)
    src = (ROOT / "scripts/install/checkup.sh").read_text()
    fn = src[src.index("_env_awk='"):src.index("\n}\n", src.index("env_value() {")) + 3]
    out = subprocess.run(["bash", "-c", f'{fn}\nenv_value "$1"', "_", key], capture_output=True,
                         text=True, env={"ENV_FILE": str(env), "PATH": "/usr/bin:/bin"})
    return out.stdout.strip()


def test_checkup_reads_env_like_compose(tmp_path):
    content = ("BOSS_TELEMETRY=on\nexport BOSS_TELEMETRY=\"off\" # turned off\n"
               "  DO_NOT_TRACK=1   # standard\nBOSS_TELEMETRY_URL='https://c.example.test/v1/telemetry/ingest'\n"
               "X=a#b\n")
    assert _env_value(tmp_path, content, "BOSS_TELEMETRY") == "off"       # export, last wins, comment
    assert _env_value(tmp_path, content, "DO_NOT_TRACK") == "1"
    assert _env_value(tmp_path, content, "BOSS_TELEMETRY_URL") == "https://c.example.test/v1/telemetry/ingest"
    assert _env_value(tmp_path, content, "X") == "a#b"                     # no space: not a comment
    assert _env_value(tmp_path, content, "MISSING") == ""


def test_a_refused_url_reads_as_a_rule(engine, client, monkeypatch):
    from apps.api.app import main as api_main

    reset_state(engine)
    monkeypatch.setattr(api_main.settings, "telemetry_url", "")
    r = client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                                  "url": "https://c.example.test/ingest"})
    assert r.status_code == 422 and "Must end in /v1/telemetry/ingest" in r.text
