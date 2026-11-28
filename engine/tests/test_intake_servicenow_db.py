"""ServiceNow intake & Table API poll safety-net tests on Postgres (P9b-3, BOSSAPP-1102).

Uses a real local fake ServiceNow HTTP server (ThreadingHTTPServer) to verify:
  * Standard Webhooks HMAC push verification (webhook-id, webhook-timestamp, webhook-signature)
  * Table API keyset polling on (sys_updated_on, sys_id) via intake_cursors & TicketHttp
  * event_intake.poll_due() running scheduled safety-net polls on active mode='push' connections
  * Cross-delivery head_key deduplication (push vs poll -> single routed run)
  * Self-update loop suppression (sys_updated_by == service_account)
"""
from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import text

from boss_engine.services import event_intake, intake_guards, scoped_budgets

from ._intake_support import (
    deploy_local_agent,
    make_connection,
    make_project,
    make_route,
    pg_engine,
    purge,
    runs_of,
    standard_webhook_request,
    token_of,
)

WHSEC = "whsec_" + base64.b64encode(b"s" * 32).decode("ascii")


class FakeServiceNowServer:
    def __init__(self):
        self.records: dict[str, list[dict[str, Any]]] = {"incident": [], "change_request": []}
        self.queries: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                outer.queries.append(self.path)
                if self.headers.get("Authorization") != "Bearer sn_tok_test":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                table = parsed.path.rsplit("/", 1)[-1]
                qs = urllib.parse.parse_qs(parsed.query)
                limit = int((qs.get("sysparm_limit") or ["50"])[0])
                q = (qs.get("sysparm_query") or [""])[0]
                rows = list(outer.records.get(table, []))
                rows.sort(key=lambda r: (r["sys_updated_on"], r["sys_id"]))
                # Apply keyset filter if present
                if "sys_updated_on>" in q:
                    ts_part = q.split("sys_updated_on>", 1)[1].split("^", 1)[0]
                    id_part = ""
                    if "sys_id>" in q:
                        id_part = q.split("sys_id>", 1)[1].split(")", 1)[0]
                    rows = [
                        r for r in rows
                        if r["sys_updated_on"] > ts_part
                        or (id_part and r["sys_updated_on"] == ts_part and r["sys_id"] > id_part)
                    ]
                body = json.dumps({"result": rows[:limit]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


_SCOPE: dict[str, Any] = {"pid": None}


@pytest.fixture()
def world():
    server = FakeServiceNowServer()
    event_intake.BUCKETS.reset()
    intake_guards.reset_for_tests()
    scoped_budgets.reset_for_tests()
    engine = pg_engine()
    pid = make_project(engine)
    _SCOPE["pid"] = pid
    try:
        yield engine, pid, server
    finally:
        purge(engine, pid)
        server.close()


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _process(engine) -> dict:
    return event_intake.process_due(engine, _settings(), project_id=_SCOPE["pid"])


def test_servicenow_push_poll_safety_net_dedupe_and_self_loop(world):
    engine, pid, server = world
    conn = make_connection(
        engine, pid, "servicenow", name="Prod ServiceNow",
        config={
            "instance_url": server.base_url,
            "tables": "incident,change_request",
            "poll_interval_seconds": 60,
            "service_account": "boss.integration",
        },
        secrets={"signing_secret": WHSEC, "api_token": "sn_tok_test"},
    )
    dep = deploy_local_agent(engine, pid, key="sre")
    make_route(engine, pid, dep["id"], conn["id"], identity_mode="owner",
               config={"match": {"field": "type", "op": "prefix", "value": "com.servicenow."}})

    with engine.begin() as c:
        stamped = c.execute(text("SELECT clock_timestamp()")).scalar()
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    t1 = (stamped - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    t2 = (stamped - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    t3 = (stamped - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")

    # 1. Push a P1 incident via Standard Webhooks
    inc1 = {
        "table": "incident",
        "sys_id": "inc_sys_001",
        "number": "INC0010001",
        "short_description": "Checkout DB latency spike",
        "description": "p99 > 4s on primary",
        "state": "1",
        "priority": "1",
        "sys_updated_on": t1,
        "sys_updated_by": "alex.ops",
        "sys_mod_count": "1",
    }
    tok = token_of(conn["url_path"])
    bad = event_intake.receive(engine, "servicenow", tok, standard_webhook_request("whsec_" + base64.b64encode(b"x" * 32).decode(), inc1))
    assert bad["http_status"] == 401

    pushed = event_intake.receive(engine, "servicenow", tok, standard_webhook_request(WHSEC, inc1, msg_id="sn_msg_1"))
    assert pushed["http_status"] == 202 and pushed["outcome"] == "received"
    _process(engine)
    assert len(runs_of(engine, pid)) == 1

    # 2. Same incident + an unseen missed incident appear in Table API poll safety net on mode='push'
    inc2 = {
        "sys_id": "inc_sys_002",
        "number": "INC0010002",
        "short_description": "Missed by webhook",
        "state": "2",
        "priority": "2",
        "sys_updated_on": t2,
        "sys_updated_by": "alex.ops",
        "sys_mod_count": "1",
    }
    self_inc = {
        "sys_id": "inc_sys_003",
        "number": "INC0010003",
        "short_description": "BOSS wrote this work note",
        "sys_updated_on": t3,
        "sys_updated_by": "boss.integration",
        "sys_mod_count": "2",
    }
    server.records["incident"] = [inc1, inc2, self_inc]

    polled = event_intake.poll_due(engine, _settings(), now=stamped - timedelta(minutes=5),
                                   connection_id=conn["id"])
    assert polled["polled"] == 1 and polled["inserted"] == 3
    _process(engine)

    # inc1 deduped against push (filtered), self_inc suppressed by service_account (filtered), inc2 admitted -> total 2 runs
    assert len(runs_of(engine, pid)) == 2

    # 3. Verify keyset cursor advanced and second poll returns 0 new deliveries
    with engine.begin() as c:
        cur = c.execute(text("SELECT cursor FROM intake_cursors WHERE connection_id = :c"),
                        {"c": conn["id"]}).scalar()
    parsed_cur = json.loads(cur)
    assert parsed_cur["incident"]["sys_id"] == "inc_sys_003"
    assert parsed_cur["incident"]["sys_updated_on"] == t3

    polled2 = event_intake.poll_due(engine, _settings(), now=stamped + timedelta(minutes=5),
                                    connection_id=conn["id"])
    assert polled2["polled"] == 1 and polled2["inserted"] == 0
