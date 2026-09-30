"""Unit tests for OVHcloud adapter — signature, lists, gated mutations.

No live OVH keys; httpx + time are mocked. Credentials must never appear in
exception messages beyond config field names.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from apps.api.app.services.github_adapter import IntegrationConfigurationError
from apps.api.app.services.ovh_adapter import (
    DEFAULT_ENDPOINT,
    OvhAdapter,
    SKILL_IDS,
    resolve_ovh_endpoint,
    sign_ovh_request,
)


AK = "test-application-key"
AS = "test-application-secret"
CK = "test-consumer-key"
ENDPOINT = "https://eu.api.ovh.com/1.0"
FIXED_TS = 1_700_000_000


class _FakeResponse:
    def __init__(self, payload, *, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        if text is not None:
            self.text = text
            self.content = text.encode("utf-8")
        elif payload is None:
            self.text = ""
            self.content = b""
        else:
            self.text = json.dumps(payload)
            self.content = self.text.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _settings(**overrides):
    values = {
        "infra_real_provision_allowed": True,
        "ovh_endpoint": ENDPOINT,
        "ovh_application_key": AK,
        "ovh_application_secret": AS,
        "ovh_consumer_key": CK,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _expected_signature(method: str, url: str, body: str = "", ts: int = FIXED_TS) -> str:
    material = "+".join([AS, CK, method.upper(), url, body, str(ts)])
    return "$1$" + hashlib.sha1(material.encode("utf-8")).hexdigest()


def test_skill_ids_cover_parent_inventory():
    assert "ovh.dedicated.server.list" in SKILL_IDS
    assert "ovh.cloud.instance.create" in SKILL_IDS
    assert "ovh.cloud.instance.destroy" in SKILL_IDS
    assert "ovh.inventory.sync" in SKILL_IDS
    assert "ovh.connection.test" in SKILL_IDS


def test_resolve_endpoint_aliases():
    assert resolve_ovh_endpoint("ovh-eu") == "https://eu.api.ovh.com/1.0"
    assert resolve_ovh_endpoint("ovh-ca") == "https://ca.api.ovh.com/1.0"
    assert resolve_ovh_endpoint("ovh-us") == "https://api.us.ovhcloud.com/1.0"
    assert resolve_ovh_endpoint(None) == DEFAULT_ENDPOINT
    assert resolve_ovh_endpoint("https://eu.api.ovh.com") == "https://eu.api.ovh.com/1.0"


def test_sign_ovh_request_matches_official_formula():
    sig = sign_ovh_request(
        application_secret=AS,
        consumer_key=CK,
        method="GET",
        url=f"{ENDPOINT}/me",
        body="",
        timestamp=FIXED_TS,
    )
    assert sig == _expected_signature("GET", f"{ENDPOINT}/me")
    assert sig.startswith("$1$")
    assert len(sig) == 3 + 40  # $1$ + sha1 hex


def test_request_sets_signed_headers(monkeypatch):
    captured: dict = {}

    def fake_get(url, timeout=None):
        # /auth/time
        return _FakeResponse(None, text=str(FIXED_TS))

    def fake_request(method, url, *, headers, content, timeout):
        captured.update(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "content": content,
                "timeout": timeout,
            }
        )
        return _FakeResponse({"nichandle": "xx12345-ovh", "email": "ops@example.test"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", fake_request)

    result = OvhAdapter(_settings()).test_connection()

    assert result["ok"] is True
    assert result["provider"] == "ovhcloud"
    assert result["summary"]["nichandle"] == "xx12345-ovh"
    assert captured["method"] == "GET"
    assert captured["url"] == f"{ENDPOINT}/me"
    headers = captured["headers"]
    assert headers["X-Ovh-Application"] == AK
    assert headers["X-Ovh-Consumer"] == CK
    assert headers["X-Ovh-Timestamp"] == str(FIXED_TS)
    assert headers["X-Ovh-Signature"] == _expected_signature("GET", f"{ENDPOINT}/me")
    # Secrets must not leak into returned payload.
    blob = json.dumps(result)
    assert AS not in blob
    assert CK not in blob


def test_list_dedicated_servers(monkeypatch):
    def fake_get(url, timeout=None):
        return _FakeResponse(None, text=str(FIXED_TS))

    def fake_request(method, url, *, headers, content, timeout):
        assert method == "GET"
        assert url.endswith("/dedicated/server")
        return _FakeResponse(["ns12345.ip-1-2-3.eu", "ns67890.ip-4-5-6.eu"])

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", fake_request)

    rows = OvhAdapter(_settings()).list_dedicated_servers()
    assert len(rows) == 2
    assert rows[0]["service_name"] == "ns12345.ip-1-2-3.eu"
    assert rows[0]["resource_type"] == "dedicated_server"
    assert rows[0]["skill_id"] == "ovh.dedicated.server.list"


def test_get_dedicated_server(monkeypatch):
    def fake_get(url, timeout=None):
        return _FakeResponse(None, text=str(FIXED_TS))

    def fake_request(method, url, *, headers, content, timeout):
        assert "ns12345.ip-1-2-3.eu" in url
        return _FakeResponse(
            {
                "name": "ns12345.ip-1-2-3.eu",
                "ip": "1.2.3.4",
                "datacenter": "gra1",
                "state": "ok",
                "commercialRange": "rise-1",
                "os": "ubuntu2404",
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", fake_request)

    detail = OvhAdapter(_settings()).get_dedicated_server("ns12345.ip-1-2-3.eu")
    assert detail["external_id"] == "ns12345.ip-1-2-3.eu"
    assert detail["ip"] == "1.2.3.4"
    assert detail["state"] == "ok"


def test_create_cloud_instance_signs_body_and_returns_id(monkeypatch):
    captured: dict = {}

    def fake_get(url, timeout=None):
        return _FakeResponse(None, text=str(FIXED_TS))

    def fake_request(method, url, *, headers, content, timeout):
        captured.update(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": (content or b"").decode("utf-8"),
            }
        )
        return _FakeResponse(
            {
                "id": "inst-abc-001",
                "name": "boss-worker-1",
                "region": "GRA11",
                "status": "BUILD",
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", fake_request)

    result = OvhAdapter(_settings(infra_real_provision_allowed=True)).create_cloud_instance(
        "project-xyz",
        name="boss-worker-1",
        flavor_id="flavor-1",
        image_id="image-1",
        region="GRA11",
    )

    assert result["skill_id"] == "ovh.cloud.instance.create"
    assert result["instance_id"] == "inst-abc-001"
    assert result["project_id"] == "project-xyz"
    assert captured["method"] == "POST"
    assert captured["url"] == f"{ENDPOINT}/cloud/project/project-xyz/instance"
    body = json.loads(captured["body"])
    assert body["name"] == "boss-worker-1"
    assert body["flavorId"] == "flavor-1"
    assert body["imageId"] == "image-1"
    assert body["region"] == "GRA11"
    assert captured["headers"]["X-Ovh-Signature"] == _expected_signature(
        "POST",
        captured["url"],
        captured["body"],
    )


def test_create_cloud_instance_fails_closed_without_provision_flag(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("httpx must not be called when gate is closed")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx, "request", boom)

    adapter = OvhAdapter(_settings(infra_real_provision_allowed=False))
    with pytest.raises(IntegrationConfigurationError, match="BOSS_INFRA_REAL_PROVISION_ALLOWED"):
        adapter.create_cloud_instance(
            "project-xyz",
            name="boss-worker-1",
            flavor_id="flavor-1",
            image_id="image-1",
            region="GRA11",
        )


def test_destroy_cloud_instance_fails_closed_without_provision_flag(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("httpx must not be called when gate is closed")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx, "request", boom)

    adapter = OvhAdapter(_settings(infra_real_provision_allowed=False))
    with pytest.raises(IntegrationConfigurationError, match="BOSS_INFRA_REAL_PROVISION_ALLOWED"):
        adapter.destroy_cloud_instance("project-xyz", "inst-abc-001")


def test_destroy_cloud_instance_when_allowed(monkeypatch):
    def fake_get(url, timeout=None):
        return _FakeResponse(None, text=str(FIXED_TS))

    def fake_request(method, url, *, headers, content, timeout):
        assert method == "DELETE"
        assert url.endswith("/cloud/project/project-xyz/instance/inst-abc-001")
        return _FakeResponse(None)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", fake_request)

    result = OvhAdapter(_settings()).destroy_cloud_instance("project-xyz", "inst-abc-001")
    assert result["skill_id"] == "ovh.cloud.instance.destroy"
    assert result["status"] == "deleted"
    assert result["instance_id"] == "inst-abc-001"


def test_reinstall_gated(monkeypatch):
    adapter = OvhAdapter(_settings(infra_real_provision_allowed=False))
    with pytest.raises(IntegrationConfigurationError, match="BOSS_INFRA_REAL_PROVISION_ALLOWED"):
        adapter.reinstall_dedicated_server("ns1", template_name="ubuntu2404-server_64")


def test_missing_credentials_fail_closed(monkeypatch):
    monkeypatch.delenv("BOSS_OVH_APPLICATION_KEY", raising=False)
    monkeypatch.delenv("BOSS_OVH_APPLICATION_SECRET", raising=False)
    monkeypatch.delenv("BOSS_OVH_CONSUMER_KEY", raising=False)
    adapter = OvhAdapter(SimpleNamespace(infra_real_provision_allowed=False))
    with pytest.raises(IntegrationConfigurationError, match="credentials"):
        adapter.test_connection()


def test_credentials_via_method_kwargs(monkeypatch):
    def fake_get(url, timeout=None):
        return _FakeResponse(None, text=str(FIXED_TS))

    def fake_request(method, url, *, headers, content, timeout):
        assert headers["X-Ovh-Application"] == "ak-kw"
        return _FakeResponse({"nichandle": "yy-ovh"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", fake_request)

    adapter = OvhAdapter(SimpleNamespace(infra_real_provision_allowed=False))
    result = adapter.test_connection(
        application_key="ak-kw",
        application_secret="as-kw",
        consumer_key="ck-kw",
        endpoint="ovh-eu",
    )
    assert result["ok"] is True
    assert result["summary"]["nichandle"] == "yy-ovh"


def test_inventory_aggregates_dedicated_and_cloud(monkeypatch):
    routes: dict[str, object] = {
        f"{ENDPOINT}/dedicated/server": ["ns1.example"],
        f"{ENDPOINT}/dedicated/server/ns1.example": {
            "name": "ns1.example",
            "ip": "10.0.0.1",
            "state": "ok",
            "datacenter": "rbx",
            "commercialRange": "advance-1",
            "os": "debian12",
        },
        f"{ENDPOINT}/cloud/project": ["proj-1"],
        f"{ENDPOINT}/cloud/project/proj-1/instance": [
            {
                "id": "i-1",
                "name": "web-1",
                "region": "GRA11",
                "status": "ACTIVE",
                "flavor": {"id": "fl-1"},
                "image": {"id": "im-1"},
            }
        ],
    }

    def fake_get(url, timeout=None):
        return _FakeResponse(None, text=str(FIXED_TS))

    def fake_request(method, url, *, headers, content, timeout):
        assert method == "GET"
        if url not in routes:
            raise AssertionError(f"unexpected url {url}")
        return _FakeResponse(routes[url])

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", fake_request)

    items = OvhAdapter(_settings()).inventory()
    types = [i["resource_type"] for i in items]
    assert types == ["dedicated_server", "cloud_project", "cloud_instance"]
    dedicated = items[0]
    assert dedicated["external_id"] == "ns1.example"
    assert dedicated["metadata"]["ip"] == "10.0.0.1"
    assert dedicated["state"] == "active"
    assert items[2]["name"] == "web-1"
    assert items[2]["metadata"]["project_id"] == "proj-1"
    assert all(i["skill_id"] == "ovh.inventory.sync" for i in items)


def test_list_reads_do_not_require_provision_gate(monkeypatch):
    """Read paths work with credentials even when real provision is disabled."""

    def fake_get(url, timeout=None):
        return _FakeResponse(None, text=str(FIXED_TS))

    def fake_request(method, url, *, headers, content, timeout):
        return _FakeResponse([])

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", fake_request)

    adapter = OvhAdapter(_settings(infra_real_provision_allowed=False))
    assert adapter.list_dedicated_servers() == []
    assert adapter.list_cloud_projects() == []
