from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from ..config import Settings
from .github_adapter import IntegrationConfigurationError


class EmailAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send_message(self, channel: str, subject: str | None, body: str) -> str:
        if not self.settings.email_real_send_allowed:
            raise IntegrationConfigurationError(
                "BOSS_EMAIL_REAL_SEND_ALLOWED is false."
            )
        if not self.settings.email_smtp_host:
            raise IntegrationConfigurationError("BOSS_EMAIL_SMTP_HOST is not configured.")
        if not self.settings.email_from_address:
            raise IntegrationConfigurationError(
                "BOSS_EMAIL_FROM_ADDRESS is not configured."
            )
        if self.settings.email_smtp_use_ssl and self.settings.email_smtp_use_tls:
            raise IntegrationConfigurationError(
                "BOSS_EMAIL_SMTP_USE_SSL and BOSS_EMAIL_SMTP_USE_TLS cannot both be true."
            )

        username = self.settings.email_smtp_username
        password = self.settings.email_smtp_password
        if bool(username) != bool(password):
            raise IntegrationConfigurationError(
                "BOSS_EMAIL_SMTP_USERNAME and BOSS_EMAIL_SMTP_PASSWORD must be set together."
            )

        recipient = self._recipient_from_channel(channel)
        message = EmailMessage()
        if self.settings.email_from_name:
            message["From"] = formataddr(
                (self.settings.email_from_name, self.settings.email_from_address)
            )
        else:
            message["From"] = self.settings.email_from_address
        message["To"] = recipient
        message["Subject"] = (subject or "BOSS notification").strip()
        message.set_content(body.strip())

        if self.settings.email_smtp_use_ssl:
            with smtplib.SMTP_SSL(
                self.settings.email_smtp_host,
                self.settings.email_smtp_port,
                timeout=30,
            ) as smtp:
                self._deliver(smtp, username, password, message)
        else:
            with smtplib.SMTP(
                self.settings.email_smtp_host,
                self.settings.email_smtp_port,
                timeout=30,
            ) as smtp:
                if self.settings.email_smtp_use_tls:
                    smtp.starttls()
                self._deliver(smtp, username, password, message)
        return f"smtp:{recipient}"

    def _deliver(
        self,
        smtp: smtplib.SMTP,
        username: str | None,
        password: str | None,
        message: EmailMessage,
    ) -> None:
        if username and password:
            # rc6 N-1: a host or org credential is never a login at an SMTP host it is not
            # bound to (smtplib writes to the socket itself, past the http guard).
            from .env_refs import check_outbound

            check_outbound(f"smtp://{getattr(smtp, '_host', '') or self.settings.email_smtp_host}/",
                           {"login": password})
            smtp.login(username, password)
        smtp.send_message(message)

    def _recipient_from_channel(self, channel: str) -> str:
        if not channel.startswith("email:"):
            raise IntegrationConfigurationError(
                "Email channels must use the email:recipient@example.com format."
            )
        recipient = channel.split(":", 1)[1].strip()
        parsed_name, parsed_address = parseaddr(recipient)
        if parsed_name or not parsed_address or "@" not in parsed_address:
            raise IntegrationConfigurationError(
                "Email channels must use the email:recipient@example.com format."
            )
        return parsed_address
