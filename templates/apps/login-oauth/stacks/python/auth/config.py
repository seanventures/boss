"""Configuration for the login-oauth template, read from the environment. Client ids and secrets
are app secrets: BOSS puts them in the environment at run time from its vault. Never write one
into code, a config file or a test."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

DEFAULTS = {
    "APP_BASE_URL": "http://localhost:3000",
    "GOOGLE_ISSUER": "https://accounts.google.com",
    "GITHUB_URL": "https://github.com",
    "GITHUB_API_URL": "https://api.github.com",
}


@dataclass(frozen=True)
class Provider:
    id: str
    kind: str  # "oidc" (discovery) or "oauth2" (GitHub)
    name: str
    client_id: str
    client_secret: str
    scope: str
    issuer: str = ""
    authorize_url: str = ""
    token_url: str = ""
    api_url: str = ""


@dataclass(frozen=True)
class Config:
    base_url: str
    session_secret: str
    admin_emails: frozenset = frozenset()
    providers: dict = field(default_factory=dict)

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env

    def get(name: str) -> str:
        return str(env.get(name) or DEFAULTS.get(name, "")).strip()

    secret = get("SESSION_SECRET")
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must be set to at least 32 characters (BOSS generates one)")
    providers: dict[str, Provider] = {}
    if get("GOOGLE_CLIENT_ID"):
        providers["google"] = Provider("google", "oidc", "Google", get("GOOGLE_CLIENT_ID"),
                                       get("GOOGLE_CLIENT_SECRET"), "openid email profile",
                                       issuer=get("GOOGLE_ISSUER").rstrip("/"))
    if get("GITHUB_CLIENT_ID"):
        web = get("GITHUB_URL").rstrip("/")
        providers["github"] = Provider("github", "oauth2", "GitHub", get("GITHUB_CLIENT_ID"),
                                       get("GITHUB_CLIENT_SECRET"), "read:user user:email",
                                       issuer=web, authorize_url=f"{web}/login/oauth/authorize",
                                       token_url=f"{web}/login/oauth/access_token",
                                       api_url=get("GITHUB_API_URL").rstrip("/") + "/")
    # "Sign in with BOSS": any OIDC issuer with discovery; BOSS fills these when the app is
    # registered as a client of its OIDC provider.
    if get("OIDC_BOSS_ISSUER") and get("OIDC_BOSS_CLIENT_ID"):
        providers["boss"] = Provider("boss", "oidc", "BOSS", get("OIDC_BOSS_CLIENT_ID"),
                                     get("OIDC_BOSS_CLIENT_SECRET"), "openid email profile",
                                     issuer=get("OIDC_BOSS_ISSUER").rstrip("/"))
    admins = frozenset(e.strip().lower() for e in get("AUTH_ADMIN_EMAILS").split(",") if e.strip())
    return Config(base_url=get("APP_BASE_URL").rstrip("/"), session_secret=secret,
                  admin_emails=admins, providers=providers)
