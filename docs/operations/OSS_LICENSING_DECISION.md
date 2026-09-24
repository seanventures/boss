# OSS licensing strategy — UNDECIDED (human legal only)

| Field | Value |
|---|---|
| **Status** | **UNDECIDED** |
| **Plane** | [BOSS-300](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/1f573439-a75d-4556-8969-39a3c902c2b8) — GAP: resolve OSS licensing strategy decision |
| **Decision owner** | Human legal counsel + product owner. **Not agents.** |
| **Date recorded** | 2026-08-13 |
| **This file** | Canonical operator record of the licensing *gate*. It does **not** choose a license. |

> **This is not a license. This is not a recommendation.**
> No OSI-approved public open-source status has been granted. Interim
> Apache-2.0 *files* in the tree are packaging evidence, not a human legal
> decision and not authorization to flip the repo public or announce OSS.

**Product direction recorded 2026-09-24:** the product owner selected Apache
License 2.0 for the **full Community repository** planned at
`seanventures/boss`, and said a counsel reference will follow. This resolves
the product preference and intended publication scope only. The counsel
reference has not been provided or recorded here, so the legal/release status
above remains **UNDECIDED** and BOSS-300 and BOSS-293 stay open. The Community
tree must also be separated from paid/private code and pass the public-flip
checks before visibility changes.

---

## 1. Hard rules for agents and contributors

1. **Do not change `LICENSE` files.** Root `LICENSE`, `engine/LICENSE`,
   `packages/api-client/LICENSE`, `packages/specpack/LICENSE`,
   `packages/goldeneye/LICENSE`, or any new license text.
2. **Do not add, rewrite, or delete a `NOTICE` file** to encode a chosen
   license. No `NOTICE` exists today; creating one implies a decision.
3. **Do not close BOSS-300.** Only a human legal/product owner may mark it
   Done after recording `Status: DECIDED` in this file with the chosen
   option, date, and counsel reference.
4. **Do not invent SPDX identifiers, CLAs, DCOs, or copyright-assignment
   terms** as if they were approved.
5. **Do not market BOSS as OSI-approved public open source** until this file
   says `DECIDED` *and* BOSS-293 public-flip gates C1–C8 are complete.
6. **Do not treat v0.8 Apache-2.0 packaging files as the strategy decision.**
   `docs/planning/oss-readiness/01-oss-engine-split-and-packaging.md` PKG-02
   once claimed the §6 strategy was “resolved for v0.8.” That claim is
   **withdrawn**. BOSS-300 remains open.

Related analysis (options only; **not** a decision):
[`docs/planning/oss-readiness/DECISION-oss-licensing-strategy.md`](../planning/oss-readiness/DECISION-oss-licensing-strategy.md).

---

## 2. Current facts (what is in the tree — not what is chosen)

| Artifact | Today | Meaning |
|---|---|---|
| Root [`LICENSE`](../../LICENSE) | Apache License 2.0 text, copyright 2026 BOSS contributors | Interim file from the v0.8 packaging slice. **Not** a public-flip decision. |
| [`engine/LICENSE`](../../engine/LICENSE) | Apache-2.0 short notice | Same. Engine extract / PyPI publish still gated. |
| [`packages/api-client/LICENSE`](../../packages/api-client/LICENSE) | Apache-2.0 pointer to root | Same. Package is `"private": true`. |
| [`packages/specpack/LICENSE`](../../packages/specpack/LICENSE) | Apache-2.0 pointer to root | Same. |
| [`packages/goldeneye/LICENSE`](../../packages/goldeneye/LICENSE) | Apache-2.0 (vendored kit) | Separate kit metadata; still in this monorepo; not a BOSS public-OSS announce. |
| **`NOTICE`** | **Absent** | Apache §4(d) NOTICE is not present. Do not add one until legal decides. |
| `engine/pyproject.toml` | `license = { file = "LICENSE" }` | Points at the interim file. |
| `packages/goldeneye/pyproject.toml` | `license = { text = "Apache-2.0" }` plus Trove “OSI Approved” classifier | Metadata only. Human legal must confirm or replace after BOSS-300. |
| `packages/specpack/packs/community_edition.yaml` | `license_candidate: AGPLv3` + `legal_review_required: true` | **Candidate**, not selected. Conflicts with Apache-2.0 *files* until a human picks one story. |
| PRD v2.0 § Commercial Strategy | AGPLv3 / Apache-or-MIT / proprietary / FSL *candidates*; “legal review required” | Product intent, not a recorded decision. |
| Repo visibility | Private / non-public launch | BOSS-293 public flip is **blocked**. |
| Community pack / edition | Self-hosted Community product | Useful self-host ≠ “this is public OSI OSS.” |

`engine/tests/test_distribution_contract.py::test_distribution_has_apache_license_evidence`
only asserts those files *exist* and contain “Apache License”. Passing that
test is **not** proof that legal chose Apache-2.0 for a public release.

---

## 3. Options under consideration (none selected)

Agents may list these for a human. **Do not rank them. Do not implement one.**

| ID | Option | What it would mean (neutral) | OSI “open source” while restricted? |
|---|---|---|---|
| P | **Proprietary / all-rights-reserved** | Keep the product closed. Rewrite or remove OSI language and Apache-2.0 files after counsel drafts replacement terms. | No |
| M | **OSI-approved permissive** (examples only: MIT, Apache-2.0) | Broad reuse, including competing hosted products. Apache-2.0 and MIT are *examples*, not a shortlist that has been chosen. | Yes, if an OSI license is actually applied to the published work |
| C | **OSI-approved copyleft** (example only: AGPLv3, the PRD *candidate*) | Network-use share-alike on the server/control plane. SDKs/schemas might use a different OSI license. | Yes, if an OSI license is actually applied |
| D | **Dual-license** | Same code under an OSI license *and* a commercial license. Requires counsel, contribution terms, and inbound assignment/CLA policy. | The OSI side can be called open source; the commercial side cannot |
| S | **Source-available / fair-source** (examples only: FSL, BSL) | Source visible with a non-compete or delayed-open window. | **No** during the restriction window. Must not be marketed as open source. |
| X | **Mixed / open-core** | Different terms on engine vs Community control plane vs console vs Cloud/Enterprise modules. Still requires a human pick per surface. | Only the OSI-licensed surfaces |

Boundary questions the human must answer *with* the license (still UNDECIDED):

- How much of BOSS is published (engine-only vs thin Community plane vs full Community product)?
- Is the console paid/proprietary?
- Will external contributions be accepted, and under what inbound terms?
- What may be called “open source” in README, site, GTM, and release notes?

Older planning notes discuss “engine-only / whole Community / thin middle”
as product-boundary options. Those are **not** license selections.

---

## 4. What this gate blocks

Until this file is `DECIDED` by a human:

| Gate | Ticket / ID | Rule |
|---|---|---|
| Engine **public flip** | **BOSS-293** | Do not mark Done. Do not make `boss-engine` (or this monorepo) public. C1 on the extract checklist is this decision. |
| Extract checklist C3 | BOSS-293 C3 | Dependency + license audit cannot be signed against an unchosen license. |
| Extract checklist C4 | BOSS-293 C4 | Community hygiene (CONTRIBUTING/CLA/CoC/SECURITY) cannot invent contribution terms. |
| Community GA **C3 announce** | [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md) C3 | “Legal/OSS readiness if public announce” stays **Open / blocked**. |
| Community GA **C4 announce** | GA living plan C4 | Do not tag or publish notes that claim public OSS / OSI-approved status. Allowed claims are in [`COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md). |
| PyPI / npm / GHCR **public OSS** publish | BOSS-293 / releasing | Private or design-partner distribution only. |
| Marketing “BOSS is open source” | GTM, one-pager, YouTube, community site | Forbidden until DECIDED **and** flip complete. |

Private extract (BOSS-291) and design-partner work on a **private** repo
(BOSS-292) may continue without choosing a public license.

---

## 5. Files that would change *after* a human decision

Do not edit these to “pick” a license. After counsel records `DECIDED` here,
a follow-up ticket should update:

### License texts and notices (human / counsel)

- [`LICENSE`](../../LICENSE) — root terms
- [`engine/LICENSE`](../../engine/LICENSE)
- [`packages/api-client/LICENSE`](../../packages/api-client/LICENSE)
- [`packages/specpack/LICENSE`](../../packages/specpack/LICENSE)
- [`packages/goldeneye/LICENSE`](../../packages/goldeneye/LICENSE) (confirm whether Goldeneye stays on its own terms)
- **`NOTICE`** (create only if the chosen license needs it)
- SPDX headers, if counsel requires them

### Package metadata

- [`engine/pyproject.toml`](../../engine/pyproject.toml) (`license` field)
- [`packages/goldeneye/pyproject.toml`](../../packages/goldeneye/pyproject.toml) (`license` text + Trove classifier)
- [`packages/api-client/package.json`](../../packages/api-client/package.json) (no `license` field today)
- Root / app `package.json` files if a license field is added
- [`packages/specpack/packs/community_edition.yaml`](../../packages/specpack/packs/community_edition.yaml) (`license_candidate` → chosen term or remove)

### Tests that pin current file text

- [`engine/tests/test_distribution_contract.py`](../../engine/tests/test_distribution_contract.py) (`test_distribution_has_apache_license_evidence`)

### Product / operator / community copy

- [`README.md`](../../README.md)
- [`docs/community/README.md`](../community/README.md)
- [`docs/community/FEATURES.md`](../community/FEATURES.md)
- [`docs/community/QUICKSTART.md`](../community/QUICKSTART.md)
- [`docs/product/BOSS_COMMUNITY_EDITION_SPEC.md`](../product/BOSS_COMMUNITY_EDITION_SPEC.md)
- [`docs/product/PRD-v2.0.md`](../product/PRD-v2.0.md) (candidate list → decided terms)
- [`docs/operations/COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md)
- [`docs/operations/GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md) C3/C4
- [`docs/product/ENGINE_EXTRACT_CHECKLIST.md`](../product/ENGINE_EXTRACT_CHECKLIST.md)
- [`engine/README.md`](../../engine/README.md)
- [`engine/CONTRIBUTING.md`](../../engine/CONTRIBUTING.md)
- [`engine/COMMUNITY_READINESS.md`](../../engine/COMMUNITY_READINESS.md)
- [`engine/RELEASING.md`](../../engine/RELEASING.md)
- [`engine/CODE_OF_CONDUCT.md`](../../engine/CODE_OF_CONDUCT.md)
- [`engine/SECURITY.md`](../../engine/SECURITY.md)

### Marketing / GTM (must not announce OSS early)

- [`docs/marketing/BOSS_OSS_ONE_PAGER.md`](../marketing/BOSS_OSS_ONE_PAGER.md)
- [`docs/marketing/GTM_EXECUTION_PLAN.md`](../marketing/GTM_EXECUTION_PLAN.md)
- [`docs/marketing/GTM_GAP_ANALYSIS.md`](../marketing/GTM_GAP_ANALYSIS.md)
- [`docs/marketing/GTM_DECK_REVISION_BRIEF.md`](../marketing/GTM_DECK_REVISION_BRIEF.md)
- [`docs/marketing/youtube/EP1_SCRIPT.md`](../marketing/youtube/EP1_SCRIPT.md) and EP2/EP3
- [`docs/product/GTM_PRODUCT_GAP_RECONCILIATION.md`](../product/GTM_PRODUCT_GAP_RECONCILIATION.md)
- Generated one-pager PDF under `output/pdf/` (regenerate; do not hand-edit)

### Planning records (status only)

- [`docs/planning/oss-readiness/DECISION-oss-licensing-strategy.md`](../planning/oss-readiness/DECISION-oss-licensing-strategy.md)
- [`docs/planning/oss-readiness/README.md`](../planning/oss-readiness/README.md)
- [`docs/planning/oss-readiness/01-oss-engine-split-and-packaging.md`](../planning/oss-readiness/01-oss-engine-split-and-packaging.md)
- [`docs/planning/oss-readiness/14-engine-repo-extraction.md`](../planning/oss-readiness/14-engine-repo-extraction.md)
- [`CHANGELOG.md`](../../CHANGELOG.md) (describe the *decision*, not a silent file swap)

### Out of repo (human ops after DECIDED)

- GitHub visibility / org for `boss-engine`
- PyPI / npm / container registry license metadata
- boss.sean.network community/marketing pages
- Plane BOSS-293 / C3 / C4 evidence attachments

---

## 6. How a human records the decision

When legal and product have actually chosen:

1. Edit **this file**: set `Status: DECIDED`, date, option ID from §3, exact
   license name/version, counsel reference, and which surfaces it covers.
2. Keep BOSS-300 **In Progress** until the LICENSE/NOTICE/metadata PR that
   implements the decision is merged. Then a human may move BOSS-300 to Done.
3. Only then un-block BOSS-293 C1 and GA living-plan C3/C4.
4. Agents may then update the file list in §5 to *match* the recorded
   decision. They still must not invent terms.

Until that happens: **legal is blocked. Public OSS is not a claim we can make.**
