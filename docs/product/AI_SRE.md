# BOSS AI SRE kernel

Map of [awesome-ai-sre](https://github.com/agamm/awesome-ai-sre) categories onto
BOSS. This is a **capability ledger**, not a claim of vendor parity.

**Not public GA.** Cloud/Enterprise FEATURES stay Partial. Remediation never
auto-merges. Spend requires a BudgetReservation. External notify is fail-closed
without credentials (dry-run receipt). Decisions/DLQ stay human.

## Operator loop

```
telemetry / alert
  → reliability.scan (cluster + dedupe)
  → incident
  → investigate (parallel hypotheses)
  → page (on-call, receipted)
  → remediations (governed code task / PR)
  → verify (stability window)
  → postmortem (evidence-only)
```

Always-on: Metatron overwatch + worker tick must run the **observe** half
(scan / diagnose / page). Apply/remediate stays validate-then-Decision.

## Category map

| awesome-ai-sre category | BOSS surface | Status |
|---|---|---|
| AI SRE agents | Metatron overwatch, focused-agents, `/v1/ai-sre/*` | ◐ always-on scan/diagnose missing |
| Production debugging | reliability diagnose + factory remediations | ◐ no live log-injection |
| Incident management | `/v1/reliability/incidents`, `/v1/oncall-sre` | ◐ no postmortem artifact |
| Observability | observability-suite, telemetry connectors, stack | ◐ NL query is plan-only |
| AIOps | reliability.scan clustering, anomaly_detector | ◐ no first-class noise API |
| Log analysis | telemetry_mining, activity log | ◐ |
| Chaos engineering | `/v1/chaos` | ◐ named scenarios only |
| Runbook automation | processes + (new) SRE runbooks | ◐ missing incident runbooks |
| Cloud cost | budget, cost_insights, FinOps | ◐ |
| LLM DevOps | factory, MCP `scripts/reliability_mcp_server.py` | ◐ MCP too thin |
| Agent benchmarks | official QA + (new) SRE bench | ◐ no SRE-bench slice |

## Swarm slices

Plane **BOSS-387** tracks this swarm. Kernel is `ai/grok/ai-sre-kernel` @
`e35e711` (`GET /v1/ai-sre/capabilities`). Docs-only honesty lives on
`ai/grok/ai-sre-docs`.

**Close only with live evidence.** Unit tests, ledgers, and docs are not
enough. FEATURES stays ◐. Do not stamp shipped or public GA.

| Slice | Intent | Status | Close only with |
|---|---|---|---|
| overwatch | Always-on Metatron/worker tick of the observe half: `reliability.scan` / diagnose / page | in flight | Live overwatch tick that runs scan/diagnose and one receipted page (or an honest dry-run receipt) |
| postmortem | Evidence-only postmortem artifact on a reliability incident | in flight | Live postmortem object with cited evidence; no invented RCA confidence |
| runbooks | Incident-attached, approval-gated runbooks | missing | Live runbook attach + gated execute; no silent production write |
| noise/ask | First-class noise-reduction view + NL ask plan (not a live PromQL executor) | in flight | Live noise API + one honest NL plan; missing stays missing |
| mcp/ui/bench | Thicker reliability MCP, Mission Control SRE surface, deterministic SRE-bench | in flight | Live MCP tools + UI against real incidents + bench scores that are not invented |

Remediation stays a governed code task / PR. Decisions and DLQ never
auto-apply. Spend needs a BudgetReservation.

## Hard rules

- **Close only with live evidence.** Do not flip FEATURES or stamp GA from this file.
- Missing evidence stays `not_scored` / `unknown`. Never invent RCA confidence.
- Remediation opens a governed code task / PR. Never silent production writes.
- `force_live` notify without credentials is 422.
- `BOSS_MICROVM_CLAIMS_FROZEN` stays true.
- FEATURES row stays ◐ until live dogfood of overwatch scan + one receipted page.
