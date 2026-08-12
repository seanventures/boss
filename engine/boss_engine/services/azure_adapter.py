"""Microsoft Azure Resource Manager + Azure OpenAI adapter.

Adapter boundary for Azure service principal auth, inventory, resource groups,
VM provision/destroy (including bare-metal / dedicated-host capable sizes when
the caller supplies them), and Azure OpenAI chat completions.

Credentials are passed per call (or resolved from env SecretRefs). Secrets are
never logged or embedded in exception messages. Real infrastructure writes are
gated by ``Settings.infra_real_provision_allowed``
(``BOSS_INFRA_REAL_PROVISION_ALLOWED``).
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ..config import Settings
from .github_adapter import IntegrationConfigurationError

# Stable ARM API versions used by this adapter.
_API_RG = "2021-04-01"
_API_RESOURCES = "2021-04-01"
_API_SUBSCRIPTION = "2022-12-01"
_API_VM = "2024-03-01"
_API_NETWORK = "2023-11-01"

_MANAGEMENT = "https://management.azure.com"
_DEFAULT_IMAGE = {
    "publisher": "Canonical",
    "offer": "0001-com-ubuntu-server-jammy",
    "sku": "22_04-lts-gen2",
    "version": "latest",
}


class AzureAdapterError(RuntimeError):
    """Raised for Azure API / adapter failures that are not pure config errors."""


class AzureAdapter:
    """httpx-backed Azure ARM + Azure OpenAI client for Resource Plane skills."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._timeout = 60.0

    # ------------------------------------------------------------------ guards

    def _require_infra_writes(self) -> None:
        if not self.settings.infra_real_provision_allowed:
            raise IntegrationConfigurationError(
                "BOSS_INFRA_REAL_PROVISION_ALLOWED is false."
            )

    @staticmethod
    def _require_text(value: Optional[str], field: str) -> str:
        text = (value or "").strip()
        if not text:
            raise IntegrationConfigurationError(f"{field} is required.")
        return text

    @staticmethod
    def _redact(message: str, *secrets: Optional[str]) -> str:
        redacted = message
        for secret in secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "***")
        # Also scrub common token-shaped fragments if present.
        redacted = re.sub(
            r"(client_secret|api[_-]?key|password|access_token)=([^&\s]+)",
            r"\1=***",
            redacted,
            flags=re.IGNORECASE,
        )
        return redacted

    def _raise_http(
        self,
        response: httpx.Response,
        *secrets: Optional[str],
    ) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = response.text[:500]
            except Exception:  # noqa: BLE001 — body is best-effort for diagnostics
                body = ""
            detail = self._redact(
                f"Azure API {response.status_code} for {response.request.method} "
                f"{response.request.url}: {body}",
                *secrets,
            )
            raise AzureAdapterError(detail) from None

    # ------------------------------------------------------------------ auth

    def _token(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ) -> str:
        tenant = self._require_text(tenant_id, "tenant_id")
        cid = self._require_text(client_id, "client_id")
        secret = self._require_text(client_secret, "client_secret")
        url = f"https://login.microsoftonline.com/{quote(tenant)}/oauth2/v2.0/token"
        try:
            response = httpx.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": cid,
                    "client_secret": secret,
                    "scope": "https://management.azure.com/.default",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise AzureAdapterError(
                self._redact(f"Azure token request failed: {exc}", secret)
            ) from None
        if response.status_code >= 400:
            self._raise_http(response, secret)
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AzureAdapterError("Azure token response missing access_token.")
        return str(token)

    def _auth_headers(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ) -> tuple[dict[str, str], str]:
        token = self._token(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        return (
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            token,
        )

    # --------------------------------------------------------------- secrets

    @staticmethod
    def _resolve_env_secret(secret_ref: str) -> str:
        ref = (secret_ref or "").strip()
        kind, _, key = ref.partition(":")
        if kind != "env" or not key:
            raise IntegrationConfigurationError(
                "admin_password_secret_ref must be an env: SecretRef "
                "(literal passwords are not accepted)."
            )
        value = os.environ.get(key)
        if not value:
            raise IntegrationConfigurationError(
                f"Missing secret for SecretRef env:{key}."
            )
        return value

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _parse_image_ref(image_ref: Any) -> dict[str, str]:
        if image_ref is None or image_ref == "":
            return dict(_DEFAULT_IMAGE)
        if isinstance(image_ref, dict):
            publisher = str(image_ref.get("publisher") or "").strip()
            offer = str(image_ref.get("offer") or "").strip()
            sku = str(image_ref.get("sku") or "").strip()
            version = str(image_ref.get("version") or "latest").strip() or "latest"
            if not (publisher and offer and sku):
                raise IntegrationConfigurationError(
                    "image_ref dict requires publisher, offer, and sku."
                )
            return {
                "publisher": publisher,
                "offer": offer,
                "sku": sku,
                "version": version,
            }
        text = str(image_ref).strip()
        # publisher:offer:sku[:version]
        parts = text.split(":")
        if len(parts) >= 3:
            return {
                "publisher": parts[0],
                "offer": parts[1],
                "sku": parts[2],
                "version": parts[3] if len(parts) > 3 and parts[3] else "latest",
            }
        raise IntegrationConfigurationError(
            "image_ref must be a dict or 'publisher:offer:sku[:version]' string."
        )

    @staticmethod
    def _normalize_tags(tags: Any) -> dict[str, str]:
        if tags is None:
            return {"boss-managed": "true"}
        result: dict[str, str] = {}
        if isinstance(tags, dict):
            for key, value in tags.items():
                k = str(key).strip()
                if k:
                    result[k] = str(value)
        elif isinstance(tags, list):
            for item in tags:
                text = str(item).strip()
                if not text:
                    continue
                if ":" in text:
                    key, _, value = text.partition(":")
                    result[key.strip()] = value.strip()
                else:
                    result[text] = "true"
        else:
            raise IntegrationConfigurationError(
                "tags must be a dict or list of key:value strings."
            )
        result.setdefault("boss-managed", "true")
        return result

    @staticmethod
    def _resource_group_from_id(resource_id: str) -> str:
        # /subscriptions/{sub}/resourceGroups/{rg}/providers/...
        parts = resource_id.strip("/").split("/")
        for idx, part in enumerate(parts):
            if part.lower() == "resourcegroups" and idx + 1 < len(parts):
                return parts[idx + 1]
        return ""

    # --------------------------------------------------------------- public

    def test_connection(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
    ) -> dict[str, Any]:
        """Validate service principal credentials against ARM."""
        sub = self._require_text(subscription_id, "subscription_id")
        headers, token = self._auth_headers(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        url = f"{_MANAGEMENT}/subscriptions/{quote(sub)}?api-version={_API_SUBSCRIPTION}"
        response = httpx.get(url, headers=headers, timeout=self._timeout)
        self._raise_http(response, client_secret, token)
        payload = response.json()
        return {
            "provider": "azure",
            "status_code": response.status_code,
            "summary": {
                "subscription_id": str(payload.get("subscriptionId") or sub),
                "display_name": str(payload.get("displayName") or ""),
                "state": str(payload.get("state") or ""),
                "tenant_id": str(tenant_id).strip(),
            },
            "provider_ref": f"azure:subscription:{sub}",
        }

    def list_resources(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
    ) -> list[dict[str, Any]]:
        """List ARM resources in the subscription (inventory sync)."""
        sub = self._require_text(subscription_id, "subscription_id")
        headers, token = self._auth_headers(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        url = (
            f"{_MANAGEMENT}/subscriptions/{quote(sub)}/resources"
            f"?api-version={_API_RESOURCES}"
        )
        items: list[dict[str, Any]] = []
        next_url: Optional[str] = url
        while next_url:
            response = httpx.get(next_url, headers=headers, timeout=self._timeout)
            self._raise_http(response, client_secret, token)
            payload = response.json()
            for raw in payload.get("value") or []:
                if not isinstance(raw, dict):
                    continue
                rid = str(raw.get("id") or "")
                items.append(
                    {
                        "id": rid,
                        "name": str(raw.get("name") or ""),
                        "type": str(raw.get("type") or ""),
                        "location": str(raw.get("location") or ""),
                        "resource_group": self._resource_group_from_id(rid),
                        "tags": dict(raw.get("tags") or {}),
                        "provider_ref": f"azure:resource:{rid}" if rid else "",
                    }
                )
            next_link = payload.get("nextLink")
            next_url = str(next_link) if next_link else None
        return items

    def ensure_resource_group(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
        name: str,
        location: str,
    ) -> dict[str, Any]:
        """Create or update a resource group (idempotent PUT)."""
        self._require_infra_writes()
        sub = self._require_text(subscription_id, "subscription_id")
        rg = self._require_text(name, "name")
        loc = self._require_text(location, "location")
        headers, token = self._auth_headers(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        url = (
            f"{_MANAGEMENT}/subscriptions/{quote(sub)}/resourcegroups/"
            f"{quote(rg)}?api-version={_API_RG}"
        )
        response = httpx.put(
            url,
            headers=headers,
            json={"location": loc, "tags": {"boss-managed": "true"}},
            timeout=self._timeout,
        )
        self._raise_http(response, client_secret, token)
        payload = response.json() if response.content else {}
        rg_id = str(
            payload.get("id")
            or f"/subscriptions/{sub}/resourceGroups/{rg}"
        )
        return {
            "name": str(payload.get("name") or rg),
            "location": str(payload.get("location") or loc),
            "id": rg_id,
            "provider_ref": f"azure:resource_group:{rg_id}",
        }

    def create_vm(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
        name: str,
        resource_group: str,
        location: str,
        size: str,
        admin_username: str,
        ssh_public_key: Optional[str] = None,
        admin_password_secret_ref: Optional[str] = None,
        image_ref: Any = None,
        tags: Any = None,
        network_interface_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Provision a virtual machine (size may be bare-metal capable).

        Creates a minimal VNet/subnet/NIC when ``network_interface_id`` is omitted
        so the skill is self-contained. Auth requires either ``ssh_public_key``
        or ``admin_password_secret_ref`` (env SecretRef only — no literals).
        """
        self._require_infra_writes()
        sub = self._require_text(subscription_id, "subscription_id")
        vm_name = self._require_text(name, "name")
        rg = self._require_text(resource_group, "resource_group")
        loc = self._require_text(location, "location")
        vm_size = self._require_text(size, "size")
        admin = self._require_text(admin_username, "admin_username")
        ssh_key = (ssh_public_key or "").strip() or None
        password_ref = (admin_password_secret_ref or "").strip() or None
        if not ssh_key and not password_ref:
            raise IntegrationConfigurationError(
                "create_vm requires ssh_public_key or admin_password_secret_ref."
            )
        if password_ref and not password_ref.startswith("env:"):
            # Reject accidental literal passwords passed in the wrong field.
            raise IntegrationConfigurationError(
                "admin_password_secret_ref must be an env: SecretRef "
                "(literal passwords are not accepted)."
            )
        admin_password: Optional[str] = None
        if password_ref:
            admin_password = self._resolve_env_secret(password_ref)

        image = self._parse_image_ref(image_ref)
        tag_map = self._normalize_tags(tags)
        headers, token = self._auth_headers(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        secrets = (client_secret, token, admin_password)

        nic_id = (network_interface_id or "").strip()
        if not nic_id:
            nic_id = self._ensure_default_nic(
                headers=headers,
                secrets=secrets,
                subscription_id=sub,
                resource_group=rg,
                location=loc,
                vm_name=vm_name,
                tags=tag_map,
            )

        os_profile: dict[str, Any] = {
            "computerName": vm_name[:64],
            "adminUsername": admin,
        }
        if ssh_key:
            os_profile["linuxConfiguration"] = {
                "disablePasswordAuthentication": True,
                "ssh": {
                    "publicKeys": [
                        {
                            "path": f"/home/{admin}/.ssh/authorized_keys",
                            "keyData": ssh_key,
                        }
                    ]
                },
            }
        if admin_password:
            os_profile["adminPassword"] = admin_password
            if not ssh_key:
                os_profile["linuxConfiguration"] = {
                    "disablePasswordAuthentication": False,
                }

        body: dict[str, Any] = {
            "location": loc,
            "tags": tag_map,
            "properties": {
                "hardwareProfile": {"vmSize": vm_size},
                "storageProfile": {
                    "imageReference": image,
                    "osDisk": {
                        "createOption": "FromImage",
                        "managedDisk": {"storageAccountType": "Standard_LRS"},
                        "name": f"{vm_name}-osdisk",
                    },
                },
                "osProfile": os_profile,
                "networkProfile": {
                    "networkInterfaces": [
                        {"id": nic_id, "properties": {"primary": True}}
                    ]
                },
            },
        }

        url = (
            f"{_MANAGEMENT}/subscriptions/{quote(sub)}/resourceGroups/{quote(rg)}"
            f"/providers/Microsoft.Compute/virtualMachines/{quote(vm_name)}"
            f"?api-version={_API_VM}"
        )
        response = httpx.put(
            url, headers=headers, json=body, timeout=self._timeout
        )
        self._raise_http(response, *secrets)
        payload = response.json() if response.content else {}
        props = payload.get("properties") or {}
        vm_id = str(
            payload.get("id")
            or (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
            )
        )
        status = str(
            (props.get("provisioningState") if isinstance(props, dict) else None)
            or "Creating"
        )
        return {
            "vm_id": vm_id,
            "name": str(payload.get("name") or vm_name),
            "resource_group": rg,
            "location": str(payload.get("location") or loc),
            "size": vm_size,
            "status": status,
            "provider_ref": f"azure:vm:{vm_id}",
            "network_interface_id": nic_id,
        }

    def destroy_vm(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
        resource_group: str,
        name: str,
    ) -> dict[str, Any]:
        """Delete a virtual machine (async ARM delete)."""
        self._require_infra_writes()
        sub = self._require_text(subscription_id, "subscription_id")
        rg = self._require_text(resource_group, "resource_group")
        vm_name = self._require_text(name, "name")
        headers, token = self._auth_headers(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        vm_id = (
            f"/subscriptions/{sub}/resourceGroups/{rg}"
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
        )
        url = (
            f"{_MANAGEMENT}/subscriptions/{quote(sub)}/resourceGroups/{quote(rg)}"
            f"/providers/Microsoft.Compute/virtualMachines/{quote(vm_name)}"
            f"?api-version={_API_VM}"
        )
        response = httpx.delete(url, headers=headers, timeout=self._timeout)
        # 200/202/204 are success; 404 treated as already gone (idempotent destroy).
        if response.status_code not in {200, 202, 204, 404}:
            self._raise_http(response, client_secret, token)
        status = "not_found" if response.status_code == 404 else "deleting"
        return {
            "name": vm_name,
            "resource_group": rg,
            "vm_id": vm_id,
            "status": status,
            "provider_ref": f"azure:vm:{vm_id}",
        }

    def azure_openai_chat(
        self,
        *,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str,
        messages: list[dict[str, Any]],
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """Invoke Azure OpenAI chat completions for a deployment.

        Not gated by infra provision flag — model spend is controlled by the
        budget/reservation path in the control plane.
        """
        base = self._require_text(endpoint, "endpoint").rstrip("/")
        key = self._require_text(api_key, "api_key")
        dep = self._require_text(deployment, "deployment")
        version = self._require_text(api_version, "api_version")
        if not messages:
            raise IntegrationConfigurationError("messages must be a non-empty list.")

        url = f"{base}/openai/deployments/{quote(dep)}/chat/completions"
        body: dict[str, Any] = {"messages": messages}
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)

        try:
            response = httpx.post(
                url,
                params={"api-version": version},
                headers={
                    "api-key": key,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise AzureAdapterError(
                self._redact(f"Azure OpenAI request failed: {exc}", key)
            ) from None
        if response.status_code >= 400:
            self._raise_http(response, key)
        payload = response.json()
        choices = payload.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "")
        usage_raw = payload.get("usage") or {}
        usage = {
            "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
            "completion_tokens": int(usage_raw.get("completion_tokens") or 0),
        }
        response_id = str(payload.get("id") or "")
        return {
            "content": content,
            "usage": usage,
            "model": str(payload.get("model") or dep),
            "provider_ref": (
                f"azure_openai:{dep}:{response_id}"
                if response_id
                else f"azure_openai:{dep}"
            ),
        }

    # ----------------------------------------------------- networking helpers

    def _ensure_default_nic(
        self,
        *,
        headers: dict[str, str],
        secrets: tuple,
        subscription_id: str,
        resource_group: str,
        location: str,
        vm_name: str,
        tags: dict[str, str],
    ) -> str:
        """Create VNet + subnet + NIC named from the VM when none is supplied."""
        sub = subscription_id
        rg = resource_group
        vnet_name = f"{vm_name}-vnet"
        subnet_name = "default"
        nic_name = f"{vm_name}-nic"

        vnet_url = (
            f"{_MANAGEMENT}/subscriptions/{quote(sub)}/resourceGroups/{quote(rg)}"
            f"/providers/Microsoft.Network/virtualNetworks/{quote(vnet_name)}"
            f"?api-version={_API_NETWORK}"
        )
        vnet_body = {
            "location": location,
            "tags": tags,
            "properties": {
                "addressSpace": {"addressPrefixes": ["10.0.0.0/16"]},
                "subnets": [
                    {
                        "name": subnet_name,
                        "properties": {"addressPrefix": "10.0.0.0/24"},
                    }
                ],
            },
        }
        vnet_resp = httpx.put(
            vnet_url, headers=headers, json=vnet_body, timeout=self._timeout
        )
        self._raise_http(vnet_resp, *secrets)
        vnet_payload = vnet_resp.json() if vnet_resp.content else {}
        subnets = ((vnet_payload.get("properties") or {}).get("subnets")) or []
        subnet_id = ""
        for subnet in subnets:
            if isinstance(subnet, dict) and subnet.get("name") == subnet_name:
                subnet_id = str(subnet.get("id") or "")
                break
        if not subnet_id:
            subnet_id = (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Network/virtualNetworks/{vnet_name}"
                f"/subnets/{subnet_name}"
            )

        nic_url = (
            f"{_MANAGEMENT}/subscriptions/{quote(sub)}/resourceGroups/{quote(rg)}"
            f"/providers/Microsoft.Network/networkInterfaces/{quote(nic_name)}"
            f"?api-version={_API_NETWORK}"
        )
        nic_body = {
            "location": location,
            "tags": tags,
            "properties": {
                "ipConfigurations": [
                    {
                        "name": "ipconfig1",
                        "properties": {
                            "subnet": {"id": subnet_id},
                            "privateIPAllocationMethod": "Dynamic",
                        },
                    }
                ]
            },
        }
        nic_resp = httpx.put(
            nic_url, headers=headers, json=nic_body, timeout=self._timeout
        )
        self._raise_http(nic_resp, *secrets)
        nic_payload = nic_resp.json() if nic_resp.content else {}
        nic_id = str(
            nic_payload.get("id")
            or (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Network/networkInterfaces/{nic_name}"
            )
        )
        return nic_id
