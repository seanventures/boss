# Project agent templates (BOSS-435)

The existing 13 Agent Studio specialists remain. These 21 additions are available through **Add agent from template** in Team structure and the project-scoped `GET /v1/agent-studio/templates` API. Each template binds to an installed RoleSpec, creates an immutable AgentVersion through the existing validation/publish path, and starts in `deterministic_brief` mode. Installation creates no job, model call, device action, or provider effect.

The UI uses three separate views: **Agent work** groups recent activity by role; **Team structure** stores a planned role layout and separately shows observed handoffs; **Agent Studio** holds the definitions that can be tested and run. Placing a role on the chart does not change task routing. Installing a template does not place a role on the chart.

| Template | RoleSpec | Intended project work |
| --- | --- | --- |
| Mobile App Operator | software_engineer | Plan Android/iOS build, test, launch, interaction, screenshots, and logs; see [runner contract](../operations/MOBILE_AGENT_RUNNER.md) for execution gap. |
| Product Researcher | pm | Customer problems, alternatives, and opportunity evidence. |
| User Researcher | pm | Interviews, usability studies, consent, and attributed findings. |
| Technical Writer | docs_writer | Task guides, examples, and migration notes grounded in implementation. |
| Accessibility Auditor | design_critic | Keyboard, semantics, contrast, motion, and assistive-technology proof. |
| Performance Engineer | qa | Repeatable latency, throughput, memory, and load checks. |
| API Integration Engineer | integration_validator | Typed contracts, retries, idempotency, and failure tests. |
| Data Engineer | software_engineer | Ingestion, schema evolution, lineage, and quality checks. |
| Analytics Engineer | pm | Metric definitions with denominators, freshness, and uncertainty. |
| Privacy Engineer | security_reviewer | Data mapping, retention, deletion, and exposure review. |
| Compliance Analyst | security_reviewer | Control ownership, evidence, and gaps for human sign-off. |
| Incident Commander | coo | Incident timeline, owners, containment decisions, and recovery. |
| Site Reliability Engineer | infra_steward | Service objectives, signals, runbooks, and recovery drills. |
| Release Manager | release_manager | Release evidence, go/no-go, staged rollout, and rollback. |
| Developer Relations | docs_writer | Developer examples, questions, and verified product feedback. |
| Community Manager | program_manager | Community triage, responses, and private security escalation. |
| Customer Support | program_manager | Reproduction, impact, workarounds, and tracked fixes. |
| Sales Engineer | pm | Verified solution fit, demos, constraints, and open questions. |
| Growth Analyst | pm | Acquisition/activation funnels and measured experiments. |
| Localization Specialist | docs_writer | Locale context, string review, truncation, and accessibility. |
| Cost Analyst | finops_sentinel | Reconciled spend drivers, forecasts, and budget pressure. |

These are **templates**, not 21 running employees. Work begins only through a governed AgentRun or Workflow with current capability, policy, budget, lease, receipt, and approval checks. Installing a template does not create a reporting line; the Org Chart continues to distinguish declared seats from observed handoffs. The mobile device runner is being developed separately in BOSS-439; installing this template does not dispatch it. Agent Studio to runner dispatch, project build and test integration, and physical-device support remain tracked in BOSS-440.
