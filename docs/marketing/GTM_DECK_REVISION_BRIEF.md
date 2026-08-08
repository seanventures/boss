# BOSS GTM Deck Revision Brief — Lead Surface and AI SRE Wedge

**Plane:** BOSS-304 · **Prepared:** 2026-08-08 · **Status:** ready to apply to the editable deck source

## Decision to encode

Lead the launch with **BOSS Engine: open-source, self-hosted governed workflow execution**, paired with a **commercial control plane** for collaborative and enterprise operating surfaces. Present **AI SRE / Autonomous Oncall** as the flagship enterprise wedge. Keep **Business Operating System** as the category vision, not the first thing a developer must understand.

```text
Developer entry: OSS Engine + MCP
        ↓
Team expansion: Commercial Control Plane
        ↓
Enterprise wedge: AI SRE / Autonomous Oncall
        ↓
Category vision: Business Operating System for AI labor
```

## Claim discipline

| Claim | Use in deck? | Grounding / limitation |
| --- | --- | --- |
| Self-hosted engine, CLI, MCP, standalone container | Yes | `engine/README.md`; local Docker/MCP verification is recorded in Plane. |
| Governed work with roles, decisions, approvals, budgets, receipts, and evidence | Yes | BOSS product thesis and governed coding pipeline. |
| AI SRE investigates alerts, produces governed remediation, and verifies recovery | Use as the enterprise product direction/wedge | Do not claim full connector parity or autonomous production remediation is already generally available. |
| Commercial collaborative control plane | Yes, as product packaging/roadmap | Do not market an OSS multi-user console as shipped. |
| Apache/OSS public availability | Hold conditional | BOSS-300 licensing/product decision remains the public-flip gate. |

## Slide-by-slide revision

### Slide 03 — Choose one entry surface

> **Start with the engine. Scale into the operating system.**
>
> BOSS Engine is the self-hosted, MCP-native workflow core that turns an outcome into governed work. The commercial control plane adds collaboration, approvals, provider operations, and organization-wide visibility.

```text
[OSS BOSS Engine]  →  [Commercial Control Plane]  →  [AI SRE enterprise wedge]
 CLI + MCP              governance + collaboration     investigate → governed fix → verify
```

**Presenter note:** “We are not leading with five dashboards. We lead with the engine developers can run, then sell the operating controls teams need.”

### Slide 05 — Add the AI SRE / Autonomous Oncall wedge

**Title:** **AI SRE / Autonomous Oncall**

> Connect telemetry and a repository; BOSS investigates the alert, builds a confidence-backed root-cause case, proposes a tested governed remediation, and verifies recovery.

**Proof points:**

1. **Read first, act deliberately** — bounded evidence collection and hypothesis-driven investigation.
2. **Remediation as code** — incidents can become a reviewed, tested, fail-closed PR instead of a handoff to another coding tool.
3. **Governed autonomy** — risk, approvals, budgets, receipts, and rollback boundaries stay visible.
4. **Learn from outcomes** — preserve evidence and feedback for future investigation quality.

**Do not use:** “fully autonomous production remediation today,” “all 20+ connectors shipped,” or an unqualified competitor-parity claim.

### Slide 06 — Packaging anchor (path B)

| Layer | Deck copy |
| --- | --- |
| **OSS Engine** | Self-hosted workflow engine, CLI, schema-typed MCP tools, local PostgreSQL, and model/provider choice. |
| **Commercial Control Plane** | Team workflow, policy governance, approvals, provider and cost operations, enterprise administration, and richer operational surfaces. |
| **Enterprise Wedge** | AI SRE / Autonomous Oncall: alert-to-evidence-to-governed-remediation for production engineering teams. |

**Footnote:** “Public packaging and licensing are subject to the BOSS-300 release decision.”

### Slide 09 — Replace generic scorecard with an oncall outcome scorecard

> **From alert noise to a governed recovery loop**

| Buyer outcome | Measure | Evidence BOSS preserves |
| --- | --- | --- |
| Faster triage | alert-to-hypothesis time | bounded telemetry and RCA evidence |
| Safer remediation | hypothesis-to-reviewed-PR time | spec, tests, review, gate, and decision records |
| Lower incident recurrence | verified recovery / reopen rate | post-fix verification and incident history |
| Controlled autonomy | actions stopped, escalated, or approved by policy | approvals, budget reservations, receipts, and audit history |

**Presenter note:** “The goal is not more autonomous chatter. It is a shorter, auditable path from a signal to a verified recovery.”

### Slide 10 — Put sequencing on one line

> **Now:** OSS engine entry + governed coding proof. **Next:** design partners and enterprise oncall wedge. **Then:** commercial control-plane expansion.

Keep licensing, public-repo flip, and incomplete hosted services out of current capability claims; list them as launch gates or roadmap items.

## Visual and narrative rules

- Use the existing BOSS dark control-plane visual language. Show one operating loop, not a new product for every slide.
- Show **alert → investigate → governed PR → verify** as the SRE wedge flow.
- Keep “AI labor” in the category thesis, “engine” at developer entry, and “oncall” in the enterprise buyer story.
- Keep a human approval/fail-closed gate visible. It is the differentiator, not friction to hide.

## Apply / review checklist

- [ ] Update slide 03 to name the OSS Engine as the lead surface.
- [ ] Add AI SRE / Autonomous Oncall to slide 05’s enterprise wedges.
- [ ] Align slide 06 packaging copy to the OSS Engine + commercial plane split.
- [ ] Replace slide 09’s generic scorecard with the oncall outcome scorecard.
- [ ] Mark unshipped connector breadth, hosted features, and licensing as roadmap or launch gates.
- [ ] Product owner reviews the positioning decision; legal/product resolves BOSS-300 before public release wording is finalized.

## Source record

- `docs/marketing/GTM_GAP_ANALYSIS.md`
- `docs/marketing/GTM_EXECUTION_PLAN.md`
- `docs/planning/12-signal-to-fix-sre-agent.md`
- `docs/product/GOVERNED_CODING_PIPELINE.md`
