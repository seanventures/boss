"""An app reports a suspected incident; BOSS links it to the incident its monitor raises and
tells the app how it ended (services/incident_signals.py).

D2 + HelpDesk AI: customers who cannot place orders file tickets; the help desk sees the cluster
and tells BOSS; BOSS opens ONE incident that links the tickets AND its own threshold alert. The
tickets corroborate the alert and never replace it — a report never starts a change, and the
monitor never closes an incident it did not raise. The channel is authenticated per app.
"""
from __future__ import annotations

import http.server
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from .test_an_app_incident_is_responded_to import FakeApp, _scrape, orders_page
from .test_an_app_reports_its_own_traffic import BASE
from boss_engine.config import get_settings
from boss_engine.services import app_metrics, incident_response as ir, incident_signals as sig
from boss_engine.services.app_deploy_manifest import ManifestError, load_manifest


class FakeHelpDesk:
    """A reporting app: serves /boss/signals behind a bearer token and records callbacks."""

    def __init__(self):
        self.token = None
        self.signals: list[dict] = []
        self.callbacks: list[tuple[dict, dict]] = []
        me = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                ok = self.headers.get("Authorization") == f"Bearer {me.token}"
                data = json.dumps({"signals": me.signals}).encode() if ok else b"{}"
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                me.callbacks.append(({k.lower(): v for k, v in self.headers.items()},
                                     {"path": self.path, "raw": raw}))
                # like the real help desk: a callback names one of ITS suspected incidents
                mine = {str(x.get("id")) for x in me.signals}
                self.send_response(204 if self.path.rsplit("/", 1)[-1] in mine else 404)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture(scope="module")
def desk():
    d = FakeHelpDesk()
    yield d
    d.srv.shutdown()


@pytest.fixture(scope="module")
def app():
    a = FakeApp()
    yield a
    a.srv.shutdown()


@pytest.fixture(autouse=True)
def _secrets_in_the_test_database(monkeypatch):
    """The app's signals token is a generated secret, stored through settings.database_url.
    Unpinned, that is whatever DATABASE_URL says, or BOSS's default: the installed BOSS's own
    database. Pinned here to the test database and its database store, before and after."""
    from ._obs_seed import PG_URL

    if PG_URL.startswith("postgres"):
        monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "database")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _target(c, pid, name, url, manifest):
    tid = f"t_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains, public_url) VALUES (:t, :p, :a, 'acme/sandbox', "
        " 'staging', 'stack', :a, 'active', 'test', '[]'::jsonb, :u)"),
        {"t": tid, "p": pid, "a": name, "u": url})
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
        " manifest, status, created_by, finished_at, code_task_run_id) VALUES (:i, :p, :t, "
        " 'acme/sandbox', 'boss/code/crun_x', :c, CAST(:m AS jsonb), 'healthy', 'test', now(), "
        " 'crun_x')"),
        {"i": f"sd_{uuid4().hex[:8]}", "p": pid, "t": tid, "c": uuid4().hex * 1,
         "m": json.dumps({"name": name, **manifest})})
    return tid


def _target_row(eng, tid):
    from boss_engine.services.deployment_health import STACK_OPEN_URL_COLUMN

    with eng.begin() as c:
        return dict(c.execute(text("SELECT t.*, " + STACK_OPEN_URL_COLUMN.format(alias="t")
                                   + " FROM deployment_targets t WHERE t.id = :t"),
                              {"t": tid}).mappings().one())


def _ticket_signal(n=4, sid="cluster-checkout-1"):
    return {"id": sid, "title": "Customers cannot place orders", "affects": "order-api",
            "summary": f"{n} tickets in 10 min: checkout error / internal error",
            "refs": [f"T-{100 + i}" for i in range(n)], "count": n, "severity": "warning"}


# ── pure ────────────────────────────────────────────────────────────────────────────────────
def test_the_manifest_declares_signals_as_paths_on_the_app():
    m = load_manifest(BASE + "signals: {path: /boss/signals, callback: /boss/incidents/}\n")
    assert m.signals == {"path": "/boss/signals", "callback": "/boss/incidents"}
    assert m.summary()["signals"] == m.signals
    for bad in ("signals: {path: 'http://evil.test/x'}", "signals: {path: /a, hook: /b}",
                "signals: {path: '/a/../b'}"):
        with pytest.raises(ManifestError):
            load_manifest(BASE + f"{bad}\n")


def test_a_signal_is_bounded_and_says_which_field_is_wrong():
    ok = sig.validate({**_ticket_signal(), "title": "x" * 900, "refs": ["a"] * 90})
    assert len(ok["title"]) == 200 and len(ok["refs"]) == 50
    for bad, said in (({"title": "t"}, "`id`"), ({"id": "1"}, "`title`"),
                      ({"id": "1", "title": "t", "severity": "panic"}, "severity"),
                      ({"id": "1", "title": "t", "refs": "T-1"}, "`refs`")):
        with pytest.raises(sig.SignalRefused) as e:
            sig.validate(bad)
        assert said in e.value.detail


def test_signatures_cover_the_body_and_expire():
    body, ts = b'{"id":"1"}', str(int(time.time()))
    good = sig.sign("tok", ts, body)
    assert sig.verify_signature("tok", ts, body, good)
    assert not sig.verify_signature("tok", ts, body + b" ", good)
    assert not sig.verify_signature("other", ts, body, good)
    assert not sig.verify_signature(None, ts, body, good)
    old = str(int(time.time()) - 3600)
    assert not sig.verify_signature("tok", old, body, sig.sign("tok", old, body))


def test_a_build_asked_to_notify_boss_gets_the_contract():
    from boss_engine.services import platform_standards as ps

    asked = ps.platform_standards("Add incident detection to the help desk API: similar "
                                  "tickets -> notify BOSS via incident signals", force=True)
    assert "/boss/signals" in asked and "X-BOSS-Signature" in asked
    plain = ps.platform_standards("A help desk API where agents track incidents", force=True)
    assert "/boss/signals" not in plain


# ── against Postgres ────────────────────────────────────────────────────────────────────────
def test_reports_open_one_incident_the_alert_then_owns(pg, app, desk):
    eng, pid = pg
    with eng.begin() as c:
        orders = _target(c, pid, "order-api", app.origin + "/",
                         {"metrics": {"path": "/metrics", "via": "public"}})
        helpdesk = _target(c, pid, "helpdesk-ai", desk.origin + "/",
                           {"signals": {"path": "/boss/signals", "callback": "/boss/incidents"}})
    desk.token = sig.channel_token(eng, get_settings(), project_id=pid, target_id=helpdesk)
    desk.signals = [_ticket_signal()]
    now = datetime.now(timezone.utc).replace(microsecond=0)

    got = sig.poll_target(eng, get_settings(), _target_row(eng, helpdesk),
                          {"path": "/boss/signals", "callback": "/boss/incidents"}, now=now)
    assert got["ok"] and got["taken"][0]["action"] == "opened", got
    inc = got["taken"][0]["incident_id"]
    with eng.begin() as c:
        row = c.execute(text("SELECT dedupe_key, status, finalize_ctx FROM incidents "
                             "WHERE id = :i"), {"i": inc}).mappings().one()
        events = c.execute(text("SELECT event_type FROM events WHERE entity_id = :i"),
                           {"i": inc}).scalars().all()
    # the incident is the ORDER API's (what the reports say is failing), reported-only
    assert row["dedupe_key"] == app_metrics.INCIDENT_PREFIX + orders
    assert row["status"] == "open" and row["finalize_ctx"]["reported_only"] is True
    assert "incident.reported" in events and "incident.opened" not in events, \
        "a report never starts a response by itself"
    # the help desk was told its tickets are linked, signed with its own token
    headers, call = desk.callbacks[-1]
    # ...at the id IT reported under (D2: BOSS used its own incident id; the help desk answered
    # 404 to both calls and the root cause never reached a ticket)
    assert call["path"] == "/boss/incidents/cluster-checkout-1"
    assert sig.verify_signature(desk.token, headers["x-boss-timestamp"], call["raw"],
                                headers["x-boss-signature"])
    told = json.loads(call["raw"])
    assert told["status"] == "investigating" and told["refs"] == ["T-100", "T-101", "T-102",
                                                                 "T-103"]
    assert told["incident_id"] == inc and told["signal_id"] == "cluster-checkout-1"
    # the same suspected incident read again is the same signal
    again = sig.poll_target(eng, get_settings(), _target_row(eng, helpdesk),
                            {"path": "/boss/signals"}, now=now + timedelta(seconds=40))
    assert again["taken"][0]["action"] == "duplicate"
    # ...and as its cluster grows under the same id, BOSS keeps every ticket (D2: the help desk
    # clustered 7 tickets while BOSS held the first 3, so the RCA would have reached 3 of 7)
    desk.signals = [_ticket_signal(n=7)]
    grew = sig.poll_target(eng, get_settings(), _target_row(eng, helpdesk),
                           {"path": "/boss/signals"}, now=now + timedelta(seconds=70))
    assert grew["taken"][0]["action"] == "updated"
    assert grew["taken"][0]["incident_id"] == inc, "a grown cluster is the same incident"
    with eng.begin() as c:
        kept = c.execute(text("SELECT payload FROM app_signal_receipts WHERE incident_id = :i"),
                         {"i": inc}).scalars().all()
        view = ir.response_view(c, pid, inc)
    assert len(kept) == 1 and kept[0]["count"] == 7
    assert kept[0]["refs"] == [f"T-{100 + i}" for i in range(7)]
    assert [len(r["refs"]) for r in view["reports"]] == [7]

    # a quiet monitor does not close what it did not raise
    app.status, app.body = 200, orders_page(100, 0, 100)
    _scrape(eng, orders, now)
    app.body = orders_page(300, 0, 300)
    quiet = _scrape(eng, orders, now + timedelta(seconds=60))
    assert quiet["alert"]["verdict"] == "ok" and quiet["alert"]["action"] is None
    # the monitor fires: the SAME incident, now alerted, and the response can start
    app.body = orders_page(330, 30, 360)
    fired = _scrape(eng, orders, now + timedelta(seconds=120))
    assert fired["alert"]["action"] == "opened" and fired["alert"]["event_id"]
    with eng.begin() as c:
        row = c.execute(text("SELECT id, finalize_ctx FROM incidents WHERE dedupe_key = :k"),
                        {"k": app_metrics.INCIDENT_PREFIX + orders}).mappings().one()
        kinds = c.execute(text("SELECT kind FROM incident_evidence WHERE incident_id = :i "
                               "ORDER BY created_at"), {"i": inc}).scalars().all()
    assert row["id"] == inc and row["finalize_ctx"]["reported_only"] is False
    assert kinds[0] == "customer_reports" and "alert" in kinds and "deployment" in kinds


def test_the_push_door_is_signed_per_app_and_rate_limited(pg, desk, monkeypatch):
    eng, pid = pg
    with eng.begin() as c:
        helpdesk = _target(c, pid, "helpdesk-ai", desk.origin + "/",
                           {"signals": {"path": "/boss/signals"}})
        silent = _target(c, pid, "other-app", desk.origin + "/", {})
    token = sig.channel_token(eng, get_settings(), project_id=pid, target_id=helpdesk)

    def push(tid, payload, *, key=token, ts=None):
        raw = json.dumps(payload).encode()
        ts = ts or str(int(time.time()))
        with eng.begin() as c:
            return sig.receive(c, get_settings(), tid, raw,
                               {"x-boss-timestamp": ts,
                                "x-boss-signature": sig.sign(key, ts, raw)})

    assert push(silent, _ticket_signal())["reason"] == "not_declared"
    assert push(helpdesk, _ticket_signal(), key="guessed")["reason"] == "bad_signature"
    assert push("t_missing", _ticket_signal())["status"] == "not_found"
    first = push(helpdesk, _ticket_signal(sid="s-0"))
    assert first["status"] == "accepted" and first["action"] == "opened"
    monkeypatch.setattr(sig, "RATE_PER_MINUTE", 3)
    results = [push(helpdesk, _ticket_signal(sid=f"s-{i}")) for i in range(1, 5)]
    assert [r.get("action") or r.get("reason") for r in results] == \
        ["linked", "linked", "rate_limited", "rate_limited"]


def test_the_resolution_and_root_cause_go_back_to_the_reporter(pg, desk):
    eng, pid = pg
    with eng.begin() as c:
        helpdesk = _target(c, pid, "helpdesk-ai", desk.origin + "/",
                           {"signals": {"path": "/boss/signals", "callback": "/boss/incidents"}})
    desk.token = sig.channel_token(eng, get_settings(), project_id=pid, target_id=helpdesk)
    desk.signals = [_ticket_signal(sid="r-1")]
    with eng.begin() as c:
        got = sig.ingest(c, _target_row(eng, helpdesk), _ticket_signal(sid="r-1"), via="push")
    before = len(desk.callbacks)
    rep = {"resolved_at": "2026-10-18T10:30:00+00:00", "minutes_to_recover": 21.5,
           "pull_request_url": "https://github.com/acme/sandbox/pull/9",
           "root_cause": {"root_cause": "Release b1 changed pricing.py; None * int."}}
    with eng.begin() as c:
        told = sig.notify_reporters(c, get_settings(), pid, got["incident_id"], rep)
        ev = c.execute(text("SELECT payload FROM incident_evidence WHERE incident_id = :i AND "
                            "kind = 'reporter_notified'"), {"i": got["incident_id"]}).scalar()
    assert told and told[0]["ok"] and told[0]["status"] == "resolved"
    assert desk.callbacks[before][1]["path"] == "/boss/incidents/r-1"
    body = json.loads(desk.callbacks[before][1]["raw"])
    assert body["status"] == "resolved" and body["pull_request_url"].endswith("/pull/9")
    assert "pricing.py" in body["root_cause"] and body["refs"][0] == "T-100"
    assert ev["http"] == 204 and desk.token not in json.dumps(ev)

    # A REPORTER THAT WAS NOT TOLD IS SAID, AND A PERSON CAN SEND IT AGAIN (D2: the help desk
    # answered 404 to both calls; only an evidence row knew)
    desk.signals = []                                   # the app no longer knows that id: 404
    with eng.begin() as c:
        sig.notify_reporters(c, get_settings(), pid, got["incident_id"], rep)
        said = sig.told_reporters(c, got["incident_id"])
        view = ir.response_view(c, pid, got["incident_id"])
    assert [(t["signal_id"], t["ok"], t["http"]) for t in said] == [("r-1", False, 404)]
    assert said[0]["app_name"] == "helpdesk-ai" and "404" in said[0]["word"]
    assert view["told"] == said
    desk.signals = [_ticket_signal(sid="r-1")]
    with eng.begin() as c:
        again = sig.tell_reporters_again(c, get_settings(), pid, got["incident_id"])
        said = sig.told_reporters(c, got["incident_id"])
    assert again[0]["ok"] and [t["ok"] for t in said] == [True]
    # a call recorded before calls named the signal is superseded by a later one to that app
    with eng.begin() as c:
        c.execute(text("INSERT INTO incident_evidence (id, incident_id, kind, ref, payload, created_at) "
                       "VALUES (:id, :i, 'reporter_notified', :t, CAST(:p AS JSONB), now() - interval '1 hour')"),
                  {"id": "ev_legacy_" + uuid4().hex[:6], "i": got["incident_id"], "t": helpdesk,
                   "p": json.dumps({"ok": False, "http": 404, "status": "resolved", "refs": 1})})
        said = sig.told_reporters(c, got["incident_id"])
    assert [(t["signal_id"], t["ok"]) for t in said] == [("r-1", True)]
