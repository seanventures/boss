# Full Community repository export — BOSS-293

**Target:** `seanventures/boss` (created empty and private on 2026-09-24).
**Source:** private `seanheiney/boss` monorepo.
**Status (2026-09-29):** the last fully qualified source
`0db60f6c8318c37cf846409363c1755f81f99bda` produced a 1,880-file
candidate with zero mechanical blockers. Its pristine Ubuntu 24.04 x86_64 VM
and unpublished one-root history passed technical review. A separate pristine
guest passed the exact installer through stdin with local file transport.
Later changes are being reconciled in [draft PR #280](https://github.com/seanheiney/boss/pull/280);
its source and export have not completed a new clean-VM qualification.
The public HTTPS path remains untested while the target is private and empty.
No code has been pushed to the target. The public flip is blocked by the
[release checklist](PUBLIC_FLIP_CHECKLIST.md).

## Product and license boundary

The product owner chose Apache License 2.0 for the **full self-hosted Community product** and supplied counsel reference `01049940` (BOSS-300). That choice covers the Community API, worker, web console, engine, CLI/MCP, installer, examples, and the specs they need to run. It does not mean the entire source monorepo is approved for publication. Apache permits third-party commercial reuse of the published code; hosted services and separately authored Cloud/Enterprise code can remain private. The [decision record](OSS_LICENSING_DECISION.md) records the exact scope.

The Community product contract is in [`BOSS_COMMUNITY_EDITION_SPEC.md`](../product/BOSS_COMMUNITY_EDITION_SPEC.md), and the active pack is `packages/specpack/packs/community_edition.yaml`. The older engine-only extraction checklist is insufficient for this target. A full Community export must install and run independently, without a private package or hosted Account Connect.

## Source inventory and reviewed split

| Surface | Community candidate | Boundary work before export |
|---|---|---|
| Runtime | `engine/`, Community API and worker code, migrations | The Community entrypoint, OpenAPI and schema are split and checksum-pinned. Project-scoped write/read authority, Plane effect receipts, and verified Plane TLS are integrated. The exact `0db60f6c` image passed representative API/worker runtime probes; any later source change requires requalification. Preserve durable tasks, policy, receipts, ProviderOps, self-hosted SSO, and the self-hosted Goldeneye control plane. |
| Web | Community Mission Control, project/organization management, tasks, decisions, live coordination | The Community root route and API client are split from paid source. The frozen-source web suite, lint/type check and production build pass; the final exported console loaded authenticated Team structure, Agent Studio, Agent work and Workflow Catalogue screens in the VM. First-run organization remains a UX follow-up. |
| Packages | API client, Community specpack, Community workflows/policies/roles and examples | The Community pack uses Apache-2.0 and omits the private commercial packaging policy. Five optional examples now declare console inputs and stored defaults; the sales and video handoffs were repaired. Package metadata and third-party notices still need final review. |
| Installer | VM bootstrap, Compose, official coding CLIs, Community docs | Exact `0db60f6c` first install, rerun, backup/scratch restore, same-tree and changed-code upgrade, safe uninstall/reinstall, health, edition, authentication, loopback ports and owner/worker key separation passed on fresh x86 VMs. A second pristine guest passed stdin-piped installation with local file transport. API/worker images contain Grok, Claude Code and Codex. No CLI authentication or model call was exercised; public HTTPS delivery remains untested. |
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

The frozen `1a8102a2` candidate selected 1,845 files with zero mechanical
blockers. Its local new-root commit is
`bb16111b6a01cb28f2a61a8b41ac0affa7f75c31` (tree
`b3eff8e199e785281b6cabc7721bba188e189fd0`); only one commit is
reachable. Gitleaks found zero findings in the export tree and reachable
history. TruffleHog, with verification disabled, produced 11 unverified,
zero verified synthetic fixture/identifier detections. The sanitized private
review is `/tmp/boss-community-ga-final-1a8102a2/history-scan-summary.md`.
This history is local only and is not publication approval.

The interim VM run passed first install, rerun with preserved secrets, backup
and restore, same-tree upgrade, safe uninstall and reinstall, API authentication
and edition checks, TUI/MCP reads, the default browser flow, and five optional
example dry runs. It is evidence for the installer design, not qualification of
the later security changes. A new exact source export must repeat the VM run.

The frozen `1a8102a2` export passed the same full lifecycle in a pristine
Ubuntu 24.04.5 x86_64 KVM guest. The sanitized [qualification report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-1a8102/qualification-report.md)
includes phase checks, image identities, TUI/MCP/API reads, seven authenticated
console screenshots, and five example dry runs. It also records a first-run
system canary that appears in customer Agent work. BOSS-438 will repair that
presentation without suppressing audit history, and the resulting source must
be exported and qualified again before publication.

The later `37f44df7` export selected 1,879 files with zero mechanical
blockers. Its unpublished local new-root history is
`4e1841e89ccac07169fa65afc03869d2ebfbd540` (tree
`ee72330c41ccbc4fbfce48ff02d7ac996f8a1825`). A path-by-path comparison
matched every selected file, executable mode and symlink. Full-history Gitleaks
found zero; TruffleHog without verification found 11 unverified synthetic
fixture or identifier matches, zero verified. The sanitized
[history report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-37f44df7/history-scan-summary.md)
records those dispositions and the four old private-source credential fields
that still lack rotation or revocation evidence. Its pristine x86 VM passed
install and runtime lifecycle; the
[sanitized report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-37f44df7/qualification-report.md)
records the Grok image gap that was repaired after this source. These results
do not qualify any later source commit.

The last fully qualified `0db60f6c` export selected 1,880 files with zero mechanical
blockers. Its local unpublished new-root commit is
`af200616ce793cb169c4fcfb15adf3f7abf508bb` (tree
`2c69a0c2e10aa8bf2685ac124386c5e3ff738a65`), with one reachable
commit and no remote. All selected paths, content, modes and symlinks match.
Full-history Gitleaks found zero; TruffleHog without verification found 11
unverified fixture or identifier matches, zero verified. The
[sanitized exact-candidate report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-0db60f6c/history-scan-summary.md)
records the dispositions. Four old private-source credential fields still
need rotation or revocation receipts. Its
[sanitized clean-VM report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-0db60f6c/qualification-report.md)
records first-run access, representative API/TUI/MCP operations, 11
human-confirmation checks, lifecycle preservation, changed-code upgrade and
a second pristine stdin-piped install using local file transport. Provider
login, paid model calls, every mapped route, and public HTTPS delivery were
not exercised. Any later source change needs its own qualification.

A separate Gitleaks scan of all 3,113 private-source commits found 13
historical matches. Four are values in a removed environment backup under
`BOSS_GITHUB_TOKEN`, `AZURE_OPENAI_KEY`, `DEEPSEEK_API_KEY`, and
`OPENROUTER_API_KEY`; their revocation or rotation is unverified. The other
nine were dispositioned as test fixtures, gateway README examples, or prose
false positives in the private
`private-source-history-disposition.md` report. No credential value is
reproduced here.
