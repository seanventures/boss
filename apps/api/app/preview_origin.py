"""THE PREVIEW ORIGIN: a listener that serves previews and nothing else (security finding #4).

A preview is someone else's app. Served on the console's origin, one path spelling the console's
matcher missed (`/v1/previews/x/%77eb/`) arrived without the sandbox CSP, and the app's script
read the CSRF token from /v1/identity/me and wrote with the operator's cookie. Matching paths
more carefully fixes that spelling; a separate origin fixes the class: nothing on this origin can
speak for anybody.

This ASGI app runs on its own port (BOSS_PREVIEW_PORT, default 3001; the console's host on that
port is the default preview origin, BOSS_PREVIEW_ORIGIN overrides it) and:

  - serves ONLY the preview capability paths, `/v1/previews/<id>/web[/...]` and
    `/v1/previews/<id>/feedback`, judged on the DECODED path as the router will see it, and on
    the raw path too; anything else is a 404 that never reaches the API;
  - STRIPS every credential before the request reaches anything: Authorization, every X-Boss-*
    header, every header a proxy uses to assert identity, and every cookie but the previewed
    app's OWN (cookies are not port-scoped, so the browser DOES send the console's session cookie
    here): only cookies carrying this preview's tag pass (boss_engine.services.preview_cookies);
  - relays a Set-Cookie only when it is one the preview gateway made for THIS preview (tagged,
    Path exactly `/v1/previews/<id>/`, no Domain), and always answers with the sandbox CSP,
    whatever the handler said. A preview PAGE's sandbox here includes allow-same-origin
    (page_csp, BOSSAPP-946), so a login app keeps its session cookie: safe only because this
    origin is never BOSS's own (preview_service.preview_origin_for refuses that).

It delegates to the API's own preview handlers (apps/api/app/main.py preview_web_proxy,
preview_websocket_proxy, submit_preview_feedback_endpoint), so there is one preview gateway.
It is started next to the API by the API's lifespan when apps/api/serve.py asks for it
(BOSS_PREVIEW_LISTEN), so tests and multi-worker runs never bind the port.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Awaitable, Callable, Optional

from boss_engine.services.preview_cookies import is_relayable_set_cookie, own_cookie_pairs
from boss_engine.services.preview_service import (
    PREVIEW_ORIGIN_SANDBOX_CSP, PREVIEW_SANDBOX_CSP, PREVIEW_TAB_NAME, is_boss_origin,
)

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The capability paths, and nothing else. The id is one segment that is not a dot segment.
PREVIEW_PATH = re.compile(r"^/v1/previews/(?!\.{1,2}/)([^/]+)/(?:web(?:/.*)?|feedback/?)$", re.S)

#: Headers that could carry a credential or name a principal. Dropped before anything sees them.
_IDENTITY_HEADER = re.compile(
    r"^(authorization|proxy-authorization|cookie|x-boss-.*|x-api-key|x-ops-token|x-csrf-token|"
    r"x-xsrf-token|x-user(-.*)?|x-forwarded-.*|forwarded|x-real-ip|x-auth-request-.*|"
    r"x-remote-user|remote-user|x-webauth-.*|tailscale-user-.*|cf-access-.*|"
    r"x-ms-client-principal.*|x-goog-authenticated-user-.*|x-amzn-oidc-.*|x-principal.*|"
    r"x-actor.*|x-sandbox-token)$",
    re.I,
)

_NOT_FOUND = b'{"detail":"Not found: this origin serves previews only."}'

#: A RELOAD AT THE APP'S OWN PATH. On this origin a preview is shown at its own paths
#: (preview_service._inject_shim, at_root): the address bar says "/tickets", not
#: "/v1/previews/<id>/web/tickets", so the app's router matches it. Reloading it (or a back/forward
#: that left the page) asks this origin for "/tickets". This page — static, no credential, no
#: request of its own — sends the tab back to ITS preview, which the shim named in the tab's
#: `window.name`; the id is checked to be one path segment, and only a preview path on this same
#: origin is ever opened. A tab with no preview named is told so.
_BOOTSTRAP = (
    "<!doctype html><meta charset=utf-8><title>Preview</title>"
    "<p id=m style=\"font:14px system-ui;padding:24px\">Opening the preview…</p>"
    "<script>(function(){var n=\"\";try{n=String(window.name||\"\")}catch(e){}"
    "var k=" + json.dumps(PREVIEW_TAB_NAME) + ",id=n.indexOf(k)===0?n.slice(k.length):\"\";"
    "if(/^[A-Za-z0-9_-]{1,100}$/.test(id)){location.replace(\"/v1/previews/\"+id+\"/web\"+"
    "location.pathname+location.search+location.hash);return;}"
    "document.getElementById(\"m\").textContent=\"This address belongs to a BOSS preview, but this "
    "tab no longer knows which one. Open the preview again from BOSS.\";})()</script>"
).encode()


def _is_page_navigation(scope: Scope) -> bool:
    """A browser loading a top-level page (a reload, back/forward), never a script's request."""
    headers = {k.lower(): v for k, v in scope.get("headers") or []}
    return (scope.get("method") == "GET"
            and headers.get(b"sec-fetch-mode") == b"navigate"
            and headers.get(b"sec-fetch-dest") in (b"document", b"iframe"))


def is_preview_capability_path(path: str) -> bool:
    return bool(PREVIEW_PATH.match(path or ""))


def preview_id_of(path: str) -> Optional[str]:
    found = PREVIEW_PATH.match(path or "")
    return found.group(1) if found else None


def stripped_headers(headers: list[tuple[bytes, bytes]],
                     preview_id: Optional[str] = None) -> list[tuple[bytes, bytes]]:
    """The request headers with every credential and identity assertion removed. With a
    `preview_id`, that preview's OWN cookies (still tagged) come back as one Cookie header."""
    out = [(k, v) for k, v in headers if not _IDENTITY_HEADER.match(k.decode("latin-1"))]
    if preview_id:
        cookies = [v.decode("latin-1") for k, v in headers if k.lower() == b"cookie"]
        own = own_cookie_pairs(preview_id, cookies)
        if own:
            out.append((b"cookie", "; ".join(f"{n}={v}" for n, v in own).encode("latin-1")))
    return out


def page_csp(scope: Scope) -> str:
    """The sandbox a preview PAGE gets here: with allow-same-origin, so the app keeps its own
    session cookie (BOSSAPP-946) — this listener is the preview origin, never BOSS's. Should a
    request arrive here under BOSS's own origin all the same (a proxy pointing the console's
    name at this port), the page stays opaque."""
    headers = {k.lower(): v for k, v in scope.get("headers") or []}
    host = headers.get(b"host", b"").decode("latin-1").strip()
    scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
    if not host or is_boss_origin(f"{scheme}://{host}"):
        return PREVIEW_SANDBOX_CSP
    return PREVIEW_ORIGIN_SANDBOX_CSP


def _isolated_response_headers(headers: list[tuple[bytes, bytes]],
                               preview_id: Optional[str] = None,
                               csp: str = PREVIEW_SANDBOX_CSP) -> list[tuple[bytes, bytes]]:
    out = [(k, v) for k, v in headers
           if k.lower() not in (b"set-cookie", b"content-security-policy",
                                b"access-control-allow-credentials")
           or (k.lower() == b"set-cookie" and preview_id
               and is_relayable_set_cookie(preview_id, v.decode("latin-1")))]
    out.append((b"content-security-policy", csp.encode()))
    return out


def _paths_of(scope: Scope) -> tuple[str, str]:
    path = scope.get("path") or ""
    raw = scope.get("raw_path")
    raw_text = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else path
    return path, raw_text.split("?", 1)[0]


async def _preflight(scope: Scope, send: Send) -> None:
    """A sandboxed preview has an opaque origin, so its JSON POST to its own backend (or the
    overlay's) is cross-origin and preflighted. Answered here, without credentials, and never
    allowing a credential header."""
    asked = ""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"access-control-request-headers":
            asked = value.decode("latin-1")
    allowed = [h.strip() for h in asked.split(",") if h.strip() and not _IDENTITY_HEADER.match(h.strip())]
    headers = [
        (b"access-control-allow-origin", b"*"),
        (b"access-control-allow-methods", b"GET, POST, PUT, PATCH, DELETE"),
        (b"access-control-max-age", b"600"),
        (b"cache-control", b"no-store"),
        (b"content-security-policy", PREVIEW_SANDBOX_CSP.encode()),
    ]
    if allowed:
        headers.append((b"access-control-allow-headers", ", ".join(allowed).encode("latin-1")))
    await send({"type": "http.response.start", "status": 204, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


def preview_only(app: ASGIApp) -> ASGIApp:
    """Wrap `app` so it serves only the preview capability paths, credential-free."""

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind == "lifespan":   # the API's own lifespan already ran; never a second one
            message = await receive()
            while message.get("type") != "lifespan.shutdown":
                if message.get("type") == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                message = await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        if kind not in ("http", "websocket"):
            return
        path, raw = _paths_of(scope)
        # Both spellings must be preview paths: the decoded one is what the router matches.
        if not (is_preview_capability_path(path) and is_preview_capability_path(raw)) or \
                "\\" in path or "\x00" in path:
            if kind == "http" and _is_page_navigation(scope) and not path.startswith("/v1/") \
                    and "\\" not in path and "\x00" not in path:
                await send({"type": "http.response.start", "status": 200, "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"content-security-policy", PREVIEW_SANDBOX_CSP.encode()),
                ]})
                await send({"type": "http.response.body", "body": _BOOTSTRAP})
                return
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 4404})
                return
            await send({"type": "http.response.start", "status": 404, "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"content-security-policy", PREVIEW_SANDBOX_CSP.encode()),
            ]})
            await send({"type": "http.response.body", "body": _NOT_FOUND})
            return
        if kind == "http" and scope.get("method") == "OPTIONS":
            await _preflight(scope, send)
            return
        inner = dict(scope)
        # The id both spellings agree on: the browser scoped the preview's cookies to the raw path.
        preview_id = preview_id_of(path) if preview_id_of(path) == preview_id_of(raw) else None
        inner["headers"] = stripped_headers(list(scope.get("headers") or []),
                                            preview_id if kind == "http" else None)
        # The previewed app answers here, never the API's authenticated surface: mark the scope so
        # the handler can tell (and so nothing downstream mistakes it for a console request).
        inner.setdefault("state", {})
        inner["state"] = {**inner["state"], "boss_preview_origin": True}

        csp = page_csp(scope)

        async def isolated_send(message: dict) -> None:
            if message.get("type") == "http.response.start":
                message = {**message, "headers": _isolated_response_headers(
                    list(message.get("headers") or []), preview_id if kind == "http" else None,
                    csp)}
            await send(message)

        await app(inner, receive, isolated_send)

    return guarded


def preview_app() -> ASGIApp:
    """The preview-only app over the API's own handlers."""
    from .main import app as api_app
    return preview_only(api_app)


def listen_settings() -> Optional[tuple[str, int]]:
    """(host, port) when apps/api/serve.py asked this process to serve the preview origin."""
    from boss_engine.services.preview_service import preview_port
    if os.environ.get("BOSS_PREVIEW_LISTEN", "").strip() not in ("1", "true", "yes"):
        return None
    port = preview_port()
    if port is None:
        return None
    return (os.environ.get("BOSS_PREVIEW_HOST") or "0.0.0.0").strip() or "0.0.0.0", port


async def serve_preview_origin(host: str, port: int) -> Optional[asyncio.Task]:
    """Start the preview listener on this event loop. Never the process's signal handlers: those
    belong to the API's own server. A port that cannot be bound is reported, not fatal."""
    import uvicorn

    config = uvicorn.Config(preview_app(), host=host, port=port, lifespan="off",
                            proxy_headers=False, server_header=False, log_level="warning")
    server = uvicorn.Server(config)

    async def run() -> None:
        try:
            await server._serve()   # serve() would capture SIGINT/SIGTERM from the API
        except SystemExit:
            print(f"[preview-origin] could not listen on {host}:{port}; previews are unavailable")
        except asyncio.CancelledError:
            server.should_exit = True
            raise

    task = asyncio.create_task(run(), name="boss-preview-origin")
    task.boss_server = server  # type: ignore[attr-defined]
    print(f"[preview-origin] previews served on {host}:{port}")
    return task


async def stop_preview_origin(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    server = getattr(task, "boss_server", None)
    if server is not None:
        server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
        task.cancel()
