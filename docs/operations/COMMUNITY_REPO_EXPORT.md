# Full Community repository export — BOSS-293

**Target:** `seanventures/boss` (created empty and private on 2026-09-24).  
**Source:** private `seanheiney/boss` monorepo.  
**Status:** export design; no code has been pushed to the target. The public flip is blocked by the [release checklist](PUBLIC_FLIP_CHECKLIST.md).

## Product and license boundary

The product owner chose Apache License 2.0 for the **full self-hosted Community product**, with a counsel reference to follow (BOSS-300). That choice covers the Community API, worker, web console, engine, CLI/MCP, installer, and the specs they need to run. It does not mean the entire source monorepo is approved for publication. Apache permits third-party commercial reuse of the published code; hosted services and separately authored Cloud/Enterprise code can remain private.

The Community product contract is in [`BOSS_COMMUNITY_EDITION_SPEC.md`](../product/BOSS_COMMUNITY_EDITION_SPEC.md), and the active pack is `packages/specpack/packs/community_edition.yaml`. The older engine-only extraction checklist is insufficient for this target. A full Community export must install and run independently, without a private package or hosted Account Connect.

## Source inventory requiring a reviewed split

| Surface | Community candidate | Boundary work before export |
|---|---|---|
| Runtime | `engine/`, Community API and worker code, migrations | Remove or separate paid service imports and routes while preserving Community's durable tasks, policy, receipts, ProviderOps, self-hosted SSO, and the self-hosted Goldeneye control plane. `apps/api/app/main.py` currently imports `cloud_product` and `commercial` at module load; `goldeneye_paas` is a Community service that must survive the split. |
| Web | Community Mission Control, project/organization management, tasks, decisions, live coordination | Split paid pages and imports from `apps/web/app/page.tsx` and `apps/web/lib/enterprise.ts` / `commercial.ts`; confirm all Community navigation still works. A runtime edition gate alone does not keep source private. |
| Packages | API client, Community specpack, Community workflows/policies/roles | Community pack carries Apache-2.0 as the product-owner candidate and no longer installs the private commercial packaging policy. Counsel reference and release record still gate publication. Review `commercial_preview` module metadata before inclusion. |
| Installer | VM bootstrap, Compose, official coding CLIs, Community docs | Verify every image and volume used by the one-line command comes from the exported repo or a public dependency. No private registry or local-only hostname may be required. |
| Operations | Public README, contributing/issue forms, security policy, license and applicable notices | Keep internal deployment, credentials, partner/customer data, private ops guidance, and commercial packaging out of the export. |

Known paid/private candidates to exclude or refactor are `apps/account_connect/`, `apps/api/app/adapters_enterprise/`, hosted Account Connect and marketplace routes, Cloud billing/managed-worker code, `engine/boss_engine/services/cloud_product.py`, Enterprise cluster/support code, `packages/specpack/packs/enterprise_edition.yaml`, and private production Goldeneye deployment material. This is an **inspection list**, not an exhaustive allowlist. The standalone Goldeneye kit and self-hosted `/v1/goldeneye/*` control plane are Community per [`GOLDENEYE_PAAS.md`](../product/GOLDENEYE_PAAS.md); they require private-reference and dependency review, not blanket exclusion. Self-hosted SSO, ProviderOps, and the core governed runtime are also Community despite some `enterprise` names in route and object paths; classification must follow behavior, not filenames.

## Export procedure and evidence

1. Make a reviewed Community-only tree from a fixed private source commit. Use an explicit inclusion manifest and dependency graph; fail when a Community import or Compose image depends on excluded code. Preserve source-to-export provenance in a private release record.
2. Start the target repository with a **new root commit**, not a push of the monorepo history. A clean current tree does not remove the committed `.env.bak-edition` or other findings from reachable history. Review the eight initial gitleaks matches and rotate any live credentials.
3. Scan the candidate tree and **all reachable target history** with gitleaks and an independent approved scanner. Review matches, private hostnames, customer data, third-party licenses, and applicable Apache NOTICE obligations. Attach redacted evidence to BOSS-293.
4. On a clean Linux VM, run the one-line installer, restart it, and prove API, worker, web, TUI, and MCP operate without private services. Exercise Claude, Codex, and Grok CLI installation and their official interactive subscription login options using test accounts. Keep credentials on the intended machine; record only redacted proof.
5. Run Community API/worker/web tests and the task → budget/receipt → decision → PR loop. Check the public issue/PR templates and private security mailbox. Confirm claims and partner notice gates from the release checklist.
6. Push the reviewed snapshot to the **private** target for final review. Change visibility only after BOSS-300 counsel record and every applicable BOSS-293 gate has evidence. Re-scan immediately before the flip.

The target repository stays private and empty until steps 1–3 produce a clean candidate. This document records the release boundary; it is not a license decision or publication approval.
