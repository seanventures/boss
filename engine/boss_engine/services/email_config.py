"""Setting up email sending, per project, without putting a password in a settings row.

WHY THIS EXISTS
---------------
Sending was environment-variable only: BOSS_EMAIL_SMTP_HOST and five others, set on the
container and changed by redeploying it. That is fine for an install somebody runs for
themselves and wrong for a product — an operator who wants a newsletter emailed should not have
to edit a compose file and restart the API to say which mail server to use.

ONE CONNECTOR, TWO WAYS TO SAY IT (operator decision 2026-10-22)
----------------------------------------------------------------
BOSS is open source: it ships the templates and an SMTP sender, and each install brings its own
mail service and domain (Brevo, SES, a company relay, Postal…). The console's one surface for it
is the Email (SMTP) connector in Skills & MCP. It takes either one connection URL —
`smtp://user:pass@host:587` (STARTTLS) or `smtps://user:pass@host:465` (implicit TLS), with the
user and password percent-encoded — or the separate host/port/TLS/login/key fields, plus a From
address and an optional From name. `connector_values` turns either into the same values, so the
stored row cannot tell which was typed. When the install enabled the local test inbox (Mailpit),
the connector may choose it instead: `mode: dev_inbox`.

WHERE EACH PIECE GOES, AND WHY THEY DIFFER
------------------------------------------
The host, port, sender and TLS flags are CONFIGURATION: worth seeing on a screen, worth putting
in a support question, harmless in a log. They live in a row.

The password is not. It goes through the same secret backend every other credential uses —
written through to the backend and referenced by a `secret_refs` row — so it is never in the
configuration table, never in an API response, and never in an event. `describe()` returns
whether one is set and nothing else: not its value, not its last four.

SETTINGS SAVED BEFORE THE CONNECTOR
-----------------------------------
The rc1 Email form and the old POST /v1/email/config saved under the same key in an older shape:
no `mode`, and a `password_last4`. `normalize` reads those rows as the connector (mode `smtp`)
and drops the last four (READ-COMPAT: nothing has to run at upgrade, and a row saved by an older
API after the upgrade still reads right).

THE ENVIRONMENT STILL WORKS. An install that already configured SMTP through env vars keeps
working untouched: the connector is consulted first and the environment is the fallback
(services/mail_transport.py has the whole order).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import quote, unquote

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id

#: The one row per project. A second mail server is a real want and not this change — adding it
#: later means a name column, which is a smaller step than unpicking a design that assumed one.
CONFIG_KEY = "email_smtp"

SECRET_NAME = "email_smtp_password"

#: Rows carry this shape number; a row without one was saved before the connector.
SHAPE = 2

#: The connector's modes: a real SMTP server, or the install's local test inbox (Mailpit).
MODES = ("smtp", "dev_inbox")

#: URL scheme → (TLS, the port it implies when the URL names none).
SCHEMES = {"smtp": ("starttls", 587), "smtps": ("ssl", 465)}

SECURITIES = ("none", "starttls", "ssl")


class EmailConfigError(ValueError):
    """Something about the SMTP settings is wrong. The message is shown to the operator; `field`
    names the input it is about (url, port, from_address…) when there is one. It never quotes the
    URL or the password back."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


_ENCODE_HINT = ("Percent-encode special characters in the user and password "
                "(/ is %2F, : is %3A, # is %23, ? is %3F, % is %25, @ is %40).")


def parse_url(url: str) -> dict[str, Any]:
    """`smtp://user:pass@host:587` → {host, port, username, password, security}.

    smtp:// is STARTTLS (587 when no port is given); smtps:// is implicit TLS (465). The user and
    password are percent-decoded. Every error says what is wrong and never repeats the URL, which
    carries the password."""
    raw = str(url or "").strip()
    if not raw:
        raise EmailConfigError(
            "Enter a connection URL, e.g. smtps://user:key@smtp.example.com:465.", "url")
    scheme, sep, rest = raw.partition("://")
    if not sep or scheme.lower() not in SCHEMES:
        raise EmailConfigError("The URL must start with smtp:// (STARTTLS, port 587) or "
                               "smtps:// (TLS, port 465).", "url")
    security, default_port = SCHEMES[scheme.lower()]
    userinfo, at, hostport = rest.rpartition("@")
    if not at:
        userinfo, hostport = "", rest
    # An unencoded /, ? or # in the password splits the URL in the wrong place.
    if re.search(r"[/?#]", userinfo):
        raise EmailConfigError(_ENCODE_HINT, "url")
    if re.search(r"[/?#]", hostport):
        raise EmailConfigError("The URL has a path or query after the host; write only "
                               "user:password@host:port. " + _ENCODE_HINT, "url")
    if hostport.startswith("["):                       # [2001:db8::1]:587
        host, _, after = hostport[1:].partition("]")
        colon, port_text = (":", after[1:]) if after.startswith(":") else ("", "")
    else:
        host, colon, port_text = hostport.rpartition(":")
        if not colon:
            host = hostport
    host = host.strip().lower()
    if not host:
        raise EmailConfigError(
            "The URL has no host, e.g. smtp://user:key@smtp.example.com:587.", "url")
    if colon:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise EmailConfigError("The port in the URL must be a number from 1 to 65535, "
                                   "usually 587 (smtp://) or 465 (smtps://).", "url")
        port = int(port_text)
    else:
        port = default_port
    user_text, _, pass_text = userinfo.partition(":")
    username, password = unquote(user_text), unquote(pass_text)
    if password and not username:
        raise EmailConfigError("The URL has a password but no user: write user:password@host.",
                               "url")
    return {"host": host, "port": port, "username": username, "password": password,
            "security": security}


def display_url(row: dict[str, Any]) -> str:
    """The stored connection as a URL WITHOUT its password — what the connector shows. Parsing it
    back gives the same server, and saving it unchanged keeps the stored key."""
    if not row or not row.get("host"):
        return ""
    scheme = "smtps" if row.get("use_ssl") else "smtp"
    user = str(row.get("username") or "")
    auth = f"{quote(user, safe='')}@" if user else ""
    return f"{scheme}://{auth}{row.get('host')}:{int(row.get('port') or 587)}"


def connector_values(*, url: Optional[str] = None, host: Optional[str] = None,
                     port: Any = None, security: Optional[str] = None,
                     username: Optional[str] = None, password: Optional[str] = None,
                     from_address: Optional[str] = None,
                     from_name: Optional[str] = None) -> dict[str, Any]:
    """What the connector stores, from either input: one URL, or the separate fields.

    Both give {host, port, username, password, security, use_tls, use_ssl, from_address,
    from_name} — the same values for the same server, whichever was typed. A URL, when given,
    decides the server and the login; the From address and name always come from their own
    fields."""
    if str(url or "").strip():
        server = parse_url(str(url))
    else:
        host_text = str(host or "").strip().lower()
        if not host_text:
            raise EmailConfigError("Enter a connection URL, or a host under Fields.", "url")
        port_raw = str(port if port is not None else "").strip()
        try:
            port_n = int(port_raw) if port_raw else 587
        except ValueError:
            raise EmailConfigError("Port must be a number, usually 587 or 465.", "port") from None
        if not 1 <= port_n <= 65535:
            raise EmailConfigError(f"Port must be between 1 and 65535 — got {port_n}.", "port")
        chosen = str(security or "").strip().lower()
        if chosen and chosen not in SECURITIES:
            raise EmailConfigError("TLS must be None, STARTTLS or SSL.", "security")
        server = {"host": host_text, "port": port_n,
                  "username": str(username or "").strip(), "password": str(password or ""),
                  "security": chosen or ("ssl" if port_n == 465 else "starttls")}
    sender = str(from_address or "").strip()
    if not sender:
        raise EmailConfigError("A From address is required, e.g. boss@example.com.",
                               "from_address")
    local, at, domain = sender.rpartition("@")
    if not at or not local or "." not in domain or any(c.isspace() for c in sender):
        raise EmailConfigError("The From address must look like boss@example.com.",
                               "from_address")
    return {**server, "from_address": sender, "from_name": str(from_name or "").strip(),
            "use_tls": server["security"] == "starttls", "use_ssl": server["security"] == "ssl"}


def save(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    host: str,
    from_address: str,
    actor: str,
    port: int = 587,
    username: str = "",
    password: Optional[str] = None,
    use_tls: bool = True,
    use_ssl: bool = False,
    from_name: str = "",
) -> dict[str, Any]:
    """Store how this project sends email. The password goes to the secret backend."""
    host = str(host or "").strip()
    if not host:
        raise EmailConfigError("an SMTP host is required, e.g. smtp.example.com.", "host")
    from_address = str(from_address or "").strip()
    if "@" not in from_address:
        raise EmailConfigError(
            f"'{from_address}' is not an address mail can be sent from.", "from_address")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise EmailConfigError("port must be a number, usually 587 or 465.", "port") from None
    if not 1 <= port <= 65535:
        raise EmailConfigError(f"port must be between 1 and 65535 — got {port}.", "port")
    if use_tls and use_ssl:
        # smtplib cannot do both, and the failure it gives is about a handshake rather than
        # about the two switches somebody set.
        raise EmailConfigError(
            "choose STARTTLS or SSL, not both. Port 587 is usually STARTTLS; 465 is SSL.",
            "security")

    secret_ref = _existing_secret_ref(connection, project_id)
    if password:
        from .secret_backends import default_secret_backend
        from .secrets_service import put_secret

        # THE BEST STORE THIS INSTALL ACTUALLY HAS, not a hardcoded one. put_secret defaults to
        # the platform store, which most installs do not run — so a hardcoded default meant the
        # password could not be saved at all on a community install with the ordinary chain.
        stored = put_secret(
            connection, settings, project_id=project_id, name=SECRET_NAME,
            value=password, secret_kind="api_token",
            backend=default_secret_backend(settings), actor=actor)
        secret_ref = stored.get("id") or stored.get("secret_ref_id") or secret_ref

    # NO LAST FOUR: the key shows as "set" and nothing more (a part of a short key is the key).
    body = {
        "shape": SHAPE, "mode": "smtp",
        "host": host, "port": port, "username": str(username or "").strip(),
        "from_address": from_address, "from_name": str(from_name or "").strip(),
        "use_tls": bool(use_tls), "use_ssl": bool(use_ssl),
        "secret_ref_id": secret_ref,
    }
    _write(connection, project_id, body, actor)
    # The event carries the host, never the password — an event stream is read by more people
    # and kept longer than anybody expects.
    emit_event(connection, project_id, "email.smtp_configured", actor,
               "project_configuration", CONFIG_KEY,
               {"host": host, "port": port, "from_address": from_address})
    return describe(connection, project_id)


def save_dev_inbox(connection: Connection, settings: Settings, *, project_id: str,
                   actor: str) -> dict[str, Any]:
    """The connector chooses the install's local test inbox (Mailpit). Refused unless the install
    enabled it: nothing here turns Mailpit on."""
    from .mail_transport import dev_inbox_enabled

    if not dev_inbox_enabled(settings):
        raise EmailConfigError("The local test inbox is off on this install. Re-run the "
                               "installer with --with-mailpit to turn it on.")
    _write(connection, project_id, {"shape": SHAPE, "mode": "dev_inbox"}, actor)
    emit_event(connection, project_id, "email.smtp_configured", actor,
               "project_configuration", CONFIG_KEY, {"mode": "dev_inbox"})
    return describe(connection, project_id)


def _write(connection: Connection, project_id: str, body: dict[str, Any], actor: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO project_configurations (id, project_id, config_key, body, updated_by)
            VALUES (:id, :p, :k, CAST(:body AS JSONB), :actor)
            ON CONFLICT (project_id, config_key) DO UPDATE
              SET body = EXCLUDED.body, updated_by = EXCLUDED.updated_by, updated_at = now()
            """
        ),
        {"id": make_id("cfg"), "p": project_id, "k": CONFIG_KEY,
         "body": json.dumps(body), "actor": actor},
    )
    from .mail_transport import forget_cached

    forget_cached()


def _existing_secret_ref(connection: Connection, project_id: str) -> Optional[str]:
    """Keep the stored password when settings are edited without retyping it.

    Re-entering a password to change a port is the kind of friction that gets solved by writing
    it in a text file somewhere.
    """
    row = _row(connection, project_id)
    return (row or {}).get("secret_ref_id")


def _row(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    try:
        found = connection.execute(
            text("SELECT body FROM project_configurations "
                 "WHERE project_id = :p AND config_key = :k"),
            {"p": project_id, "k": CONFIG_KEY},
        ).scalar()
    except Exception:  # noqa: BLE001 — an install predating the table must still send by env
        return None
    if isinstance(found, str):
        try:
            found = json.loads(found)
        except json.JSONDecodeError:
            return None
    return normalize(found) if isinstance(found, dict) else None


def normalize(body: dict[str, Any]) -> Optional[dict[str, Any]]:
    """A stored row in the connector's shape, whenever it was saved (READ-COMPAT).

    A row with no `shape` was saved before the connector (rc1's Email form, the old
    POST /v1/email/config, the Kit's first Email card): it is an SMTP connection, so it reads as
    mode `smtp`, and its `password_last4` is dropped — the key shows as "set" and nothing more.
    A row with no host that is not the dev inbox is not a connection."""
    row = dict(body)
    row.pop("password_last4", None)
    row["mode"] = row.get("mode") if row.get("mode") in MODES else "smtp"
    row.setdefault("shape", 1)
    if row["mode"] == "smtp":
        if not row.get("host"):
            return None
        row["use_tls"] = bool(row.get("use_tls"))
        row["use_ssl"] = bool(row.get("use_ssl"))
        try:
            row["port"] = int(row.get("port") or (465 if row["use_ssl"] else 587))
        except (TypeError, ValueError):
            row["port"] = 465 if row["use_ssl"] else 587
    return row


def describe(connection: Connection, project_id: str) -> dict[str, Any]:
    """What is configured, for a screen. Never the password — only whether there is one."""
    row = _row(connection, project_id)
    if not row:
        return {"configured": False}
    if row["mode"] == "dev_inbox":
        return {"configured": True, "mode": "dev_inbox", "has_password": False}
    return {
        "configured": True,
        "mode": "smtp",
        "url": display_url(row),
        "host": row.get("host"),
        "port": row.get("port"),
        "username": row.get("username") or "",
        "from_address": row.get("from_address"),
        "from_name": row.get("from_name") or "",
        "use_tls": bool(row.get("use_tls")),
        "use_ssl": bool(row.get("use_ssl")),
        # Whether, not what — not even its last four.
        "has_password": bool(row.get("secret_ref_id")),
    }


def settings_for(connection: Connection, settings: Settings, project_id: str) -> Settings:
    """Settings with this project's SMTP connector applied, or unchanged when it has none (or it
    chose the dev inbox, which services/mail_transport.py resolves).

    The environment remains the fallback, so an install that configured SMTP the old way keeps
    working and this is an additional way to configure rather than a replacement.
    """
    row = _row(connection, project_id)
    if not row or row.get("mode") != "smtp":
        return settings
    return _applied(settings, row, _password(connection, settings, row))


def _password(connection: Connection, settings: Settings, row: dict[str, Any]) -> Optional[str]:
    """The stored key, read from the secret backend when mail is sent (kept nowhere else)."""
    if not row.get("secret_ref_id"):
        # No stored key: a server that takes no login. The environment's key is never lent to
        # a server the connector named.
        return None
    try:
        from .secret_backends import resolve_secret_ref

        return resolve_secret_ref(connection, str(row["secret_ref_id"]), settings)
    except Exception:  # noqa: BLE001
        # A password that cannot be read is better reported by the send attempt, which
        # says which server refused, than by a config lookup that says nothing useful.
        return None


def _applied(settings: Settings, row: dict[str, Any], password: Optional[str]) -> Settings:
    if not row.get("username"):
        # A server that takes no login is sent no key, even one stored for an older login.
        password = None
    return settings.model_copy(update={
        "email_smtp_host": row.get("host"),
        "email_smtp_port": int(row.get("port") or 587),
        "email_smtp_username": row.get("username") or None,
        "email_smtp_password": password,
        "email_from_address": row.get("from_address"),
        "email_from_name": row.get("from_name") or None,
        "email_smtp_use_tls": bool(row.get("use_tls")),
        "email_smtp_use_ssl": bool(row.get("use_ssl")),
        # CONFIGURING IS CONSENTING. An operator who has just typed their mail server and
        # password into BOSS has said to send; making them also set an environment variable is
        # a second switch for a decision they already made. The env flag still governs an
        # install that configured SMTP that way and never used this screen.
        "email_real_send_allowed": True,
    })
