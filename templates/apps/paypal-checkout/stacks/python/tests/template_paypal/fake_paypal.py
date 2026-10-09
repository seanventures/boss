"""An in-process fake of the PayPal REST endpoints the template calls (OAuth token, Orders v2,
captures, refunds, verify-webhook-signature). No test reaches PayPal.

Webhook signatures: real PayPal signs with a certificate and its verify API checks it. The fake
keeps that contract: an event signed by `sign_paypal_event` with the fake's signing secret (made per
test) verifies as SUCCESS; a forged or edited event, or another webhook id, is FAILURE."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _canonical(event) -> bytes:
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()


def sign_paypal_event(event: dict, *, secret: str, webhook_id: str) -> dict:
    transmission_id = str(uuid.uuid4())
    time_ = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(_canonical(event)).hexdigest()
    sig = base64.b64encode(hmac.new(secret.encode(), f"{transmission_id}|{time_}|{webhook_id}|{digest}".encode(),
                                    hashlib.sha256).digest()).decode()
    return {"paypal-auth-algo": "SHA256withRSA",
            "paypal-cert-url": "https://api-m.sandbox.paypal.com/v1/notifications/certs/CERT-fake",
            "paypal-transmission-id": transmission_id, "paypal-transmission-sig": sig,
            "paypal-transmission-time": time_}


def new_credentials() -> dict:
    """Sandbox-shaped credentials made per test. Never real ones."""
    return {"client_id": "A" + secrets.token_urlsafe(30), "client_secret": "E" + secrets.token_urlsafe(30),
            "webhook_id": secrets.token_hex(8).upper()}


class FakePayPal:
    def __init__(self, *, client_id: str, client_secret: str, webhook_id: str) -> None:
        self.signing_secret = secrets.token_hex(32)
        self.orders: dict[str, dict] = {}
        self.tokens: set[str] = set()
        self.token_requests = 0
        self.requests: list[dict] = []
        self.refunds: list[dict] = []
        self.verifications: list[dict] = []
        self.capture_by_request_id: dict[str, dict] = {}
        self.down = False
        self.decline_next = False
        self.pending_next = False
        self.capture_amount_override = None
        self.expires_in = 32400
        fake = self
        expected_basic = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _error(self, status, name, issue=None):
                self._json(status, {"name": name, "message": name, "details": [{"issue": issue}] if issue else []})

            def _handle(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path = self.path
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.requests.append({"method": self.command, "path": path, "headers": headers, "body": raw})
                if fake.down:
                    return self._error(503, "SERVICE_UNAVAILABLE")
                if self.command == "POST" and path == "/v1/oauth2/token":
                    fake.token_requests += 1
                    if headers.get("authorization") != expected_basic or raw != b"grant_type=client_credentials":
                        return self._error(401, "invalid_client")
                    token = "A21AA" + secrets.token_hex(16)
                    fake.tokens.add(token)
                    return self._json(200, {"access_token": token, "token_type": "Bearer", "expires_in": fake.expires_in})
                if headers.get("authorization", "").removeprefix("Bearer ") not in fake.tokens:
                    return self._error(401, "AUTHENTICATION_FAILURE")
                body = json.loads(raw) if raw else {}
                rid = headers.get("paypal-request-id")
                if self.command == "POST" and path == "/v2/checkout/orders":
                    oid = secrets.token_hex(9).upper()
                    order = {"id": oid, "status": "PAYER_ACTION_REQUIRED", "intent": body.get("intent"),
                             "purchase_units": body.get("purchase_units"), "request_id": rid,
                             "payment_source": body.get("payment_source"),
                             "links": [{"rel": "payer-action", "method": "GET",
                                        "href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"}]}
                    fake.orders[oid] = order
                    return self._json(201, order)
                m = re.fullmatch(r"/v2/checkout/orders/([A-Z0-9]+)/capture", path)
                if self.command == "POST" and m:
                    order = fake.orders.get(m.group(1))
                    if order is None:
                        return self._error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
                    if rid and rid in fake.capture_by_request_id:
                        return self._json(201, fake.capture_by_request_id[rid])
                    if order["status"] == "COMPLETED":
                        return self._error(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED")
                    if order["status"] != "APPROVED":
                        return self._error(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED")
                    if fake.decline_next:
                        fake.decline_next = False
                        return self._error(422, "UNPROCESSABLE_ENTITY", "INSTRUMENT_DECLINED")
                    unit = order["purchase_units"][0]
                    capture = {"id": "CAP" + secrets.token_hex(6).upper(),
                               "status": "PENDING" if fake.pending_next else "COMPLETED",
                               "amount": fake.capture_amount_override or {
                                   "currency_code": unit["amount"]["currency_code"], "value": unit["amount"]["value"]},
                               "custom_id": unit.get("custom_id"),
                               "supplementary_data": {"related_ids": {"order_id": order["id"]}}}
                    fake.pending_next = False
                    unit["payments"] = {"captures": [capture]}
                    order["status"] = "COMPLETED"
                    if rid:
                        fake.capture_by_request_id[rid] = order
                    return self._json(201, order)
                m = re.fullmatch(r"/v2/checkout/orders/([A-Z0-9]+)", path)
                if self.command == "GET" and m:
                    order = fake.orders.get(m.group(1))
                    return self._json(200, order) if order else self._error(404, "RESOURCE_NOT_FOUND")
                m = re.fullmatch(r"/v2/payments/captures/([A-Z0-9]+)/refund", path)
                if self.command == "POST" and m:
                    refund = {"id": "REF" + secrets.token_hex(6).upper(), "status": "COMPLETED",
                              "capture_id": m.group(1), "amount": body.get("amount"), "request_id": rid}
                    fake.refunds.append(refund)
                    return self._json(201, refund)
                if self.command == "POST" and path == "/v1/notifications/verify-webhook-signature":
                    fake.verifications.append(body)
                    digest = hashlib.sha256(_canonical(body.get("webhook_event"))).hexdigest()
                    expected = base64.b64encode(hmac.new(
                        fake.signing_secret.encode(),
                        f"{body.get('transmission_id')}|{body.get('transmission_time')}|{body.get('webhook_id')}|{digest}".encode(),
                        hashlib.sha256).digest()).decode()
                    ok = body.get("webhook_id") == webhook_id and hmac.compare_digest(
                        str(body.get("transmission_sig") or ""), expected)
                    return self._json(200, {"verification_status": "SUCCESS" if ok else "FAILURE"})
                return self._error(404, "NOT_FOUND")

            do_GET = _handle
            do_POST = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def approve(self, order_id: str) -> None:
        self.orders[order_id]["status"] = "APPROVED"

    def revoke_tokens(self) -> None:
        self.tokens.clear()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
