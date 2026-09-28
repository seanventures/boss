# OSS licensing strategy — Community decision recorded

> **Status: DECIDED — Apache License 2.0 for the reviewed full Community export; counsel reference 01049940.**

| Field | Decision |
|---|---|
| **Status** | **DECIDED** for the BOSS Community publication scope below |
| **License** | Apache License 2.0 |
| **Scope** | First-party code and documentation selected for the full, self-hosted BOSS Community repository at `seanventures/boss`, including its control plane, console, engine, installer, examples, SDK, and specpack. Cloud and Enterprise code stays outside that export. Third-party components retain their own terms and notices. |
| **Decision owner** | Product owner, reporting legal counsel approval |
| **Counsel reference** | `01049940`, supplied by the product owner on 2026-09-28 |
| **Date recorded** | 2026-09-28 |
| **Plane** | [BOSS-300](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/1f573439-a75d-4556-8969-39a3c902c2b8) |

The product owner selected Apache License 2.0 for the full Community repository
on 2026-09-24 and confirmed counsel approval with reference `01049940` on
2026-09-28. This records the human decision; it does not complete the code
boundary, history review, installer acceptance, or public release. The target
repository is still private and empty. BOSS-300 remains In Progress until the
license, notice, package metadata, and documentation implementation is merged
and verified. BOSS-293 remains In Progress until all public flip gates pass.

## Publication boundary

The approved Community scope is the reviewed export, not the private BOSS
monorepo wholesale. The exporter must exclude Cloud and Enterprise code,
private configuration, secrets, and internal-only material. A passing export
check is necessary but does not replace review of the exact target commit and
reachable history. The product owner must approve the final public visibility
change after the recorded gates pass.

The root `LICENSE` already contains Apache License 2.0 text. The existing
`engine/LICENSE`, `packages/api-client/LICENSE`, and
`packages/specpack/LICENSE` point to Apache terms. Those files are implementation
inputs, not evidence that every exported file is covered correctly. The
Community export must contain the approved root `LICENSE`; package metadata,
notices, third-party attributions, and links must agree with the approved scope.
The separate Goldeneye kit and vendored dependencies require their existing
terms and notices to be preserved and reviewed before export.

## Implementation and release gates

1. Update the Community pack and policy metadata to reflect Apache License 2.0
   for Community. Remove obsolete AGPL candidate and legal-review-pending
   claims from current Community packaging. Keep historical options clearly
   marked as historical analysis.
2. Export a reviewed Community candidate and verify its root `LICENSE`, all
   license links, package metadata, dependency attributions, and paid-code
   exclusion. Record the candidate commit, file count, and checker result.
3. Complete a full reachable-history secret scan of the candidate publication
   history. Review the 13 currently reported private-source findings, including the
   historical environment backup; rotate any live credentials and scan again.
4. Pass clean x86 VM installer, upgrade, backup, and recovery acceptance on the
   exact export candidate. Retain Linux ARM64 runtime evidence as a separate
   result.
5. Satisfy the public Community claims, security, partner, and live QA gates in
   [PUBLIC_FLIP_CHECKLIST.md](PUBLIC_FLIP_CHECKLIST.md) and
   [GA_LIVING_PLAN.md](GA_LIVING_PLAN.md). Do not infer those from the license
   decision.
6. Merge the reviewed implementation and attach the commit, tests, scan
   reports, and release artifact to Plane BOSS-300 and BOSS-293. Only then may
   BOSS-300 be closed by the human owner and public publication be considered.

## Historical context

The earlier [licensing analysis](../planning/oss-readiness/DECISION-oss-licensing-strategy.md)
compared engine-only, full Community, and thin Community boundaries, along
with permissive, copyleft, and restricted licenses. Its recommendations were
not approvals. The 2026-09-28 decision above supersedes that undecided posture
for the reviewed full Community export. Cloud and Enterprise licensing remains
outside this decision.
