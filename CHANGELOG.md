# Changelog

All notable changes to BOSS are documented in this file.

## v0.2.0 - 2026-06-02

This is the first clean founder-dogfood release cut from the current API-first control plane.

### Added

- Canonical Postgres-backed control plane for:
  - tasks
  - jobs and leases
  - decisions
  - artifacts
  - receipts
  - budget reservations and cost receipts
  - events
  - deadletters
  - health snapshots
- API-first surfaces for:
  - CEO command intake
  - Metatron status queries
  - budget quote, reserve, release, and status
  - decision inbox and decision actions
  - receipts, audit export, and replay bundles
  - role and pack discovery/install
  - entitlement source management and routing logs
  - comms inbox/outbox
  - self-improvement proposals
- Pull-based worker runtime with:
  - leases
  - heartbeats
  - retries
  - reclaim
  - deadletters
- Software-factory founder flow:
  - command -> plan -> approval -> PR proposal -> PR review -> merge gate / merge job -> deploy review / deploy job
- GitHub-backed sandbox PR, merge, and deployment integrations
- Model entitlement broker with provider-aware routing logs
- Health snapshot generation and top-risk derivation
- Communications plane with receipt-backed, reservation-backed outbound delivery
- Self-improvement workflow with proposal, canary, rollout, and rollback records

### Changed

- Budget state now affects execution at lease time:
  - `orange` defers non-critical work
  - `red` halts non-`p0` work
  - `red` freezes higher-risk merge, deploy, and outbound comms effects
- Sandbox PR proposals now capture real GitHub check-run evidence into `test_report` and `review_packet` artifacts.
- Merge approval now fails closed unless the linked validation report is in a passed state.
- Documentation now includes a shipped implementation-status report in `docs/product/IMPLEMENTATION_STATUS.md`.

### Verification

- `make lint`
- `make test`
- `make smoke`
- OpenAPI parse check

### Known constraints

- Identity, membership, and RBAC are not complete yet.
- Deployment is recorded through real GitHub deployment state, but not yet through a full health-verified staged rollout pipeline.
- Role trust scoring, autonomy changes, and role-drift enforcement are not fully runtime-enforced.
