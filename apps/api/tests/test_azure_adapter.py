"""Unit tests for AzureAdapter — mocked httpx, no live Azure required."""

from __future__ import annotations

import httpx
import pytest

from apps.api.app.config import Settings
from apps.api.app.services.azure_adapter import (
    AzureAdapter,
    AzureAdapterError,
)
from apps.api.app.services.github_adapter import IntegrationConfigurationError


def _settings(**overrides) -> Settings:
    base = {
        "BOSS_INFRA_REAL_PROVISION_ALLOWED": True,
    }
    base.update(overrides)
    return Settings(**base)


def _adapter(**settings_overrides) -> AzureAdapter:
    return AzureAdapter(_settings(**settings_overrides))


def _response(
    method: str,
    url: str,
    status_code: int,
    *,
    json_body: dict | list | None = None,
    text: str = "",
) -> httpx.Response:
    request = httpx.Request(method, url)
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, text=text, request=request)


_CREDS = {
    "tenant_id": "tenant-abc",
    "client_id": "client-xyz",
    "client_secret": "super-secret-client-value",
    "subscription_id": "sub-12345678",
}

_TOKEN_URL = "https://login.microsoftonline.com/tenant-abc/oauth2/v2.0/token"
_TOKEN_BODY = {"access_token": "azure-access-token-VALUE", "token_type": "Bearer"}


def _install_httpx(monkeypatch, handler):
    def fake_request(method: str, url: str, **kwargs) -> httpx.Response:
        return handler(method, url, **kwargs)

    # httpx.get/post/put/delete all call request under the hood in some versions;
    # monkeypatch both the convenience methods and request for robustness.
    monkeypatch.setattr(httpx, "request", fake_request)

    def fake_get(url, **kwargs):
        return handler("GET", url, **kwargs)

    def fake_post(url, **kwargs):
        return handler("POST", url, **kwargs)

    def fake_put(url, **kwargs):
        return handler("PUT", url, **kwargs)

    def fake_delete(url, **kwargs):
        return handler("DELETE", url, **kwargs)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "put", fake_put)
    monkeypatch.setattr(httpx, "delete", fake_delete)


# ---------------------------------------------------------------------------
# connection + inventory
# ---------------------------------------------------------------------------


def test_test_connection_returns_subscription_summary(monkeypatch):
    calls: list[dict] = []

    def handler(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        if "/subscriptions/sub-12345678" in url and "resource" not in url:
            return _response(
                method,
                url,
                200,
                json_body={
                    "subscriptionId": "sub-12345678",
                    "displayName": "BOSS Dev",
                    "state": "Enabled",
                },
            )
        raise AssertionError(f"Unexpected request: {method} {url}")

    _install_httpx(monkeypatch, handler)
    result = _adapter().test_connection(**_CREDS)

    assert result["provider"] == "azure"
    assert result["status_code"] == 200
    assert result["summary"]["subscription_id"] == "sub-12345678"
    assert result["summary"]["display_name"] == "BOSS Dev"
    assert result["summary"]["state"] == "Enabled"
    assert result["provider_ref"] == "azure:subscription:sub-12345678"
    assert any("login.microsoftonline.com" in c["url"] for c in calls)
    token_call = next(c for c in calls if "login.microsoftonline.com" in c["url"])
    assert token_call["data"]["grant_type"] == "client_credentials"
    assert token_call["data"]["client_id"] == "client-xyz"


def test_list_resources_maps_arm_items(monkeypatch):
    def handler(method, url, **kwargs):
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        if "/resources" in url:
            return _response(
                method,
                url,
                200,
                json_body={
                    "value": [
                        {
                            "id": (
                                "/subscriptions/sub-12345678/resourceGroups/rg-a"
                                "/providers/Microsoft.Compute/virtualMachines/web-1"
                            ),
                            "name": "web-1",
                            "type": "Microsoft.Compute/virtualMachines",
                            "location": "eastus",
                            "tags": {"env": "staging", "boss-managed": "true"},
                        },
                        {
                            "id": (
                                "/subscriptions/sub-12345678/resourceGroups/rg-a"
                                "/providers/Microsoft.Storage/storageAccounts/bossdata"
                            ),
                            "name": "bossdata",
                            "type": "Microsoft.Storage/storageAccounts",
                            "location": "eastus",
                            "tags": {},
                        },
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {method} {url}")

    _install_httpx(monkeypatch, handler)
    items = _adapter().list_resources(**_CREDS)

    assert len(items) == 2
    assert items[0]["name"] == "web-1"
    assert items[0]["type"] == "Microsoft.Compute/virtualMachines"
    assert items[0]["resource_group"] == "rg-a"
    assert items[0]["tags"]["env"] == "staging"
    assert items[0]["provider_ref"].startswith("azure:resource:")
    assert items[1]["name"] == "bossdata"


# ---------------------------------------------------------------------------
# resource group + VM
# ---------------------------------------------------------------------------


def test_ensure_resource_group_puts_arm_resource(monkeypatch):
    put_bodies: list[dict] = []

    def handler(method, url, **kwargs):
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        if method == "PUT" and "resourcegroups" in url.lower():
            put_bodies.append(kwargs.get("json") or {})
            return _response(
                method,
                url,
                200,
                json_body={
                    "id": (
                        "/subscriptions/sub-12345678/resourceGroups/boss-rg"
                    ),
                    "name": "boss-rg",
                    "location": "eastus",
                },
            )
        raise AssertionError(f"Unexpected request: {method} {url}")

    _install_httpx(monkeypatch, handler)
    result = _adapter().ensure_resource_group(
        **_CREDS, name="boss-rg", location="eastus"
    )

    assert result["name"] == "boss-rg"
    assert result["location"] == "eastus"
    assert result["id"].endswith("/resourceGroups/boss-rg")
    assert put_bodies[0]["location"] == "eastus"
    assert put_bodies[0]["tags"]["boss-managed"] == "true"


def test_create_vm_with_ssh_creates_nic_and_vm(monkeypatch):
    calls: list[dict] = []

    def handler(method, url, **kwargs):
        calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        if "virtualNetworks" in url and method == "PUT":
            return _response(
                method,
                url,
                200,
                json_body={
                    "id": (
                        "/subscriptions/sub-12345678/resourceGroups/rg-a"
                        "/providers/Microsoft.Network/virtualNetworks/web-1-vnet"
                    ),
                    "properties": {
                        "subnets": [
                            {
                                "name": "default",
                                "id": (
                                    "/subscriptions/sub-12345678/resourceGroups/rg-a"
                                    "/providers/Microsoft.Network/virtualNetworks"
                                    "/web-1-vnet/subnets/default"
                                ),
                            }
                        ]
                    },
                },
            )
        if "networkInterfaces" in url and method == "PUT":
            return _response(
                method,
                url,
                200,
                json_body={
                    "id": (
                        "/subscriptions/sub-12345678/resourceGroups/rg-a"
                        "/providers/Microsoft.Network/networkInterfaces/web-1-nic"
                    )
                },
            )
        if "virtualMachines" in url and method == "PUT":
            body = kwargs.get("json") or {}
            return _response(
                method,
                url,
                201,
                json_body={
                    "id": (
                        "/subscriptions/sub-12345678/resourceGroups/rg-a"
                        "/providers/Microsoft.Compute/virtualMachines/web-1"
                    ),
                    "name": "web-1",
                    "location": "eastus",
                    "properties": {
                        "provisioningState": "Creating",
                        "hardwareProfile": {
                            "vmSize": body.get("properties", {})
                            .get("hardwareProfile", {})
                            .get("vmSize")
                        },
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {method} {url}")

    _install_httpx(monkeypatch, handler)
    result = _adapter().create_vm(
        **_CREDS,
        name="web-1",
        resource_group="rg-a",
        location="eastus",
        size="Standard_B2s",
        admin_username="azureuser",
        ssh_public_key="ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ test@boss",
        image_ref="Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest",
        tags={"env": "staging"},
    )

    assert result["name"] == "web-1"
    assert result["resource_group"] == "rg-a"
    assert result["location"] == "eastus"
    assert result["size"] == "Standard_B2s"
    assert result["status"] == "Creating"
    assert result["provider_ref"].startswith("azure:vm:")
    assert "web-1-nic" in result["network_interface_id"]

    vm_call = next(c for c in calls if "virtualMachines" in c["url"])
    props = vm_call["json"]["properties"]
    assert props["hardwareProfile"]["vmSize"] == "Standard_B2s"
    assert props["osProfile"]["adminUsername"] == "azureuser"
    assert "ssh" in props["osProfile"]["linuxConfiguration"]
    assert props["storageProfile"]["imageReference"]["publisher"] == "Canonical"
    assert vm_call["json"]["tags"]["boss-managed"] == "true"
    assert vm_call["json"]["tags"]["env"] == "staging"


def test_create_vm_accepts_bare_metal_size(monkeypatch):
    def handler(method, url, **kwargs):
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        if "virtualNetworks" in url:
            return _response(
                method,
                url,
                200,
                json_body={
                    "properties": {
                        "subnets": [
                            {
                                "name": "default",
                                "id": "/subscriptions/s/resourceGroups/r/"
                                "providers/Microsoft.Network/virtualNetworks/v/"
                                "subnets/default",
                            }
                        ]
                    }
                },
            )
        if "networkInterfaces" in url:
            return _response(
                method,
                url,
                200,
                json_body={"id": "/subscriptions/s/resourceGroups/r/nic"},
            )
        if "virtualMachines" in url:
            body = kwargs.get("json") or {}
            size = body["properties"]["hardwareProfile"]["vmSize"]
            return _response(
                method,
                url,
                201,
                json_body={
                    "id": "/subscriptions/s/resourceGroups/r/providers/"
                    "Microsoft.Compute/virtualMachines/metal-1",
                    "name": "metal-1",
                    "location": "eastus",
                    "properties": {
                        "provisioningState": "Creating",
                        "hardwareProfile": {"vmSize": size},
                    },
                },
            )
        raise AssertionError(f"Unexpected: {method} {url}")

    _install_httpx(monkeypatch, handler)
    result = _adapter().create_vm(
        **_CREDS,
        name="metal-1",
        resource_group="rg-a",
        location="eastus",
        size="Standard_M128ms",  # bare-metal capable SKU example
        admin_username="azureuser",
        ssh_public_key="ssh-rsa AAAA test",
    )
    assert result["size"] == "Standard_M128ms"


def test_create_vm_resolves_password_secret_ref(monkeypatch):
    captured_os_profile: dict = {}

    def handler(method, url, **kwargs):
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        if "virtualNetworks" in url:
            return _response(
                method,
                url,
                200,
                json_body={
                    "properties": {
                        "subnets": [
                            {
                                "name": "default",
                                "id": "/subscriptions/s/rg/vnet/subnets/default",
                            }
                        ]
                    }
                },
            )
        if "networkInterfaces" in url:
            return _response(method, url, 200, json_body={"id": "/nic/id"})
        if "virtualMachines" in url:
            body = kwargs.get("json") or {}
            captured_os_profile.update(body["properties"]["osProfile"])
            return _response(
                method,
                url,
                201,
                json_body={
                    "id": "/vm/id",
                    "name": "web-pw",
                    "location": "eastus",
                    "properties": {"provisioningState": "Creating"},
                },
            )
        raise AssertionError(url)

    _install_httpx(monkeypatch, handler)
    monkeypatch.setenv("BOSS_TEST_AZURE_VM_PASSWORD", "ResolvedPassw0rd!")

    result = _adapter().create_vm(
        **_CREDS,
        name="web-pw",
        resource_group="rg-a",
        location="eastus",
        size="Standard_B1s",
        admin_username="azureuser",
        admin_password_secret_ref="env:BOSS_TEST_AZURE_VM_PASSWORD",
    )
    assert result["name"] == "web-pw"
    assert captured_os_profile["adminPassword"] == "ResolvedPassw0rd!"


def test_create_vm_rejects_literal_password(monkeypatch):
    adapter = _adapter()
    with pytest.raises(IntegrationConfigurationError, match="env: SecretRef"):
        adapter.create_vm(
            **_CREDS,
            name="web-1",
            resource_group="rg-a",
            location="eastus",
            size="Standard_B1s",
            admin_username="azureuser",
            admin_password_secret_ref="not-a-secret-ref-literal",
        )


def test_destroy_vm_deletes_arm_resource(monkeypatch):
    deletes: list[str] = []

    def handler(method, url, **kwargs):
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        if method == "DELETE" and "virtualMachines" in url:
            deletes.append(url)
            return _response(method, url, 202, text="")
        raise AssertionError(f"Unexpected: {method} {url}")

    _install_httpx(monkeypatch, handler)
    result = _adapter().destroy_vm(
        **_CREDS, resource_group="rg-a", name="web-1"
    )

    assert result["name"] == "web-1"
    assert result["resource_group"] == "rg-a"
    assert result["status"] == "deleting"
    assert result["provider_ref"].startswith("azure:vm:")
    assert any("virtualMachines/web-1" in u for u in deletes)


def test_destroy_vm_is_idempotent_on_404(monkeypatch):
    def handler(method, url, **kwargs):
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        if method == "DELETE":
            return _response(method, url, 404, text="Not Found")
        raise AssertionError(url)

    _install_httpx(monkeypatch, handler)
    result = _adapter().destroy_vm(
        **_CREDS, resource_group="rg-a", name="gone"
    )
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# gates + secret hygiene
# ---------------------------------------------------------------------------


def test_write_ops_gated_when_provision_disallowed():
    adapter = _adapter(BOSS_INFRA_REAL_PROVISION_ALLOWED=False)
    with pytest.raises(
        IntegrationConfigurationError, match="BOSS_INFRA_REAL_PROVISION_ALLOWED"
    ):
        adapter.ensure_resource_group(
            **_CREDS, name="rg", location="eastus"
        )
    with pytest.raises(IntegrationConfigurationError, match="is false"):
        adapter.create_vm(
            **_CREDS,
            name="web-1",
            resource_group="rg",
            location="eastus",
            size="Standard_B1s",
            admin_username="azureuser",
            ssh_public_key="ssh-rsa AAAA",
        )
    with pytest.raises(IntegrationConfigurationError, match="is false"):
        adapter.destroy_vm(**_CREDS, resource_group="rg", name="web-1")


def test_test_connection_allowed_when_provision_disallowed(monkeypatch):
    """Read-only connection test must not require provision gate."""

    def handler(method, url, **kwargs):
        if "login.microsoftonline.com" in url:
            return _response(method, url, 200, json_body=_TOKEN_BODY)
        return _response(
            method,
            url,
            200,
            json_body={
                "subscriptionId": "sub-12345678",
                "displayName": "BOSS Dev",
                "state": "Enabled",
            },
        )

    _install_httpx(monkeypatch, handler)
    result = _adapter(BOSS_INFRA_REAL_PROVISION_ALLOWED=False).test_connection(
        **_CREDS
    )
    assert result["status_code"] == 200


def test_http_errors_do_not_leak_client_secret(monkeypatch):
    secret = "super-secret-client-value"

    def handler(method, url, **kwargs):
        if "login.microsoftonline.com" in url:
            # Echo secret in error body the way a misconfigured proxy might.
            return _response(
                method,
                url,
                401,
                text=f"invalid_client secret={secret}",
            )
        raise AssertionError(url)

    _install_httpx(monkeypatch, handler)
    with pytest.raises(AzureAdapterError) as exc_info:
        _adapter().test_connection(**_CREDS)
    message = str(exc_info.value)
    assert secret not in message
    assert "super-secret" not in message


def test_missing_required_credentials_raise_config_error():
    adapter = _adapter()
    with pytest.raises(IntegrationConfigurationError, match="tenant_id"):
        adapter.test_connection(
            tenant_id="",
            client_id="c",
            client_secret="s",
            subscription_id="sub",
        )


# ---------------------------------------------------------------------------
# Azure OpenAI
# ---------------------------------------------------------------------------


def test_azure_openai_chat_returns_content_and_usage(monkeypatch):
    captured: dict = {}

    def handler(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["headers"] = kwargs.get("headers")
        captured["json"] = kwargs.get("json")
        return _response(
            method,
            url,
            200,
            json_body={
                "id": "chatcmpl-abc",
                "model": "gpt-4o",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Hello from Azure OpenAI",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                },
            },
        )

    _install_httpx(monkeypatch, handler)
    result = _adapter().azure_openai_chat(
        endpoint="https://boss-aoai.openai.azure.com",
        api_key="aoai-key-SECRET",
        deployment="gpt-4o",
        api_version="2024-12-01-preview",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
    )

    assert result["content"] == "Hello from Azure OpenAI"
    assert result["usage"] == {"prompt_tokens": 12, "completion_tokens": 8}
    assert result["model"] == "gpt-4o"
    assert result["provider_ref"] == "azure_openai:gpt-4o:chatcmpl-abc"
    assert captured["method"] == "POST"
    assert (
        captured["url"]
        == "https://boss-aoai.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
    )
    assert captured["params"]["api-version"] == "2024-12-01-preview"
    assert captured["headers"]["api-key"] == "aoai-key-SECRET"
    assert captured["json"]["max_tokens"] == 64
    assert captured["json"]["messages"][0]["content"] == "hi"


def test_azure_openai_chat_not_gated_by_infra_flag(monkeypatch):
    def handler(method, url, **kwargs):
        return _response(
            method,
            url,
            200,
            json_body={
                "id": "1",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    _install_httpx(monkeypatch, handler)
    result = _adapter(BOSS_INFRA_REAL_PROVISION_ALLOWED=False).azure_openai_chat(
        endpoint="https://boss-aoai.openai.azure.com",
        api_key="k",
        deployment="gpt-4o",
        api_version="2024-12-01-preview",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result["content"] == "ok"


def test_azure_openai_errors_do_not_leak_api_key(monkeypatch):
    api_key = "aoai-key-SECRET-VALUE"

    def handler(method, url, **kwargs):
        return _response(
            method,
            url,
            401,
            text=f"Unauthorized key={api_key}",
        )

    _install_httpx(monkeypatch, handler)
    with pytest.raises(AzureAdapterError) as exc_info:
        _adapter().azure_openai_chat(
            endpoint="https://boss-aoai.openai.azure.com",
            api_key=api_key,
            deployment="gpt-4o",
            api_version="2024-12-01-preview",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert api_key not in str(exc_info.value)
    assert "SECRET-VALUE" not in str(exc_info.value)
