"""Server-side sessions for an ASGI app: the cookie holds only a random id, signed with
SESSION_SECRET; the data stays on the server. `request.session` works as with Starlette's
SessionMiddleware (Authlib keeps its login state there), plus:

  * `rotate_session(request)` gives the session a NEW id when the response is sent (call it at
    sign-in: no session fixation);
  * clearing the session deletes it on the server and expires the cookie.

The in-memory store is for one process; give a shared one (Redis, the database) when the app
runs more than one."""
from __future__ import annotations

import secrets
import threading
import time
from http.cookies import SimpleCookie
from typing import Optional

from itsdangerous import BadSignature, TimestampSigner


class MemorySessionStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def get(self, sid: str) -> Optional[dict]:
        with self._lock:
            hit = self._data.get(sid)
            if hit is None or hit[0] < time.time():
                self._data.pop(sid, None)
                return None
            return dict(hit[1])

    def set(self, sid: str, data: dict, max_age: int) -> None:
        with self._lock:
            self._data[sid] = (time.time() + max_age, dict(data))

    def delete(self, sid: str) -> None:
        with self._lock:
            self._data.pop(sid, None)


def rotate_session(request) -> None:
    request.scope["session_rotate"] = True


class ServerSessionMiddleware:
    def __init__(self, app, *, secret: str, https_only: bool, store=None, cookie_name: str = "sid",
                 max_age: int = 7 * 24 * 3600, same_site: str = "lax") -> None:
        self.app = app
        self.signer = TimestampSigner(secret, salt="login-oauth.session")
        self.store = store or MemorySessionStore()
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.flags = f"Path=/; HttpOnly; SameSite={same_site.capitalize()}" + ("; Secure" if https_only else "")

    def _sid_from(self, scope) -> Optional[str]:
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                jar = SimpleCookie()
                try:
                    jar.load(value.decode("latin-1"))
                except Exception:  # noqa: BLE001 - a malformed cookie header is no session
                    return None
                if self.cookie_name in jar:
                    try:
                        return self.signer.unsign(jar[self.cookie_name].value, max_age=self.max_age).decode()
                    except BadSignature:
                        return None
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        sid = self._sid_from(scope)
        loaded = self.store.get(sid) if sid else None
        if loaded is None:
            sid = None
        scope["session"] = dict(loaded or {})

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                session = scope["session"]
                rotate = scope.get("session_rotate")
                cookie = None
                if not session:
                    if sid:
                        self.store.delete(sid)
                        cookie = f"{self.cookie_name}=; {self.flags}; Max-Age=0"
                elif rotate or sid is None or session != loaded:
                    new_sid = sid
                    if rotate or sid is None:
                        if sid:
                            self.store.delete(sid)
                        new_sid = secrets.token_urlsafe(32)
                    self.store.set(new_sid, session, self.max_age)
                    if new_sid != sid:
                        signed = self.signer.sign(new_sid).decode()
                        cookie = f"{self.cookie_name}={signed}; {self.flags}; Max-Age={self.max_age}"
                if cookie:
                    message.setdefault("headers", [])
                    message["headers"] = list(message["headers"]) + [(b"set-cookie", cookie.encode("latin-1"))]
            await send(message)

        await self.app(scope, receive, send_wrapper)
