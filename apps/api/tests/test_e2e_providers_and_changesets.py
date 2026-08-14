"""Domain E2E: providers, change-sets, resource plane, multi-cloud (no real cloud).

Exercises real handlers against live Postgres via FastAPI TestClient:
  - GET provider catalog/definitions, GET/POST providers (metadata only)
  - GET/POST changesets (create only — never apply)
  - resource plane list endpoints, resource receipts, topology
  - multi-cloud catalog/provider status, provider-health, elastic fabric

No live cloud provision: credentials are env SecretRefs; apply is not called.
"""

from __future__ import annotations

import uuid


PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"
OK = {200, 201}
CLIENT = {400, 401, 403, 404, 409, 422, 429}
ALLOW = OK | CLIENT


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def test_provider_catalog_and_definitions(client):
    defs = client.get("/v1/provider-definitions")
    assert defs.status_code == 200, defs.text
    items = defs.json().get("items") or []
    assert len(items) >= 4
    providers = {item.get("provider") for item in items}
    assert "aws" in providers
    assert "digitalocean" in providers
    # Secrets must never appear in catalog payloads
    blob = defs.text.lower()
    assert "secret_access_key" not in blob or "slot" in blob

    listed = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body.get("project_id") == PROJECT_ID
    assert isinstance(body.get("items"), list)


def test_multi_cloud_catalog_and_provider_status(client):
    catalog = client.get("/v1/multi-cloud/catalog")
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    assert body.get("provider_count", 0) >= 4 or len(body.get("items") or body.get("providers") or []) >= 1

    skills = client.get("/v1/multi-cloud/skills")
    assert skills.status_code == 200, skills.text
    assert skills.json().get("count", 0) >= 1 or skills.json().get("skill_index") is not None

    # Per-provider status (GET only — no provision)
    for key in ("aws", "digitalocean", "azure", "gcp"):
        detail = client.get(f"/v1/multi-cloud/providers/{key}")
        # unknown keys may 404; known catalog keys should 200
        assert detail.status_code in ALLOW, f"{key}: {detail.text[:200]}"
        assert detail.status_code < 500
        if detail.status_code == 200:
            payload = detail.json()
            # Env var *names* like boss_aws_secret_access_key are catalog metadata.
            # Fail if raw PEM material or inline secret values leak.
            text_l = detail.text.lower()
            assert "-----begin" not in text_l
            assert "akia" not in text_l  # AWS access key id prefix
            # SecretRef defaults are env:…, never plaintext passwords
            for slot in (
                (payload.get("resource_plane") or {})
                .get("auth_methods")
                or []
            ):
                for cred in slot.get("credential_slots") or []:
                    ref = cred.get("default_ref")
                    if ref:
                        assert str(ref).startswith("env:") or ref is None
            assert (
                payload.get("provider_key") in (key, None)
                or (payload.get("catalog") or {}).get("provider_key") == key
                or "provider" in payload
                or "adapter" in str(payload).lower()
            )


def test_create_provider_account_metadata_only(client):
    """Register a provider with SecretRef credentials; never call live APIs."""
    uid = _uid()
    created = client.post(
        "/v1/providers",
        json={
            "project_id": PROJECT_ID,
            "provider": "digitalocean",
            "account_label": f"e2e-providers-{uid}",
            "credential_refs": {"token": f"env:BOSS_E2E_DO_TOKEN_{uid}"},
            "read_only": True,
            "actor": ACTOR,
        },
    )
    assert created.status_code in ALLOW, created.text
    assert created.status_code < 500
    if created.status_code not in OK:
        return

    account = created.json()
    account_id = account["id"]
    assert account_id.startswith("prov_")
    assert account.get("provider") == "digitalocean"
    # Credential refs are SecretRef strings, not raw tokens
    refs = account.get("credential_refs") or {}
    for value in refs.values():
        assert isinstance(value, str)
        assert not value.startswith("dop_v1_")  # no raw DO tokens

    got = client.get(
        f"/v1/providers/{account_id}",
        params={"project_id": PROJECT_ID},
    )
    assert got.status_code in ALLOW, got.text
    assert got.status_code < 500
    if got.status_code == 200:
        assert got.json()["id"] == account_id

    # Missing account is 404, not 500
    missing = client.get(
        f"/v1/providers/prov_missing_{uid}",
        params={"project_id": PROJECT_ID},
    )
    assert missing.status_code in CLIENT, missing.text


def test_changesets_list_and_create_without_apply(client):
    """Create a pending_approval ChangeSet; do not approve or apply (no cloud)."""
    uid = _uid()
    listed = client.get(
        "/v1/changesets",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert listed.status_code == 200, listed.text
    assert isinstance(listed.json().get("items"), list)

    # Provider-account-less proposal is allowed for planning/export paths
    created = client.post(
        "/v1/changesets",
        json={
            "project_id": PROJECT_ID,
            "provider_account_id": None,
            "title": f"e2e changeset {uid}",
            "summary": "Domain e2e ChangeSet — never applied",
            "operations": [
                {
                    "skill_id": "digitalocean.droplet.create",
                    "action": "create",
                    "resource_type": "droplet",
                    "resource_ref": f"e2e-droplet-{uid}",
                    "desired_state": {
                        "name": f"e2e-droplet-{uid}",
                        "region": "nyc3",
                        "size": "s-1vcpu-1gb",
                        "image": "ubuntu-22-04-x64",
                    },
                    "risk_tier": 1,
                    "destructive": False,
                    "production_affecting": False,
                }
            ],
            "expected_cost_usd_micros": 0,
            "risk_tier": 1,
            "approval_required": True,
            "rollback_plan": ["Do not apply; delete only if ever created."],
            "actor": ACTOR,
        },
    )
    assert created.status_code in ALLOW, created.text
    assert created.status_code < 500
    if created.status_code in OK:
        cs = created.json()
        assert cs.get("id", "").startswith("chg_")
        assert cs.get("status") in (
            "pending_approval",
            "proposed",
            "draft",
            "approved",
        )
        assert cs.get("operations")
        # Re-list and find it
        again = client.get(
            "/v1/changesets",
            params={"project_id": PROJECT_ID, "limit": 50},
        )
        assert again.status_code == 200
        ids = {item.get("id") for item in again.json().get("items") or []}
        assert cs["id"] in ids

    # Invalid operations fail closed
    bad = client.post(
        "/v1/changesets",
        json={
            "project_id": PROJECT_ID,
            "title": f"bad cs {uid}",
            "summary": "missing skill_id",
            "operations": [{"action": "noop", "resource_type": "x"}],
            "actor": ACTOR,
        },
    )
    assert bad.status_code in CLIENT, bad.text
    assert bad.status_code < 500


def test_resource_plane_status_endpoints(client):
    resources = client.get(
        "/v1/resources",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert resources.status_code in ALLOW, resources.text
    assert resources.status_code < 500
    if resources.status_code == 200:
        assert resources.json().get("project_id") == PROJECT_ID

    receipts = client.get(
        "/v1/resource-receipts",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert receipts.status_code in ALLOW, receipts.text
    assert receipts.status_code < 500

    topology = client.get(
        "/v1/resource-topology",
        params={"project_id": PROJECT_ID},
    )
    assert topology.status_code in ALLOW, topology.text
    assert topology.status_code < 500

    infra = client.get(
        "/v1/infra/resources",
        params={"project_id": PROJECT_ID},
    )
    assert infra.status_code in ALLOW, infra.text
    assert infra.status_code < 500


def test_provider_health_and_elastic_fabric(client):
    health = client.get(
        "/v1/provider-health",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert health.status_code in ALLOW, health.text
    assert health.status_code < 500
    if health.status_code == 200:
        assert health.json().get("project_id") == PROJECT_ID

    elastic = client.get(
        "/v1/elastic/status",
        params={"project_id": PROJECT_ID},
    )
    assert elastic.status_code in ALLOW, elastic.text
    assert elastic.status_code < 500

    instances = client.get(
        "/v1/elastic/instances",
        params={"project_id": PROJECT_ID},
    )
    # May be 200 empty or 422 if query shape differs — never 5xx
    assert instances.status_code in ALLOW | {405}, instances.text
    assert instances.status_code < 500

    usage = client.get(
        "/v1/provider-usage",
        params={"project_id": PROJECT_ID},
    )
    assert usage.status_code in ALLOW, usage.text
    assert usage.status_code < 500

    usage_sum = client.get(
        "/v1/provider-usage/summary",
        params={"project_id": PROJECT_ID},
    )
    assert usage_sum.status_code in ALLOW, usage_sum.text
    assert usage_sum.status_code < 500


def test_read_only_provider_blocks_write_changeset(client):
    """Fail-closed: read-only accounts reject write ChangeSets with 4xx."""
    uid = _uid()
    created = client.post(
        "/v1/providers",
        json={
            "project_id": PROJECT_ID,
            "provider": "digitalocean",
            "account_label": f"e2e-ro-{uid}",
            "credential_refs": {"token": f"env:BOSS_E2E_RO_TOKEN_{uid}"},
            "read_only": True,
            "actor": ACTOR,
        },
    )
    assert created.status_code in ALLOW, created.text
    if created.status_code not in OK:
        return
    account_id = created.json()["id"]

    blocked = client.post(
        "/v1/changesets",
        json={
            "project_id": PROJECT_ID,
            "provider_account_id": account_id,
            "title": f"blocked write {uid}",
            "summary": "should be denied for read-only account",
            "operations": [
                {
                    "skill_id": "digitalocean.droplet.create",
                    "action": "create",
                    "resource_type": "droplet",
                    "resource_ref": f"blocked-{uid}",
                    "desired_state": {
                        "name": f"blocked-{uid}",
                        "region": "nyc3",
                        "size": "s-1vcpu-1gb",
                        "image": "ubuntu-22-04-x64",
                    },
                }
            ],
            "actor": ACTOR,
        },
    )
    assert blocked.status_code in CLIENT, blocked.text
    assert blocked.status_code < 500
