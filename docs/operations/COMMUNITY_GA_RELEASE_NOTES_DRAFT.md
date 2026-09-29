# BOSS Community release notes — review draft

**Status:** Internal draft for BOSS-293. Do not publish, tag or announce until
`PUBLIC_FLIP_CHECKLIST.md` is complete and the release owner signs off.
**Last qualified source:** `0db60f6c8318c37cf846409363c1755f81f99bda`.
The active [integration PR #280](https://github.com/seanheiney/boss/pull/280)
needs a new exact export and release qualification before these notes can ship.

## What this candidate includes

- A self-hosted Community control plane for tasks, plans, jobs, budgets,
  approvals, receipts and event history. The console groups planned Team
  structure, Agent Studio definitions and recorded Agent work.
- A one-line Ubuntu/Debian installer that creates local credentials, seeds a
  project, starts the API, worker and console on loopback, and supports rerun,
  backup/restore, upgrade and safe uninstall/reinstall. The current draft
  backs up before changing source during rerun or upgrade. The runtime images
  include Claude Code, Codex CLI and Grok Build binaries; provider credentials
  and subscription logins must be supplied separately by the operator.
- Thirty-four bundled agent definitions, twelve workflow templates and five
  opt-in example workflows. Installing definitions or examples starts no work.
- Authenticated API, terminal and MCP access to the same Community control
  plane. The terminal addresses all 973 mounted `/v1` operator routes. MCP
  classifies all 986 mounted HTTP operations: 853 use API policy, 38 require
  exact human confirmation, 28 remain held for route-specific safety work,
  and 67 are internal or protocol endpoints.

## Current limits to state with the release

- A mapped operation can still be denied by project permissions, approval,
  budget or edition policy. The 28 held MCP routes include direct host or
  deployment effects and approval transitions that need verified human actor
  attribution. API and terminal calls retain their own server-side checks.
- New Community Discovery web research, live investigations, BOSS-initiated
  SSH deployment planning and typed-decision model execution remain paused
  until their spend, lease, receipt and recovery paths are complete. Existing
  records remain readable.
- Saving a provider key or detecting a coding CLI is a configuration check;
  it is not a successful live model call. No provider login or paid model call
  was part of candidate qualification.
- The last qualified source passed local tests and export/history review;
  two pristine Ubuntu x86 VMs passed installed runtime and installer checks,
  including a local-file stdin-piped first install. Public HTTPS delivery and
  live provider logins were not tested. The active integration source still
  needs a new export, scans, and VM lifecycle. Final-image terms,
  live release-commit QA, target controls and owner approval remain release
  gates. Do not describe it as public GA until those gates pass.

## Evidence for release review

- `docs/community/CONTROL_SURFACES.md` and the API/TUI/MCP capability matrices.
- `docs/operations/COMMUNITY_REPO_EXPORT.md` and
  `docs/operations/PUBLIC_FLIP_CHECKLIST.md`.
- Plane BOSS-293 for exact source, exported history, VM runtime and gate
  evidence; BOSS-300 for the first-party Apache decision and third-party terms.

Review this wording against `COMMUNITY_GA_CLAIMS_FREEZE.md` and any partner
notice requirement before publication.
