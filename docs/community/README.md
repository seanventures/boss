# BOSS Community

**The open-source Business Operating System for governed AI work.**

BOSS Community is a fully functional, self-hosted product — not a crippled trial of BOSS Cloud. You run the control plane, bring your own model and infrastructure keys, and keep every receipt on your side of the boundary.

| | |
|---|---|
| **Product site** | https://boss.sean.network/community/ |
| **Main product** | https://boss.sean.network/ |
| **Spec** | [BOSS_COMMUNITY_EDITION_SPEC.md](../product/BOSS_COMMUNITY_EDITION_SPEC.md) |
| **Features** | [FEATURES.md](./FEATURES.md) |
| **Quickstart** | [QUICKSTART.md](./QUICKSTART.md) |
| **Cloud path** | [BOSS_CLOUD_ENTERPRISE_SPEC.md](../product/BOSS_CLOUD_ENTERPRISE_SPEC.md) |

## The story in four lines

1. **Outcome in** — a goal becomes a durable command, not a disposable chat.  
2. **Governed work** — tasks, leases, budgets, and role-scoped workers.  
3. **Evidence out** — tests, artifacts, and receipts decide “done.”  
4. **You remain CEO** — sensitive actions stay approvable and reverseable.

## What you get

- Self-hosted **control plane** (API + worker + Postgres)  
- **CLI + MCP + Mission Control**  
- **BYOK** models and ProviderOps for infra you already own  
- **Software Factory** path to review-ready PRs  
- **No Cloud required** — optional Easy Connect later  

## Operating invariants

- No external effect without a `Receipt`.  
- No spendy action without a `BudgetReservation`.  
- No worker execution without a lease.  
- No silent drops.  
- No self-modification without PR, tests, approval, and rollback.  

## Start here

```bash
# See QUICKSTART.md for the full path. Conceptually:
# 1. Postgres
# 2. API + worker (docker compose or make dev)
# 3. Export a model key
# 4. boss CLI / MCP / open Mission Control
```

## Contributing

Prefer small, reviewable PRs with tests for state machines and budget/receipt paths. Read `AGENTS.md` and `docs/product/PRD-v2.0.md` before architecture work. Track work in Plane when operating inside the BOSS dogfood project.

## License

Packaging license is under active legal review (`license_candidate` in the community pack). Do not invent a final public license statement beyond what is published with a release.
