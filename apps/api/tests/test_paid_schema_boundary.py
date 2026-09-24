"""Community schema imports remain independent of paid route models."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from fastapi.routing import APIRoute

from apps.api.app.main import app
from apps.api.app.enterprise_extensions import paid_schemas

ROOT = Path(__file__).resolve().parents[3]


def test_community_schema_imports_without_paid_extension():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = f"{ROOT}:{ROOT / 'engine'}"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            dedent(
                """
                import importlib.abc
                import sys

                class BlockPaidSchema(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == 'apps.api.app.enterprise_extensions.paid_schemas':
                            raise ImportError('private models deliberately unavailable')
                        return None

                sys.meta_path.insert(0, BlockPaidSchema())
                from boss_engine import schemas
                from apps.api.app import schemas as shim
                assert shim is schemas
                assert 'account_connect_status' in schemas.EditionStatusResponse.model_fields
                assert 'account_connect_org' in schemas.EditionStatusResponse.model_fields
                assert hasattr(schemas, 'EnterpriseIdentityProviderSummary')
                assert hasattr(schemas, 'EnterpriseScimUserSummary')
                assert not hasattr(schemas, 'AccountConnectLinkRequest')
                assert not hasattr(schemas, 'HostedMarketplaceSyncRequest')
                assert not hasattr(schemas, 'EnterpriseClusterUpsertRequest')
                assert 'apps.api.app.enterprise_extensions.paid_schemas' not in sys.modules
                """
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_paid_routes_use_moved_models_and_keep_db_response_contract(client):
    route_models = {
        route.path: route.response_model
        for route in app.routes
        if isinstance(route, APIRoute)
    }
    assert route_models["/v1/account-connect/link"] is paid_schemas.AccountConnectMutationResponse
    assert route_models["/v1/marketplace/listings"] is paid_schemas.HostedMarketplaceListingListResponse
    assert route_models["/v1/enterprise/clusters"] is paid_schemas.EnterpriseClusterSummary

    edition = client.get("/v1/edition", params={"project_id": "boss-dogfood"})
    assert edition.status_code == 200
    assert "account_connect_status" in edition.json()
    assert "account_connect_org" in edition.json()

    marketplace = client.get("/v1/marketplace/listings", params={"project_id": "boss-dogfood"})
    assert marketplace.status_code == 200
    assert "items" in marketplace.json()

    cluster = client.get("/v1/enterprise/clusters", params={"project_id": "boss-dogfood"})
    assert cluster.status_code == 200
    assert "items" in cluster.json()

    denied = client.post(
        "/v1/enterprise/clusters",
        json={"project_id": "boss-dogfood", "actor": "release_manager"},
    )
    assert denied.status_code == 403
