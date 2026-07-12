# 05 · Infrastructure-as-Code Generation & Multi-Cloud

> **Summary** — BOSS ships **zero** Terraform/Pulumi/CloudFormation today, and that is *deliberate*: desired state is canonical in the database via the Elastic Resource Plane's `ChangeSet` model, with "Terraform export/sync later, TF not the source of truth" written into the PRD (`docs/product/PRD-v1.1.md:86,209`) and technical spec (`docs/product/TECHNICAL_SPEC-v1.1.md:249-253`). This spec designs the two complementary tracks that were always intended: **(Track 1)** bidirectional export/sync of Resource Plane ChangeSets to `.tf` while keeping the DB canonical, and **(Track 2)** giving the code agents the ability to *author* Terraform/IaC as verified artifacts for a target app, plus **multi-cloud** AWS/GCP/Azure provider adapters. This underpins the enterprise CI/CD ask (see `04-*` CI/CD generation) — you cannot orchestrate a staging→prod deploy pipeline for an enterprise app until BOSS can provision the enterprise's cloud footprint.
>
> · **user ask:** 9 · **module:** Infrastructure-as-Code · **priority:** P2-enterprise

---

## 1. Context & Goal (enterprise multi-cloud reach)

BOSS's reach is currently capped by two hard limits:

1. **Provider breadth.** The Resource Plane can only actuate against DigitalOcean, Cloudflare, Supabase, and a generic SSH host (`apps/api/app/services/resource_plane.py:46-113`). There is **no AWS, GCP, or Azure resource provisioning** anywhere in the product. Enterprises live on the big three clouds, so BOSS cannot today provision the environment an enterprise app needs to run.
2. **IaC illiteracy.** The code agents can emit `.py/.js/.ts/.go/.java/.rb/.rs/.json/.md` and a handful of config extensions (`apps/api/app/services/code_agent.py:353-354`), but **`.tf` is not in that set**, and the verify harness is Python-`unittest`-only. BOSS literally cannot write and verify a Terraform module for a customer.

The goal of this module is to make BOSS **multi-cloud-capable and IaC-literate** without violating the architecture's founding principle that **the BOSS database — not Terraform state — is the canonical source of desired state** (`TECHNICAL_SPEC-v1.1.md:251-253`). We do this along two tracks that serve two different audiences:

- **Track 1 (export/sync)** serves teams that already run Terraform/OpenTofu and want *IaC parity* with what BOSS manages — a readable, version-controllable mirror of the ChangeSet-managed footprint, plus drift reconciliation against real `.tf`.
- **Track 2 (generative IaC)** serves the code-generation pipeline itself — when BOSS builds an app for a customer, it should be able to produce the app's own infrastructure (`main.tf`, provider blocks, modules) as first-class *verified* artifacts, the same way it produces application code today.

Multi-cloud provider adapters (Track 3, a supporting track) unlock both: Track 1 can only export what BOSS can model, and Track 2's generated Terraform is only credible if BOSS can `terraform plan` it against a real provider.

## 2. Current State (ground truth)

### 2.1 The Elastic Resource Plane is the "no-Terraform" answer

BOSS deliberately chose a database-canonical, ChangeSet-driven model *instead of* Terraform as its state store:

- `resource_plane.py` is ~269 KB / 6,764 lines (`apps/api/app/services/resource_plane.py`). It owns provider accounts, inventory, billing ledger, deployment registry, **ChangeSets**, **ResourceReceipts**, reconciliation, **drift** classification, janitor, and autoscaler policy — the full lifecycle Terraform would otherwise own.
- **ChangeSet lifecycle** is the write path: an operation carries a `desired_state` JSONB blob (`resource_plane.py:1628-1660`), goes through dual-control approval (`_approver_roles`, `approve` at `:1786-1914`), and only an **approved** ChangeSet may be applied (`apply_changeset:1914`, guard at `:1935`). Every apply reserves budget (`:1947`) and emits a **ResourceReceipt**.
- **Drift** is already modeled: `list_drift_reports:766` and drift-classification logic compare recorded `desired_state` against observed provider inventory. **Rollback** plans are first-class (`_deployment_rollback_plan:3490`, recorded rollback from `desired_state` at `:2748`).
- **Provider actuation** is via direct provider HTTP APIs inside typed adapters — `httpx` calls dispatched by capability strings like `digitalocean.droplet.create` / `cloudflare.worker.deploy` (`resource_plane.py:11`, `:3599`, `:4253+`). Capabilities are declared per provider in `SUPPORTED_PROVIDERS` and persisted via `_upsert_capabilities` (`:212-289`).

### 2.2 The deliberate no-TF stance (do not regress this)

This is written into the product contract, not incidental:

- `PRD-v1.1.md:85-86` — "Direct provider APIs are allowed in early versions only through typed provider adapters and modeled `ChangeSet` records. Later versions may export or sync Terraform/OpenTofu-style plans, but **BOSS desired state remains canonical in the database plus durable artifacts**."
- `PRD-v1.1.md:209` — "**Do not make Terraform/OpenTofu the canonical state store.**"
- `TECHNICAL_SPEC-v1.1.md:249-253` — "Terraform/OpenTofu Position … BOSS desired state is canonical in database records plus artifacts. Direct provider APIs are allowed in early versions through ChangeSets. Later versions should optionally export or sync Terraform/OpenTofu-style plans … **Terraform/OpenTofu is not the BOSS source of truth unless explicitly configured as a downstream export/sync target.**"

Everything below must preserve this: TF is a **downstream projection and an inbound artifact**, never the authority.

### 2.3 The provider gap (multi-cloud)

`SUPPORTED_PROVIDERS` (`resource_plane.py:46`) contains exactly: `digitalocean`, `cloudflare`, `supabase`, `openai` (cost only), `anthropic` (cost only), `ssh_host`. There is **no `aws`, `gcp`, or `azure`** entry — no EC2/VPC/RDS/S3, no GKE/Cloud SQL, no Azure equivalents. Each provider is a dict of `{credential_ref, capabilities[]}`; new providers follow that exact shape and add a matching adapter dispatch block for their capabilities.

### 2.4 The generation gap (IaC as artifact)

- `code_agent.py:353-354` whitelists file extensions the agent may write; `.tf`, `.tf.json`, `.hcl` are absent. The native lane also caps repo context at 40 files / ~20 KB (`code_agent.py:352-359`), which already strains whole-app coherence and would strain a Terraform module tree.
- Verification is Python-only: the governed pipeline runs `python3 -m unittest discover` (`governed_code_task_service.py:~50`). There is **no `terraform validate`, no `terraform plan`, no `tflint`, no `checkov`** anywhere. So even if an agent wrote `.tf`, nothing would verify it — the Verify axis (the "V" in the VAG loop) has no IaC lane.

### 2.5 Why this blocks CI/CD (link to spec 04)

The CI/CD generation module (spec `04-*`) designs how BOSS authors and orchestrates staging→prod pipelines for customer apps. Those pipelines deploy *into an environment that must first exist*: a VPC, a cluster or app runtime, a database, DNS, secrets. Today BOSS can deploy a workload onto a pre-existing DO app or SSH host, but it cannot **stand up the target environment** on a customer's cloud. IaC/provisioning is therefore a **hard prerequisite** for enterprise CI/CD: spec 04 authors the pipeline; spec 05 authors and provisions the substrate the pipeline deploys onto. The two share the ChangeSet/Decision gate and deployment registry.

## 3. Target Design & Approach

Three tracks. Track 3 (adapters) is a shared enabler; Tracks 1 and 2 are the two products.

```
                    ┌─────────────────────────────────────────────┐
   CANONICAL  ─────►│  Resource Plane DB (ChangeSets, desired_state,│
   (unchanged)      │  ResourceReceipts, drift, rollback)          │◄─── canonical, ALWAYS wins
                    └───────┬─────────────────────────────┬────────┘
                            │ Track 1 (export)             │ Track 1 (import/drift-sync)
                            ▼                              ▲
                    ┌──────────────┐   terraform plan   ┌──┴───────────┐
                    │  .tf / .tofu │◄──────────────────►│ real cloud   │
                    │  projection  │                    │ (state file  │
                    └──────────────┘                    │  = downstream│
                            ▲                            │  mirror only)│
   Track 2 (generate)       │                           └──────────────┘
   code agents author ──────┘         Track 3: aws/gcp/azure adapters
   .tf artifacts + verify lane        in SUPPORTED_PROVIDERS power both
```

### 3.1 Track 1 — Export/Sync (design-blessed, DB stays canonical)

**Principle:** a ChangeSet's `desired_state` is the authority; `.tf` is a *deterministic serialization* of it, and Terraform state is a *downstream mirror*, never read back as truth (`TECHNICAL_SPEC-v1.1.md:251-253`).

- **Exporter (`terraform_export.py`, new).** A pure function `changeset_to_hcl(changeset) -> TerraformModule` that maps each ChangeSet operation's `(action, resource_type, resource_ref, desired_state)` tuple onto a canonical HCL resource block. Emit as **`.tf.json`** (Terraform's JSON dialect) first — it round-trips losslessly and is trivially generated from the JSONB `desired_state`, avoiding an HCL pretty-printer as a v1 dependency. Provider blocks are derived from the provider account's `credential_ref` (rendered as a `SecretRef` pointer, never the literal secret — `TECHNICAL_SPEC-v1.1.md:74`). Output is stored as a **durable artifact** attached to the ChangeSet, and optionally committed to the customer repo via `github_adapter.upsert_file`.
- **Determinism & stable addresses.** Terraform resource addresses (`aws_instance.web`) must map 1:1 and stably to BOSS `resource_ref`s so re-exports diff cleanly. Maintain a `resource_ref → tf_address` mapping in a new `terraform_exports` table; generate `import` blocks (Terraform ≥1.5) so an operator can adopt BOSS-managed resources into their own state without recreation.
- **Drift-sync (inbound projection, NOT inbound authority).** Extend the existing drift path (`list_drift_reports:766`): a new `terraform_drift_reconcile` compares (a) BOSS `desired_state`, (b) a customer-supplied `.tf`/plan JSON, and (c) live provider inventory. Divergence produces a **drift report + a corrective ChangeSet proposal** through the normal approval gate — it never silently mutates the DB. This is the "sync" half: `.tf` edits become *proposed* ChangeSets, keeping the human-gated, dual-control model intact.
- **Opt-in only.** Export/sync is off unless a deployment target sets `terraform_export = enabled` (mirrors the "unless explicitly configured as a downstream export/sync target" clause). Default posture is unchanged: direct-adapter actuation, no `.tf`.

### 3.2 Track 2 — Generative IaC (code agents write & verify Terraform)

This makes IaC a *first-class code artifact* in the existing VAG (Verify-Attest-Gate) pipeline rather than a Resource Plane concern.

- **Allow the extension.** Add `.tf`, `.tf.json`, `.tfvars`, `.hcl` to the writable set (`code_agent.py:353-354`). Add an `infrastructure` role/persona (alongside the existing correctness/spec/security roles) whose prompt frames Terraform-specific concerns (provider pinning, remote backend, least-privilege IAM, no hardcoded secrets, `for_each` over count).
- **A real verification lane (the load-bearing piece).** Today Verify = Python unittest only. Add an **IaC verify lane** to the governed pipeline that runs, in a sandbox with no cloud credentials:
  1. `terraform fmt -check` / `tofu fmt -check` — style gate.
  2. `terraform validate` — syntactic + provider-schema validity (needs `terraform init -backend=false` with providers mirrored/cached in the sandbox).
  3. `tflint` (+ cloud rulesets) — lint / best-practice gate.
  4. `checkov` **or** `tfsec` — security/compliance scan, feeding the existing security review axis.
  5. **`terraform plan`** — the strongest signal. Two credential postures: **plan-dry** (no creds, `-refresh=false`, validates the graph resolves) as the default gate; **plan-live** (scoped read-only creds against a real account) as an optional higher-assurance gate for enterprise, gated behind an approval Decision because it touches a real provider. `plan` output becomes a review-packet artifact.
- **Fit into VAG gates.** Reuse the governed gate machinery (`governed_code_task_service.py`) — an IaC task's gate passes only when the lane above is green. This directly closes the "verify harness is Python-only" gap for the IaC domain and gives Terraform artifacts the same attest/gate rigor as application code.
- **Context handling.** Terraform modules are multi-file and cross-referential; the 40-file/20 KB native-lane cap (`code_agent.py:353`) will truncate a real module tree. Route IaC tasks through the governed/micro-VM lane (git as state carrier) rather than the native lane, and add a module-aware context packer that always includes `*.tf`, `variables.tf`, `outputs.tf`, and `versions.tf` regardless of the byte cap.

### 3.3 Track 3 — Multi-cloud provider adapters (shared enabler)

Add `aws`, and optionally `gcp` and `azure`, to `SUPPORTED_PROVIDERS` (`resource_plane.py:46`) following the exact DigitalOcean/Cloudflare dict shape (`{credential_ref, capabilities[]}`) plus a matching capability-dispatch block:

- **`aws`** — `credential_ref: env:BOSS_AWS_*` (access key/secret or an assume-role `SecretRef`); capabilities such as `provider_connection_test`, `resource_inventory_sync`, `aws.ec2.instance.create/destroy`, `aws.s3.bucket.create`, `aws.rds.instance.create`, `aws.iam.role.upsert`, `aws.route53.record.upsert`, `aws.costs.sync`. Actuation via `boto3` (or `httpx`-signed calls) mirroring the existing per-capability dispatch pattern used for DO/Cloudflare (`resource_plane.py:3599+`).
- **`gcp`** / **`azure`** (optional, phase 2) — same shape, `google-cloud-*` / `azure-*` SDKs, analogous capability sets.
- These adapters power **both** tracks: Track 1 exports them to `aws_*` / `google_*` / `azurerm_*` Terraform resources; Track 2's generated `.tf` can be `plan`-verified against them.
- **Preserve the ChangeSet gate.** New providers write **only** through ChangeSets + ResourceReceipts + budget reservation (`apply_changeset:1914-1975`), same dual-control and drift/rollback machinery. No new bypass path.

### 3.4 Preserving DB-canonical (the invariant across all tracks)

- Track 1 export is a **projection**; Track 1 import produces **proposed ChangeSets**, never direct DB writes.
- Track 2 Terraform is an **artifact BOSS produces**; when BOSS *operates* that infra it still does so through ChangeSets (or the customer runs the `.tf` themselves, entirely outside BOSS's canonical scope).
- Terraform state files are treated as **untrusted downstream mirrors** — read for drift comparison, never as the desired-state authority.
- The `boss_edition` / entitlement gating story is unchanged; multi-cloud adapters and export/sync are entitlement-gated capabilities.

## 4. Implementation Tickets

### T1 — AWS provider adapter in the Resource Plane
- **Scope:** Add `aws` to `SUPPORTED_PROVIDERS` with a v1 capability set (EC2, S3, RDS, IAM, Route53, cost sync) and implement the capability-dispatch/actuation block using `boto3`, following the DigitalOcean/Cloudflare adapter pattern. Wire connection-test, `resource_inventory_sync`, and `costs.sync`. Credentials via `SecretRef` (assume-role supported).
- **Key files:** `apps/api/app/services/resource_plane.py` (`:46` providers, `:212` capabilities, `:3599`/`:4253+` dispatch), config (`apps/api/app/config.py` for `BOSS_AWS_*`), `requirements.txt` (`boto3`).
- **Acceptance:**
  - [ ] `aws` appears in `SUPPORTED_PROVIDERS` with a documented capability list.
  - [ ] `provider_connection_test` succeeds against a real AWS account with read-only creds.
  - [ ] Create/destroy of at least EC2 + S3 + one RDS/IAM resource works **only** through an approved ChangeSet, producing a ResourceReceipt.
  - [ ] `resource_inventory_sync` and `aws.costs.sync` populate inventory/billing ledger.
  - [ ] Destructive ops honor dual-control and budget reservation; rollback plan recorded from `desired_state`.
- **Size:** L

### T2 — Terraform/OpenTofu exporter (Track 1 export)
- **Scope:** New `terraform_export.py` with `changeset_to_hcl()` emitting `.tf.json`, a `resource_ref → tf_address` mapping table (`terraform_exports`), provider blocks rendered from `SecretRef` pointers (no literal secrets), and `import` block generation. Store output as a durable ChangeSet artifact; optional repo commit via `github_adapter.upsert_file`. Opt-in per deployment target.
- **Key files:** `apps/api/app/services/terraform_export.py` (new), `resource_plane.py` (ChangeSet read model, `desired_state` at `:1628`), `github_adapter.py` (`upsert_file:148`), new migration for `terraform_exports`.
- **Acceptance:**
  - [ ] A ChangeSet across DO + AWS serializes to valid `.tf.json` that `terraform validate` accepts.
  - [ ] Re-export of an unchanged ChangeSet is byte-stable (deterministic addresses).
  - [ ] Secrets appear only as `SecretRef`/variable references, never literals (unit test asserts this).
  - [ ] Export is off by default; enabled only when the target opts in.
  - [ ] Generated `import` blocks let an operator adopt BOSS resources into external state without recreation.
- **Size:** M

### T3 — IaC verification lane (Track 2 verify)
- **Scope:** Add an IaC verify lane to the governed pipeline: `fmt -check`, `terraform validate` (with `init -backend=false` and cached providers), `tflint`, `checkov`/`tfsec`, and `terraform plan` (plan-dry default; plan-live behind an approval Decision). Emit plan/scan output into the review packet. Provider mirror/cache so `init` works offline in the sandbox.
- **Key files:** `apps/api/app/services/governed_code_task_service.py` (verify stage ~`:50`), `apps/api/app/services/software_factory.py` (review-packet / gate wiring), sandbox image/deps (`terraform`/`tofu`, `tflint`, `checkov`).
- **Acceptance:**
  - [ ] An IaC task's gate is green only when fmt+validate+tflint+security scan+plan-dry all pass.
  - [ ] `terraform validate` runs with no cloud credentials in the default lane.
  - [ ] `plan-live` requires an approval Decision before scoped creds are used.
  - [ ] Security-scan findings surface on the existing security review axis, not as advisory noise.
  - [ ] Plan output is attached to the review packet as an artifact.
- **Size:** L

### T4 — Enable IaC authoring in code agents (Track 2 generate)
- **Scope:** Add `.tf/.tf.json/.tfvars/.hcl` to the writable extension set; add an `infrastructure` role/persona with Terraform-aware guidance (provider pinning, remote backend, least-privilege IAM, no hardcoded secrets). Route IaC tasks through the governed/micro-VM lane and add a module-aware context packer that always includes core `*.tf` files past the byte cap.
- **Key files:** `apps/api/app/services/code_agent.py` (`:353-354` exts, `:110-211` roles, `:352-359` context packer), pipeline profiles (`pipeline_profiles.py`).
- **Acceptance:**
  - [ ] Agent can write a multi-file Terraform module (`main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`).
  - [ ] IaC tasks run on the governed lane, not the truncating native lane.
  - [ ] `variables.tf`/`outputs.tf`/`versions.tf` are always in agent context regardless of the 40-file/20 KB cap.
  - [ ] Generated module passes the T3 verify lane end-to-end on a sample AWS app.
  - [ ] No secret literals in generated `.tf` (scan gate from T3 catches regressions).
- **Size:** M

### T5 — Terraform drift-sync (Track 1 inbound)
- **Scope:** Extend the drift path so BOSS can ingest a customer `.tf`/plan-JSON and reconcile it against `desired_state` + live inventory, producing a drift report and a **proposed** corrective ChangeSet through the normal approval gate. Never mutate the DB directly from `.tf`.
- **Key files:** `resource_plane.py` (`list_drift_reports:766`, drift classification, ChangeSet proposal path `:1575-1660`), `terraform_export.py` (shared address mapping).
- **Acceptance:**
  - [ ] A `.tf` edit that diverges from `desired_state` yields a drift report referencing the affected `resource_ref`s.
  - [ ] Reconciliation emits a proposed ChangeSet, gated by dual-control approval — no silent DB write.
  - [ ] Live-inventory divergence (real drift) and `.tf` divergence are distinguished in the report.
  - [ ] DB remains canonical: rejecting the proposal leaves desired state unchanged.
- **Size:** M

### T6 — (Optional, phase 2) GCP + Azure adapters
- **Scope:** Add `gcp` and `azure` to `SUPPORTED_PROVIDERS` following T1's pattern (`google-cloud-*` / `azure-*` SDKs), with connection-test, inventory sync, cost sync, and a core compute/storage/DB/DNS capability set. Extend the exporter (T2) to `google_*` / `azurerm_*` resources.
- **Key files:** `resource_plane.py` (`:46`+ dispatch), `config.py`, `requirements.txt`, `terraform_export.py`.
- **Acceptance:**
  - [ ] `gcp` and `azure` connection-tests pass against real accounts.
  - [ ] Core create/destroy works through ChangeSets with receipts.
  - [ ] Exporter emits valid `google_*` / `azurerm_*` blocks that `terraform validate` accepts.
- **Size:** L

## 5. Dependencies, Sequencing & Priority

**Priority:** P2-enterprise. This module has no bearing on the OSS engine split (spec 07) or on single-cloud dogfooding; it lights up when BOSS is sold into multi-cloud enterprises.

**Internal sequencing:**

```
T1 (AWS adapter) ─┬─► T2 (exporter) ─► T5 (drift-sync)
                  └─► T3 (verify lane) ─► T4 (agent authoring)
T6 (GCP/Azure) depends on T1 pattern + T2 exporter
```

- **T1 is the keystone** — both tracks need at least one big-three adapter to be credible. Do it first.
- **T3 before T4:** never let agents write Terraform you cannot verify (the whole point is closing the "Python-only verify" gap). The verify lane must exist before authoring is enabled.
- **T2 before T5:** drift-sync reuses the exporter's stable address mapping.
- **T6 is optional/phase-2**, gated on enterprise demand for GCP/Azure specifically.

**Cross-spec dependencies:**

- **Depends on nothing new** in the engine-split work — but the multi-cloud adapters compound the existing budget/entitlement coupling in the Resource Plane (research digest §OSS-split), so keep new adapter code free of new direct budget imports where possible.
- **Blocks / underpins `04-*` (CI/CD generation):** enterprise deploy pipelines provision-then-deploy; T1's AWS adapter and T4's generated IaC are prerequisites for spec 04's staging→prod orchestration onto customer clouds. Sequence T1 ahead of spec 04's enterprise-cloud pipeline work.
- **Shares** the ChangeSet/Decision gate and deployment registry with spec 04 — coordinate so a "provision + deploy" command produces one coherent gated flow, not two disjoint ones.

## 6. Open Decisions

1. **HCL vs `.tf.json` for export.** `.tf.json` round-trips losslessly from JSONB and needs no pretty-printer (proposed v1). But operators expect human-readable `.tf`. Ship `.tf.json` first and add an HCL pretty-printer (e.g. `hclwrite` via a Go sidecar, or a Python HCL emitter) later? Or invest in HCL from day one?
2. **Terraform vs OpenTofu as the default binary.** OpenTofu is the license-clean (MPL) choice and aligns with the OSS posture; some enterprises standardize on HashiCorp Terraform. Support both binaries behind a config flag, or pick one default?
3. **`plan-live` credential model.** Scoped read-only creds against a real account give the strongest verify signal but touch a live provider from a sandbox. Is an approval Decision per plan-live run sufficient, or do we need a dedicated read-only-verify IAM role provisioned per customer?
4. **Where does generated IaC live?** Committed into the customer's application repo alongside code (one repo), or a separate infra repo? Affects T4 context packing and T2 export target.
5. **How far does Track 1 sync go?** Report-and-propose only (safe, proposed), or an opt-in auto-apply for low-risk drift? The PRD's dual-control default argues for propose-only; enterprises may want auto-heal for tier-1 drift.
6. **Adapter breadth vs Terraform passthrough.** For long-tail AWS services, do we hand-write a capability per resource type (high fidelity, high cost) or let Track 2's generated Terraform cover the long tail while the native adapter covers only the common core? Likely hybrid — define the boundary.
7. **Provider mirror in the verify sandbox.** `terraform init` needs provider plugins; a network-isolated sandbox needs a local provider mirror. Bundle a curated mirror into the sandbox image, or allow a scoped registry egress?
