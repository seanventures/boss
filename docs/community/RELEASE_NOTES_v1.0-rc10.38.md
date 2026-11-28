# BOSS Community 1.0.0-rc.10.38 candidate

RC10.38 unifies the full wave of P1 platform, Principal Engineer intelligence, Acceptance Agent, Quick Lane, Kiro Crew parity, MetaTron CLI, and security/budget/sandbox hardening slices on top of `v1.0-rc10.37`.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## Highlights

**Principal Engineer Intelligence (`BOSSAPP-784`–`792`)**
- **Study before change (PE-A)**: structured `StudyRecord`, red-first reproduction gate for bug fixes, inverted risk tier for existing code, read-only KG tools (`kg_callers`, `kg_blast_radius`, `kg_precedent`, `kg_why`) on the `boss_kg` gateway server, and PR claim-to-study verification.
- **Learned context & agent setup (PE-B)**: steers, retry notes, PR rejections/comments, and recurring findings write project learnings with real evidence, auto-propose scoped standards on repeat/confirmation, and load agent role/console instructions, progressive guide skills, scoped standards, and 5 seeded org-editable PE opinions into governed builds within a token budget.
- **Full-change review & relabel guard (PE-C)**: diff hunks rendered uncut before file context (even past 12k chars), seat sharding for oversized diffs, blast-radius callers in review scope, and an outcome-relabel guard flagging failure-to-`not_applicable`/success heuristics.
- **Transitive regression protection & replay gate (PE-D)**: transitive reverse import closure + P0 test selection in `verify_manifest.py`, plus replay gate and `### Replay outcomes` in PR descriptions.
- **Jev-first decision ladder & safe deadlines (PE-F)**: unified 5-rung ladder with decider attribution and override-rate tracking, pre-step applicability from app profile + manifest before launch, and reversible auto-apply vs irreversible remind-and-escalate deadlines.
- **Preview honesty (PE-K)**: `procps` in `Dockerfile.community`, monorepo root `dev` script exclusion, inferred `PYTHONPATH` for guessed Python entries, and run-path requirement before ranking a Python entry as a server.
- **Done contracts & completion checks (PE-L)**, **Clean PR & planning (PE-Phase 2)**, and **First Mate, skill/standard experiments, MCP output budgeting & objective loops (PE-Phase 3)**.

**Security, Budget, Engine & Sandbox Hardening (`BOSSAPP-1481`–`1490`, `1502`)**
- Atomic deployment-gate consumption (`deployment_gate_uses`), rejected onboarding workflow run finalization, and hidden approval fields in deploy tool schemas (`BOSSAPP-1481`).
- Atomic `secret_material.owner_scope` claims, injective secret ref keys with legacy read continuity, GHES slot `api_base` binding, and alias-key refusal (`BOSSAPP-1489`, `1490`).
- Transaction-scoped canonical advisory locks for budget admission counting unexpired reservations (`BOSSAPP-1502`).
- Run fencing + P8 replica epoch alignment, scripted-engine zero-provider-call review path, per-grant proxy request/token caps (HTTP 429 `run_budget_exhausted`), and provider-only limit classification (`BOSSAPP-1482`, `1484`, `1485`, `1488`).
- Post-commit subscription-pool login deletion, org-pool relocation preferring model home, `(actor, target_pool)` sign-in isolation, and sticky BOSS-owned group-writable `run_dir` (`01770`) (`BOSSAPP-1483`, `1486`).

**Acceptance Agent, Quick Lane, Kiro Crew & Platform M2–M5**
- Acceptance Agent backend step/policy, merged Run Report v2, and ticket filing/sync (`BOSSAPP-1222`, `1223`, `1225`).
- Quick Lane dedicated warm queue, full context, independent validation, limits, and entry points (`BOSSAPP-795`–`799`).
- Kiro Crew P1 & P2 parity: compliance-bounded autonomy, goal-owning area agents, GitLab/Azure DevOps `RepoHost`, harness pins, zero-token jobs/monitors, prebuilt install, learned skills, and structured supervisor (`BOSSAPP-935`–`942`).
- Cleared all 13 quarantined web unit test files (`tests/known-failures/web.txt` now at **0** quarantined files; `BOSSAPP-1467`).
- MetaTron CLI Waves C & D, Eventing M2.3, Model Routing M5, App Map, Failure Pre-check proposals, One Rules Book, Self-Fix gaps, and PR test evidence (`#350`, `#359`–`#366`, `#370`).

## Upgrade notes
- Run `boss upgrade` or `remote.sh update` as usual.
- Schema updates are additive and idempotent (`ensure_schema` verified twice on clean Postgres).
