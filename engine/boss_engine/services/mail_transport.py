"""THE ONE WAY BOSS SENDS EMAIL.

Every sender — invitations, password reset, sign-in codes, workflow `email.send`, notify steps,
alert contact points, the outbox, the Test buttons — resolves its mail server here and hands the
message to EmailAdapter from here. tests/test_one_mail_transport.py fails when a module builds an
EmailAdapter, reads the SMTP settings, or imports smtplib anywhere else.

WHY: identity mail read only the environment while the console saved SMTP per project, so an
operator who set up mail on the Email screen still saw invitations "not emailed". Two readers of
one setting is a bug waiting for its second report; one resolver cannot disagree with itself.

WHICH SERVER, in order:
  1. the server saved in the console (Settings › Email) for the project — saving it is the
     operator's consent to send (email_config.settings_for);
  2. the environment (BOSS_EMAIL_SMTP_*), which sends for real only with
     BOSS_EMAIL_REAL_SEND_ALLOWED=true;
  3. the dev inbox (BOSS_EMAIL_DEV_INBOX, the bundled Mailpit) — nothing leaves the machine, so
     no switch is needed, and the operator sees every message without any provider account;
  4. nothing: the caller is told why, in words.

BOSS never ships a sender, a domain or a provider credential: every install sends as itself.
"""
from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..config import Settings, get_settings
from . import email_adapter
from .email_adapter import EmailAdapter, InlineImage
from .github_adapter import IntegrationConfigurationError

#: Where a dev inbox delivers when the install set no From of its own. `localhost` is reserved
#: (RFC 6761): it is nobody's domain, so it can never be mistaken for a real sender.
DEV_INBOX_FROM = "boss@localhost"


@dataclass(frozen=True)
class MailRoute:
    """Which server a message would go through, and why."""

    settings: Settings
    #: console | env | dev_inbox | none
    source: str
    #: For a screen: "smtp.example.com as boss@example.com", or why nothing is configured.
    detail: str

    @property
    def ready(self) -> bool:
        return self.source != "none"

    @property
    def real(self) -> bool:
        """True when mail leaves this machine (a provider), False for the dev inbox."""
        return self.source in ("console", "env")

    def describe(self) -> dict[str, Any]:
        out = {"ready": self.ready, "source": self.source, "detail": self.detail,
               "real": self.real}
        if self.source == "dev_inbox":
            out["inbox_url"] = self.settings.email_dev_inbox_url or None
        return out


@dataclass(frozen=True)
class Delivery:
    """What happened to one message: sent | failed | not_configured, with the real reason."""

    status: str
    reason: str = ""
    receipt: str = ""
    source: str = "none"

    @property
    def sent(self) -> bool:
        return self.status == "sent"

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason or None, "via": self.source}


def _saved_route(connection: Any, settings: Settings, project_id: str) -> Optional[Settings]:
    from . import email_config

    row = email_config._row(connection, project_id)
    if not row or not row.get("host"):
        return None
    return email_config.settings_for(connection, settings, project_id)


def _with_connection(fn, settings: Settings):
    """Run `fn(connection)` on a short read-only connection of our own (identity mail has none)."""
    try:
        from ..db import begin, get_engine

        with begin(get_engine(settings)) as connection:
            return fn(connection)
    except Exception:  # noqa: BLE001 — no database here: the environment still decides
        return None


def _dev_inbox(settings: Settings) -> Optional[Settings]:
    raw = str(getattr(settings, "email_dev_inbox", "") or "").strip()
    if not raw:
        return None
    host, _, port = raw.rpartition(":") if ":" in raw else (raw, "", "1025")
    try:
        port_n = int(port)
    except ValueError:
        return None
    return settings.model_copy(update={
        "email_smtp_host": host, "email_smtp_port": port_n,
        "email_smtp_username": None, "email_smtp_password": None,
        "email_smtp_use_tls": False, "email_smtp_use_ssl": False,
        "email_from_address": settings.email_from_address or DEV_INBOX_FROM,
        "email_real_send_allowed": True,
    })


def route(connection: Any = None, settings: Optional[Settings] = None,
          project_id: Optional[str] = None) -> MailRoute:
    """The server a message from `project_id` goes through. Identity mail passes no project and
    gets the install's default project — the one the console's Email screen edits."""
    settings = settings or get_settings()
    project_id = project_id or settings.default_project_id
    if connection is not None:
        try:
            with connection.begin_nested():
                saved = _saved_route(connection, settings, project_id)
        except Exception:  # noqa: BLE001 — an install predating the table sends by env
            saved = None
    else:
        saved = _with_connection(lambda c: _saved_route(c, settings, project_id), settings)
    if saved is not None:
        return MailRoute(saved, "console",
                         f"{saved.email_smtp_host} as {saved.email_from_address}")

    why = "No mail server is set. Add one under Settings › Email."
    if settings.email_smtp_host and settings.email_from_address:
        if settings.email_real_send_allowed:
            return MailRoute(settings, "env",
                             f"{settings.email_smtp_host} as {settings.email_from_address}")
        why = ("SMTP is set in the environment but BOSS_EMAIL_REAL_SEND_ALLOWED is false, "
               "so nothing would be sent.")
    elif settings.email_smtp_host:
        why = "SMTP is set in the environment without a From address (BOSS_EMAIL_FROM_ADDRESS)."

    dev = _dev_inbox(settings)
    if dev is not None:
        return MailRoute(dev, "dev_inbox",
                         f"the dev inbox at {dev.email_smtp_host}:{dev.email_smtp_port} — "
                         "nothing leaves this machine")
    return MailRoute(settings, "none", why)


# A screen that asks "is mail set up?" on every request should not open a connection each time.
_READY_CACHE: dict[str, tuple[float, bool]] = {}
_READY_TTL_SECONDS = 15.0


def ready(project_id: Optional[str] = None, *, settings: Optional[Settings] = None) -> bool:
    settings = settings or get_settings()
    key = project_id or settings.default_project_id
    hit = _READY_CACHE.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    value = route(None, settings, key).ready
    _READY_CACHE[key] = (now + _READY_TTL_SECONDS, value)
    return value


def forget_cached() -> None:
    """Saving the Email settings takes effect on the next message, not 15 seconds later."""
    _READY_CACHE.clear()


def _channel(to: str) -> str:
    to = str(to or "").strip()
    return to if to.startswith("email:") else f"email:{to}"


def send_with(settings: Settings, to: str, subject: Optional[str], text: str, *,
              html: Optional[str] = None, inline_images: tuple[InlineImage, ...] = ()) -> str:
    """Send through exactly these settings. Only for testing values that are not saved yet (the
    Email connection's Test); everything else goes through `send`."""
    adapter = EmailAdapter(settings)
    if html:
        return adapter.send_message(_channel(to), subject, text, html=html,
                                    inline_images=inline_images)
    return adapter.send_message(_channel(to), subject, text)


def send(to: str, subject: Optional[str], text: str, *, html: Optional[str] = None,
         inline_images: tuple[InlineImage, ...] = (), connection: Any = None,
         settings: Optional[Settings] = None, project_id: Optional[str] = None) -> str:
    """Send one message. Returns the receipt; raises IntegrationConfigurationError when no server
    is set (with the reason), and the SMTP error itself when the server refuses."""
    chosen = route(connection, settings, project_id)
    if not chosen.ready:
        raise IntegrationConfigurationError(chosen.detail)
    return send_with(chosen.settings, to, subject, text, html=html, inline_images=inline_images)


def deliver(to: str, subject: Optional[str], text: str, *, html: Optional[str] = None,
            inline_images: tuple[InlineImage, ...] = (), connection: Any = None,
            settings: Optional[Settings] = None, project_id: Optional[str] = None) -> Delivery:
    """`send`, answered as a Delivery that never raises: the status and the real reason, scrubbed
    of the credential, for a screen that must say why a message did not go."""
    chosen = route(connection, settings, project_id)
    if not chosen.ready:
        return Delivery("not_configured", chosen.detail, source="none")
    try:
        receipt = send_with(chosen.settings, to, subject, text, html=html,
                            inline_images=inline_images)
    except Exception as exc:  # noqa: BLE001 — the reason IS the result
        return Delivery("failed", failure_reason(exc, chosen.settings), source=chosen.source)
    return Delivery("sent", receipt=receipt, source=chosen.source)


def failure_reason(exc: BaseException, settings: Settings) -> str:
    """What went wrong, in words an operator can act on — never any part of the credential."""
    # smtplib is imported by the wire alone (the outbound-credential scan pins its senders).
    smtplib = email_adapter.smtplib
    host = f"{settings.email_smtp_host}:{settings.email_smtp_port}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        said = _server_said(exc.smtp_code, exc.smtp_error)
        why = (f"{host} refused the login ({said}). Use the SMTP login and SMTP key from your "
               "provider, not the account password.")
    elif isinstance(exc, smtplib.SMTPSenderRefused):
        why = (f"{host} refused the From address ({_server_said(exc.smtp_code, exc.smtp_error)}). "
               "Send from an address on a domain you verified with the provider.")
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        first = next(iter(exc.recipients.values()), (0, b""))
        why = f"{host} refused the recipient ({_server_said(first[0], first[1])})."
    elif isinstance(exc, smtplib.SMTPResponseException):
        why = f"{host} answered {_server_said(exc.smtp_code, exc.smtp_error)}."
    elif isinstance(exc, smtplib.SMTPNotSupportedError):
        why = f"{host} does not offer STARTTLS. Try port 465 with SSL, or turn STARTTLS off."
    elif isinstance(exc, ssl.SSLError):
        why = (f"The TLS handshake with {host} failed ({exc.reason or exc}). Port 587 is usually "
               "STARTTLS; 465 is SSL.")
    elif isinstance(exc, socket.gaierror):
        why = f"{settings.email_smtp_host} does not resolve. Check the SMTP host."
    elif isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
        why = f"{host} refused the connection. Check the host and port."
    elif isinstance(exc, (socket.timeout, TimeoutError)):
        why = f"{host} did not answer in time. Check the host, the port and any firewall."
    elif isinstance(exc, (smtplib.SMTPServerDisconnected, OSError)):
        why = f"Could not talk to {host}: {exc}"
    else:
        why = str(exc) or type(exc).__name__
    return scrub(why, settings)[:400]


def _server_said(code: Any, message: Any) -> str:
    text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message or "")
    return f"{code} {' '.join(text.split())}".strip()


def scrub(text: str, settings: Settings) -> str:
    """Remove the SMTP password (and the login, which for some providers is a key) from a reason."""
    for secret in (settings.email_smtp_password, settings.email_smtp_username):
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return text
