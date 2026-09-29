# BOSS Community release notes — review draft

**Status:** Local release candidate named `v1.0-rc1`. This is not a public GA.
Do not publish, push, announce, or mark BOSS-293 done.
`PUBLIC_FLIP_CHECKLIST.md` is not signed off.
**Release name:** `v1.0-rc1`, an annotated local tag on the commit that records
this name. It is not pushed.
**Package version:** `1.0.0-rc.1` on root, web, API client, engine, and the Helm
chart. `create-boss` stays `0.1.0`. Helm and Kubernetes image tags stay
`0.14.0` because those images were not rebuilt or pushed.
`v0.14.0` still peels to `d8069168cc85a88f4551edc8ff4c7361ebc19519`. Do not
move it.

The previous draft called `0db60f6c8318c37cf846409363c1755f81f99bda` the last
qualified source. That claim is retired. `0db60f6c` is an ancestor of this
candidate, not the candidate. Evidence for `0db60f6c` does not qualify
`46c10ca9`.

## What this candidate is

A self-hosted Community control plane for tasks, plans, jobs, budgets,
approvals, receipts, and event history, at source `46c10ca9`, with the console
grouping planned Team structure, Agent Studio definitions, and recorded Agent
work. It is an integration candidate for review, not a publication.

- A one-line Ubuntu/Debian installer creates local credentials, seeds a
  project, starts the API, worker, and console on loopback, and supports
  rerun, backup/restore, upgrade, and safe uninstall/reinstall. The draft
  backs up before changing source during rerun or upgrade. Runtime images
  include Claude Code, Codex CLI, and Grok Build binaries. Provider
  credentials and subscription logins are supplied separately by the operator.
- Thirty-four bundled agent definitions, twelve workflow templates, and five
  opt-in example workflows. Installing definitions or examples starts no work.
  Catalog proof tests at `e63439e9` cover those counts.
- Authenticated API, terminal, and MCP access the same Community control
  plane. The notes carried forward from earlier integration commits still
  describe the terminal as addressing all 973 mounted `/v1` operator routes,
  and MCP as classifying all 986 mounted HTTP operations: 853 use API policy,
  38 require exact human confirmation, 28 remain held for route-specific
  safety work, and 67 are internal or protocol endpoints. Those counts were
  not re-measured for `46c10ca9`.

## What changed since the previous qualified note

The previous qualified note was source `0db60f6c`. This candidate already
contains the following as ancestors:

- Claude setup-token import is disabled (`97379d13`). CLI-owned
  `claude auth login` plus `--connect-claude-local` remains.
- Delivery schedules refuse missing required inputs and can be edited in
  place (`71adbf9a`).
- Agent catalog proof tests (`e63439e9`): 34 definitions, 12 workflow
  templates, 5 opt-in examples. Install does not start work.
- Open-PR ledger (`dab630e7`): 103 open PRs, none needing a port; #207 held.
- Gate gap (`7fdc91e1`): PF gates are not signed off. #284, #286, #287, and
  #288 are in source only. They are not a Goldeneye deploy and not live
  verification. That note also said the source patches do not fix the direct
  API pool-pressure 500 or the isolated overview 502.
- Export pins refreshed (`46c10ca9`). A later export of that SHA wrote 1894
  files, 0 mechanical blockers, gitleaks clean, and `ready_for_publication`
  false. That export was a local quarantine, not a publication.

## Still not evidenced

Do not describe `46c10ca9` as qualified, deployed, or public GA. These are
still not evidenced for this SHA:

- Clean VM of this SHA.
- Image-license counsel.
- Secret-rotation receipts.
- Live `qa-live` on this release commit.
- Claims signoff.
- Release-owner approval.
- Public installer URL.
- Goldeneye deploy of log retention.
- Pool-pressure 500 fixed.
- Isolated overview 502 fixed.

The gate-gap ancestor left PF-1 through PF-9 unsigned. This note does not
sign them off for `46c10ca9`. A mapped operation can still be denied by
project permissions, approval, budget, or edition policy. The held MCP routes
include direct host or deployment effects and approval transitions that need
verified human actor attribution. API and terminal calls retain their own
server-side checks.

New Community Discovery web research, live investigations, and BOSS-initiated
SSH deployment planning remain paused until their spend, lease, receipt, and
recovery paths are complete. A typed-decision workflow attempt spends only
after a committed budget reservation, model lease, and deduplicated receipt;
TypeSafe key proof stays paused, and that path is not Community-export or
clean-VM qualified. Existing records remain readable. Saving a provider key
or detecting a coding CLI is a configuration check, not a successful live
model call. No provider login or paid model call is part of this candidate's
qualification, because this candidate is not qualified.

Older `0db60f6c` local tests, export review, and pristine Ubuntu x86 VM runs
(including a local-file stdin-piped first install) stay historical. They used
local file transport, not a public HTTPS installer URL, and they do not cover
`46c10ca9`.

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
