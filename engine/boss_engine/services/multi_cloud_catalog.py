"""Unified multi-cloud infra + model catalog for Azure, AWS, DigitalOcean, OVHcloud.

This module is the product-facing inventory of capabilities. Resource Plane
adapters implement the skills; Model Gateway / openai_adapter / bedrock handle
model invocation. Catalog never holds secrets.
"""

from __future__ import annotations

from typing import Any


MULTI_CLOUD_PROVIDERS: list[dict[str, Any]] = [
    {
        "provider_key": "aws",
        "display_name": "Amazon Web Services",
        "categories": ["infrastructure", "models", "bare_metal"],
        "adapter_status": "available",
        "default_auth_method": "access_key",
        "credential_env": [
            "BOSS_AWS_ACCESS_KEY_ID",
            "BOSS_AWS_SECRET_ACCESS_KEY",
            "BOSS_AWS_SESSION_TOKEN",
            "BOSS_AWS_REGION",
        ],
        "infra_skills": [
            "aws.ec2.instance.create",
            "aws.ec2.instance.destroy",
            "aws.s3.bucket.create",
            "aws.rds.instance.create",
            "aws.iam.role.upsert",
            "aws.route53.record.upsert",
            "aws.costs.sync",
        ],
        "model_skills": [
            "aws.bedrock.converse",
            "aws.bedrock.list_models",
        ],
        "bare_metal_notes": (
            "Use EC2 instance types containing '.metal' (e.g. m5.metal, c5.metal). "
            "Same create skill as VMs; set desired_state.instance_type."
        ),
        "connection_test": "sts:GetCallerIdentity",
    },
    {
        "provider_key": "azure",
        "display_name": "Microsoft Azure",
        "categories": ["infrastructure", "models", "bare_metal"],
        "adapter_status": "available",
        "default_auth_method": "service_principal",
        "credential_env": [
            "BOSS_AZURE_TENANT_ID",
            "BOSS_AZURE_CLIENT_ID",
            "BOSS_AZURE_CLIENT_SECRET",
            "BOSS_AZURE_SUBSCRIPTION_ID",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_KEY",
            "AZURE_OPENAI_DEPLOYMENT",
        ],
        "infra_skills": [
            "azure.resource_group.ensure",
            "azure.vm.create",
            "azure.vm.destroy",
            "azure.inventory.sync",
        ],
        "model_skills": [
            "azure.openai.chat",
        ],
        "bare_metal_notes": (
            "Use dedicated host / bare-metal VM sizes when available in the region "
            "(e.g. sizes with 'metal' or Dedicated Host). Set desired_state.vm_size."
        ),
        "connection_test": "ARM subscriptions list + token",
    },
    {
        "provider_key": "digitalocean",
        "display_name": "DigitalOcean",
        "categories": ["infrastructure", "models"],
        "adapter_status": "available",
        "default_auth_method": "api_token",
        "credential_env": [
            "BOSS_DIGITALOCEAN_TOKEN",
            "BOSS_DO_SPACES_KEY",
            "BOSS_DO_SPACES_SECRET",
            "BOSS_DO_INFERENCE_BASE_URL",
            "BOSS_DO_INFERENCE_API_KEY",
        ],
        "infra_skills": [
            "digitalocean.droplet.create",
            "digitalocean.droplet.destroy",
            "digitalocean.app.deploy",
            "digitalocean.firewall.ensure",
            "digitalocean.floating_ip.create",
            "digitalocean.billing.sync",
        ],
        "model_skills": [
            "digitalocean.inference.chat",
        ],
        "bare_metal_notes": (
            "DO does not sell classic dedicated bare metal; use high-CPU dedicated "
            "Droplet sizes or App Platform. For true bare metal, prefer OVH/AWS metal."
        ),
        "connection_test": "GET /v2/account",
    },
    {
        "provider_key": "ovhcloud",
        "display_name": "OVHcloud",
        "categories": ["infrastructure", "bare_metal"],
        "adapter_status": "available",
        "default_auth_method": "application_consumer_key",
        "credential_env": [
            "BOSS_OVH_APPLICATION_KEY",
            "BOSS_OVH_APPLICATION_SECRET",
            "BOSS_OVH_CONSUMER_KEY",
            "BOSS_OVH_ENDPOINT",
            "BOSS_OVH_CLOUD_PROJECT_ID",
        ],
        "infra_skills": [
            "ovh.dedicated.server.list",
            "ovh.cloud.instance.create",
            "ovh.cloud.instance.destroy",
            "ovh.inventory.sync",
        ],
        "model_skills": [],
        "bare_metal_notes": (
            "Dedicated servers via /dedicated/server are first-class bare metal. "
            "Public Cloud instances are VMs; use dedicated for physical hosts."
        ),
        "connection_test": "GET /me",
    },
]


def multi_cloud_catalog() -> dict[str, Any]:
    """Return the multi-cloud product catalog (no secrets)."""
    return {
        "version": 1,
        "product": "boss_multi_cloud",
        "gates": {
            "infra_real_provision": "BOSS_INFRA_REAL_PROVISION_ALLOWED",
            "ssh_host_deploy": "BOSS_SSH_HOST_DEPLOY_ALLOWED",
        },
        "providers": list(MULTI_CLOUD_PROVIDERS),
        "provider_count": len(MULTI_CLOUD_PROVIDERS),
        "skill_index": _skill_index(),
    }


def _skill_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for prov in MULTI_CLOUD_PROVIDERS:
        key = prov["provider_key"]
        for skill in prov.get("infra_skills") or []:
            index[skill] = key
        for skill in prov.get("model_skills") or []:
            index[skill] = key
    return index


def provider_entry(provider_key: str) -> dict[str, Any]:
    key = (provider_key or "").strip().lower()
    for item in MULTI_CLOUD_PROVIDERS:
        if item["provider_key"] == key:
            return item
    raise ValueError(f"Unknown multi-cloud provider: {provider_key}")


def providers_for_category(category: str) -> list[dict[str, Any]]:
    cat = (category or "").strip().lower()
    return [p for p in MULTI_CLOUD_PROVIDERS if cat in (p.get("categories") or [])]
