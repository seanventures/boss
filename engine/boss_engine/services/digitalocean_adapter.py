from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

from ..config import Settings
from .github_adapter import IntegrationConfigurationError

API_BASE = "https://api.digitalocean.com/v2"


@dataclass
class DropletProvisionResult:
    droplet_id: str
    name: str
    region: str
    size: str
    status: str
    api_url: str


@dataclass
class DropletTerminationResult:
    droplet_id: str
    api_url: str


@dataclass
class AppDeploymentResult:
    app_id: str
    deployment_id: str
    status: str
    phase: str
    api_url: str


def _setting_or_env(settings: Settings, attr: str, *env_names: str) -> Optional[str]:
    """Read optional settings field, then environment variables (first non-empty wins)."""
    value = getattr(settings, attr, None)
    if value is not None and str(value).strip():
        return str(value).strip()
    for name in env_names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return None


class DigitalOceanAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------
    # Auth / gating
    # ------------------------------------------------------------------

    def _require_token(self) -> str:
        token = self.settings.digitalocean_token
        if not token:
            raise IntegrationConfigurationError(
                "BOSS_DIGITALOCEAN_TOKEN is not configured."
            )
        return token

    def _require_provision(self) -> None:
        if not self.settings.infra_real_provision_allowed:
            raise IntegrationConfigurationError(
                "BOSS_INFRA_REAL_PROVISION_ALLOWED is false."
            )

    def _headers(self, *, mutate: bool = True) -> dict[str, str]:
        """Build DO API headers.

        Mutating calls (create/delete/ensure) require
        ``BOSS_INFRA_REAL_PROVISION_ALLOWED``. Read-only calls only need a token.
        """
        if mutate:
            self._require_provision()
        token = self._require_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{API_BASE}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        mutate: bool = True,
        json: Any = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        response = httpx.request(
            method,
            self._url(path),
            headers=self._headers(mutate=mutate),
            json=json,
            timeout=timeout,
        )
        response.raise_for_status()
        return response

    # ------------------------------------------------------------------
    # Connection / inventory (read-only)
    # ------------------------------------------------------------------

    def list_account(self) -> dict:
        """GET /v2/account — raw account payload."""
        response = self._request("GET", "/account", mutate=False)
        return response.json()

    def test_connection(self) -> dict:
        """Explicit connection test via GET /v2/account.

        Returns a normalized summary suitable for ProviderOps health checks.
        """
        payload = self.list_account()
        account = payload.get("account") if isinstance(payload, dict) else None
        if not isinstance(account, dict):
            account = payload if isinstance(payload, dict) else {}
        return {
            "provider": "digitalocean",
            "ok": True,
            "status_code": 200,
            "summary": {
                "email": account.get("email"),
                "uuid": account.get("uuid"),
                "status": account.get("status"),
                "droplet_limit": account.get("droplet_limit"),
                "team": (account.get("team") or {}).get("name")
                if isinstance(account.get("team"), dict)
                else account.get("team"),
            },
            "raw": payload,
        }

    def list_droplets(self) -> list[dict]:
        """List droplets (paginated fetch of first page; per_page=200)."""
        response = self._request(
            "GET", "/droplets?per_page=200", mutate=False
        )
        payload = response.json()
        droplets = payload.get("droplets") if isinstance(payload, dict) else None
        return list(droplets or [])

    def get_droplet(self, droplet_id: str | int) -> dict:
        response = self._request(
            "GET", f"/droplets/{droplet_id}", mutate=False
        )
        payload = response.json()
        droplet = payload.get("droplet") if isinstance(payload, dict) else None
        if not isinstance(droplet, dict):
            raise IntegrationConfigurationError(
                f"DigitalOcean droplet {droplet_id} not found in response."
            )
        return droplet

    def list_apps(self) -> list[dict]:
        """List App Platform apps (first page)."""
        response = self._request("GET", "/apps?per_page=200", mutate=False)
        payload = response.json()
        apps = payload.get("apps") if isinstance(payload, dict) else None
        return list(apps or [])

    def list_regions_sizes(self) -> dict:
        """Optional metadata: available regions + sizes."""
        regions_resp = self._request("GET", "/regions", mutate=False)
        sizes_resp = self._request("GET", "/sizes", mutate=False)
        regions_payload = regions_resp.json()
        sizes_payload = sizes_resp.json()
        return {
            "regions": list(
                (regions_payload.get("regions") if isinstance(regions_payload, dict) else None)
                or []
            ),
            "sizes": list(
                (sizes_payload.get("sizes") if isinstance(sizes_payload, dict) else None)
                or []
            ),
        }

    # ------------------------------------------------------------------
    # Existing droplet / app mutations (preserve signatures)
    # ------------------------------------------------------------------

    def create_droplet(
        self,
        *,
        name: str,
        region: str,
        size: str,
        tags: list[str],
        user_tags: list[str],
    ) -> DropletProvisionResult:
        payload: dict[str, object] = {
            "name": name,
            "region": region,
            "size": size,
            "image": self.settings.digitalocean_default_image,
            "tags": list(dict.fromkeys([*tags, *user_tags])),
            "backups": False,
            "ipv6": False,
            "monitoring": True,
        }
        ssh_keys = list(self.settings.digitalocean_ssh_key_id_values)
        if ssh_keys:
            payload["ssh_keys"] = ssh_keys
        if self.settings.digitalocean_vpc_uuid:
            payload["vpc_uuid"] = self.settings.digitalocean_vpc_uuid

        response = self._request("POST", "/droplets", json=payload, mutate=True)
        droplet = response.json()["droplet"]
        droplet_id = str(droplet["id"])
        return DropletProvisionResult(
            droplet_id=droplet_id,
            name=str(droplet["name"]),
            region=str((droplet.get("region") or {}).get("slug") or region),
            size=str(droplet.get("size_slug") or size),
            status=str(droplet.get("status") or "new"),
            api_url=f"{API_BASE}/droplets/{droplet_id}",
        )

    def delete_droplet(self, droplet_id: str) -> DropletTerminationResult:
        self._request("DELETE", f"/droplets/{droplet_id}", mutate=True)
        return DropletTerminationResult(
            droplet_id=str(droplet_id),
            api_url=f"{API_BASE}/droplets/{droplet_id}",
        )

    def create_app_deployment(
        self,
        *,
        app_id: str,
        force_build: bool,
    ) -> AppDeploymentResult:
        response = self._request(
            "POST",
            f"/apps/{app_id}/deployments",
            json={"force_build": force_build},
            mutate=True,
        )
        deployment = response.json()["deployment"]
        deployment_id = str(deployment["id"])
        phase = str(deployment.get("phase") or deployment.get("status") or "pending")
        return AppDeploymentResult(
            app_id=app_id,
            deployment_id=deployment_id,
            status=str(deployment.get("status") or phase),
            phase=phase,
            api_url=f"{API_BASE}/apps/{app_id}/deployments/{deployment_id}",
        )

    # ------------------------------------------------------------------
    # Firewall ensure (create or update by name)
    # ------------------------------------------------------------------

    def ensure_firewall(
        self,
        *,
        name: str,
        inbound_rules: list[dict],
        outbound_rules: list[dict],
        tags: list[str] | None = None,
        droplet_ids: list[int | str] | None = None,
    ) -> dict:
        """Create or update a named cloud firewall (skill: digitalocean.firewall.ensure)."""
        self._require_provision()
        tag_list = list(tags or [])
        droplet_id_list = [int(d) for d in (droplet_ids or [])]

        existing = self._find_firewall_by_name(name)
        body: dict[str, Any] = {
            "name": name,
            "inbound_rules": list(inbound_rules or []),
            "outbound_rules": list(outbound_rules or []),
            "tags": tag_list,
            "droplet_ids": droplet_id_list,
        }

        if existing:
            firewall_id = str(existing["id"])
            response = self._request(
                "PUT",
                f"/firewalls/{firewall_id}",
                json=body,
                mutate=True,
            )
            # PUT may return empty body on some DO versions; re-fetch if needed.
            payload = response.json() if response.content else {}
            firewall = payload.get("firewall") if isinstance(payload, dict) else None
            if not isinstance(firewall, dict):
                firewall = self._get_firewall(firewall_id)
            return {
                "action": "updated",
                "firewall": firewall,
                "firewall_id": str(firewall.get("id") or firewall_id),
                "name": str(firewall.get("name") or name),
                "api_url": f"{API_BASE}/firewalls/{firewall_id}",
                "skill_id": "digitalocean.firewall.ensure",
            }

        response = self._request("POST", "/firewalls", json=body, mutate=True)
        firewall = response.json()["firewall"]
        firewall_id = str(firewall["id"])
        return {
            "action": "created",
            "firewall": firewall,
            "firewall_id": firewall_id,
            "name": str(firewall.get("name") or name),
            "api_url": f"{API_BASE}/firewalls/{firewall_id}",
            "skill_id": "digitalocean.firewall.ensure",
        }

    def _find_firewall_by_name(self, name: str) -> dict | None:
        response = self._request("GET", "/firewalls?per_page=200", mutate=False)
        payload = response.json()
        for fw in payload.get("firewalls") or []:
            if isinstance(fw, dict) and fw.get("name") == name:
                return fw
        return None

    def _get_firewall(self, firewall_id: str) -> dict:
        response = self._request(
            "GET", f"/firewalls/{firewall_id}", mutate=False
        )
        payload = response.json()
        firewall = payload.get("firewall") if isinstance(payload, dict) else None
        if not isinstance(firewall, dict):
            return {"id": firewall_id}
        return firewall

    # ------------------------------------------------------------------
    # Floating IP
    # ------------------------------------------------------------------

    def create_floating_ip(
        self,
        *,
        region: str | None = None,
        droplet_id: int | str | None = None,
    ) -> dict:
        """Reserve a floating IP, optionally assigning it to a droplet.

        skill_id: digitalocean.floating_ip.create
        """
        self._require_provision()
        if not region and droplet_id is None:
            raise IntegrationConfigurationError(
                "create_floating_ip requires region and/or droplet_id."
            )

        body: dict[str, Any] = {}
        if droplet_id is not None:
            body["droplet_id"] = int(droplet_id)
        elif region:
            body["region"] = region

        response = self._request("POST", "/floating_ips", json=body, mutate=True)
        payload = response.json()
        floating_ip = payload.get("floating_ip") if isinstance(payload, dict) else None
        if not isinstance(floating_ip, dict):
            raise IntegrationConfigurationError(
                "DigitalOcean floating IP response missing floating_ip object."
            )
        ip = str(floating_ip.get("ip") or "")
        return {
            "action": "created",
            "floating_ip": floating_ip,
            "ip": ip,
            "region": (floating_ip.get("region") or {}).get("slug")
            if isinstance(floating_ip.get("region"), dict)
            else floating_ip.get("region") or region,
            "droplet_id": (
                str((floating_ip.get("droplet") or {}).get("id"))
                if isinstance(floating_ip.get("droplet"), dict)
                and floating_ip.get("droplet")
                else (str(droplet_id) if droplet_id is not None else None)
            ),
            "api_url": f"{API_BASE}/floating_ips/{ip}" if ip else f"{API_BASE}/floating_ips",
            "skill_id": "digitalocean.floating_ip.create",
        }

    def assign_floating_ip(
        self,
        *,
        ip: str,
        droplet_id: int | str,
    ) -> dict:
        """Optional assign of an existing floating IP to a droplet."""
        self._require_provision()
        response = self._request(
            "POST",
            f"/floating_ips/{ip}/actions",
            json={"type": "assign", "droplet_id": int(droplet_id)},
            mutate=True,
        )
        payload = response.json()
        action = payload.get("action") if isinstance(payload, dict) else payload
        return {
            "action": "assign",
            "ip": ip,
            "droplet_id": str(droplet_id),
            "result": action,
            "api_url": f"{API_BASE}/floating_ips/{ip}/actions",
            "skill_id": "digitalocean.floating_ip.assign",
        }

    # ------------------------------------------------------------------
    # Spaces (S3-compatible) — validate keys; create when boto3 available
    # ------------------------------------------------------------------

    def ensure_space_bucket(
        self,
        *,
        name: str,
        region: str = "nyc3",
        access_key: str | None = None,
        secret_key: str | None = None,
        endpoint_url: str | None = None,
    ) -> dict:
        """Ensure a Spaces bucket exists.

        skill_id: digitalocean.spaces.bucket.ensure

        Requires Spaces access keys via kwargs or BOSS_DO_SPACES_KEY/SECRET.
        Uses boto3 S3-compatible API when available.
        """
        self._require_provision()
        key = access_key or _setting_or_env(
            self.settings,
            "do_spaces_key",
            "BOSS_DO_SPACES_KEY",
            "DO_SPACES_KEY",
        )
        secret = secret_key or _setting_or_env(
            self.settings,
            "do_spaces_secret",
            "BOSS_DO_SPACES_SECRET",
            "DO_SPACES_SECRET",
        )
        if not key or not secret:
            raise IntegrationConfigurationError(
                "configure BOSS_DO_SPACES_KEY/SECRET"
            )

        endpoint = endpoint_url or f"https://{region}.digitaloceanspaces.com"
        try:
            import boto3  # type: ignore
            from botocore.exceptions import ClientError  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency path
            raise IntegrationConfigurationError(
                "boto3 is required for DigitalOcean Spaces; "
                "install boto3 and configure BOSS_DO_SPACES_KEY/SECRET"
            ) from exc

        client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
        )

        created = False
        try:
            client.head_bucket(Bucket=name)
        except ClientError:
            create_kwargs: dict[str, Any] = {"Bucket": name}
            # DO Spaces expects LocationConstraint for non-default regions.
            if region and region != "us-east-1":
                create_kwargs["CreateBucketConfiguration"] = {
                    "LocationConstraint": region
                }
            client.create_bucket(**create_kwargs)
            created = True

        return {
            "action": "created" if created else "exists",
            "bucket": name,
            "region": region,
            "endpoint_url": endpoint,
            "skill_id": "digitalocean.spaces.bucket.ensure",
        }

    # ------------------------------------------------------------------
    # Inference (OpenAI-compatible)
    # ------------------------------------------------------------------

    def inference_chat(
        self,
        *,
        messages: list[dict],
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict | None = None,
    ) -> dict:
        """OpenAI-compatible chat completions against DO Inference.

        skill_id: digitalocean.inference.chat

        Resolves base URL from ``base_url`` kwarg, settings.do_inference_base_url,
        BOSS_DO_INFERENCE_BASE_URL, or DO_INFERENCE_BASE_URL.
        """
        resolved_base = (
            base_url
            or _setting_or_env(
                self.settings,
                "do_inference_base_url",
                "BOSS_DO_INFERENCE_BASE_URL",
                "DO_INFERENCE_BASE_URL",
            )
        )
        if not resolved_base:
            raise IntegrationConfigurationError(
                "DO inference base URL not configured "
                "(set BOSS_DO_INFERENCE_BASE_URL or pass base_url)."
            )

        resolved_key = (
            api_key
            or _setting_or_env(
                self.settings,
                "do_inference_api_key",
                "BOSS_DO_INFERENCE_API_KEY",
                "DO_INFERENCE_API_KEY",
            )
            or self.settings.digitalocean_token
        )
        if not resolved_key:
            raise IntegrationConfigurationError(
                "DO inference API key not configured "
                "(set BOSS_DO_INFERENCE_API_KEY / BOSS_DIGITALOCEAN_TOKEN or pass api_key)."
            )

        root = resolved_base.rstrip("/") + "/"
        # Accept either .../v1 or bare host; always POST .../v1/chat/completions
        if root.rstrip("/").endswith("/v1"):
            url = urljoin(root if root.endswith("/") else root + "/", "chat/completions")
        else:
            url = urljoin(root, "v1/chat/completions")

        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if extra:
            body.update(extra)

        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60.0,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "provider": "digitalocean",
            "model": model,
            "base_url": resolved_base,
            "response": payload,
            "skill_id": "digitalocean.inference.chat",
        }
