"""Stripe settings for the `stripe-checkout` template. Test mode is the default: a live key is
refused unless STRIPE_MODE=live, and a test key is refused when STRIPE_MODE=live, so a preview can
never charge a real card by accident and a deploy can never silently run on test keys."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional

_SECRET = {"test": re.compile(r"^sk_test_[A-Za-z0-9]+$"), "live": re.compile(r"^sk_live_[A-Za-z0-9]+$")}
_PUBLISHABLE = {"test": re.compile(r"^pk_test_[A-Za-z0-9]+$"), "live": re.compile(r"^pk_live_[A-Za-z0-9]+$")}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StripeConfig:
    mode: str
    secret_key: str
    publishable_key: str
    webhook_secret: str
    currency: str
    base_url: str
    refunds_enabled: bool
    webhook_tolerance_seconds: int = 300  # Stripe-Signature older than this is refused

    def __repr__(self) -> str:  # never print a key
        return f"StripeConfig(mode={self.mode!r}, currency={self.currency!r})"


def load_stripe_config(env: Optional[Mapping[str, str]] = None) -> StripeConfig:
    env = os.environ if env is None else env
    mode = (env.get("STRIPE_MODE") or "test").strip().lower()
    if mode not in ("test", "live"):
        raise ConfigError("STRIPE_MODE must be 'test' or 'live'")
    secret_key = (env.get("STRIPE_SECRET_KEY") or "").strip()
    publishable_key = (env.get("STRIPE_PUBLISHABLE_KEY") or "").strip()
    webhook_secret = (env.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    # Never echo a key in an error: name the variable and the shape it must have, nothing else.
    if not _SECRET[mode].match(secret_key):
        raise ConfigError(f"STRIPE_SECRET_KEY is not a {mode}-mode secret key (STRIPE_MODE={mode})")
    if publishable_key and not _PUBLISHABLE[mode].match(publishable_key):
        raise ConfigError(f"STRIPE_PUBLISHABLE_KEY is not a {mode}-mode publishable key (STRIPE_MODE={mode})")
    if not webhook_secret:
        raise ConfigError("STRIPE_WEBHOOK_SECRET is not set")
    currency = (env.get("STRIPE_CURRENCY") or "usd").strip().lower()
    if not re.fullmatch(r"[a-z]{3}", currency):
        raise ConfigError("STRIPE_CURRENCY must be a 3-letter ISO code")
    return StripeConfig(
        mode=mode, secret_key=secret_key, publishable_key=publishable_key, webhook_secret=webhook_secret,
        currency=currency, base_url=(env.get("APP_BASE_URL") or "").rstrip("/"),
        refunds_enabled=(env.get("PAYMENTS_REFUNDS_ENABLED") or "false").lower() == "true",
    )
