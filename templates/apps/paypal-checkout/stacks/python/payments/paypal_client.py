"""A small PayPal REST client (Orders v2, Payments v2, webhook verification) on httpx. No SDK:
PayPal's server SDKs change often; these calls are stable and documented.

The OAuth access token is cached until shortly before it expires and refreshed once on a 401."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx


class PayPalError(Exception):
    def __init__(self, status: int, body: Optional[dict]) -> None:
        self.status = status
        self.body = body or {}
        super().__init__(f"PayPal answered {status} {self.body.get('name', '')}".strip())

    def issues(self) -> list[str]:
        """PayPal's error detail issue codes, e.g. INSTRUMENT_DECLINED, ORDER_ALREADY_CAPTURED."""
        return [d.get("issue") for d in self.body.get("details") or [] if isinstance(d, dict)]


class PayPalClient:
    def __init__(self, *, api_base: str, client_id: str, client_secret: str,
                 now: Callable[[], float] = time.monotonic, timeout: float = 20.0) -> None:
        self.api_base = api_base
        self._auth = (client_id, client_secret)
        self._now = now
        self._http = httpx.Client(timeout=timeout)
        self._lock = threading.Lock()
        self._token: Optional[tuple[str, float]] = None  # (value, expires_at)

    def access_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and self._now() < self._token[1]:
                return self._token[0]
            r = self._http.post(f"{self.api_base}/v1/oauth2/token", auth=self._auth,
                                data={"grant_type": "client_credentials"})
            body = _json(r)
            if r.status_code != 200 or not body.get("access_token"):
                raise PayPalError(r.status_code, body)
            # Refresh a minute early so a token never expires mid-request.
            self._token = (body["access_token"], self._now() + max(0, int(body.get("expires_in") or 0) - 60))
            return self._token[0]

    def request(self, method: str, path: str, *, body: Any = None, request_id: Optional[str] = None) -> dict:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.access_token(force=attempt > 0)}",
                       "Prefer": "return=representation"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id  # PayPal's idempotency key
            r = self._http.request(method, f"{self.api_base}{path}", headers=headers,
                                   json=body if body is not None else None)
            if r.status_code == 401 and attempt == 0:
                continue  # token revoked or expired early: refresh once
            out = _json(r)
            if r.status_code >= 400:
                raise PayPalError(r.status_code, out)
            return out
        raise PayPalError(401, {})

    def create_order(self, body: dict, request_id: str) -> dict:
        return self.request("POST", "/v2/checkout/orders", body=body, request_id=request_id)

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"/v2/checkout/orders/{quote(order_id, safe='')}")

    def capture_order(self, order_id: str, request_id: str) -> dict:
        return self.request("POST", f"/v2/checkout/orders/{quote(order_id, safe='')}/capture", body={},
                            request_id=request_id)

    def refund_capture(self, capture_id: str, amount: dict, request_id: str) -> dict:
        return self.request("POST", f"/v2/payments/captures/{quote(capture_id, safe='')}/refund",
                            body={"amount": amount}, request_id=request_id)

    def verify_webhook(self, *, headers: dict, event: dict, webhook_id: str) -> bool:
        """PayPal's verify-webhook-signature API: PayPal checks the certificate and the signature."""
        out = self.request("POST", "/v1/notifications/verify-webhook-signature", body={
            "auth_algo": headers["paypal-auth-algo"], "cert_url": headers["paypal-cert-url"],
            "transmission_id": headers["paypal-transmission-id"],
            "transmission_sig": headers["paypal-transmission-sig"],
            "transmission_time": headers["paypal-transmission-time"],
            "webhook_id": webhook_id, "webhook_event": event})
        return out.get("verification_status") == "SUCCESS"


def _json(r: httpx.Response) -> dict:
    try:
        out = r.json()
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}
