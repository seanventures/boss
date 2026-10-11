"""An in-process fake of the parts of the Stripe API the template calls. The real `stripe` SDK
talks to it (base_addresses override), so requests are encoded exactly as in production and no
test ever reaches api.stripe.com."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class FakeStripe:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.refunds: list[dict] = []
        self.requests: list[dict] = []
        self.idempotency: dict[str, dict] = {}
        self.down = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
                fake.requests.append({"method": self.command, "path": self.path, "form": form,
                                      "headers": {k.lower(): v for k, v in self.headers.items()}})
                if fake.down:
                    return self._json(500, {"error": {"type": "api_error", "message": "down"}})
                if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                    return self._json(401, {"error": {"type": "invalid_request_error"}})
                key = self.headers.get("Idempotency-Key")
                if self.command == "POST" and self.path == "/v1/checkout/sessions":
                    if key and key in fake.idempotency:
                        return self._json(200, fake.idempotency[key])
                    items = []
                    i = 0
                    while f"line_items[{i}][quantity]" in form:
                        items.append({"quantity": int(form[f"line_items[{i}][quantity]"]),
                                      "unit_amount": int(form[f"line_items[{i}][price_data][unit_amount]"]),
                                      "currency": form[f"line_items[{i}][price_data][currency]"],
                                      "name": form[f"line_items[{i}][price_data][product_data][name]"]})
                        i += 1
                    sid = "cs_test_" + secrets.token_hex(12)
                    session = {"id": sid, "object": "checkout.session", "mode": form.get("mode"),
                               "url": f"https://checkout.stripe.test/c/pay/{sid}",
                               "amount_total": sum(x["unit_amount"] * x["quantity"] for x in items),
                               "currency": items[0]["currency"] if items else None,
                               "client_reference_id": form.get("client_reference_id"),
                               "metadata": {"order_id": form.get("metadata[order_id]")},
                               "payment_status": "unpaid", "status": "open",
                               "payment_intent": "pi_" + secrets.token_hex(12),
                               "success_url": form.get("success_url"), "cancel_url": form.get("cancel_url"),
                               "line_items_sent": items}
                    fake.sessions[sid] = session
                    if key:
                        fake.idempotency[key] = session
                    return self._json(200, session)
                m = re.match(r"^/v1/checkout/sessions/([A-Za-z0-9_]+)", self.path)
                if self.command == "GET" and m:
                    s = fake.sessions.get(m.group(1))
                    if s is None:
                        return self._json(404, {"error": {"type": "invalid_request_error", "message": "No such session"}})
                    return self._json(200, s)
                if self.command == "POST" and self.path == "/v1/refunds":
                    refund = {"id": "re_" + secrets.token_hex(8), "object": "refund", "status": "succeeded",
                              "payment_intent": form.get("payment_intent"), "amount": int(form.get("amount", 0)),
                              "idempotency_key": key}
                    fake.refunds.append(refund)
                    return self._json(200, refund)
                return self._json(404, {"error": {"type": "invalid_request_error", "message": "unknown route"}})

            do_GET = _handle
            do_POST = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def pay(self, session_id: str, **overrides) -> dict:
        s = self.sessions[session_id]
        s.update({"payment_status": "paid", "status": "complete", **overrides})
        return dict(s)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def sign_stripe_payload(payload: str, secret: str, timestamp: int | None = None) -> str:
    """A Stripe-Signature header computed the documented way: v1 = HMAC-SHA256(secret, "<t>.<payload>")."""
    ts = int(time.time()) if timestamp is None else timestamp
    v1 = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


def new_webhook_secret() -> str:
    """A fresh signing secret per test, in Stripe's shape. Never a real one."""
    return "whsec_" + secrets.token_hex(24)


def new_test_keys() -> tuple[str, str]:
    return "sk_test_" + secrets.token_hex(16), "pk_test_" + secrets.token_hex(16)
