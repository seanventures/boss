# Knowledge graph case studies: banking and insurance

Companion to [KNOWLEDGE_GRAPH_SPEC.md](KNOWLEDGE_GRAPH_SPEC.md) and [KNOWLEDGE_GRAPH_CASE_STUDY.md](KNOWLEDGE_GRAPH_CASE_STUDY.md).
Status: draft for the operator · 2026-10-21.

## Why banking and insurance

The pattern we keep hearing from people in these sectors:
1. many agents do pieces of the work;
2. what they find and decide is captured as a knowledge graph;
3. that graph is fed back so the next agent, or the next analyst, answers better.

These sectors want this more than most, for three reasons:
- **Decisions must be explained.** Regulators ask *why* a change shipped, *why* an alert was closed, *why* a claim was paid.
- **Institutional knowledge is fragile.** Business rules live in a large, ageing Java monolith and in a few senior people's heads.
- **Rework is expensive.** The same investigation is done again because the last one's reasoning was never captured.

BOSS's knowledge graph is built for exactly this, because BOSS **records the governed history itself**: who decided, on what evidence, with which sign-off. Other tools have to reconstruct it afterwards.

The cases below fall into two groups:
- **Software delivery inside a bank or insurer** (cases 1–3). BOSS does this today, and the knowledge graph makes it stronger.
- **Business-operations agent work** (cases 4–5). BOSS provides the governed agent platform: workflows, Passports, sign-offs, budgets, audit and the knowledge loop. The domain systems plug in through MCP connectors.

Every case lists what BOSS has, what it must add, and how we would **prove** it, using open code or public synthetic data and never real customer data.

---

## Case 1 · Core-banking modernisation without losing the business rules

**The problem.** A bank is breaking an ageing core-banking **Java monolith** into services. The interest, fee and posting rules are buried in code that nobody fully understands. Migrations break things that matter quietly: rounding, day-count conventions, the order of operations at end of day.

**How BOSS runs it.**
- **Juno's panel** scopes each slice: one product, one rule family.
- **Atlas** maps the slice's dependencies **from the code graph** (callers, shared tables, batch jobs).
- **Helm's crew** builds the service with your coding agent.
- **Vera** writes characterisation tests from the old behaviour.
- **Rook** proves differences by reverting.
- **A person signs** each cut-over.

**What enters the graph.**
- *Code:* packages, classes and methods, plus tables and batch jobs.
- *Lifecycle:* which slice moved which rule, and the reviewer findings.
- *Learned:* each extracted **business invariant**, for example "savings interest uses ACT/365, rounds half-even at posting", linked to the old and new code that implement it. Each one is `governed` once a person signs it.

**How the intelligence feeds back.**
- Every later slice starts with the invariants already found, so they aren't rediscovered.
- Reviewers check a new slice against every invariant touching its tables.
- Rook's past findings ("the day-count bug class") become review checks automatically.

**Regulatory fit.** Change management evidence (SOX-style), traceable from requirement to sign-off. The bank keeps knowledge of its own systems after the people who built them leave.

**BOSS today and gaps.**
- *Today:* workflows, crew, reviews, sign-offs, the code graph (Java), the lifecycle graph.
- *Gaps:*
  - "characterisation test" as a step kind;
  - invariant extraction from the monolith's code (Archivist recipe).

**How we prove it (open code).** Use **[Apache Fineract](https://github.com/apache/fineract)** (Apache-2.0, Java, an open-source core-banking platform) as "the legacy core".
- Tasks: extract the loan-interest and fee rules into a new service, slice by slice. Each slice has hidden tests comparing old and new on generated loan schedules.
- Measure, with and without the knowledge layers, as in the retail protocol:
  - defects caught before release;
  - rework (review rounds);
  - cost and time per slice;
  - **invariants rediscovered vs reused**.

---

## Case 2 · Regulatory change, traced from the clause to the code

**The problem.** A new or amended rule arrives, for example a reporting change, a fee-disclosure rule, or a resilience requirement. The bank must find every system it affects, change them, and prove to auditors what changed and why. Today this is spreadsheets and meetings, repeated for every change.

**How BOSS runs it.**
1. The regulation text is ingested (Phase 5 document sources).
2. Juno's panel turns clauses into requirements.
3. Atlas finds the affected code and data fields through the graph.
4. Helm's crew implements the change.
5. Vera tests it against the requirement.
6. A **compliance owner signs** each requirement as met.
7. Quill writes the evidence pack.

**What enters the graph.** A traceable line:
- `clause → requirement → data field / report line → code symbol → test → release → sign-off`.
- The interpretations a person approved, for example "'customer' in clause 4.2 includes joint-account holders", become `governed` facts.

**How the intelligence feeds back.**
- The next change to the same regulation, or a similar one, starts from the existing trace and the approved interpretations.
- Jev cites precedent: "last time clause 4.x changed, these 6 systems were affected; 2 were missed at first."
- Auditors get the trace on demand.

**Regulatory fit.** Data lineage expectations (BCBS 239-style risk-data principles), change governance, audit evidence.

**BOSS today and gaps.**
- *Today:* the lifecycle graph, sign-offs, release cards, the code graph.
- *Gaps:*
  - document ingestion (Phase 5);
  - **data-field-level edges** (which column feeds which report line);
  - a "regulatory change" workflow template;
  - an evidence-pack export.

**How we prove it.**
- Use Fineract plus a **public regulation text** (any published reporting standard), with a synthetic change ("add field X to report Y").
- Measure:
  - recall of affected systems against a hand-built answer key;
  - time and cost to implement;
  - completeness of the audit trace;
  - the **second change to the same clause** (learning).

---

## Case 3 · Operational resilience: payments incidents that don't repeat

**The problem.** Payment platforms fail in recurring ways: certificate expiry, a queue backlog at cut-off, a downstream scheme outage. Each incident is diagnosed from scratch by whoever is on call, and resilience regulations require fast classification, reporting and root-cause analysis.

**How BOSS runs it.** This is BOSS's existing operations loop:
1. **Beacon** triages (no code access in triage, per the stage scopes).
2. Beacon investigates, with code seeded from the evidence.
3. **Helm's crew** fixes.
4. **A person approves** the release.
5. Beacon verifies recovery on BOSS's monitor and writes the report.

**What enters the graph.**
- Each incident's signals, root cause, fix, runbook and **time to recover**, linked to the services and code involved.
- Approved post-incident actions become `governed` facts, for example "payments-gateway must alert at 70% queue depth before cut-off".

**How the intelligence feeds back.**
- Triage cites precedent: "this signature matched incident #41: certificate rotation; runbook R-7 resolved it in 6 min."
- Investigations start from known causes (`known_causes` already exists).
- Repeated causes become **prevention tasks**: a new alert rule or a code fix, proposed by Beacon.

**Regulatory fit.** Operational-resilience regimes (for example DORA in the EU) expect incident classification, timely reporting, root-cause analysis and lessons learned. BOSS produces the incident record and report as a by-product.

**BOSS today and gaps.**
- *Today:* monitoring, alerts, incidents, the response workflow, reports, `known_causes`.
- *Gaps:*
  - an incident-classification step (severity criteria per regulation);
  - a regulatory-report template;
  - "repeated cause → prevention task" automation.

**How we prove it.** The retail study's T4 → T9 design, re-skinned:
- a payments service on staging;
- planted fault classes (an expired certificate, a cut-off backlog), each injected twice.

Measure **time to root cause and to recover on the repeat** with the knowledge layers vs without.

---

## Case 4 · AML alert investigation (business operations)

**The problem.**
- Transaction-monitoring systems raise many alerts, and most are false positives.
- Analysts gather the same evidence again and again (customer profile, counterparties, past alerts), and write narratives by hand.
- Their reasoning, why an alert was closed, rarely gets reused.
- Regulators expect consistent, explainable decisions.

**How BOSS runs it.** An "alert investigation" workflow:
1. A **collector agent** gathers evidence through MCP connectors to case management, KYC and transactions. It is read-only, with its Passport ceiling scoped to those systems.
2. A **network agent** maps related parties.
3. An **analyst agent** drafts a recommendation **citing precedent**.
4. A **person (the analyst) decides**. The decision and its reason are recorded.
5. If escalated, a narrative agent drafts the report and **a person files it**.

**What enters the graph.** A *separate, domain* graph in the same store, with its own layer and permissions:
- entities (customer, account, counterparty, device);
- typologies;
- past alerts and their **human decisions with reasons**;
- reviewer QA findings.

Approved closure rationales become `governed` precedent. Agent-drafted links stay `inferred` until a person confirms them.

**How the intelligence feeds back.**
- New alerts on related entities start with what was already learned.
- Jev and the analyst agent cite the closest precedents ("closed 3 similar alerts: payroll pattern, documented").
- QA findings ("insufficient source-of-funds evidence") become checks on future drafts.
- **Consistency** across analysts improves because everyone draws on the same precedent.

**Governance (the reason BOSS fits).**
- A person makes every determination; agents never close or file.
- Passports scope data access, and personal data is scrubbed from what models see where possible.
- Everything is audited, retention is per policy, and data can be forgotten by lineage.
- Model-risk documentation can be produced for each agent: purpose, inputs, limits, and outcomes measured by the learning loop.

**BOSS today and gaps.**
- *Today:* workflows, Passports, human decisions with reasons, audit, budget, the knowledge store with tiers, scrubbing and forget.
- *Gaps:*
  - a **domain-graph layer** (entities that are not code);
  - MCP connectors for case management;
  - a document/narrative output step;
  - **field-level PII policy** (what a model may see);
  - an agent model-risk card.

**How we prove it (public synthetic data, never real customers).** Use a public **synthetic AML transaction dataset** (several exist, for example IBM's published synthetic AML transactions on Kaggle; verify the licence first) to generate alerts.
- Simulate the analyst with a scripted answers file, as in the retail study.
- Measure:
  - time per alert;
  - the agreement of the recommendation with the labelled ground truth;
  - consistency across simulated analysts;
  - **reuse of precedent on related alerts**;
  - model cost per alert.

---

## Case 5 · Insurance claims and underwriting that learn from adjusters

**The problem.**
- Claims handlers and underwriters apply guidelines that live in documents and experience.
- Similar claims are handled inconsistently.
- Fraud patterns spotted by one adjuster don't reach the others.
- AI-assisted decisions in insurance face explicit fairness and explainability expectations.

**How BOSS runs it.** A "claim review" workflow:
1. An intake agent extracts facts from the claim documents.
2. A coverage agent checks them against the **governed guideline facts** (each a policy clause linked to an interpretation a person approved).
3. A fraud-signals agent compares the claim to past patterns.
4. A draft decision goes to **an adjuster, who decides**.
5. Underwriting uses the same pattern for referrals.

**What enters the graph.**
- Policy clauses → interpretations (governed);
- claims → decisions and reasons;
- adjuster overrides of agent drafts (the most valuable signal);
- confirmed fraud patterns;
- guideline changes with validity dates. Facts are temporal, so a claim is judged on the rule in force at its date.

**How the intelligence feeds back.**
- Agents draft against approved interpretations and the closest precedents, and cite them.
- Every override teaches: repeated overrides of the same kind become a **proposed guideline fact** for a manager to approve (the promotion queue).
- Fraud patterns confirmed once are checked on every new claim.

**Governance.**
- A person decides every claim and referral.
- Every draft cites its sources.
- Fairness monitoring runs on outcomes (the learning loop already records them).
- Temporal facts give "which rule applied when".

**BOSS today and gaps.** The same as case 4: the domain-graph layer, document-intake connectors, field-level PII policy, the model-risk card, plus **fairness metrics** on the learning loop.

**How we prove it.**
- Use a public synthetic insurance-claims dataset (verify its licence) and a written sample policy wording.
- Planted guideline changes test the temporal behaviour.
- Measure:
  - handling time;
  - agreement with labelled outcomes;
  - consistency;
  - the override rate falling as knowledge accumulates (the learning curve);
  - cost per claim.

---

## What BOSS needs to build, across all five

| Capability | Needed by | Where it fits |
|---|---|---|
| Data-field lineage edges | 2 | Phase 1 follow-up |
| Document ingestion (regulation texts, policy wordings, guidelines) | 2, 5 | Phase 5 |
| **Domain-graph layer** (non-code entities) with its own permissions | 4, 5 | New: after Phase 4, on the same store and tiers |
| Field-level PII policy (what models may see) | 4, 5 | Extends Phase 2 scrubbing |
| Workflow templates: regulatory change, incident classification, alert investigation, claim review | 2–5 | Workflow library |
| Evidence-pack and report exports | 2, 3 | Quill + docs step |
| Agent model-risk card (purpose, inputs, limits, measured outcomes) | 4, 5 | Passport extension |
| Fairness and consistency metrics on the learning loop | 4, 5 | Phase 4 extension |

## Recommended order

1. **Retail (NorthPeak on Saleor)** as planned. It proves the knowledge loop in BOSS's strongest area, software delivery.
2. **Case 1 + Case 3 on Fineract.** These are banking stories with no new domain layer: core modernisation and payments resilience. They give the strongest "BOSS for banks" demo soonest.
3. **Case 2**, once document ingestion (Phase 5) lands.
4. **Cases 4–5**, after the domain-graph layer and PII policy exist. These open business operations as a market, and need the most care.

Every case uses open code or public synthetic data, sets its success criteria before running, and reports failed attempts in cost, as in the retail protocol.
