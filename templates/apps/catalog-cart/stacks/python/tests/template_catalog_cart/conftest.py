"""Fixtures for the catalog-cart tests: an app on the seed catalog with a test-only user source."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the app's root

BASE_URL = "https://app.example.test"  # https, so the cart cookie must be Secure


@pytest.fixture
def store():
    from catalog import CatalogStore, load_seed

    return CatalogStore(load_seed())


@pytest.fixture
def app(store):
    from fastapi import FastAPI

    from catalog import create_catalog
    from catalog.routes import CatalogConfig

    # The signed-in user stands in as a header HERE ONLY; the app's real one is its session's.
    catalog = create_catalog(CatalogConfig(cart_secret="fake-cart-secret-for-the-template-tests-only",
                                           secure_cookies=True), store=store,
                             get_user_id=lambda request: request.headers.get("x-test-user"))
    app = FastAPI()
    app.include_router(catalog.router)
    return app
