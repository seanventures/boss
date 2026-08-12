# Multi-cloud infra + models — Azure, AWS, DigitalOcean, OVHcloud

**Status:** Active implementation  
**Runtime:** Provider adapters + Resource Plane + Model Gateway + Elastic fabric  
**APIs:** ProviderOps accounts/ChangeSets, `/v1/multi-cloud/*`, Model Gateway leases  
**Gates:** `BOSS_INFRA_REAL_PROVISION_ALLOWED`, budget reservations, receipts, SecretRefs  

---

## Goal

Full BYOK support so BOSS can **connect keys**, **test connections**, **sync inventory/billing**,
**provision/destroy compute (including bare metal where the vendor offers it)**, **deploy services**,
and **invoke/settle models** across:

| Provider | Infra focus | Models focus |
|---|---|---|
| **AWS** | EC2 (incl. metal), S3, RDS, IAM, Route53, costs | Bedrock Converse (Claude/Titan/etc.) |
| **Azure** | VM (+ dedicated host / bare-metal SKUs), RG, storage, DNS | Azure OpenAI deployments |
| **DigitalOcean** | Droplets, Apps, Spaces, firewall, load balancers | OpenAI-compatible Inference (when configured) |
| **OVHcloud** | Dedicated bare metal, VPS, public cloud instances | Optional AI endpoints / self-host on metal |

Community edition: **your keys**. No silent provision. Every write is a ChangeSet + Receipt.

---

## Hard rules

1. Credentials only via SecretRef / env refs — never returned to agents or public APIs.  
2. Real provider writes require `BOSS_INFRA_REAL_PROVISION_ALLOWED=true` and account write mode.  
3. Destructive ops need approval path when policy demands it.  
4. Budget reservation before spendy create/model invoke.  
5. Fail closed when adapter missing or key invalid — never invent resources.  
6. Bare metal and VMs both register into elastic fabric / external_resources after apply.

---

## Operator flow (infra)

```http
POST /v1/provider-accounts
{ "project_id", "provider": "azure"|"aws"|"digitalocean"|"ovhcloud", "credential_ref", … }

POST /v1/provider-accounts/{id}/test
POST /v1/provider-accounts/{id}/sync   # inventory + billing

POST /v1/changesets
{ "operations": [{ "skill_id": "azure.vm.create", "desired_state": {…} }] }
POST /v1/changesets/{id}/approve
POST /v1/changesets/{id}/apply
```

## Operator flow (models)

```http
# Bind provider account / secret as model source (gateway)
POST /v1/model-gateway/…   # existing lease/invoke path
# Or multi-cloud catalog:
GET  /v1/multi-cloud/catalog
POST /v1/multi-cloud/model-sources/connect
```

---

## Skill IDs (target)

### AWS
- `aws.ec2.instance.create` / `.destroy` (including `instance_type` metal.*)
- `aws.s3.bucket.create`, `aws.rds.instance.create`, `aws.iam.role.upsert`, `aws.route53.record.upsert`
- `aws.costs.sync`
- `aws.bedrock.converse` / model catalog list (adapter, receipted)

### Azure
- `azure.resource_group.ensure`
- `azure.vm.create` / `.destroy`
- `azure.disk.create`, `azure.public_ip.ensure` (as needed by VM)
- `azure.openai.chat` (model invoke via Azure OpenAI)
- connection test + inventory + cost sync (when API available)

### DigitalOcean
- `digitalocean.droplet.create` / `.destroy`
- `digitalocean.app.deploy`
- `digitalocean.spaces.bucket.ensure` (optional)
- `digitalocean.firewall.ensure` (optional)
- `digitalocean.billing.sync`
- `digitalocean.inference.chat` (optional OpenAI-compat)

### OVHcloud
- `ovh.dedicated.server.list` / inventory
- `ovh.dedicated.server.reinstall` (gated destructive)
- `ovh.vps.create` / `.destroy` (when API allows)
- `ovh.cloud.instance.create` / `.destroy` (Public Cloud)
- connection test + inventory sync

---

## Adapter modules

| Module | Owner |
|---|---|
| `digitalocean_adapter.py` | DO API |
| `aws` (resource_plane + bedrock_adapter) | AWS |
| `azure_adapter.py` | Azure ARM + Azure OpenAI |
| `ovh_adapter.py` | OVH API (AK/AS/CK) |
| `multi_cloud_catalog.py` | Unified catalog + connect helpers |

---

## Done means

- Catalog lists all four with capabilities + credential slots  
- Connection test works with mocked HTTP for each  
- At least one create/destroy (or reinstall for dedicated) skill wired into Resource Plane apply  
- Model path: Azure OpenAI + Bedrock invoke helpers with cost estimation hooks  
- Elastic fabric can request workers with `provider` in {digitalocean, aws, azure, ovhcloud}  
- Tests green without live credentials; live smoke opt-in and fail-closed  
