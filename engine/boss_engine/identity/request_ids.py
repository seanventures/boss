"""One request id per API request, on every security event it causes (IDENTITY.md §8.2: "Every
row records … request_id"). Owned by stream S5.

`RequestIdMiddleware` (pure ASGI, so it runs before routing and the identity guard):
  - keeps an incoming `X-Request-Id` when it is a short, safe token (a proxy's or the console's,
    so one id follows a click from the browser to the log), otherwise mints `req_<26 base32>`;
  - writes it into the request's headers, so the guard's RequestCredentials and therefore every
    AuthContext carry it (`SecurityEvent.from_context` copies it onto the event);
  - sets `current_request_id` for code that records without a context;
  - echoes it on the response as `X-Request-Id`, so a person can quote it to an admin.

Wire it once in main.py (lead's zone): `app.add_middleware(RequestIdMiddleware)`.
"""
from __future__ import annotations

import base64
import re
import secrets
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Optional

HEADER = "x-request-id"
_SAFE = re.compile(r"^[A-Za-z0-9._:\-]{8,64}$")

current_request_id: ContextVar[Optional[str]] = ContextVar("identity_request_id", default=None)


def new_request_id() -> str:
    raw = base64.b32encode(secrets.token_bytes(17)).decode("ascii").rstrip("=").lower()
    return f"req_{raw[:26]}"


def accept(value: Optional[str]) -> Optional[str]:
    """An incoming id is kept only if it cannot carry anything but an id."""
    value = (value or "").strip()
    return value if _SAFE.match(value) else None


class RequestIdMiddleware:
    def __init__(self, app: Callable[..., Awaitable[Any]]):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != HEADER.encode()]
        incoming = next((v.decode("latin-1") for k, v in scope.get("headers", [])
                         if k.lower() == HEADER.encode()), None)
        request_id = accept(incoming) or new_request_id()
        headers.append((HEADER.encode(), request_id.encode()))
        scope = dict(scope, headers=headers)
        token = current_request_id.set(request_id)

        async def send_with_id(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                out = [(k, v) for k, v in message.get("headers", [])
                       if k.lower() != HEADER.encode()]
                out.append((b"x-request-id", request_id.encode()))
                message = dict(message, headers=out)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            current_request_id.reset(token)
