# BOSS Community release notes — review draft

**Status:** Local release candidate named `v1.0-rc1`. This is not a public GA.
Do not publish, announce, or mark BOSS-293 done.
`ready_for_publication` stays false.
`PUBLIC_FLIP_CHECKLIST.md` is not signed off.
**Product source:** `1f4176e6520dd7556d74f58ee3fecd8cf1f1a348`, plus the
qualification fixes committed after `1d400797`. The annotated tag `v1.0-rc1`
was moved to `1d400797` and is not moved again by those fixes. The release
commit on `main` is the tip of `ai/codex/boss-293-community-integrate`.
**Tag:** `v1.0-rc1` still peels to `c96f9ac33ff96cbb255a5e6a60e5d549e48a7061`.
It has not been moved onto the product source above, and it has not been pushed.
**Package version:** `1.0.0-rc.1` on root, web, API client, engine, and the Helm
chart. `create-boss` stays `0.1.0`. Helm and Kubernetes image tags stay
`0.14.0` because those images were not rebuilt or pushed.
`v0.14.0` still peels to `d8069168cc85a88f4551edc8ff4c7361ebc19519`. Do not
move it.

Earlier drafts named `0db60f6c8318c37cf846409363c1755f81f99bda` and then
`46c10ca9` as the candidate. Both are ancestors. Evidence collected for
either SHA does not qualify `1f4176e6`.

## What this candidate is

A self-hosted Community control plane for tasks, plans, jobs, budgets,
approvals, receipts, and event history, at product source `1f4176e6`, with
the console grouping planned Team structure, Agent Studio definitions, and
recorded Agent work. The delivery console selects the computer where BOSS
was installed and shows whether that computer's hardware agent is installed.
Additional machines stay unselected until an operator adds them. It is an
integration candidate for review, not a publication.

- A one-line Ubuntu/Debian installer creates local credentials, seeds a
  project, starts the API, worker, and console on loopback, and supports
  rerun, backup/restore, upgrade, and safe uninstall/reinstall. When the
  installer is already inside a BOSS git checkout and no repository URL is
  set, an empty install directory receives that checkout rather than a fresh
  GitHub clone. A tree stamped by `scripts/install/remote.sh` with
  `.boss-shipped-commit` and `scripts/install/community-compose.yml` is the
  install source. The draft backs up before changing source during rerun or
  upgrade. Each compose project tags its runtime images with that project
  name. Runtime images include Claude Code, Codex CLI, and Grok Build
  binaries. Provider credentials and subscription logins are supplied
  separately by the operator.
- The default hardware place is that installer computer. One hardware agent
  installs on that computer and on every other BOSS-connected machine. A
  place is ready only when the agent is installed. Optional provider accounts
  are `aws`, `digitalocean`, or `generic`. Registering an account stores a
  secret reference. A capacity proposal names the hardware agent and stays
  planned until approval, an active budget reservation, and a change-set
  dedupe key. Tests do not call a provider.
- Thirty-four bundled agent definitions, twelve workflow templates and five
  opt-in example workflows. Installing definitions or examples starts no work.
  Catalog proof tests at `e63439e9` cover those counts.
- Choosing a workflow for a change runs that workflow. Inline remains the
  default when the operator does not pick one.
- Authenticated API, terminal, and MCP access the same Community control
  plane. The matrix checked into this tree
  (`docs/community/mcp-api-capability-matrix.json`, summarized in
  `docs/community/MCP.md`) accounts for 993 Community and 1,034 Enterprise
  operations. Community counts on that matrix: 860 API-policy, 64 exact human
  confirmation, 2 operator-only, 67 excluded. The two operator-only operations
  are internal infrastructure and are outside this product release. Paid-only
  additions on that page are 39 API-policy and 2 operator-only. A mapped
  operation can still be denied by project permissions, approval, budget, or
  edition policy.

## What changed since the previous qualified note

The previous qualified note was source `0db60f6c`. This candidate already
contains the following as ancestors:

- Operator Claude setup-token import stores the token only in the local
  secret store. `--accept-database-fallback` does not put that token in
  Postgres. CLI-owned `claude auth login` plus `--connect-claude-local`,
  Codex auth-file import, and `grok login --device-auth` plus
  `--connect-grok-local` remain. None of those setup actions call a model.
  A human still completes the live login. This note does not mark those
  logins done. Backups still blank `CLAUDE_CODE_OAUTH_TOKEN` and
  `CODEX_AUTH_JSON`.
- The product routes that were held for deploy, autonomy, org, and sandbox
  work are unheld behind their own safeguards. Wizard session-token import
  is part of this candidate.
- Typed-decision spending requires a budget reservation, a job lease, a
  per-attempt deduplicated receipt, settlement, and a visible retry.
  TypeSafe key proof stays paused.
- Delivery schedules refuse missing required inputs and can be edited in
  place (`71adbf9a`).
- Agent catalog proof tests (`e63439e9`): 34 definitions, 12 workflow
  templates, 5 opt-in examples. Install does not start work.
- Open-PR ledger (`dab630e7`): 103 open PRs, none needing a port.
- Gate gap (`7fdc91e1`): PF-1 through PF-9 are not signed off. Source patches
  for #284, #286, #287, and #288 are already in the tree. They are not a live
  deploy, and they are not product scope for this community release.
- Community `main.py` export pins on this tree are source
  `5bd9b720f20e9385fb7aa34b12c2a979efc36311b1473397d9f7146daa2fe14c` and output
  `2efe93e90db8fad2c77b8f3bae36e5f091faa22386ca92fa37d8c7361a0878d3`, with one
  account gate. A parent-run export of ancestor `d73e0cea` wrote 1908 files,
  and the export of `1d400797` wrote 1914 files, both with 0 mechanical
  blockers and `ready_for_publication` false. The pin above is the main.py
  rewrite after the qualification fixes. `ready_for_publication` stays false.

## Qualification fixes after `1d400797`

A fresh run of the 63 tests that the older qualify-46 process reported as
failed now passes on this tree. Missing project grants return 403 before an
MCP hold. Named discovery and meetings still reserve budget when a model key
is set. A decision question routes to a meeting. Self-improvement evidence
names every empty field. A missing optional fleet table does not abort the
captain transaction. `ready_for_publication` stays false.

## Still not evidenced

Do not describe `1f4176e6` as qualified, deployed, or public GA. These are
still not evidenced for this product source:

- Clean VM of this SHA.
- Public installer URL. The one-liner still stops on
  `REPLACE_WITH_REVIEWED_40_CHARACTER_SHA`.
- Image publication. Helm and Kubernetes tags remain `0.14.0`.
- Image-license counsel.
- Secret-rotation receipts.
- Live `qa-live` on this release commit.
- Live Claude, Codex, and Grok operator logins.
- Claims signoff.
- Release-owner approval.
- Partner notice.

The gate-gap ancestor left PF-1 through PF-9 unsigned. This note does not
sign them off. API and terminal calls retain their own server-side checks.

New Community Discovery web research, live investigations, and BOSS-initiated
SSH deployment planning remain paused until their spend, lease, receipt, and
recovery paths are complete. A typed-decision workflow attempt spends only
after a committed budget reservation, model lease, and deduplicated receipt;
TypeSafe key proof stays paused. Existing records remain readable. Saving a
provider key or detecting a coding CLI is a configuration check, not a
successful live model call.

Older local install proof of `2e987f1f`, older `0db60f6c` tests, and pristine
Ubuntu x86 VM runs stay historical. They do not cover `1f4176e6`.

## Evidence for release review

- `docs/community/CONTROL_SURFACES.md` and the API/TUI/MCP capability matrices.
- `docs/operations/COMMUNITY_REPO_EXPORT.md` and
  `docs/operations/PUBLIC_FLIP_CHECKLIST.md`.
- `.ai/community-ga-gate-gap.md` for the PF gap recorded at `7fdc91e1`.
- Plane BOSS-293 for exact source, exported history, VM runtime, and gate
  evidence. Leave BOSS-293 open. BOSS-300 is the first-party Apache decision
  and third-party terms, and image-license counsel for this SHA is still open.

Review this wording against `COMMUNITY_GA_CLAIMS_FREEZE.md` and any partner
notice requirement before publication. Publication is not authorized.
