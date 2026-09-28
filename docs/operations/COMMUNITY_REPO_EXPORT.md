# Full Community repository export — BOSS-293

**Target:** `seanventures/boss` (created empty and private on 2026-09-24).
**Source:** private `seanheiney/boss` monorepo.
**Status (2026-09-28):** integrated export candidate under review; no code has
been pushed to the target. The public flip is blocked by the
[release checklist](PUBLIC_FLIP_CHECKLIST.md).

## Product and license boundary

The product owner chose Apache License 2.0 for the **full self-hosted Community product** and supplied counsel reference `01049940` (BOSS-300). That choice covers the Community API, worker, web console, engine, CLI/MCP, installer, examples, and the specs they need to run. It does not mean the entire source monorepo is approved for publication. Apache permits third-party commercial reuse of the published code; hosted services and separately authored Cloud/Enterprise code can remain private. The [decision record](OSS_LICENSING_DECISION.md) records the exact scope.

The Community product contract is in [`BOSS_COMMUNITY_EDITION_SPEC.md`](../product/BOSS_COMMUNITY_EDITION_SPEC.md), and the active pack is `packages/specpack/packs/community_edition.yaml`. The older engine-only extraction checklist is insufficient for this target. A full Community export must install and run independently, without a private package or hosted Account Connect.

## Source inventory and reviewed split

| Surface | Community candidate | Boundary work before export |
|---|---|---|
| Runtime | `engine/`, Community API and worker code, migrations | The Community entrypoint, OpenAPI and schema are split and checksum-pinned. The final API write-auth review and exported runtime acceptance are in progress. Preserve durable tasks, policy, receipts, ProviderOps, self-hosted SSO, and the self-hosted Goldeneye control plane. |
| Web | Community Mission Control, project/organization management, tasks, decisions, live coordination | The Community root route and API client are split from paid source. The integrated source web suite and production build pass. Verify the final export build and runtime. |
| Packages | API client, Community specpack, Community workflows/policies/roles and examples | The Community pack uses Apache-2.0 and omits the private commercial packaging policy. Package metadata and third-party notices still need final review. |
| Installer | VM bootstrap, Compose, official coding CLIs, Community docs | The source-tree x86 VM lifecycle passed. Repeat on the exact export; verify public dependencies and CLI login guidance. |
| Operations | Public README, contributing/issue forms, security policy, license and applicable notices | The root `LICENSE` is checksum-pinned in the export. Finish third-party terms, public-reference, private-host and reachable-history reviews. |

Known paid/private candidates to exclude or refactor are `apps/account_connect/`, `apps/api/app/adapters_enterprise/`, hosted Account Connect and marketplace routes, Cloud billing/managed-worker code, `engine/boss_engine/services/cloud_product.py`, Enterprise cluster/support code, `packages/specpack/packs/enterprise_edition.yaml`, and private production Goldeneye deployment material. This is an **inspection list**, not an exhaustive allowlist. The standalone Goldeneye kit and self-hosted `/v1/goldeneye/*` control plane are Community per [`GOLDENEYE_PAAS.md`](../product/GOLDENEYE_PAAS.md); they require private-reference and dependency review, not blanket exclusion. Self-hosted SSO, ProviderOps, and the core governed runtime are also Community despite some `enterprise` names in route and object paths; classification must follow behavior, not filenames.

## Export procedure and evidence

1. Make a reviewed Community-only tree from a fixed private source commit. Use an explicit inclusion manifest and dependency graph; fail when a Community import or Compose image depends on excluded code. Preserve source-to-export provenance in a private release record.
2. Start the target repository with a **new root commit**, not a push of the monorepo history. A clean current tree does not remove the committed `.env.bak-edition` or other findings from reachable history. The private source-history review currently has 13 redacted gitleaks findings; disposition them and rotate any live credentials.
3. Scan the candidate tree and **all reachable target history** with gitleaks and an independent approved scanner. Review matches, private hostnames, customer data, third-party licenses, and applicable Apache NOTICE obligations. Attach redacted evidence to BOSS-293.
4. On a clean Linux VM, run the one-line installer, restart it, and prove API, worker, web, TUI, and MCP operate without private services. Exercise Claude, Codex, and Grok CLI installation and their official interactive subscription login options using test accounts. Keep credentials on the intended machine; record only redacted proof.
5. Run Community API/worker/web tests and the task → budget/receipt → decision → PR loop. Check the public issue/PR templates and private security mailbox. Confirm claims and partner notice gates from the release checklist.
6. Push the reviewed snapshot to the **private** target for final review after the exact candidate tree, its proposed history, and dependencies pass review. Change visibility only after every applicable BOSS-293 gate has evidence. Re-scan immediately before the flip.

The target repository stays private and empty until steps 1–3 produce a clean candidate. The counsel decision is recorded, but this document is not publication approval.
