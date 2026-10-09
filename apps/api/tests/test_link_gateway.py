"""The link gateway, adversarially, on real sockets (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.9).

A is a tiny API with a login that seeds BOSS_TEST_USERS; C is another app's preview; "BOSS" is a
stand-in for the API on a port outside the preview range. B holds only what BOSS gives it
(BOSS_LINK_BANK_URL, _TOKEN, _USERS). B gets in to A, as A's test user, and to nothing else: not C,
not BOSS, not with another link's pass, not after its workload stops, not after revocation
(measured: within 5 s). The network fence that stops B going AROUND the gateway is proved on the
real container by the compose end-to-end (tests/e2e/links).
"""
from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from apps.egress.boss_egress import links as gateway_mod
from boss_engine.app_links import service as links
from boss_engine.identity import vault

LINK_POOL = (47110, 47119)
PREVIEW_POOL = (47210, 47299)


def _free(lo: int, hi: int) -> int:
    for port in range(lo, hi + 1):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


class Fake:
    """An app: A (login + me + echo), or a plain marker app."""

    def __init__(self, port: int, name: str, users: list[dict] | None = None):
        self.name, self.users, self.hits, self.tokens = name, users or [], [], {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _reply(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.hits.append(self.path)
                if self.path == "/echo":
                    return self._reply(200, {"app": fake.name, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.path == "/me":
                    who = fake.tokens.get((self.headers.get("authorization") or "").removeprefix("Bearer "))
                    return self._reply(200 if who else 401, {"app": fake.name, "user": who})
                return self._reply(200, {"app": fake.name, "path": self.path})

            def do_POST(self):
                fake.hits.append(self.path)
                data = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                if self.path == "/login":
                    ok = any(u["username"] == data.get("username") and u["password"] == data.get("password")
                             for u in fake.users)
                    if not ok:
                        return self._reply(401, {"error": "bad login"})
                    tok = uuid.uuid4().hex
                    fake.tokens[tok] = data["username"]
                    return self._reply(200, {"token": tok})
                return self._reply(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def env(engine):
    import os

    saved = {k: os.environ.get(k) for k in ("BOSS_MASTER_KEY", links.LINK_PORTS_ENV, links.PREVIEW_PORTS_ENV,
                                            links.GATEWAY_HOST_ENV, links.PREVIEW_HOST_ENV)}
    os.environ.update({"BOSS_MASTER_KEY": base64.b64encode(b"s5-gateway-test-only-master-key!").decode(),
                       links.LINK_PORTS_ENV: f"{LINK_POOL[0]}-{LINK_POOL[1]}",
                       links.PREVIEW_PORTS_ENV: f"{PREVIEW_POOL[0]}-{PREVIEW_POOL[1]}",
                       links.GATEWAY_HOST_ENV: "127.0.0.1", links.PREVIEW_HOST_ENV: "127.0.0.1"})
    vault.reset_master_key_cache()
    w = IdentityWorld.create(engine)
    w.runs = {"bank": f"ctr_{uuid.uuid4().hex[:10]}", "attacker": f"ctr_{uuid.uuid4().hex[:10]}",
              "ledger": f"ctr_{uuid.uuid4().hex[:10]}"}
    w.titles = {"bank": "Banking", "attacker": "Bank attacker", "ledger": "Ledger viewer"}
    w.previews = {k: f"prev_{uuid.uuid4().hex[:10]}" for k in w.runs}
    w.ports = {"bank": _free(*PREVIEW_POOL)}
    w.boss_port = _free(47300, 47399)                     # "the API": outside the preview range
    _seed(engine, w)
    with engine.begin() as db:
        w.link = links.create_link(db, project_id=w.project_a, consumer=w.runs["attacker"], provider="Banking",
                                   alias="BANK", env="preview", credentials="test_users", created_by="test")
        w.link2 = links.create_link(db, project_id=w.project_a, consumer=w.runs["attacker"], provider="Ledger viewer",
                                    alias="LEDGER", env="preview", credentials="none", created_by="test")
        users = json.loads(links.provider_environment(db, project_id=w.project_a, app_key="banking", env="preview",
                                                      rotate=True)["BOSS_TEST_USERS"])
    w.a = Fake(w.ports["bank"], "A", users)
    w.ports["ledger"] = _free(w.ports["bank"] + 1, PREVIEW_POOL[1])
    w.c = Fake(w.ports["ledger"], "C")
    w.boss = Fake(w.boss_port, "BOSS")
    _seed(engine, w)
    with engine.begin() as db:
        # B is previewing: its preview's environment, exactly as preview_service builds it.
        w.b_env = links.workload_environment(db, project_id=w.project_a, run_id=w.runs["attacker"], env="preview",
                                             consumer_kind="preview", consumer_id=w.previews["attacker"],
                                             expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    stop = threading.Event()
    gw = gateway_mod.LinkGateway(engine)
    listeners = gateway_mod.LinkListeners(gw, "127.0.0.1")
    threading.Thread(target=gateway_mod.run, args=(engine, stop), kwargs={"gateway": gw, "listeners": listeners},
                     daemon=True).start()
    _wait_open(w.link["gateway_port"])
    w.gw = gw
    yield w
    stop.set()
    time.sleep(1.2)
    for f in (w.a, w.c, w.boss):
        f.close()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    vault.reset_master_key_cache()


def _seed(engine, w):
    with engine.begin() as db:
        for key, rid in w.runs.items():
            db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) VALUES (:i, :p, :t, 'completed') "
                            "ON CONFLICT (id) DO NOTHING"), {"i": rid, "p": w.project_a, "t": w.titles[key]})
            port = w.ports.get(key)
            db.execute(text("INSERT INTO app_previews (id, project_id, run_id, port, url, status, mode, expires_at) "
                            "VALUES (:i, :p, :r, :port, :u, 'running', 'process', now() + interval '1 hour') "
                            "ON CONFLICT (id) DO NOTHING"),
                       {"i": w.previews[key], "p": w.project_a, "r": rid, "port": port,
                        "u": f"http://127.0.0.1:{port}"})


@pytest.fixture(autouse=True)
def seeded(env, engine):
    _seed(engine, env)        # the suite's clean_tables empties code_task_runs and app_previews per test
    env.gw.invalidate()
    yield


def _wait_open(port: int, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"port {port} never opened")


def _call(port: int, method: str, target: str, headers: dict | None = None, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs["content-type"] = "application/json"
    conn.request(method, target, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw or b"{}")
    except ValueError:
        return resp.status, {"raw": raw.decode(errors="replace")}


def _b(env):
    port = int(env.b_env["BOSS_LINK_BANK_URL"].rsplit(":", 1)[1])
    return port, {"x-boss-link": env.b_env["BOSS_LINK_BANK_TOKEN"]}


def test_b_signs_in_to_a_as_its_test_user_through_the_link(env):
    port, h = _b(env)
    user = json.loads(env.b_env["BOSS_LINK_BANK_USERS"])[0]
    status, body = _call(port, "POST", "/login", h, {"username": user["username"], "password": user["password"]})
    assert status == 200, body
    status, me = _call(port, "GET", "/me", {**h, "authorization": f"Bearer {body['token']}"})
    assert (status, me) == (200, {"app": "A", "user": user["username"]})
    # A sees B's own Authorization, never the pass; and a clean origin (D8).
    _, echo = _call(port, "GET", "/echo", {**h, "authorization": "Bearer b-own", "host": f"127.0.0.1:{env.ports['ledger']}"})
    assert echo["app"] == "A"
    assert "x-boss-link" not in echo["headers"] and echo["headers"]["authorization"] == "Bearer b-own"
    assert echo["headers"]["host"] == f"127.0.0.1:{env.ports['bank']}"


@pytest.mark.parametrize("name, how, status, reason", [
    ("no pass", lambda h: {}, 401, "no_link_pass"),
    ("forged pass", lambda h: {"x-boss-link": "boss_lnk_" + "A" * 43}, 401, "link_pass_dead"),
    ("a run token, not a pass", lambda h: {"x-boss-link": "boss_egr_" + "A" * 40}, 401, "link_pass_dead"),
])
def test_without_the_workloads_pass_nothing_is_forwarded(env, name, how, status, reason):
    port, h = _b(env)
    before = len(env.a.hits)
    assert _call(port, "GET", "/echo", how(h)) == (status, {"error": "link_refused", "reason": reason})
    assert len(env.a.hits) == before


def test_the_gateway_reaches_only_a_never_boss_or_another_preview(env):
    port, h = _b(env)
    seen = (len(env.boss.hits), len(env.c.hits))
    # An absolute-form target naming the API, or another preview, is refused outright.
    for target in (f"http://127.0.0.1:{env.boss_port}/v1/projects", f"http://127.0.0.1:{env.ports['ledger']}/",
                   "http://169.254.169.254/latest/meta-data/", "http://db:5432/"):
        assert _call(port, "GET", target, h)[1]["reason"] == "absolute_form_refused"
    # Traversal and encoded separators never reach A either.
    for target in ("/a/../v1/admin", "/a/%2e%2e/v1", "/a//b"):
        assert _call(port, "GET", target, h)[0] == 400
    # A network-path reference is only ever a path on A (the server collapses the leading //).
    assert _call(port, "GET", f"//127.0.0.1:{env.boss_port}/v1", h)[1]["app"] == "A"
    # A Host header naming another preview is ignored: it is still A that answers.
    assert _call(port, "GET", "/who", {**h, "host": f"127.0.0.1:{env.ports['ledger']}"})[1]["app"] == "A"
    assert (len(env.boss.hits), len(env.c.hits)) == seen


def test_a_pass_opens_only_its_own_links_port(env):
    # B's BANK pass on B's LEDGER port, and B's LEDGER pass on the BANK port: both refused.
    bank_port, bank_h = _b(env)
    ledger_port = int(env.b_env["BOSS_LINK_LEDGER_URL"].rsplit(":", 1)[1])
    ledger_h = {"x-boss-link": env.b_env["BOSS_LINK_LEDGER_TOKEN"]}
    assert _call(ledger_port, "GET", "/echo", bank_h)[1]["reason"] == "wrong_link"
    assert _call(bank_port, "GET", "/echo", ledger_h)[1]["reason"] == "wrong_link"
    assert _call(ledger_port, "GET", "/echo", ledger_h)[1]["app"] == "C"      # its own link works


def test_a_port_with_no_link_refuses_connections(env):
    with env_conn() as s:
        free = [p for p in range(LINK_POOL[0], LINK_POOL[1] + 1)
                if p not in (env.link["gateway_port"], env.link2["gateway_port"])][0]
        assert s.connect_ex(("127.0.0.1", free)) != 0


def env_conn():
    return socket.socket()


def test_a_tampered_preview_row_cannot_point_the_link_at_boss(env, engine):
    port, h = _b(env)
    before = len(env.boss.hits)
    with engine.begin() as db:
        db.execute(text("UPDATE app_previews SET port = :p WHERE id = :i"),
                   {"p": env.boss_port, "i": env.previews["bank"]})
    env.gw.invalidate()
    try:
        assert _call(port, "GET", "/v1/projects", h)[1]["reason"] == "upstream_outside_preview_ports"
        with engine.begin() as db:
            db.execute(text("UPDATE app_previews SET port = 8000 WHERE id = :i"), {"i": env.previews["bank"]})
        env.gw.invalidate()
        assert _call(port, "GET", "/v1/projects", h)[1]["reason"] == "upstream_is_boss"
    finally:
        with engine.begin() as db:
            db.execute(text("UPDATE app_previews SET port = :p WHERE id = :i"),
                       {"p": env.ports["bank"], "i": env.previews["bank"]})
        env.gw.invalidate()
    assert len(env.boss.hits) == before


def test_when_bs_preview_stops_its_pass_stops(env, engine):
    port, h = _b(env)
    with engine.begin() as db:
        db.execute(text("UPDATE app_previews SET status = 'stopped' WHERE id = :i"), {"i": env.previews["attacker"]})
    env.gw.invalidate()
    assert _call(port, "GET", "/echo", h) == (401, {"error": "link_refused", "reason": "consumer_not_live"})


def test_when_as_preview_is_gone_b_is_told_so(env, engine):
    port, h = _b(env)
    with engine.begin() as db:
        db.execute(text("UPDATE app_previews SET status = 'stopped' WHERE id = :i"), {"i": env.previews["bank"]})
    env.gw.invalidate()
    assert _call(port, "GET", "/echo", h)[1]["reason"] == "provider_not_running"


def test_the_cache_alone_ends_a_revoked_link_within_its_bound(engine, env):
    """Even with no NOTIFY delivered, a status change is seen within CACHE_SECONDS (< 5 s)."""
    from boss_engine.identity import link_rules

    now = [100.0]
    gw = gateway_mod.LinkGateway(engine, clock=lambda: now[0],
                                 upstream=lambda m, u, h, b: gateway_mod.GatewayResponse(200, [], b"ok"))
    port, h = _b(env)
    assert gw.handle(port, "GET", "/echo", h, b"").status == 200
    with engine.begin() as db:
        db.execute(text("UPDATE app_links SET status = 'revoked' WHERE id = :i"), {"i": env.link["id"]})
    try:
        now[0] += link_rules.CACHE_SECONDS + 0.01
        assert gw.handle(port, "GET", "/echo", h, b"").status == 403
        assert link_rules.CACHE_SECONDS < 5
    finally:
        with engine.begin() as db:
            db.execute(text("UPDATE app_links SET status = 'active' WHERE id = :i"), {"i": env.link["id"]})


def test_zz_revoking_the_link_cuts_b_off_within_5_seconds(env, engine):
    port, h = _b(env)
    assert _call(port, "GET", "/echo", h)[0] == 200
    with engine.begin() as db:
        links.revoke_link(db, project_id=env.project_a, link_id=env.link["id"], revoked_by="test")
    t0 = time.monotonic()
    while True:
        try:
            status, body = _call(port, "GET", "/echo", h)
        except OSError:
            status, body = 0, {"reason": "connection_refused"}
        if status != 200:
            break
        assert time.monotonic() - t0 < 5, "still forwarding 5 s after revocation"
        time.sleep(0.05)
    elapsed = time.monotonic() - t0
    assert elapsed < 5
    assert body["reason"] in ("link_pass_dead", "no_link_on_port", "link_revoked", "connection_refused")
    # And the port itself closes.
    end = time.monotonic() + 5
    while time.monotonic() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                break
        time.sleep(0.05)
    else:
        raise AssertionError("the revoked link's port is still open")
