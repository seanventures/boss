"""Fixtures for the login-oauth tests: the fake provider and an app wired to it."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the app's root
sys.path.insert(0, str(Path(__file__).resolve().parent))      # login_fake_idp

from login_fake_idp import FakeIdp  # noqa: E402

BASE_URL = "https://app.example.test"  # https, so the session cookie must be Secure


@pytest.fixture(scope="session")
def idp():
    fake = FakeIdp()
    fake.clients.update({"fake-google-client": "fake-google-secret",
                         "fake-github-client": "fake-github-secret",
                         "fake-boss-client": "fake-boss-secret"})
    yield fake
    fake.close()


@pytest.fixture
def app(idp):
    from auth.config import load_config
    from main import create_app

    idp.tamper = {}
    idp.user = {"sub": "g-1", "email": "ada@example.com", "email_verified": True, "name": "Ada", "picture": None}
    return create_app(load_config({
        "APP_BASE_URL": BASE_URL,
        "SESSION_SECRET": "fake-session-secret-for-the-template-tests-only",
        "AUTH_ADMIN_EMAILS": "boss@example.com",
        "GOOGLE_ISSUER": idp.issuer, "GOOGLE_CLIENT_ID": "fake-google-client",
        "GOOGLE_CLIENT_SECRET": "fake-google-secret",
        "GITHUB_URL": idp.issuer, "GITHUB_API_URL": f"{idp.issuer}/api",
        "GITHUB_CLIENT_ID": "fake-github-client", "GITHUB_CLIENT_SECRET": "fake-github-secret",
        "OIDC_BOSS_ISSUER": idp.issuer, "OIDC_BOSS_CLIENT_ID": "fake-boss-client",
        "OIDC_BOSS_CLIENT_SECRET": "fake-boss-secret",
    }))
