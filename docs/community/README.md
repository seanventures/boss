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

Private design-partner invite: [`docs/operations/DESIGN_PARTNER_ONBOARDING.md`](../operations/DESIGN_PARTNER_ONBOARDING.md)
(`make onboard`). That path does not claim a public repository or that
sandboxes always work.

**One-line (recommended):**

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash
```

That installer checks your machine, installs dependencies via Docker, prompts for BYOK model keys, starts Postgres + API + worker (+ Mission Control), seeds `boss-dogfood`, and verifies health. See [QUICKSTART.md](./QUICKSTART.md) and [`scripts/install/README.md`](../../scripts/install/README.md).

## Contributing

Prefer small, reviewable PRs with tests for state machines and budget/receipt paths. Read `AGENTS.md` and `docs/product/PRD-v2.0.md` before architecture work. Track work in Plane when operating inside the BOSS dogfood project.

## License

The product owner confirmed counsel approval for Apache License 2.0 for the reviewed BOSS Community export (reference `01049940`). The public repository is not published yet; see the [licensing decision](../operations/OSS_LICENSING_DECISION.md) and [public release checklist](../operations/PUBLIC_FLIP_CHECKLIST.md) for remaining gates.
