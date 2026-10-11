"""PayPal settings for the `paypal-checkout` template. SANDBOX by default: the app talks to the live
PayPal API only when PAYPAL_ENV=live AND APP_ENV=production, so a build or a preview can never move
real money (PayPal's sandbox and live credentials look alike, so the host is the guard)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional

HOSTS = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PayPalConfig:
    env: str
    api_base: str  # fixed per environment; never taken from configuration
    client_id: str
    client_secret: str
    webhook_id: str  # empty: webhooks are refused (the return page still completes orders)
    currency: str
    base_url: str
    refunds_enabled: bool

    def __repr__(self) -> str:  # never print a credential
        return f"PayPalConfig(env={self.env!r}, currency={self.currency!r})"


def load_paypal_config(env: Optional[Mapping[str, str]] = None) -> PayPalConfig:
    env = os.environ if env is None else env
    paypal_env = (env.get("PAYPAL_ENV") or "sandbox").strip().lower()
    if paypal_env not in HOSTS:
        raise ConfigError("PAYPAL_ENV must be 'sandbox' or 'live'")
    app_env = (env.get("APP_ENV") or "development").strip().lower()
    if paypal_env == "live" and app_env != "production":
        raise ConfigError("PAYPAL_ENV=live is only allowed when APP_ENV=production; previews and builds use the sandbox")
    client_id = (env.get("PAYPAL_CLIENT_ID") or "").strip()
    client_secret = (env.get("PAYPAL_CLIENT_SECRET") or "").strip()
    if not client_id:
        raise ConfigError("PAYPAL_CLIENT_ID is not set")
    if not client_secret:
        raise ConfigError("PAYPAL_CLIENT_SECRET is not set")
    currency = (env.get("PAYPAL_CURRENCY") or "usd").strip().lower()
    if not re.fullmatch(r"[a-z]{3}", currency):
        raise ConfigError("PAYPAL_CURRENCY must be a 3-letter ISO code")
    return PayPalConfig(env=paypal_env, api_base=HOSTS[paypal_env], client_id=client_id, client_secret=client_secret,
                        webhook_id=(env.get("PAYPAL_WEBHOOK_ID") or "").strip(), currency=currency,
                        base_url=(env.get("APP_BASE_URL") or "").rstrip("/"),
                        refunds_enabled=(env.get("PAYMENTS_REFUNDS_ENABLED") or "false").lower() == "true")
