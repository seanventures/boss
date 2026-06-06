"""HTTP integration tests for BOSS control plane -> Account Connect service."""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

import apps.api.app.main as api_main
from apps.account_connect.app.config import get_settings
from apps.account_connect.app.main import app as account_connect_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_http(url: str, timeout_seconds: float = 10.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as error:  # noqa: BLE001 - poll until ready
            last_error = error
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


@pytest.fixture(scope="module")
def live_account_connect(database_url):
    import os

    get_settings.cache_clear()
    port = _free_port()
    token = "test-account-connect-token"
    org_id = "org_boss_cloud"
    base_url = f"http://127.0.0.1:{port}"
    os.environ["AC_DATABASE_URL"] = database_url
    os.environ["AC_SERVICE_TOKEN"] = token
    os.environ["AC_PUBLIC_BASE_URL"] = base_url
    os.environ["AC_DEFAULT_ORG_ID"] = org_id
    get_settings.cache_clear()

    config = uvicorn.Config(
        account_connect_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_http(f"{base_url}/v1/health")

    yield {
        "base_url": base_url,
        "token": token,
        "org_id": org_id,
    }

    get_settings.cache_clear()


def test_account_connect_live_sync_maps_entitlement_key_to_capability(
    client,
    live_account_connect,
):
    previous_edition = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    previous_token = api_main.settings.account_connect_token
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = live_account_connect["base_url"]
    api_main.commercial.settings.account_connect_url = live_account_connect["base_url"]
    api_main.settings.account_connect_token = live_account_connect["token"]
    api_main.commercial.settings.account_connect_token = live_account_connect["token"]
    try:
        link = client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": live_account_connect["org_id"],
                "actor": "ceo",
            },
        )
        assert link.status_code == 200

        synced = client.post(
            "/v1/account-connect/sync",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": live_account_connect["org_id"],
                "actor": "ceo",
            },
        )
        assert synced.status_code == 200
        assert synced.json()["synced_entitlement_count"] >= 1

        entitlements = client.get(
            "/v1/entitlements",
            params={"project_id": "boss-dogfood"},
        )
        hosted_rows = [
            item
            for item in entitlements.json()["items"]
            if item["source"] == "account_connect_hosted"
        ]
        assert hosted_rows
        assert hosted_rows[0]["capability_key"] == "cloud_control_plane"
    finally:
        api_main.settings.boss_edition = previous_edition
        api_main.commercial.settings.boss_edition = previous_edition
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url
        api_main.settings.account_connect_token = previous_token
        api_main.commercial.settings.account_connect_token = previous_token


def test_account_connect_live_marketplace_sync(client, live_account_connect):
    previous_edition = api_main.settings.boss_edition
    previous_url = api_main.settings.account_connect_url
    previous_token = api_main.settings.account_connect_token
    api_main.settings.boss_edition = "cloud"
    api_main.commercial.settings.boss_edition = "cloud"
    api_main.settings.account_connect_url = live_account_connect["base_url"]
    api_main.commercial.settings.account_connect_url = live_account_connect["base_url"]
    api_main.settings.account_connect_token = live_account_connect["token"]
    api_main.commercial.settings.account_connect_token = live_account_connect["token"]
    try:
        client.post(
            "/v1/account-connect/link",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": live_account_connect["org_id"],
                "actor": "ceo",
            },
        )
        client.post(
            "/v1/account-connect/sync",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": live_account_connect["org_id"],
                "actor": "ceo",
            },
        )

        synced = client.post(
            "/v1/marketplace/sync",
            json={
                "project_id": "boss-dogfood",
                "hosted_org_id": live_account_connect["org_id"],
                "actor": "ceo",
            },
        )
        assert synced.status_code == 201
        payload = synced.json()
        assert payload["listing_count"] >= 1
        assert payload["sync_run"]["status"] == "synced"

        listings = client.get(
            "/v1/marketplace/listings",
            params={"project_id": "boss-dogfood"},
        )
        assert listings.status_code == 200
        pack_ids = [item["pack_id"] for item in listings.json()["items"]]
        assert "core_v1" in pack_ids
    finally:
        api_main.settings.boss_edition = previous_edition
        api_main.commercial.settings.boss_edition = previous_edition
        api_main.settings.account_connect_url = previous_url
        api_main.commercial.settings.account_connect_url = previous_url
        api_main.settings.account_connect_token = previous_token
        api_main.commercial.settings.account_connect_token = previous_token
