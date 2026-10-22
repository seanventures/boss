"""A reference northpeak-fulfilment for T1: it proves the hidden suite, never shown to agents.

The harness's own test runs the T1 hidden suite against this service (it must pass) and against
it with T1_REFERENCE_BREAK=signature (it must fail). So the suite is known to accept a correct
service and to catch a wrong one before any model credit is spent.
Stdlib HTTP plus psycopg; env PORT, DATABASE_URL, NORTHPEAK_WEBHOOK_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

DSN = os.environ["DATABASE_URL"]
SECRET = os.environ["NORTHPEAK_WEBHOOK_SECRET"].encode()
BREAK = os.environ.get("T1_REFERENCE_BREAK", "")
DDL = """CREATE TABLE IF NOT EXISTS stock_reservation (order_line_id TEXT PRIMARY KEY, variant_id TEXT NOT NULL,
         quantity INT NOT NULL, created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)"""


def reserve(order: dict) -> dict:
    out = {"reserved": [], "rejected": []}
    with psycopg.connect(DSN) as conn:
        for line in order["lines"]:
            with conn.transaction():
                # One variant at a time: concurrent orders queue here instead of overselling.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (line["variant_id"],))
                if conn.execute("SELECT 1 FROM stock_reservation WHERE order_line_id = %s", (line["id"],)).fetchone():
                    out["reserved"].append(line["id"])          # a repeat delivery: already held
                    continue
                available = conn.execute(
                    """SELECT coalesce((SELECT sum(quantity - quantity_allocated) FROM warehouse_stock
                                        WHERE product_variant_id = %(v)s), 0)
                            - coalesce((SELECT sum(quantity) FROM stock_reservation
                                        WHERE variant_id = %(v)s AND expires_at > now()), 0)""",
                    {"v": line["variant_id"]}).fetchone()[0]
                if line["quantity"] > available:
                    out["rejected"].append(line["id"])
                    continue
                conn.execute("INSERT INTO stock_reservation VALUES (%s, %s, %s, now(), now() + interval '15 minutes')",
                             (line["id"], line["variant_id"], line["quantity"]))
                out["reserved"].append(line["id"])
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200 if self.path == "/healthz" else 404, {})

    def do_POST(self):
        if self.path != "/webhooks/saleor/order-created":
            return self._send(404, {})
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        expected = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        if BREAK != "signature" and not hmac.compare_digest(expected, self.headers.get("Saleor-Signature", "")):
            return self._send(401, {})
        self._send(200, reserve(json.loads(raw)["order"]))


if __name__ == "__main__":
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(DDL)
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
