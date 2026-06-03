from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import Settings
from .github_adapter import IntegrationConfigurationError


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


class DigitalOceanAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        if not self.settings.infra_real_provision_allowed:
            raise IntegrationConfigurationError(
                "BOSS_INFRA_REAL_PROVISION_ALLOWED is false."
            )
        if not self.settings.digitalocean_token:
            raise IntegrationConfigurationError(
                "BOSS_DIGITALOCEAN_TOKEN is not configured."
            )
        return {
            "Authorization": f"Bearer {self.settings.digitalocean_token}",
            "Content-Type": "application/json",
        }

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

        response = httpx.post(
            "https://api.digitalocean.com/v2/droplets",
            headers=self._headers(),
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        droplet = response.json()["droplet"]
        droplet_id = str(droplet["id"])
        return DropletProvisionResult(
            droplet_id=droplet_id,
            name=str(droplet["name"]),
            region=str((droplet.get("region") or {}).get("slug") or region),
            size=str(droplet.get("size_slug") or size),
            status=str(droplet.get("status") or "new"),
            api_url=f"https://api.digitalocean.com/v2/droplets/{droplet_id}",
        )

    def delete_droplet(self, droplet_id: str) -> DropletTerminationResult:
        response = httpx.delete(
            f"https://api.digitalocean.com/v2/droplets/{droplet_id}",
            headers=self._headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        return DropletTerminationResult(
            droplet_id=str(droplet_id),
            api_url=f"https://api.digitalocean.com/v2/droplets/{droplet_id}",
        )
