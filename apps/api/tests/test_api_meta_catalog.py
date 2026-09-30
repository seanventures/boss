"""Meta API catalog + OpenAPI coverage discovery endpoints."""

from __future__ import annotations


def test_meta_api_catalog_lists_domains_and_many_routes(client):
    response = client.get("/v1/meta/api-catalog")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product"] == "boss"
    assert body["route_count"] >= 100
    assert body["domain_count"] >= 5
    ids = {d["id"] for d in body["domains"]}
    assert "core" in ids or "meta" in ids
    # shared-server and multi-cloud routes should be classified when present
    all_paths = [
        r["path"]
        for d in body["domains"]
        for r in d.get("routes") or []
    ]
    assert any(p.startswith("/v1/") for p in all_paths)


def test_meta_api_catalog_domain_slice(client):
    full = client.get("/v1/meta/api-catalog")
    assert full.status_code == 200
    domains = [d["id"] for d in full.json()["domains"] if d.get("route_count", 0) > 0]
    assert domains
    domain = domains[0]
    response = client.get(f"/v1/meta/api-catalog/{domain}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == domain
    assert "routes" in body


def test_meta_api_catalog_unknown_domain(client):
    response = client.get("/v1/meta/api-catalog/not-a-real-domain-xyz")
    assert response.status_code == 404


def test_openapi_coverage_endpoint(client):
    response = client.get("/v1/meta/openapi-coverage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "route_count" in body
    assert "documented_route_count" in body
    assert "documentation_coverage" in body
    assert "missing_from_openapi" in body
    assert body["openapi_json"] == "/openapi.json"


def test_multi_cloud_and_shared_server_reachable(client):
    mc = client.get("/v1/multi-cloud/catalog")
    assert mc.status_code == 200, mc.text
    assert mc.json()["provider_count"] >= 4

    ge = client.get("/v1/shared-server/packages")
    assert ge.status_code == 200, ge.text
    assert "items" in ge.json()
