"""Slack SOCKET MODE (P9a-3, opt-in; boss-eventing-lld.md §3.7, A.1).

For an install Slack cannot reach (no public URL), a Slack connection may run in `socket` mode:
BOSS opens the WebSocket to Slack (`apps.connections.open` with the app-level `xapp-` token) and
Slack pushes the same Events API payloads down it. Each envelope is stored through the front
door's own insert (`event_intake.accept`, deduplicated on `event_id`) and acked by its
`envelope_id` only AFTER the row committed — an envelope we could not store is not acked, so
Slack sends it again.

ONE CONSUMER PER CONNECTION, INSTALL-WIDE. Every API process runs the supervisor; a database
lease (`extension_job_leases`, job `slack_socket:<connection>`) lets exactly one of them hold the
socket, renewed while it is open; a process that dies loses it within the lease TTL and another
takes over. Socket Mode is not allowed in the public Slack Marketplace and keeps at most 10
sockets per app — one is all BOSS opens.

The app-level token is read from the secret store when the socket opens and never logged.
"""
from __future__ import annotations

import json
import logging
import socket as _socket
import secrets as _secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger("boss.intake.slack.socket")

LEASE_EXTENSION = "boss.intake"
LEASE_TTL = timedelta(seconds=60)
RENEW_SECONDS = 20.0
SUPERVISE_SECONDS = 10.0
HOLDER = f"{_socket.gethostname()[:40]}:{_secrets.token_hex(3)}"


class Reconnect(Exception):
    """Slack asked us to reconnect (a `disconnect` envelope), or the socket closed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def job_name(connection_id: str) -> str:
    return f"slack_socket:{connection_id}"


# ── the lease ───────────────────────────────────────────────────────────────────────────────

def take_lease(engine: Any, connection_id: str, holder: str = HOLDER,
               now: Optional[datetime] = None) -> bool:
    """Take or renew the connection's socket lease. True when `holder` holds it."""
    from ....extensions.jobs import claim, ensure_row

    now = now or _now()
    with engine.begin() as db:
        ensure_row(db, LEASE_EXTENSION, job_name(connection_id), now)
        return claim(db, LEASE_EXTENSION, job_name(connection_id), holder, now, LEASE_TTL)


def release_lease(engine: Any, connection_id: str, holder: str = HOLDER, *, ok: bool = True,
                  error: Optional[str] = None) -> None:
    from ....extensions.jobs import finish

    now = _now()
    with engine.begin() as db:
        finish(db, LEASE_EXTENSION, job_name(connection_id), holder, next_run_at=now,
               status="ok" if ok else "failed", error=error, now=now)


# ── one envelope ────────────────────────────────────────────────────────────────────────────

def handle_envelope(engine: Any, settings: Any, row: Mapping[str, Any],
                    envelope: Mapping[str, Any]) -> Optional[dict]:
    """Store one Socket Mode envelope; return the ack to send (None: do not ack — Slack will
    deliver it again). Raises Reconnect on Slack's `disconnect`."""
    from ....services import event_intake
    from ...intake import REGISTRY
    from ..contract import RawRequest, Verified

    kind = str(envelope.get("type") or "")
    envelope_id = str(envelope.get("envelope_id") or "")
    if kind == "hello":
        return None
    if kind == "disconnect":
        raise Reconnect(str(envelope.get("reason") or "disconnect"))
    if kind == "interactive":
        # In Socket Mode, decision-button presses and modal submissions arrive HERE, not at the
        # endpoint URL: they take the same path the URL does (chat_channels.handle_inbound).
        return handle_interactive(engine, settings, row, envelope)
    if kind != "events_api":
        # Slash commands: BOSS has none; acked so Slack does not resend them.
        return {"envelope_id": envelope_id} if envelope_id else None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return {"envelope_id": envelope_id} if envelope_id else None
    retry = int(envelope.get("retry_attempt") or 0)
    headers = {"content-type": "application/json"}
    if retry:
        headers["x-slack-retry-num"] = str(retry)
        if envelope.get("retry_reason"):
            headers["x-slack-retry-reason"] = str(envelope["retry_reason"])[:60]
    request = RawRequest(method="POST", headers=headers,
                         body=json.dumps(payload, separators=(",", ":")).encode())
    adapter = REGISTRY.get("slack")
    got = event_intake.accept(
        engine, adapter, row, request,
        Verified(scheme="slack_socket", retry={"num": retry, "reason": envelope.get(
            "retry_reason")} if retry else None),
        settings=settings, handshake=False)
    if int(got.get("http_status") or 500) >= 300:
        return None
    return {"envelope_id": envelope_id} if envelope_id else None


def handle_interactive(engine: Any, settings: Any, row: Mapping[str, Any],
                       envelope: Mapping[str, Any]) -> Optional[dict]:
    """A Socket Mode `interactive` envelope (block_actions / view_submission) through the chat
    endpoint's own handler: the signed single-use token, the identity link and the decision
    rules all apply exactly as on the URL. The ack can carry the modal's answer."""
    from ....services import chat_channels
    from ....services.chat_adapters import SlackChat
    from ....services.chat_reply import _kick

    envelope_id = str(envelope.get("envelope_id") or "")
    payload = envelope.get("payload")
    endpoint_id = row.get("chat_endpoint_id")
    if not isinstance(payload, dict) or not endpoint_id:
        return {"envelope_id": envelope_id} if envelope_id else None
    inbound = SlackChat()._interaction(payload)  # noqa: SLF001 — the one interaction parser
    with engine.begin() as c:
        endpoint = chat_channels.get_endpoint(c, str(endpoint_id))
        if endpoint is None or endpoint.get("status") != "active":
            return {"envelope_id": envelope_id} if envelope_id else None
        result = chat_channels.handle_inbound(c, settings, endpoint, inbound)
    if result.get("status") == "note_required":
        try:
            with engine.begin() as c:
                chat_channels.open_note_form(c, settings, endpoint, result, inbound)
        except Exception:  # noqa: BLE001 — the press is answered in words by the outbox
            log.warning("socket mode: note form not opened", exc_info=True)
    _kick()
    ack: dict = {"envelope_id": envelope_id} if envelope_id else {}
    if inbound.kind == "note_submitted":
        ack["payload"] = ({"response_action": "clear"}
                          if result.get("status") in ("done", "pending_second") else
                          {"response_action": "errors",
                           "errors": {"note": str(result.get("words") or "Refused")[:150]}})
    return ack or None


# ── the socket ──────────────────────────────────────────────────────────────────────────────

def open_url(app_token: str) -> str:
    """`apps.connections.open` with the app-level token: the WebSocket URL to connect to."""
    from ....services.chat_adapters import SLACK_API, TRANSPORT, ChatSendFailed

    status, data = TRANSPORT("POST", f"{SLACK_API}/apps.connections.open",
                             {"Content-Type": "application/x-www-form-urlencoded",
                              "Authorization": f"Bearer {app_token}"}, b"",
                             "intake.slack.socket")
    if status >= 300 or not isinstance(data, dict) or not data.get("ok") or not data.get("url"):
        error = data.get("error") if isinstance(data, dict) else None
        raise ChatSendFailed(f"Slack refused apps.connections.open ({status}): "
                             f"{error or 'unknown'}")
    return str(data["url"])


def _default_connect(url: str) -> Any:
    from websockets.sync.client import connect

    return connect(url, open_timeout=10, close_timeout=5, max_size=2 ** 22)


#: url -> a socket with recv(timeout=) / send(str) / close(). Tests replace it.
CONNECT: Callable[[str], Any] = _default_connect


class Consumer:
    """The one socket of one connection, while this process holds its lease."""

    def __init__(self, engine: Any, settings: Any, connection_id: str, *,
                 holder: str = HOLDER):
        self.engine, self.settings, self.connection_id, self.holder = (
            engine, settings, connection_id, holder)
        self.stop = threading.Event()
        self.acked = 0

    def _row(self) -> Optional[dict]:
        with self.engine.begin() as c:
            row = c.execute(text("SELECT * FROM intake_connections WHERE id = :c"),
                            {"c": self.connection_id}).mappings().first()
        return dict(row) if row else None

    def _app_token(self, row: Mapping[str, Any]) -> str:
        from ....services import event_intake

        with self.engine.begin() as c:
            conn = event_intake.to_intake_connection(c, self.settings, row)
            return conn.secret("app_token") or ""

    def run_once(self) -> str:
        """Hold the socket until the lease is lost, Slack asks to reconnect, or stop. Returns
        why it ended."""
        row = self._row()
        if row is None or row["status"] != "active" or row["mode"] != "socket":
            return "not_socket"
        token = self._app_token(row)
        if not token:
            return "no_app_token"
        ws = CONNECT(open_url(token))
        renewed = time.monotonic()
        try:
            while not self.stop.is_set():
                if time.monotonic() - renewed >= RENEW_SECONDS:
                    if not take_lease(self.engine, self.connection_id, self.holder):
                        return "lease_lost"
                    renewed = time.monotonic()
                try:
                    message = ws.recv(timeout=1.0)
                except TimeoutError:
                    continue
                if message is None:
                    return "closed"
                try:
                    envelope = json.loads(message)
                except ValueError:
                    continue
                if not isinstance(envelope, dict):
                    continue
                try:
                    ack = handle_envelope(self.engine, self.settings, row, envelope)
                except Reconnect:
                    return "reconnect"
                if ack is not None:
                    ws.send(json.dumps(ack))
                    self.acked += 1
            return "stopped"
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


_RUNNING: dict[str, threading.Thread] = {}
_LOCK = threading.Lock()
_STOP = threading.Event()
_STARTED = threading.Event()


def _consume(engine: Any, settings: Any, connection_id: str) -> None:
    from ....db import background

    with background():
        consumer = Consumer(engine, settings, connection_id)
        why, ok = "stopped", True
        try:
            while not _STOP.is_set():
                why = consumer.run_once()
                if why in ("not_socket", "no_app_token", "lease_lost", "stopped"):
                    break
                if not take_lease(engine, connection_id):
                    why = "lease_lost"
                    break
        except Exception as error:  # noqa: BLE001 — the supervisor retries on its next sweep
            ok, why = False, f"{type(error).__name__}: {error}"[:300]
            log.warning("slack socket %s ended: %s", connection_id, why)
        finally:
            try:
                release_lease(engine, connection_id, ok=ok, error=None if ok else why)
            except Exception:  # noqa: BLE001
                pass
            with _LOCK:
                _RUNNING.pop(connection_id, None)


def supervise(engine: Any, settings: Any) -> list[str]:
    """One sweep: every active socket-mode Slack connection this process can lease gets a
    consumer thread. Returns the connections it started."""
    from ....db import background
    from ....services import projects_archive

    with background():
        with engine.begin() as c:
            ids = [str(r[0]) for r in c.execute(text(
                "SELECT id FROM intake_connections WHERE kind = 'slack' AND mode = 'socket' "
                "AND status = 'active' AND " + projects_archive.live_sql("intake_connections.project_id")
            )).all()]
        started = []
        for connection_id in ids:
            with _LOCK:
                if connection_id in _RUNNING:
                    continue
            if not take_lease(engine, connection_id):
                continue
            thread = threading.Thread(target=_consume, args=(engine, settings, connection_id),
                                      daemon=True, name=f"boss-slack-socket-{connection_id[-6:]}")
            with _LOCK:
                _RUNNING[connection_id] = thread
            thread.start()
            started.append(connection_id)
        return started


def start(engine: Any, settings: Any) -> bool:
    if _STARTED.is_set():
        return False
    _STARTED.set()
    _STOP.clear()

    def loop() -> None:
        while not _STOP.is_set():
            try:
                supervise(engine, settings)
            except Exception:  # noqa: BLE001 — the next sweep tries again
                log.debug("slack socket supervisor sweep failed", exc_info=True)
            _STOP.wait(SUPERVISE_SECONDS)

    threading.Thread(target=loop, daemon=True, name="boss-slack-socket-supervisor").start()
    return True


def stop() -> None:
    _STOP.set()
    _STARTED.clear()


__all__ = ["CONNECT", "Consumer", "HOLDER", "LEASE_TTL", "Reconnect", "handle_envelope",
           "handle_interactive",
           "job_name", "open_url", "release_lease", "start", "stop", "supervise", "take_lease"]
