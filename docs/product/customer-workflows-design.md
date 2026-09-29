# Customer Delivery Workflows — Design

**Status:** proposed, awaiting approval
**Author:** drafted 2026-08-30
**Constraint from the request:** *"The BOSS infra level workflow should not change. We need to
define customer level workflow steps — these are not supported by BOSS today."*

---

## 1. What already exists

This matters more than anything proposed below, because most of the hard part is built. I
surveyed the running system before designing.

### 1.1 A real workflow runtime (WFL-001)

`engine/boss_engine/services/workflows.py` — 2,952 lines, with a full schema behind it:

```
workflow_definitions  →  workflow_versions  →  workflow_mappings
                              ↓
                         workflow_runs  →  workflow_step_runs
                                             ↓
                    workflow_compensation_runs / workflow_migrations
```

What it already gives us, and would be expensive and risky to rebuild:

| Capability | Where |
|---|---|
| Immutable, content-hashed versions | `workflow_versions.content_hash`, unique per definition |
| Deterministic selection of a version for a class of work | `workflow_mappings` (selector + scope + priority) |
| Step leasing, heartbeats, attempts, retry-after | `workflow_step_runs.lease_owner / attempt / run_after` |
| Human approval mid-run | `workflow_step_runs.decision_id → decisions(id)` |
| Evidence per step | `workflow_step_runs.evidence` |
| Nested workflows | `child_workflow_run_id`, `subworkflow` kind |
| Compensation (undo) | `workflow_compensation_runs` |
| Version migration of a live run | `workflow_migrations` |
| A driver | `process_manager.py`, ticked by the worker each loop |
| A complete HTTP surface | 20 endpoints — create, publish, map, run, claim, complete, fail, heartbeat, pause, resume, migrate, compensate |

**Step kinds today:** `task`, `skill`, `integration`, `agent_group`, `decision`, `timer`,
`subworkflow`, `condition`, `parallel`, `join`, `bounded_loop`.

Two of these are worth calling out because they answer part of the request directly:

- **`skill`** steps already bind a `skill_id` and fail closed if the skill is missing or
  disabled. *"Define the skill for those workflow steps so the Agent knows what to execute"* is
  already the runtime's model — 17 skills are registered today, each with a kind, a risk tier
  and input/output schemas.
- **`decision`** steps already block a run on a human answer. *Configurable approval* is a
  policy layer over a mechanism that exists.

### 1.2 Thirty-three existing definitions — all BOSS's own machinery

`governed_coding_pipeline`, `meeting_consensus`, `deployment_run`, `adversarial_verification_execute`,
`budget_gated_job`, `self_improvement`, and 27 others. These are how BOSS governs itself.

**These do not change.** Nothing in this design edits a system definition, alters an existing
step kind, or changes the behaviour of a published version. Everything below is additive.

---

## 2. What is genuinely missing

1. **No separation between BOSS's workflows and a customer's.** One namespace, one table. A
   customer-facing editor pointed at this today could edit `governed_coding_pipeline`.
2. **No step that performs a product capability.** A step can call a skill, an integration, or
   an agent group. None of them says "do the research", "convene the panel", "build it",
   "deploy it", "watch it". The five things a delivery workflow is made of are exactly the five
   things a step cannot currently be.
3. **No catalogue.** No industry-standard delivery workflows to pick from.
4. **No per-step approval policy.** `decision` is a step you place; there is no
   `auto | manual` setting on an ordinary step, no approver, no SLA, no timeout behaviour.
5. **No association from a project or a feature to a workflow.** `workflow_mappings` has the
   mechanism (selector → version) but nothing sets it from the product surface.
6. **No builder.** No drag-and-drop, no fork-and-save, no "apply to this project".

---

## 3. Design

### 3.1 Two tiers, one engine

Add `namespace` to `workflow_definitions`: `'system'` (the 33, frozen) or `'product'`
(customer). Every customer-facing API and UI is hard-scoped to `namespace = 'product'`.

**Why reuse the engine rather than build a parallel one.** Leasing, retry, evidence,
compensation, and mid-run approval are the parts that are hard to get right, and they are
already correct and under test. A second runtime would duplicate all of it and diverge. The
risk of sharing tables — a customer feature reaching BOSS's internals — is real, and is
answered by the namespace guard plus a test that asserts a product API cannot read, write,
version, or run a `system` definition.

**Migration is trivial and safe:** the column defaults to `'system'`, so the 33 keep working
untouched and every new customer workflow is explicitly `'product'`.

### 3.2 The capability step — one new kind, additive

```jsonc
{
  "id": "build",
  "kind": "capability",
  "binding": {
    "capability": "build",
    "inputs": { "from_step": "spec", "risk_tier": 1 }
  },
  "approval": { "mode": "manual", "approver_role": "product_owner", "sla_hours": 24 }
}
```

`capability` is a **new** kind. No existing kind changes, so no existing definition is affected.
It dispatches to services BOSS already has:

| Capability | What the step does | Backed by |
|---|---|---|
| `research` | Web research → cited dossier, evidence-gated | Discovery pipeline (`create_discovery`) |
| `meeting` | Agent panel debates → PRD | `request_meeting` / `meeting_service` |
| `spec_review` | Human reads the PRD and approves | `decisions` |
| `build` | Governed code task → tested, gated PR | `create_code_task` / governed pipeline |
| `review` | Adversarial, security, consistency seats | governed pipeline review seats |
| `test` | Authoritative test execution + evidence | sandbox validation |
| `preview` | Start a live preview of the app | `start_preview` |
| `deploy` | Ship to a bound target | `deploy_app` / `deployment_run` |
| `monitor` | Register health checks and thresholds | `deployment_health` |
| `notify` | Post the outcome to a channel | `mattermost.post_real` skill |
| `document` | Publish the change record | `publish_document` |

Each capability declares its **inputs, outputs, required skills, and risk tier** in one registry
(`engine/boss_engine/services/workflow_capabilities.py`), so:

- the builder can render a palette from it,
- validation can reject a workflow whose steps cannot be satisfied in this project,
- and **the agent knows what to execute** — which was the point of asking for skills on steps.

A capability that a project cannot satisfy (no deploy target bound, no repo connected) fails
**at publish time with a readable reason**, not at 2am in the middle of a run.

### 3.3 Approval: configurable, but not below the floor

```jsonc
"approval": {
  "mode": "auto" | "manual",
  "approver_role": "product_owner",
  "sla_hours": 24,
  "on_timeout": "block" | "escalate" | "approve"
}
```

- `manual` → the step materialises a `decision` and waits. Already supported by the runtime.
- `auto` → proceeds, and still writes a receipt saying it was auto-approved and under which
  policy. An unattended decision that leaves no record is how an audit fails.
- `on_timeout: "approve"` is offered because real teams need it, and is **recorded as a
  decision made by policy**, never as a decision made by a person.

**A risk floor overrides configuration.** A capability with `risk_tier >= 2` (deploy to
production, merge a PR, spend above a threshold) is manual regardless of what the workflow
says. Governance that can be configured away is not governance. This is a rule in the
capability registry, not a setting.

### 3.4 The predefined catalogue

Twelve, chosen to cover how software actually gets delivered. `[A]` = approval gate by default.

| # | Workflow | Steps |
|---|---|---|
| 1 | **Standard Product Delivery** | research → meeting → spec_review `[A]` → build → review → test → preview → deploy `[A]` → monitor → notify |
| 2 | **Minor Feature** | meeting → build → review → test → preview → deploy `[A]` → notify |
| 3 | **Fast Fix / Hotfix** | build → test → review → deploy `[A]` → notify |
| 4 | **Research-Led Discovery** | research → research → meeting → spec_review `[A]` → document |
| 5 | **Backend Service Setup** | meeting → spec_review `[A]` → build (scaffold) → build (endpoints) → test → deploy `[A]` → monitor |
| 6 | **Frontend / UX Certification** | meeting → build → review (a11y + visual) → preview → ux_review `[A]` → deploy `[A]` → monitor |
| 7 | **Security Hardening** | research (threats) → build → review (security seat) → test → security_review `[A]` → deploy `[A]` |
| 8 | **Data / Migration Change** | meeting → spec_review `[A]` → build → test (with rollback proof) → dba_review `[A]` → deploy `[A]` → monitor |
| 9 | **Compliance-Ready Release** | every step of #1, plus document + evidence bundle + sign-off `[A]` |
| 10 | **Experiment (A/B)** | meeting → build (variant) → deploy (flagged) `[A]` → monitor (metric window) → decision `[A]` → deploy or revert |
| 11 | **Third-Party Integration** | research → meeting → integration setup `[A]` → build → test → deploy `[A]` → monitor |
| 12 | **Deprecation / Sunset** | meeting → notify (consumers) → timer (grace period) → build (removal) → deploy `[A]` → document |

Seeded as `namespace='product'`, `status='active'`, **owned by BOSS and read-only**. A customer
never edits a catalogue workflow in place — they **fork** it (§3.6), which is what makes
"reset to the standard" possible later.

### 3.5 Applying a workflow to a project or a feature

`workflow_mappings` already resolves *selector → version* by scope and priority. We add the
selector vocabulary the product needs:

| Selector | Value | Meaning |
|---|---|---|
| `work_class` | `project.new` | A new project starts here |
| `work_class` | `feature.major` | A major feature |
| `work_class` | `feature.minor` | A minor feature |
| `work_class` | `bugfix` | A fix |
| `project` | `<project_id>` | This project overrides the org default |

So an org sets its defaults once, a project overrides where it differs, and a single feature can
still be launched on a named workflow. Priority and scope already do the resolution; we are
supplying vocabulary, not mechanism.

### 3.6 Editing and building

**Fork-to-edit.** "Customer modifies a pre-existing workflow" = clone the catalogue version into
a new `product` definition owned by that customer, then edit. The original stays pristine, the
copy records what it was forked from, and the diff against the standard stays visible.

**Save = publish a new version.** A published version is immutable and content-hashed; editing
produces version N+1. Runs in flight keep the version they started on (the runtime already does
this), so saving a workflow never changes the meaning of work already underway.

**The builder.** Phase 4. A palette of capability steps and control steps
(`condition`, `parallel`, `join`, `timer`, `decision`), a canvas, drag to order, click a step to
set its inputs and approval. Deliberately **linear-with-branches first**, not a free-form graph:
the twelve catalogue workflows are sequences with a few gates and parallel review fans, and a
general DAG editor is a much larger surface that most customers will never use. The underlying
spec is already a graph, so a graph canvas can come later without a data migration.

**Validation is continuous, not on save.** A step whose capability the project cannot satisfy is
marked in the canvas as you place it, with the reason and the fix.

### 3.7 Custom steps — BUILT

A named, reusable step a customer defines: `workflow_custom_steps` holds the label, the thing it
binds, and the inputs it declares. It appears in the same builder palette as the eighteen
built-in capabilities, and runs through `workflow_capability_runner` like any other step.
The readable workflow plan accepts the stored custom input objects
(`name`, `required`, `description`) as well as built-in capability input names.
It names optional inputs without presenting them as fields the operator must
supply to start a run.

**The risk tier is never a field the customer fills in.** It is resolved from whatever the step
binds — the skill's registered `config.risk_tier`, or the MCP server's tier and its per-tool
override — at publish time and again at run time. An unclassified binding resolves to the FLOOR,
not to zero: a tool nobody has looked at is treated as dangerous. Without this, "custom step"
would be the documented way around every approval gate in the product.

**Two binding kinds, not three.** A skill and an MCP tool are each independently callable. An
integration connection is not — in BOSS it grants authority *to* a skill (`capability_resolutions`)
and is never invoked on its own — so offering it would have produced a step that publishes and
then has nothing to run. To use an integration, bind the skill that runs through it.

**Defining a step is what grants the runner permission to call the skill.** Skills declare their
`allowed_callers`, and none of them named a workflow runner that did not exist when they were
written — so without a grant every custom step is denied on its first run. The grant is explicit
(somebody chose this skill, by name, in this project), scoped (only to skills a definition row
exists for), recorded (`workflow.custom_step_created` names the actor), and revocable (delete the
step). It widens *who may call*, never *what may be done*: risk tier, cost, policy and data
boundary reach the runtime exactly as the registry wrote them.

Two runtime gaps had to be closed to make this real, and both mattered independently of custom
steps:

* An approved gate never resumed. The runtime's resume logic only handles `decision`-kind steps,
  so a capability gate opened, a person approved it, and the step waited forever.
* `approval.mode: manual` was stored on a step and then ignored at run time, which made the risk
  floor advisory — a step marked "always requires a person" was claimed and executed unattended.

Gates now open for **any** step whose effective approval is manual, and `gate_completes_step`
distinguishes the two kinds that wait: an approval gate (answering it *is* the work) from an
ordinary step awaiting sign-off (answering it only unblocks the work).

---

## 4. What this deliberately does not do

- **Does not touch the 33 system workflows** — no edits, no behaviour change, no shared editing
  surface. A test enforces this.
- **Does not let a workflow bypass a governance gate.** Approval mode is configurable above the
  risk floor and not below it.
- **Does not become a general BPMN engine.** No arbitrary scripting in steps, no
  customer-authored code paths in the runtime.
- **Does not replace the Factory.** A workflow *orchestrates* code tasks, meetings and deploys;
  it does not reimplement them.

---

## 5. Phased implementation

Each phase is shippable on its own and leaves the system working.

### Phase 0 — Separation (small, unblocks everything)
- `namespace` column on `workflow_definitions`, defaulting to `'system'`.
- Product API scope guard + tests that a product caller cannot reach a system definition.
- **Proven by:** a test that every one of the 33 stays `system`, and that each product endpoint
  refuses a system id.
- **Risk:** very low. Additive column, default preserves today's behaviour.

### Phase 1 — The capability step
- `capability` step kind + `workflow_capabilities.py` registry (inputs, outputs, skills, risk).
- Dispatch to the existing services for `research`, `meeting`, `build`, `deploy`, `monitor`
  first — the five in the request — then the rest.
- Publish-time validation with readable reasons.
- **Proven by:** a two-step product workflow (`meeting → build`) that runs end to end and opens
  a real PR.
- **Risk:** medium. New execution path. Mitigated by being additive — no existing kind changes —
  and by each capability calling a service that is already in production use.

### Phase 2 — The catalogue and applying it
- Seed the twelve, `namespace='product'`, read-only.
- Selector vocabulary + "apply to project" and "launch this feature on workflow X".
- **Proven by:** applying **Standard Product Delivery** to a project and watching a feature walk
  the steps.
- **Risk:** low.

### Phase 3 — Seeing it in the console
- A project's workflow, its current step, what is waiting on whom, and per-step evidence.
- Deep-links to the artefacts each step produced (PRD, PR, preview, deployment, logs).
- **Proven by:** a live run readable end to end by someone who did not start it.
- **Risk:** low. Read-only.

### Phase 4 — The builder
- Palette, drag-to-order, per-step config, continuous validation.
- Fork-to-edit, save-as-new-version, apply.
- **Proven by:** forking a catalogue workflow, adding a review step, saving, applying, running.
- **Risk:** medium — mostly UI. The data model is unchanged by this phase.

### Phase 5 — Approval policy
- `auto | manual` per step, approver role, SLA, timeout behaviour, escalation.
- Receipts for auto-approvals; the risk floor enforced and visible in the editor.
- **Proven by:** a step that auto-approves under policy and one that blocks, both with a record
  an auditor can read.
- **Risk:** low mechanically, **high in consequence** — this is where a mistake lets something
  ship unreviewed. Gets the most adversarial testing.

### Phase 6 — Custom steps — DONE
- Customer-attached skills and MCP tools, under provenance and the risk floor.
- `workflow_custom_steps` + palette + define form + runner dispatch + the permission grant.
- **Risk:** highest, as expected — it surfaced two live holes in the approval floor (see §3.7).

---

## 6. Open questions

1. **Scope of "customer".** Is a workflow owned by an **org** (shared across its projects) or by
   a project? The mapping table supports both; the default matters for how teams work.
2. **Who approves.** `approver_role` implies a role directory per customer. Is that the existing
   `roles` table, or a separate human-approver concept?
3. **Catalogue fit.** Do the twelve in §3.4 match how your customers actually deliver, and is
   anything missing that you have seen asked for?
4. **Failure policy.** When a step fails and its attempts are exhausted, should the default be
   pause-for-a-human, or compensate-and-roll-back? The runtime supports both.
5. **How much of the twelve to seed first.** All of them in Phase 2, or the three most common
   (#1, #2, #3) to get the shape right before writing the other nine?
6. **Unclassified skills.** Every skill registered in `boss-dogfood` today has no `risk_tier` in
   its config, so every custom step bound to one resolves to the floor and always stops for a
   person. That is the safe direction, but it means a customer's first custom step always gates.
   Classifying the shipped skills is the fix; guessing zero for them is not.
7. **Triggers.** Schedule, webhook and data-change starts. Every competitor has them, BOSS has
   none — a workflow can only be started by hand or by a mapping. This is the one genuine gap
   the competitor research turned up that is still open.
