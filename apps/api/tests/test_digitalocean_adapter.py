from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from apps.api.app.services.digitalocean_adapter import (
    DigitalOceanAdapter,
    DropletProvisionResult,
)
from apps.api.app.services.github_adapter import IntegrationConfigurationError


def _settings(**overrides):
    values = {
        "infra_real_provision_allowed": True,
        "digitalocean_token": "do-test-token",
        "digitalocean_default_image": "ubuntu-24-04-x64",
        "digitalocean_vpc_uuid": None,
        "digitalocean_ssh_key_id_values": (),
        "do_inference_base_url": None,
        "do_inference_api_key": None,
        "do_spaces_key": None,
        "do_spaces_secret": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, content: bytes | None = None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        if content is not None:
            self.content = content
        else:
            self.content = b"{}" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "https://api.digitalocean.com/v2/"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


def test_test_connection_returns_account_summary(monkeypatch):
    captured = {}

    def fake_request(method, url, *, headers, json, timeout):
        captured.update(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        assert method == "GET"
        assert url.endswith("/account")
        assert headers["Authorization"] == "Bearer do-test-token"
        # Read path must not require provision flag (token-only headers still ok)
        return _FakeResponse(
            {
                "account": {
                    "email": "ops@example.com",
                    "uuid": "acct-uuid",
                    "status": "active",
                    "droplet_limit": 10,
                    "team": {"name": "goldeneye"},
                }
            }
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    adapter = DigitalOceanAdapter(_settings())
    result = adapter.test_connection()

    assert result["ok"] is True
    assert result["provider"] == "digitalocean"
    assert result["summary"]["email"] == "ops@example.com"
    assert result["summary"]["uuid"] == "acct-uuid"
    assert result["summary"]["team"] == "goldeneye"


def test_test_connection_requires_token_not_provision_flag(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        return _FakeResponse({"account": {"email": "a@b.c", "uuid": "u"}})

    monkeypatch.setattr(httpx, "request", fake_request)
    adapter = DigitalOceanAdapter(
        _settings(infra_real_provision_allowed=False, digitalocean_token="tok")
    )
    result = adapter.test_connection()
    assert result["ok"] is True

    with pytest.raises(IntegrationConfigurationError, match="DIGITALOCEAN_TOKEN"):
        DigitalOceanAdapter(
            _settings(digitalocean_token=None)
        ).test_connection()


def test_list_droplets_parses_payload(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        assert method == "GET"
        assert "/droplets" in url
        return _FakeResponse(
            {
                "droplets": [
                    {"id": 1, "name": "web-1", "status": "active"},
                    {"id": 2, "name": "web-2", "status": "off"},
                ]
            }
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    droplets = DigitalOceanAdapter(_settings()).list_droplets()
    assert len(droplets) == 2
    assert droplets[0]["name"] == "web-1"


def test_get_droplet(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        assert url.endswith("/droplets/99")
        return _FakeResponse({"droplet": {"id": 99, "name": "solo"}})

    monkeypatch.setattr(httpx, "request", fake_request)
    droplet = DigitalOceanAdapter(_settings()).get_droplet(99)
    assert droplet["id"] == 99


def test_list_apps(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        assert "/apps" in url
        return _FakeResponse({"apps": [{"id": "app-1", "spec": {"name": "api"}}]})

    monkeypatch.setattr(httpx, "request", fake_request)
    apps = DigitalOceanAdapter(_settings()).list_apps()
    assert apps[0]["id"] == "app-1"


def test_list_regions_sizes(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        if url.endswith("/regions"):
            return _FakeResponse({"regions": [{"slug": "nyc3"}]})
        if url.endswith("/sizes"):
            return _FakeResponse({"sizes": [{"slug": "s-1vcpu-1gb"}]})
        raise AssertionError(url)

    monkeypatch.setattr(httpx, "request", fake_request)
    meta = DigitalOceanAdapter(_settings()).list_regions_sizes()
    assert meta["regions"][0]["slug"] == "nyc3"
    assert meta["sizes"][0]["slug"] == "s-1vcpu-1gb"


def test_create_droplet_still_works_with_mocks(monkeypatch):
    captured = {}

    def fake_request(method, url, *, headers, json, timeout):
        captured.update(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        assert method == "POST"
        assert url.endswith("/droplets")
        return _FakeResponse(
            {
                "droplet": {
                    "id": 123456,
                    "name": json["name"],
                    "region": {"slug": json["region"]},
                    "size_slug": json["size"],
                    "status": "new",
                }
            }
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    result = DigitalOceanAdapter(_settings()).create_droplet(
        name="boss-preview",
        region="nyc3",
        size="s-1vcpu-1gb",
        tags=["boss-managed"],
        user_tags=["preview"],
    )

    assert isinstance(result, DropletProvisionResult)
    assert result.droplet_id == "123456"
    assert result.region == "nyc3"
    assert captured["json"]["tags"] == ["boss-managed", "preview"]
    assert captured["headers"]["Authorization"] == "Bearer do-test-token"


def test_create_droplet_requires_provision_flag():
    with pytest.raises(IntegrationConfigurationError, match="INFRA_REAL_PROVISION"):
        DigitalOceanAdapter(
            _settings(infra_real_provision_allowed=False)
        ).create_droplet(
            name="x",
            region="nyc3",
            size="s-1vcpu-1gb",
            tags=[],
            user_tags=[],
        )


def test_ensure_firewall_creates_when_missing(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, url, *, headers, json, timeout):
        calls.append((method, url, json))
        if method == "GET" and "/firewalls" in url:
            return _FakeResponse({"firewalls": []})
        if method == "POST" and url.endswith("/firewalls"):
            return _FakeResponse(
                {
                    "firewall": {
                        "id": "fw-1",
                        "name": json["name"],
                        "inbound_rules": json["inbound_rules"],
                        "outbound_rules": json["outbound_rules"],
                        "tags": json["tags"],
                        "droplet_ids": json["droplet_ids"],
                    }
                }
            )
        raise AssertionError((method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    inbound = [
        {
            "protocol": "tcp",
            "ports": "22",
            "sources": {"addresses": ["0.0.0.0/0"]},
        }
    ]
    outbound = [
        {
            "protocol": "tcp",
            "ports": "1-65535",
            "destinations": {"addresses": ["0.0.0.0/0", "::/0"]},
        }
    ]
    result = DigitalOceanAdapter(_settings()).ensure_firewall(
        name="boss-edge",
        inbound_rules=inbound,
        outbound_rules=outbound,
        tags=["boss"],
        droplet_ids=[11, "12"],
    )

    assert result["action"] == "created"
    assert result["firewall_id"] == "fw-1"
    assert result["skill_id"] == "digitalocean.firewall.ensure"
    assert calls[-1][0] == "POST"
    assert calls[-1][2]["droplet_ids"] == [11, 12]
    assert calls[-1][2]["tags"] == ["boss"]


def test_ensure_firewall_updates_when_name_exists(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        if method == "GET" and "/firewalls" in url:
            return _FakeResponse(
                {
                    "firewalls": [
                        {"id": "fw-existing", "name": "boss-edge"},
                    ]
                }
            )
        if method == "PUT" and url.endswith("/firewalls/fw-existing"):
            return _FakeResponse(
                {
                    "firewall": {
                        "id": "fw-existing",
                        "name": "boss-edge",
                        "inbound_rules": json["inbound_rules"],
                    }
                }
            )
        raise AssertionError((method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    result = DigitalOceanAdapter(_settings()).ensure_firewall(
        name="boss-edge",
        inbound_rules=[],
        outbound_rules=[],
    )
    assert result["action"] == "updated"
    assert result["firewall_id"] == "fw-existing"


def test_ensure_firewall_requires_provision_flag():
    with pytest.raises(IntegrationConfigurationError, match="INFRA_REAL_PROVISION"):
        DigitalOceanAdapter(
            _settings(infra_real_provision_allowed=False)
        ).ensure_firewall(name="x", inbound_rules=[], outbound_rules=[])


def test_create_floating_ip_by_region(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        assert method == "POST"
        assert url.endswith("/floating_ips")
        assert json == {"region": "nyc3"}
        return _FakeResponse(
            {
                "floating_ip": {
                    "ip": "203.0.113.10",
                    "region": {"slug": "nyc3"},
                    "droplet": None,
                }
            }
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    result = DigitalOceanAdapter(_settings()).create_floating_ip(region="nyc3")
    assert result["ip"] == "203.0.113.10"
    assert result["skill_id"] == "digitalocean.floating_ip.create"
    assert result["region"] == "nyc3"


def test_create_floating_ip_with_droplet_assign(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        assert json == {"droplet_id": 42}
        return _FakeResponse(
            {
                "floating_ip": {
                    "ip": "203.0.113.11",
                    "region": {"slug": "sfo3"},
                    "droplet": {"id": 42},
                }
            }
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    result = DigitalOceanAdapter(_settings()).create_floating_ip(droplet_id=42)
    assert result["droplet_id"] == "42"
    assert result["ip"] == "203.0.113.11"


def test_assign_floating_ip(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        assert method == "POST"
        assert url.endswith("/floating_ips/203.0.113.10/actions")
        assert json == {"type": "assign", "droplet_id": 7}
        return _FakeResponse({"action": {"status": "in-progress", "type": "assign"}})

    monkeypatch.setattr(httpx, "request", fake_request)
    result = DigitalOceanAdapter(_settings()).assign_floating_ip(
        ip="203.0.113.10", droplet_id=7
    )
    assert result["action"] == "assign"
    assert result["skill_id"] == "digitalocean.floating_ip.assign"


def test_ensure_space_bucket_requires_keys():
    adapter = DigitalOceanAdapter(_settings())
    with pytest.raises(
        IntegrationConfigurationError, match="configure BOSS_DO_SPACES_KEY/SECRET"
    ):
        adapter.ensure_space_bucket(name="boss-artifacts")


def test_ensure_space_bucket_with_keys_uses_boto3(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class _FakeS3:
        def head_bucket(self, **kwargs):
            calls.append(("head_bucket", kwargs))
            # Simulate missing bucket
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadBucket",
            )

        def create_bucket(self, **kwargs):
            calls.append(("create_bucket", kwargs))
            return {}

    class _FakeBoto3:
        def client(self, service, **kwargs):
            assert service == "s3"
            assert kwargs["aws_access_key_id"] == "spaces-key"
            assert kwargs["aws_secret_access_key"] == "spaces-secret"
            assert "nyc3.digitaloceanspaces.com" in kwargs["endpoint_url"]
            return _FakeS3()

    import sys

    # botocore.exceptions.ClientError is needed by adapter + fake
    import botocore.exceptions  # noqa: F401 — present in env with boto3/botocore

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3())
    result = DigitalOceanAdapter(_settings()).ensure_space_bucket(
        name="boss-artifacts",
        region="nyc3",
        access_key="spaces-key",
        secret_key="spaces-secret",
    )
    assert result["action"] == "created"
    assert result["bucket"] == "boss-artifacts"
    assert result["skill_id"] == "digitalocean.spaces.bucket.ensure"
    assert any(name == "create_bucket" for name, _ in calls)


def test_inference_chat_posts_openai_compatible(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return _FakeResponse(
            {
                "id": "chatcmpl-1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "hello from do",
                        }
                    }
                ],
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    adapter = DigitalOceanAdapter(
        _settings(
            do_inference_base_url="https://inference.do.example/v1",
            do_inference_api_key="inf-key",
        )
    )
    result = adapter.inference_chat(
        messages=[{"role": "user", "content": "hi"}],
        model="llama3-8b-instruct",
    )

    assert result["skill_id"] == "digitalocean.inference.chat"
    assert result["model"] == "llama3-8b-instruct"
    assert result["response"]["choices"][0]["message"]["content"] == "hello from do"
    assert captured["url"] == "https://inference.do.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer inf-key"
    assert captured["json"]["messages"][0]["content"] == "hi"


def test_inference_chat_accepts_kwargs_and_env(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json})
        return _FakeResponse({"id": "x", "choices": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("BOSS_DO_INFERENCE_BASE_URL", "https://env.do.example")
    monkeypatch.setenv("BOSS_DO_INFERENCE_API_KEY", "env-key")

    adapter = DigitalOceanAdapter(_settings(digitalocean_token=None))
    adapter.inference_chat(
        messages=[{"role": "user", "content": "ping"}],
        model="m1",
    )
    assert captured["url"] == "https://env.do.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer env-key"

    # Explicit kwargs win over env
    adapter.inference_chat(
        messages=[{"role": "user", "content": "pong"}],
        model="m2",
        base_url="https://kw.do.example/v1",
        api_key="kw-key",
    )
    assert captured["url"] == "https://kw.do.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer kw-key"


def test_inference_chat_requires_base_url(monkeypatch):
    monkeypatch.delenv("BOSS_DO_INFERENCE_BASE_URL", raising=False)
    monkeypatch.delenv("DO_INFERENCE_BASE_URL", raising=False)
    with pytest.raises(IntegrationConfigurationError, match="base URL"):
        DigitalOceanAdapter(_settings()).inference_chat(
            messages=[{"role": "user", "content": "x"}],
            model="m",
        )


def test_list_account_alias(monkeypatch):
    def fake_request(method, url, *, headers, json, timeout):
        return _FakeResponse({"account": {"email": "z@z.z"}})

    monkeypatch.setattr(httpx, "request", fake_request)
    payload = DigitalOceanAdapter(_settings()).list_account()
    assert payload["account"]["email"] == "z@z.z"
