# Public-flip readiness checklist (engine repo)

| Field | Value |
|---|---|
| **Plane** | **BOSS-293** (this checklist) · **BOSS-300** (license — human legal / blocked) · BOSS-291 (extract) · BOSS-292 (design partners) |
| **GA living plan** | [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md) **C3** (legal/OSS readiness if public announce) · **C4** (tag + notes with allowed claims only) |
| **Extract pointer** | [`docs/product/ENGINE_EXTRACT_CHECKLIST.md`](../product/ENGINE_EXTRACT_CHECKLIST.md) Phase C |
| **License decision** | [`DECISION-oss-licensing-strategy.md`](../planning/oss-readiness/DECISION-oss-licensing-strategy.md) — **Status: OPEN / UNDECIDED** |
| **Mechanical check** | `make public-flip-check` (`scripts/check_public_flip.py`) — **fails while license is UNDECIDED** |
| **Date** | 2026-08-13 |
| **Verdict** | **NOT READY.** Do not flip `boss-engine` (or this monorepo) public. Do not stamp BOSS-293 or BOSS-300 Done. |

**2026-09-24 scope note:** The product owner requested a full Community repository at
`seanventures/boss` and selected Apache License 2.0 as the intended license.
The required counsel reference and a reviewed Community/paid code boundary are
still pending. This engine-extract checklist does not, by itself, clear a full
monorepo publication. A redacted gitleaks scan of `origin/main` history found
eight matches, including a committed `.env.bak-edition`; findings need review
and any live credentials need rotation before any public push. The report is
stored outside the repository under the task's `public-readiness` audit folder.

> **Hard rule:** Authoring this file is not a public flip, not a license choice, and not GA.
> Engine `LICENSE` files (Apache-2.0) **bias** options A/C; they are **not** a substitute for
> the BOSS-300 human legal/product decision. Do not change `LICENSE`. Do not enable GitHub
> Actions. Do not change repository visibility from this checklist.

---

## How to use this checklist

Each row is evidence-gated. A row is **Done** only when the listed evidence exists
(artifact path, scan report, Plane comment, or signed decision). Docs without
evidence stay **Open**. `make public-flip-check` passing is **necessary but not
sufficient** — it only covers mechanical bits plus the license-status fail-closed
gate.

**Exit BOSS-293** only when every **Blocker?** = yes row is Done **and** BOSS-300
status is `DECIDED` in the decision doc (not inferred from code).

---

## Gate table

| ID | Gate | Owner | Blocker? | Evidence required | Status (2026-08-13) |
|---|---|---|---|---|---|
| PF-1 | **License decision (BOSS-300)** | Legal + Product (human) | **Yes — hard** | Decision doc header `Status: DECIDED` with chosen option A/B/C (or recorded alternative), date, and signer. NOTICE / package metadata / marketing language agree. Plane BOSS-300 comment with the decision — then and only then may 300 move Done. | **BLOCKED / Open.** Doc is `Status: OPEN`. Recommendation C is **not** a decision. Engine Apache-2.0 files are **not** a decision. |
| PF-2 | **SECURITY.md private reporting channel** | Security + Release owner | **Yes** | `engine/SECURITY.md` names an approved private destination (security advisory URL and/or monitored mailbox). CoC reporting destination agrees. | **Partial.** Product owner supplied `boss@sean.net`; root and engine policies now name it on this branch. Confirm mailbox monitoring and merge before marking Done. |
| PF-3 | **Secret scan (full history)** | Security | **Yes** | gitleaks + trufflehog (or approved equivalent) on the approved publication history *and* a second pre-flip pass. Zero unresolved hits. Report attached to BOSS-293. | **Blocked.** Initial `origin/main` gitleaks pass found eight matches. Finding review, credential rotation, target-history scan, and second pass remain. |
| PF-4 | **No-credential git history** | Eng + Security | **Yes** | After PF-3: no live secrets, customer data, private hostnames, or unapproved internal endpoints in reachable history. Rewrite only **before** first shared/public push. | **Blocked.** A historical `.env.bak-edition` is among the scan matches. A clean current tree cannot clear reachable history. |
| PF-5 | **Community claims freeze** | Product + Marketing | **Yes** (for any public announce) | Public README, release notes, blogs, videos, and landing copy use **only** [`COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md) Allowed claims. Forbidden claims absent. GA living plan **C4**. | **Partial (doc).** Freeze exists and is in force. Flip-time copy review is not signed. Do not announce “public OSS” / “Community GA” off this row. |
| PF-6 | **14-day qa-live** | Ops + QA | **Yes** if the flip is bundled with Community GA language. **No** for a silent private-repo hygiene rehearsal. | [`QA_LIVE_14DAY.md`](QA_LIVE_14DAY.md) shows **14 consecutive PASS** days with logs. GA living plan **C1**. Do not backfill. | **Open.** Calendar is **Day 1/14** (2026-08-13). Same-day re-run does not add a day. |
| PF-7 | **Private → public GitHub steps** | Release owner (human) | **Yes** | Operator runbook below executed **after** PF-1…PF-6 (and PF-8/PF-9). Visibility change recorded (who / when / repo URL). Actions remain **off** unless a separate, approved enablement ticket says otherwise. | **Open.** Not executed. This repo must stay private. |
| PF-8 | **Engine extract checklist (pointer)** | Architect + Eng | **Yes** (extract must exist before flip) | Live private `boss-engine` per [`ENGINE_EXTRACT_CHECKLIST.md`](../product/ENGINE_EXTRACT_CHECKLIST.md) Phase A exit (BOSS-291 execution, not merely the authored checklist). Phase C rows C1–C8 map here. | **Partial (pointer).** Checklist authored; BOSS-291 closed as **checklist**, not as a live extracted SoT. Monorepo `engine/` is still in-tree. |
| PF-9 | **Partner comms** | Product / Partner owner | **Yes** if any design partner has private-repo access | Written notice **before** visibility change: date, what becomes public, what they must redact, support channel after flip. BOSS-292 exit (2+ externals self-serve) or an explicit “no partners yet — flip still blocked on PF-1” note. | **Open.** Onboarding pack exists (`engine/DESIGN_PARTNER_ONBOARDING.md`). Live partner completions and pre-flip notice are not on record. |

Status key: **BLOCKED** = cannot proceed until a named human decision or missing artifact; **Open** = work remaining; **Partial** = artifact exists but is not flip evidence; **Done** = evidence attached (none of the blocker rows are Done).

---

## Cross-links (do not treat as substitutes)

| Doc | Role |
|---|---|
| [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md) **C3** | Community GA “legal/OSS readiness if public announce” — **this file is the evidence table** |
| [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md) **C4** | Tag + notes must use the claims freeze only |
| [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md) **C1** | 14-day `qa-live` (PF-6) |
| [`COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md) | Allowed vs Forbidden public language |
| [`QA_LIVE_14DAY.md`](QA_LIVE_14DAY.md) | Calendar + no-backfill rule |
| [`ENGINE_EXTRACT_CHECKLIST.md`](../product/ENGINE_EXTRACT_CHECKLIST.md) Phase C | Extract-centric twin of this table (C1–C9) |
| [`14-engine-repo-extraction.md`](../planning/oss-readiness/14-engine-repo-extraction.md) | Spec for private extract → partners → flip |
| [`DECISION-oss-licensing-strategy.md`](../planning/oss-readiness/DECISION-oss-licensing-strategy.md) | BOSS-300 options A / B / C — still OPEN |
| [`engine/COMMUNITY_READINESS.md`](../../engine/COMMUNITY_READINESS.md) | Engine-kit hard gates (hygiene files, not approval) |
| [`engine/SECURITY.md`](../../engine/SECURITY.md) | Private vulnerability reporting to `boss@sean.net`; monitoring check pending |
| [`engine/DESIGN_PARTNER_ONBOARDING.md`](../../engine/DESIGN_PARTNER_ONBOARDING.md) | Partner pack (BOSS-292) |
| [`GTM_PRODUCT_GAP_RECONCILIATION.md`](../product/GTM_PRODUCT_GAP_RECONCILIATION.md) | Public-OSS language still blocked on #300 |

---

## PF-7 — Private → public GitHub steps (do not run yet)

Execute only after PF-1 is **DECIDED** and PF-2…PF-6, PF-8, PF-9 evidence is attached
to BOSS-293. A human release owner performs these steps; agents do **not**.

1. Confirm the target is the **approved org** private `boss-engine` repo (not a personal account; not this monorepo unless legal explicitly chose monorepo-public).
2. Confirm BOSS-300 decision text, LICENSE/NOTICE/headers, and README license badge **match**. **Do not invent or edit LICENSE** to force a match.
3. Attach PF-3/PF-4 scan reports (extracted history + second pre-flip pass).
4. Confirm `SECURITY.md` has a real private reporting channel (PF-2).
5. Re-read the claims freeze (PF-5). Strip any “public OSS / Community GA / unattended factory” language that is still Forbidden.
6. Notify design partners in writing (PF-9) and wait the agreed notice window.
7. Confirm GitHub **Actions stay disabled** unless a separate approved ticket enables them. This checklist does **not** enable Actions.
8. Confirm branch protection, secret scanning alerts, and push rules on the private repo still apply after the visibility change.
9. Change visibility **private → public** on the approved repo only. Record actor, timestamp, and public URL in BOSS-293.
10. Cut the release tag **only** with C4-allowed claims in the notes. Link the tag from BOSS-293.
11. Watch secret-scanning / dependabot / first-hour issues. Rollback plan: if a secret or license error appears, revert visibility to private if the org still allows it, rotate credentials, and keep BOSS-293 In Progress.

**Rollback note:** GitHub public-to-private is not a full unpublish (clones, forks, and caches may remain). Treat the flip as one-way for secret and license purposes.

---

## PF-9 — Partner comms (minimum contents)

Send **before** PF-7 step 9. Keep partner names out of the public repo.

- What becomes public (engine tree / commit range / package name).
- What stays private (goldeneye, customer tenants, this monorepo if still private, credentials).
- Date/time of the flip and the public URL that will exist afterwards.
- Ask partners to close or redact private issues that contain hostnames, keys, or customer names.
- Post-flip support channel and private security reporting at `boss@sean.net`.
- Reminder that the license in force is whatever BOSS-300 **DECIDED** — not a guess.

If no partners have been invited, write that fact on BOSS-292/293 and keep PF-9 Open
until Product signs “flip with zero partners.” Silence is not a sign-off.

---

## Mechanical check (not a flip)

```bash
make public-flip-check
# equivalent: python3 scripts/check_public_flip.py
```

What it **does**:

- Fails closed when [`DECISION-oss-licensing-strategy.md`](../planning/oss-readiness/DECISION-oss-licensing-strategy.md) is not `Status: DECIDED`.
- Verifies `engine/SECURITY.md` exists.
- Verifies [`COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md) exists.
- Verifies this checklist file exists.
- Verifies git does not track real `.env` / credential files (`.env.example` is allowed).

What it **does not** do:

- Choose or change a license.
- Run gitleaks/trufflehog or rewrite history.
- Enable Actions or change GitHub visibility.
- Count Day 1/14 as 14 days.
- Stamp BOSS-293 or BOSS-300 Done.

Today’s expected result: **exit non-zero** (`PUBLIC FLIP: BLOCKED`) because BOSS-300
is UNDECIDED. A green mechanical subset (files exist, no committed `.env`) is
printed for honesty and is **not** a pass.

---

## Explicit non-goals

- Do not flip any repository public from this work.
- Do not close Plane **BOSS-300** or **BOSS-293**.
- Do not edit root or `engine/LICENSE` to “resolve” PF-1.
- Do not enable GitHub Actions.
- Do not weaken the claims freeze to make C4 look ready.

---

## Evidence log (append-only)

| When | What | Who |
|---|---|---|
| 2026-08-13 | Authored this evidence-gated checklist. Wired `make public-flip-check`. License still UNDECIDED (BOSS-300). 293 remains In Progress. | grok (BOSS-293) |
