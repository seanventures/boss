"""Setting up email sending, per project, without putting a password in a settings row.

WHY THIS EXISTS
---------------
Sending was environment-variable only: BOSS_EMAIL_SMTP_HOST and five others, set on the
container and changed by redeploying it. That is fine for an install somebody runs for
themselves and wrong for a product — an operator who wants a newsletter emailed should not have
to edit a compose file and restart the API to say which mail server to use.

WHERE EACH PIECE GOES, AND WHY THEY DIFFER
------------------------------------------
The host, port, sender and TLS flags are CONFIGURATION: worth seeing on a screen, worth putting
in a support question, harmless in a log. They live in a row.

The password is not. It goes through the same secret backend every other credential uses —
written through to the backend and referenced by a `secret_refs` row — so it is never in the
configuration table, never in an API response, and never in an event. `describe()` returns
whether one is set and nothing else.

THE ENVIRONMENT STILL WORKS. An install that already configured SMTP through env vars keeps
working untouched: project configuration is consulted first and the environment is the
fallback, so this adds a way to configure rather than replacing one.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id

#: The one row per project. A second mail server is a real want and not this change — adding it
#: later means a name column, which is a smaller step than unpicking a design that assumed one.
CONFIG_KEY = "email_smtp"

SECRET_NAME = "email_smtp_password"


class EmailConfigError(ValueError):
    """Something about the SMTP settings is wrong. The message is shown to the operator."""


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
        raise EmailConfigError("an SMTP host is required, e.g. smtp.example.com.")
    from_address = str(from_address or "").strip()
    if "@" not in from_address:
        raise EmailConfigError(
            f"'{from_address}' is not an address mail can be sent from.")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise EmailConfigError("port must be a number, usually 587 or 465.") from None
    if not 1 <= port <= 65535:
        raise EmailConfigError(f"port must be between 1 and 65535 — got {port}.")
    if use_tls and use_ssl:
        # smtplib cannot do both, and the failure it gives is about a handshake rather than
        # about the two switches somebody set.
        raise EmailConfigError(
            "choose STARTTLS or SSL, not both. Port 587 is usually STARTTLS; 465 is SSL.")

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

    body = {
        "host": host, "port": port, "username": str(username or "").strip(),
        "from_address": from_address, "from_name": str(from_name or "").strip(),
        "use_tls": bool(use_tls), "use_ssl": bool(use_ssl),
        "secret_ref_id": secret_ref,
    }
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
    # The event carries the host, never the password — an event stream is read by more people
    # and kept longer than anybody expects.
    emit_event(connection, project_id, "email.smtp_configured", actor,
               "project_configuration", CONFIG_KEY,
               {"host": host, "port": port, "from_address": from_address})
    return describe(connection, project_id)


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
    if isinstance(found, dict):
        return found
    if isinstance(found, str):
        try:
            return json.loads(found)
        except json.JSONDecodeError:
            return None
    return None


def describe(connection: Connection, project_id: str) -> dict[str, Any]:
    """What is configured, for a screen. Never the password — only whether there is one."""
    row = _row(connection, project_id)
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "host": row.get("host"),
        "port": row.get("port"),
        "username": row.get("username") or "",
        "from_address": row.get("from_address"),
        "from_name": row.get("from_name") or "",
        "use_tls": bool(row.get("use_tls")),
        "use_ssl": bool(row.get("use_ssl")),
        # Whether, not what.
        "has_password": bool(row.get("secret_ref_id")),
    }


def settings_for(connection: Connection, settings: Settings, project_id: str) -> Settings:
    """Settings with this project's SMTP applied, or unchanged when it has none.

    The environment remains the fallback, so an install that configured SMTP the old way keeps
    working and this is an additional way to configure rather than a replacement.
    """
    row = _row(connection, project_id)
    if not row or not row.get("host"):
        return settings

    password = settings.email_smtp_password
    if row.get("secret_ref_id"):
        try:
            from .secret_backends import resolve_secret_ref

            password = resolve_secret_ref(connection, str(row["secret_ref_id"]), settings)
        except Exception:  # noqa: BLE001
            # A password that cannot be read is better reported by the send attempt, which
            # says which server refused, than by a config lookup that says nothing useful.
            pass

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
