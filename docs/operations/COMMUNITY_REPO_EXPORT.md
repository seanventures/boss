# Full Community repository export — BOSS-293

**Target:** `seanventures/boss` (created empty and private on 2026-09-24).
**Source:** private `seanheiney/boss` monorepo.
**Status (2026-09-28):** interim source `29daa8ca` produced a 1,816-file candidate with
zero mechanical blockers and passed a clean x86 VM lifecycle. New security changes
require a fresh exact export and VM repeat; no code has been
pushed to the target. The public flip is blocked by the
[release checklist](PUBLIC_FLIP_CHECKLIST.md).

## Product and license boundary

The product owner chose Apache License 2.0 for the **full self-hosted Community product** and supplied counsel reference `01049940` (BOSS-300). That choice covers the Community API, worker, web console, engine, CLI/MCP, installer, examples, and the specs they need to run. It does not mean the entire source monorepo is approved for publication. Apache permits third-party commercial reuse of the published code; hosted services and separately authored Cloud/Enterprise code can remain private. The [decision record](OSS_LICENSING_DECISION.md) records the exact scope.

The Community product contract is in [`BOSS_COMMUNITY_EDITION_SPEC.md`](../product/BOSS_COMMUNITY_EDITION_SPEC.md), and the active pack is `packages/specpack/packs/community_edition.yaml`. The older engine-only extraction checklist is insufficient for this target. A full Community export must install and run independently, without a private package or hosted Account Connect.

## Source inventory and reviewed split

| Surface | Community candidate | Boundary work before export |
|---|---|---|
| Runtime | `engine/`, Community API and worker code, migrations | The Community entrypoint, OpenAPI and schema are split and checksum-pinned. Project-scoped write/read authority, Plane effect receipts, and verified Plane TLS are integrated. Interim exported runtime acceptance passed; security fixes and a final repeat remain. Preserve durable tasks, policy, receipts, ProviderOps, self-hosted SSO, and the self-hosted Goldeneye control plane. |
| Web | Community Mission Control, project/organization management, tasks, decisions, live coordination | The Community root route and API client are split from paid source. The integrated source web suite and production build pass. Verify the final export build and runtime. |
| Packages | API client, Community specpack, Community workflows/policies/roles and examples | The Community pack uses Apache-2.0 and omits the private commercial packaging policy. Five optional examples now declare console inputs and stored defaults; the sales and video handoffs were repaired. Package metadata and third-party notices still need final review. |
| Installer | VM bootstrap, Compose, official coding CLIs, Community docs | The source-tree and interim `29daa8ca` export passed the clean x86 VM lifecycle. Repeat on the final exact export after the security changes. |
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

The interim `29daa8ca` candidate's local new-root history is `87d06bfddf47872d67e5c66dd3802bbd34838f76`
(tree `26760fad320de3edf4ea9f650c46e010ac9aff93`). Gitleaks found zero
findings in its exported tree and reachable history; TruffleHog produced 11
unverified fixture/identifier detections and zero verified findings. The
sanitized disposition is in the private task artifact
`community-history-scan-29daa8ca.md`. This local history has not been pushed.

The interim VM run passed first install, rerun with preserved secrets, backup
and restore, same-tree upgrade, safe uninstall and reinstall, API authentication
and edition checks, TUI/MCP reads, the default browser flow, and five optional
example dry runs. It is evidence for the installer design, not qualification of
the later security changes. A new exact source export must repeat the VM run.

A separate Gitleaks scan of all 3,113 private-source commits found 13
historical matches. Four are values in a removed environment backup under
`BOSS_GITHUB_TOKEN`, `AZURE_OPENAI_KEY`, `DEEPSEEK_API_KEY`, and
`OPENROUTER_API_KEY`; their revocation or rotation is unverified. The other
nine were dispositioned as test fixtures, gateway README examples, or prose
false positives in the private
`private-source-history-disposition.md` report. No credential value is
reproduced here.
