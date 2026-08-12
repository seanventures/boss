"""OVHcloud provider adapter (bare metal + public cloud).

Auth uses Application Key + Application Secret + Consumer Key with the
official OVH request signature. Credentials must never be logged.

Mutating calls (create/destroy/reinstall) fail closed unless
``BOSS_INFRA_REAL_PROVISION_ALLOWED`` is true. Read paths (list/get/test/
inventory) only need credentials.

Receipts and ChangeSets are owned by the Resource Plane; this module returns
structured dict results only.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from ..config import Settings
from .github_adapter import IntegrationConfigurationError

# Public skill ids for Resource Plane / multi-cloud catalog wiring.
SKILL_IDS = (
    "ovh.dedicated.server.list",
    "ovh.dedicated.server.reinstall",
    "ovh.cloud.instance.create",
    "ovh.cloud.instance.destroy",
    "ovh.inventory.sync",
    "ovh.connection.test",
)

# Region endpoint aliases → API base (with /1.0 API root).
_ENDPOINT_ALIASES: dict[str, str] = {
    "ovh-eu": "https://eu.api.ovh.com/1.0",
    "eu": "https://eu.api.ovh.com/1.0",
    "https://eu.api.ovh.com/1.0": "https://eu.api.ovh.com/1.0",
    "https://eu.api.ovh.com": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
    "ca": "https://ca.api.ovh.com/1.0",
    "https://ca.api.ovh.com/1.0": "https://ca.api.ovh.com/1.0",
    "https://ca.api.ovh.com": "https://ca.api.ovh.com/1.0",
    "ovh-us": "https://api.us.ovhcloud.com/1.0",
    "us": "https://api.us.ovhcloud.com/1.0",
    "https://api.us.ovhcloud.com/1.0": "https://api.us.ovhcloud.com/1.0",
    "https://api.us.ovhcloud.com": "https://api.us.ovhcloud.com/1.0",
}

DEFAULT_ENDPOINT = "https://eu.api.ovh.com/1.0"


def resolve_ovh_endpoint(endpoint: str | None) -> str:
    """Normalize endpoint alias or URL to a full /1.0 base URL."""
    raw = (endpoint or "").strip() or DEFAULT_ENDPOINT
    key = raw.rstrip("/").lower()
    if key in _ENDPOINT_ALIASES:
        return _ENDPOINT_ALIASES[key]
    # Allow custom bases; ensure /1.0 suffix when a host-only root is given.
    if key.endswith("/1.0"):
        return raw.rstrip("/")
    return raw.rstrip("/") + "/1.0"


def sign_ovh_request(
    *,
    application_secret: str,
    consumer_key: str,
    method: str,
    url: str,
    body: str,
    timestamp: int | str,
) -> str:
    """Return OVH signature header value: ``$1$`` + SHA1 hex digest.

    Signature material (joined by ``+``):
    ``AS + CK + METHOD + URL + BODY + TIMESTAMP``
    """
    material = "+".join(
        [
            application_secret,
            consumer_key,
            method.upper(),
            url,
            body or "",
            str(timestamp),
        ]
    )
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()
    return f"$1${digest}"


class OvhAdapter:
    """OVHcloud API boundary for dedicated bare metal and public cloud."""

    provider_key = "ovhcloud"
    skill_ids = SKILL_IDS

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        endpoint: str | None = None,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        timeout: float = 30.0,
    ):
        self.settings = settings
        self.timeout = timeout
        # Prefer explicit kwargs, then Settings attrs (if present), then env.
        self._default_endpoint = self._pick(
            endpoint,
            self._settings_attr("ovh_endpoint"),
            os.environ.get("BOSS_OVH_ENDPOINT"),
            DEFAULT_ENDPOINT,
        )
        self._default_application_key = self._pick(
            application_key,
            self._settings_attr("ovh_application_key"),
            os.environ.get("BOSS_OVH_APPLICATION_KEY"),
        )
        self._default_application_secret = self._pick(
            application_secret,
            self._settings_attr("ovh_application_secret"),
            os.environ.get("BOSS_OVH_APPLICATION_SECRET"),
        )
        self._default_consumer_key = self._pick(
            consumer_key,
            self._settings_attr("ovh_consumer_key"),
            os.environ.get("BOSS_OVH_CONSUMER_KEY"),
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _pick(*values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    def _settings_attr(self, name: str) -> Any:
        if self.settings is None:
            return None
        return getattr(self.settings, name, None)

    def _provision_allowed(self) -> bool:
        if self.settings is not None:
            return bool(getattr(self.settings, "infra_real_provision_allowed", False))
        raw = os.environ.get("BOSS_INFRA_REAL_PROVISION_ALLOWED", "")
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def _require_provision_gate(self) -> None:
        if not self._provision_allowed():
            raise IntegrationConfigurationError(
                "BOSS_INFRA_REAL_PROVISION_ALLOWED is false."
            )

    def _resolve_credentials(
        self,
        *,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
    ) -> tuple[str, str, str, str]:
        ak = self._pick(application_key, self._default_application_key)
        as_ = self._pick(application_secret, self._default_application_secret)
        ck = self._pick(consumer_key, self._default_consumer_key)
        ep = resolve_ovh_endpoint(
            self._pick(endpoint, self._default_endpoint, DEFAULT_ENDPOINT)
        )
        missing: list[str] = []
        if not ak:
            missing.append("application_key / BOSS_OVH_APPLICATION_KEY")
        if not as_:
            missing.append("application_secret / BOSS_OVH_APPLICATION_SECRET")
        if not ck:
            missing.append("consumer_key / BOSS_OVH_CONSUMER_KEY")
        if missing:
            raise IntegrationConfigurationError(
                "OVH credentials not configured: " + ", ".join(missing)
            )
        return str(ak), str(as_), str(ck), ep

    def _timestamp(self, endpoint: str) -> int:
        """Prefer server time when reachable; fall back to local unix time."""
        try:
            response = httpx.get(f"{endpoint}/auth/time", timeout=min(self.timeout, 5.0))
            response.raise_for_status()
            return int(response.text.strip() or response.json())
        except Exception:
            return int(time.time())

    def _request(
        self,
        method: str,
        path: str,
        *,
        application_key: str,
        application_secret: str,
        consumer_key: str,
        endpoint: str,
        json_body: Mapping[str, Any] | list[Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        base = endpoint.rstrip("/")
        path_part = path if path.startswith("/") else f"/{path}"
        url = f"{base}{path_part}"
        if query:
            # Stable, sorted query string for signature + request.
            qs = urlencode(sorted((str(k), str(v)) for k, v in query.items() if v is not None))
            if qs:
                url = f"{url}?{qs}"

        body = ""
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)

        timestamp = self._timestamp(endpoint)
        signature = sign_ovh_request(
            application_secret=application_secret,
            consumer_key=consumer_key,
            method=method,
            url=url,
            body=body,
            timestamp=timestamp,
        )
        headers = {
            "X-Ovh-Application": application_key,
            "X-Ovh-Consumer": consumer_key,
            "X-Ovh-Timestamp": str(timestamp),
            "X-Ovh-Signature": signature,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = httpx.request(
            method.upper(),
            url,
            headers=headers,
            content=body.encode("utf-8") if body else None,
            timeout=self.timeout,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    # ------------------------------------------------------------------ public API
    def test_connection(
        self,
        *,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """GET /me — verify AK/AS/CK without mutating resources."""
        ak, as_, ck, ep = self._resolve_credentials(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        me = self._request(
            "GET",
            "/me",
            application_key=ak,
            application_secret=as_,
            consumer_key=ck,
            endpoint=ep,
        )
        if not isinstance(me, dict):
            me = {"value": me}
        return {
            "provider": self.provider_key,
            "ok": True,
            "endpoint": ep,
            "skill_id": "ovh.connection.test",
            "summary": {
                "nichandle": me.get("nichandle") or me.get("customerCode"),
                "email": me.get("email"),
                "name": me.get("name") or me.get("firstname"),
                "organisation": me.get("organisation"),
                "country": me.get("country"),
            },
            "raw_keys": sorted(str(k) for k in me.keys()),
        }

    def list_dedicated_servers(
        self,
        *,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /dedicated/server — list bare-metal service names (+ light rows)."""
        ak, as_, ck, ep = self._resolve_credentials(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        payload = self._request(
            "GET",
            "/dedicated/server",
            application_key=ak,
            application_secret=as_,
            consumer_key=ck,
            endpoint=ep,
        )
        names: list[str]
        if isinstance(payload, list):
            names = [str(item) for item in payload]
        else:
            names = []
        return [
            {
                "provider": self.provider_key,
                "resource_type": "dedicated_server",
                "service_name": name,
                "external_id": name,
                "skill_id": "ovh.dedicated.server.list",
            }
            for name in names
        ]

    def get_dedicated_server(
        self,
        service_name: str,
        *,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """GET /dedicated/server/{serviceName}."""
        name = (service_name or "").strip()
        if not name:
            raise IntegrationConfigurationError("service_name is required.")
        ak, as_, ck, ep = self._resolve_credentials(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        detail = self._request(
            "GET",
            f"/dedicated/server/{name}",
            application_key=ak,
            application_secret=as_,
            consumer_key=ck,
            endpoint=ep,
        )
        if not isinstance(detail, dict):
            detail = {"value": detail}
        return {
            "provider": self.provider_key,
            "resource_type": "dedicated_server",
            "service_name": name,
            "external_id": str(detail.get("name") or name),
            "name": str(detail.get("reverse") or detail.get("name") or name),
            "state": str(detail.get("state") or "unknown"),
            "ip": detail.get("ip"),
            "datacenter": detail.get("datacenter"),
            "commercial_range": detail.get("commercialRange"),
            "os": detail.get("os"),
            "rack": detail.get("rack"),
            "raw": detail,
        }

    def reinstall_dedicated_server(
        self,
        service_name: str,
        *,
        template_name: str,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """POST /dedicated/server/{serviceName}/reinstall — destructive, gated."""
        self._require_provision_gate()
        name = (service_name or "").strip()
        template = (template_name or "").strip()
        if not name:
            raise IntegrationConfigurationError("service_name is required.")
        if not template:
            raise IntegrationConfigurationError("template_name is required.")
        ak, as_, ck, ep = self._resolve_credentials(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        body: dict[str, Any] = {"operatingSystem": template, **extra}
        # Older API variants use templateName; accept either via extra override.
        if "templateName" not in body and "operatingSystem" in body:
            pass
        result = self._request(
            "POST",
            f"/dedicated/server/{name}/reinstall",
            application_key=ak,
            application_secret=as_,
            consumer_key=ck,
            endpoint=ep,
            json_body=body,
        )
        if not isinstance(result, dict):
            result = {"value": result}
        return {
            "provider": self.provider_key,
            "skill_id": "ovh.dedicated.server.reinstall",
            "service_name": name,
            "template_name": template,
            "task": result,
            "status": str(result.get("status") or result.get("state") or "accepted"),
        }

    def list_cloud_projects(
        self,
        *,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /cloud/project — public cloud project ids."""
        ak, as_, ck, ep = self._resolve_credentials(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        payload = self._request(
            "GET",
            "/cloud/project",
            application_key=ak,
            application_secret=as_,
            consumer_key=ck,
            endpoint=ep,
        )
        projects: list[dict[str, Any]] = []
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    pid = str(item.get("project_id") or item.get("projectId") or item.get("id") or "")
                    projects.append(
                        {
                            "provider": self.provider_key,
                            "resource_type": "cloud_project",
                            "project_id": pid,
                            "external_id": pid,
                            "name": str(item.get("description") or item.get("name") or pid),
                            "raw": item,
                        }
                    )
                else:
                    pid = str(item)
                    projects.append(
                        {
                            "provider": self.provider_key,
                            "resource_type": "cloud_project",
                            "project_id": pid,
                            "external_id": pid,
                            "name": pid,
                        }
                    )
        return projects

    def list_cloud_instances(
        self,
        project_id: str,
        *,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /cloud/project/{projectId}/instance."""
        pid = (project_id or "").strip()
        if not pid:
            raise IntegrationConfigurationError("project_id is required.")
        ak, as_, ck, ep = self._resolve_credentials(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        payload = self._request(
            "GET",
            f"/cloud/project/{pid}/instance",
            application_key=ak,
            application_secret=as_,
            consumer_key=ck,
            endpoint=ep,
        )
        instances: list[dict[str, Any]] = []
        if not isinstance(payload, list):
            return instances
        for item in payload:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id") or "")
            instances.append(
                {
                    "provider": self.provider_key,
                    "resource_type": "cloud_instance",
                    "project_id": pid,
                    "instance_id": iid,
                    "external_id": iid,
                    "name": str(item.get("name") or iid),
                    "region": item.get("region"),
                    "status": str(item.get("status") or "unknown"),
                    "flavor_id": (item.get("flavor") or {}).get("id")
                    if isinstance(item.get("flavor"), dict)
                    else item.get("flavorId"),
                    "image_id": (item.get("image") or {}).get("id")
                    if isinstance(item.get("image"), dict)
                    else item.get("imageId"),
                    "raw": item,
                }
            )
        return instances

    def create_cloud_instance(
        self,
        project_id: str,
        *,
        name: str,
        flavor_id: str,
        image_id: str,
        region: str,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
        monthly_billing: bool = False,
        ssh_key_id: str | None = None,
        user_data: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """POST /cloud/project/{projectId}/instance — gated mutation."""
        self._require_provision_gate()
        pid = (project_id or "").strip()
        if not pid:
            raise IntegrationConfigurationError("project_id is required.")
        if not (name and flavor_id and image_id and region):
            raise IntegrationConfigurationError(
                "name, flavor_id, image_id, and region are required."
            )
        ak, as_, ck, ep = self._resolve_credentials(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        body: dict[str, Any] = {
            "name": name,
            "flavorId": flavor_id,
            "imageId": image_id,
            "region": region,
            "monthlyBilling": bool(monthly_billing),
        }
        if ssh_key_id:
            body["sshKeyId"] = ssh_key_id
        if user_data is not None:
            body["userData"] = user_data
        body.update(extra)
        result = self._request(
            "POST",
            f"/cloud/project/{pid}/instance",
            application_key=ak,
            application_secret=as_,
            consumer_key=ck,
            endpoint=ep,
            json_body=body,
        )
        if not isinstance(result, dict):
            result = {"value": result}
        instance_id = str(result.get("id") or "")
        return {
            "provider": self.provider_key,
            "skill_id": "ovh.cloud.instance.create",
            "project_id": pid,
            "instance_id": instance_id,
            "external_id": instance_id,
            "name": str(result.get("name") or name),
            "region": str(result.get("region") or region),
            "status": str(result.get("status") or "BUILD"),
            "flavor_id": flavor_id,
            "image_id": image_id,
            "raw": result,
        }

    def destroy_cloud_instance(
        self,
        project_id: str,
        instance_id: str,
        *,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """DELETE /cloud/project/{projectId}/instance/{instanceId} — gated."""
        self._require_provision_gate()
        pid = (project_id or "").strip()
        iid = (instance_id or "").strip()
        if not pid or not iid:
            raise IntegrationConfigurationError(
                "project_id and instance_id are required."
            )
        ak, as_, ck, ep = self._resolve_credentials(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        result = self._request(
            "DELETE",
            f"/cloud/project/{pid}/instance/{iid}",
            application_key=ak,
            application_secret=as_,
            consumer_key=ck,
            endpoint=ep,
        )
        if not isinstance(result, dict):
            result = {"value": result} if result else {}
        return {
            "provider": self.provider_key,
            "skill_id": "ovh.cloud.instance.destroy",
            "project_id": pid,
            "instance_id": iid,
            "external_id": iid,
            "status": "deleted",
            "raw": result,
        }

    def inventory(
        self,
        *,
        application_key: str | None = None,
        application_secret: str | None = None,
        consumer_key: str | None = None,
        endpoint: str | None = None,
        include_instance_details: bool = True,
    ) -> list[dict[str, Any]]:
        """Unified inventory: dedicated servers + cloud projects/instances."""
        creds = dict(
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
            endpoint=endpoint,
        )
        resources: list[dict[str, Any]] = []

        for server in self.list_dedicated_servers(**creds):
            service_name = str(server.get("service_name") or "")
            try:
                detail = self.get_dedicated_server(service_name, **creds)
            except Exception as exc:
                # Keep partial inventory visible; never silent-drop the row.
                resources.append(
                    {
                        "provider": self.provider_key,
                        "resource_type": "dedicated_server",
                        "external_id": service_name,
                        "name": service_name,
                        "state": "unknown",
                        "health_status": "error",
                        "ownership": "external",
                        "environment": "unknown",
                        "tags": ["ovh", "dedicated"],
                        "metadata": {
                            "service_name": service_name,
                            "error": type(exc).__name__,
                        },
                        "skill_id": "ovh.inventory.sync",
                    }
                )
                continue
            resources.append(
                {
                    "provider": self.provider_key,
                    "resource_type": "dedicated_server",
                    "external_id": detail["external_id"],
                    "name": detail["name"],
                    "state": _map_state(detail.get("state")),
                    "health_status": str(detail.get("state") or "unknown"),
                    "ownership": "external",
                    "environment": "unknown",
                    "tags": ["ovh", "dedicated", "bare_metal"],
                    "metadata": {
                        "service_name": service_name,
                        "ip": detail.get("ip"),
                        "datacenter": detail.get("datacenter"),
                        "commercial_range": detail.get("commercial_range"),
                        "os": detail.get("os"),
                    },
                    "skill_id": "ovh.inventory.sync",
                }
            )

        for project in self.list_cloud_projects(**creds):
            pid = str(project.get("project_id") or "")
            resources.append(
                {
                    "provider": self.provider_key,
                    "resource_type": "cloud_project",
                    "external_id": pid,
                    "name": str(project.get("name") or pid),
                    "state": "active",
                    "health_status": "ok",
                    "ownership": "external",
                    "environment": "unknown",
                    "tags": ["ovh", "public_cloud", "project"],
                    "metadata": {"project_id": pid},
                    "skill_id": "ovh.inventory.sync",
                }
            )
            if not include_instance_details or not pid:
                continue
            try:
                instances = self.list_cloud_instances(pid, **creds)
            except Exception as exc:
                resources.append(
                    {
                        "provider": self.provider_key,
                        "resource_type": "cloud_instance_list_error",
                        "external_id": f"{pid}:instances",
                        "name": f"{pid} instances",
                        "state": "unknown",
                        "health_status": "error",
                        "ownership": "external",
                        "environment": "unknown",
                        "tags": ["ovh", "public_cloud"],
                        "metadata": {
                            "project_id": pid,
                            "error": type(exc).__name__,
                        },
                        "skill_id": "ovh.inventory.sync",
                    }
                )
                continue
            for inst in instances:
                resources.append(
                    {
                        "provider": self.provider_key,
                        "resource_type": "cloud_instance",
                        "external_id": str(inst.get("instance_id") or ""),
                        "name": str(inst.get("name") or ""),
                        "state": _map_state(inst.get("status")),
                        "health_status": str(inst.get("status") or "unknown"),
                        "ownership": "external",
                        "environment": "unknown",
                        "tags": ["ovh", "public_cloud", "instance"],
                        "metadata": {
                            "project_id": pid,
                            "region": inst.get("region"),
                            "flavor_id": inst.get("flavor_id"),
                            "image_id": inst.get("image_id"),
                        },
                        "skill_id": "ovh.inventory.sync",
                    }
                )

        return resources


def _map_state(raw: Any) -> str:
    value = str(raw or "unknown").strip().lower()
    if value in {"ok", "active", "running", "enabled", "up"}:
        return "active"
    if value in {"building", "build", "installing", "reinstalling", "pending", "creating"}:
        return "provisioning"
    if value in {"stopped", "shutoff", "disabled", "suspended"}:
        return "stopped"
    if value in {"error", "fault", "rescue"}:
        return "error"
    if value in {"deleted", "deleting"}:
        return "deleted"
    return value or "unknown"
