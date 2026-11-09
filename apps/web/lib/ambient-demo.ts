/**
 * SAMPLE DATA for Agents at work (`?aa_demo=1`, the tests and the wireframe export).
 *
 * Every body below is in the API's own wire shape (AUTONOMOUS_AGENTS_SPEC.md §7, §7.1 — what
 * services/ambient/read_model.py returns), so the screens drawn from it are the screens the real
 * API draws. The lead item is the design's Order API incident (§11.0): alert → Chief of Staff →
 * SRE → investigator and engineer → squad vote (2 agree · 1 dissent) → staging rollback →
 * recovered in 4m → postmortem.
 */
import { LEVEL_WORDS } from "./autonomy-levels";

type Wire = Record<string, unknown>;
type N = { id: string; type: string; label: string; stage: number; badges?: string[]; ref?: Wire };
type E = { from: string; to: string; kind: string; label: string; dissent?: boolean; ref?: Wire };

const STAGE: Record<string, number> = { signal: 0, boss: 1, agent: 1, person: 1, policy: 1, squad: 1, action: 2, decision: 3, needs_you: 3, run: 4, outcome: 5 };
const T0 = "2026-11-05T09:12:00+00:00";
const at = (min: number) => new Date(Date.parse(T0) + min * 60_000).toISOString();

const node = (id: string, type: string, label: string, badges: string[] = [], ref?: Wire): N =>
  ({ id, type, label, stage: STAGE[type], badges, ref: ref ?? (id.startsWith("party:") ? { kind: "party", id: id.slice(6) } : undefined) });
const edge = (from: string, to: string, kind: string, label = kind, dissent = false, ref?: Wire): E => ({ from, to, kind, label, dissent, ref });
const party = (who: string, label: string, badges: string[] = []) => node(`party:${who}`, who.startsWith("person:") ? "person" : who.startsWith("policy:") ? "policy" : who.startsWith("squad:") ? "squad" : "agent", label, badges);

type Item = {
  id: string; title: string; lifecycle: string; task_class: string; problem_type: string; level: string; severity: string;
  outcome: string; cost: number; dissent?: boolean; overridden?: boolean; owner: string; resolved_by?: string | null;
  minutes: number; needs_you_decision_id?: string | null; nodes: N[]; edges: E[]; links?: Wire[];
};

const COS = "agent:chief_of_staff";

const ITEMS: Item[] = [
  {
    id: "work_order_api", title: "order-api 5xx at 6.6%", lifecycle: "resolved", task_class: "incident_staging_mitigation",
    problem_type: "Incident · staging", level: "A2", severity: "high", outcome: "resolved_by_agents", cost: 1.84, dissent: true,
    owner: "agent:sre", resolved_by: "agent:sre", minutes: 4.2,
    nodes: [
      node("signal:sig_5xx", "signal", "5xx 6.6% order-api", ["high", "×3"], { kind: "signal", id: "sig_5xx" }),
      party(COS, "Chief of Staff"),
      party("agent:sre", "SRE lead", ["1 finding"]),
      party("agent:investigator", "Investigator", ["2 findings"]),
      party("agent:app_engineer", "App engineer", ["1 finding"]),
      party("agent:adversarial_reviewer", "Adversarial reviewer"),
      node("proposal:wim_rb", "action", "Roll back staging", ["medium"], { kind: "message", id: "wim_rb" }),
      node("decision:act_rb", "decision", "Rollback to 6bf2", ["A2", "squad", "dissent", "succeeded"], { kind: "act", id: "act_rb" }),
      node("workflow_run:wfr_rb", "run", "Rollback run", [], { kind: "workflow_run", id: "wfr_rb" }),
      node("outcome", "outcome", "Recovered 4m", ["resolved"], { kind: "work_item", id: "work_order_api" }),
    ],
    edges: [
      edge("signal:sig_5xx", `party:${COS}`, "raised", "raised"),
      edge(`party:${COS}`, "party:agent:sre", "handoff", "routed", false, { kind: "message", id: "wim_route" }),
      edge("party:agent:sre", "party:agent:investigator", "handoff", "handed off", false, { kind: "message", id: "wim_h1" }),
      edge("party:agent:sre", "party:agent:app_engineer", "handoff", "handed off", false, { kind: "message", id: "wim_h2" }),
      edge("party:agent:investigator", "proposal:wim_rb", "proposed", "proposed"),
      edge("party:agent:app_engineer", "proposal:wim_rb", "vote", "approve", false, { kind: "message", id: "wim_v1" }),
      edge("party:agent:sre", "proposal:wim_rb", "vote", "approve", false, { kind: "message", id: "wim_v2" }),
      edge("party:agent:adversarial_reviewer", "proposal:wim_rb", "vote", "reject", true, { kind: "message", id: "wim_v3" }),
      edge("proposal:wim_rb", "decision:act_rb", "settles", "settles"),
      edge("decision:act_rb", "workflow_run:wfr_rb", "acted", "started"),
      edge("workflow_run:wfr_rb", "outcome", "resulted", "resulted"),
    ],
    links: [{ kind: "workflow_run", ref: "wfr_rb" }, { kind: "incident", ref: "inc_31" }],
  },
  {
    id: "work_payments", title: "payments p99 4.8s after release", lifecycle: "needs_you", task_class: "incident_response",
    problem_type: "Incident", level: "A1", severity: "high", outcome: "needs_you", cost: 0.92, owner: "agent:sre", minutes: 11,
    needs_you_decision_id: "dec_prod_rb",
    nodes: [
      node("signal:sig_p99", "signal", "p99 4.8s payments", ["high"], { kind: "signal", id: "sig_p99" }),
      party(COS, "Chief of Staff"),
      party("agent:sre", "SRE lead", ["2 findings"]),
      party("agent:investigator", "Investigator", ["1 finding"]),
      party("agent:security_reviewer", "Security reviewer"),
      node("proposal:wim_prb", "action", "Roll back prod", ["high"], { kind: "message", id: "wim_prb" }),
      node("decision:act_prb", "decision", "Recommend rollback", ["A1", "squad"], { kind: "act", id: "act_prb" }),
      node("needs_you", "needs_you", "Needs you", ["needs_you"], { kind: "decision", id: "dec_prod_rb" }),
    ],
    edges: [
      edge("signal:sig_p99", `party:${COS}`, "raised", "raised"),
      edge(`party:${COS}`, "party:agent:sre", "handoff", "routed", false, { kind: "message", id: "wim_route2" }),
      edge("party:agent:sre", "party:agent:investigator", "handoff", "handed off"),
      edge("party:agent:investigator", "proposal:wim_prb", "proposed", "proposed"),
      edge("party:agent:sre", "proposal:wim_prb", "vote", "approve"),
      edge("party:agent:security_reviewer", "proposal:wim_prb", "vote", "approve"),
      edge("proposal:wim_prb", "decision:act_prb", "settles", "settles"),
      edge("decision:act_prb", "needs_you", "asks", "asks"),
    ],
  },
  {
    id: "work_flaky", title: "checkout_spec flaky 3 of 20", lifecycle: "in_progress", task_class: "flaky_test",
    problem_type: "Flaky test", level: "A2", severity: "medium", outcome: "running", cost: 0.31, owner: "agent:qa", minutes: 6,
    nodes: [
      node("signal:sig_flaky", "signal", "checkout_spec flaky", ["medium", "×3"], { kind: "signal", id: "sig_flaky" }),
      party(COS, "Chief of Staff"),
      party("agent:qa", "QA", ["1 finding"]),
      node("proposal:wim_fix", "action", "Open draft PR", ["low"], { kind: "message", id: "wim_fix" }),
      node("decision:act_fix", "decision", "Fix the wait", ["A2", "agent"], { kind: "act", id: "act_fix" }),
      node("code_task:ctr_fix", "run", "Build", [], { kind: "code_task", id: "ctr_fix" }),
    ],
    edges: [
      edge("signal:sig_flaky", `party:${COS}`, "raised", "raised"),
      edge(`party:${COS}`, "party:agent:qa", "claimed", "routed"),
      edge("party:agent:qa", "proposal:wim_fix", "proposed", "proposed"),
      edge("proposal:wim_fix", "decision:act_fix", "settles", "settles"),
      edge("decision:act_fix", "code_task:ctr_fix", "acted", "started"),
    ],
  },
  {
    id: "work_ci", title: "CI red on acme/shop#41", lifecycle: "resolved", task_class: "ci_fix_boss_pr",
    problem_type: "CI fix", level: "A2", severity: "medium", outcome: "resolved_by_agents", cost: 0.64, owner: "agent:software_engineer",
    resolved_by: "agent:software_engineer", minutes: 18,
    nodes: [
      node("signal:sig_ci", "signal", "CI red #41", ["medium"], { kind: "signal", id: "sig_ci" }),
      party(COS, "Chief of Staff"),
      party("agent:software_engineer", "Software engineer", ["1 finding"]),
      party("agent:code_reviewer", "Code reviewer"),
      node("proposal:wim_ci", "action", "Open draft PR", ["low"], { kind: "message", id: "wim_ci" }),
      node("decision:act_ci", "decision", "Pin node 20", ["A2", "agent", "merged"], { kind: "act", id: "act_ci" }),
      node("pull_request:acme/shop#42", "run", "Pull request", [], { kind: "pull_request", id: "acme/shop#42" }),
      node("outcome", "outcome", "Resolved 18m", ["resolved"], { kind: "work_item", id: "work_ci" }),
    ],
    edges: [
      edge("signal:sig_ci", `party:${COS}`, "raised", "raised"),
      edge(`party:${COS}`, "party:agent:software_engineer", "claimed", "routed"),
      edge("party:agent:software_engineer", "proposal:wim_ci", "proposed", "proposed"),
      edge("party:agent:code_reviewer", "proposal:wim_ci", "critique", "agree"),
      edge("proposal:wim_ci", "decision:act_ci", "settles", "settles"),
      edge("decision:act_ci", "pull_request:acme/shop#42", "acted", "started"),
      edge("pull_request:acme/shop#42", "outcome", "resulted", "resulted"),
    ],
  },
  {
    id: "work_dep", title: "lodash 4.17.20 has a CVE", lifecycle: "resolved", task_class: "security_patch",
    problem_type: "Security patch", level: "A2", severity: "medium", outcome: "overridden", cost: 0.48, overridden: true,
    owner: "agent:security_reviewer", resolved_by: "person:param", minutes: 95,
    nodes: [
      node("signal:sig_cve", "signal", "CVE in lodash", ["medium"], { kind: "signal", id: "sig_cve" }),
      party(COS, "Chief of Staff"),
      party("agent:security_reviewer", "Security reviewer", ["1 finding"]),
      party("person:param", "Param"),
      node("decision:act_dep", "decision", "Bump to 4.17.21", ["A2", "agent", "overridden"], { kind: "act", id: "act_dep" }),
      node("decision:act_dep_o", "decision", "Replace lodash", ["person"], { kind: "act", id: "act_dep_o" }),
      node("outcome", "outcome", "Resolved 1h", ["resolved"], { kind: "work_item", id: "work_dep" }),
    ],
    edges: [
      edge("signal:sig_cve", `party:${COS}`, "raised", "raised"),
      edge(`party:${COS}`, "party:agent:security_reviewer", "claimed", "routed"),
      edge("party:agent:security_reviewer", "decision:act_dep", "decided", "decided"),
      edge("party:person:param", "decision:act_dep_o", "decided", "decided"),
      edge("decision:act_dep_o", "decision:act_dep", "overrides", "overrode", true),
      edge("decision:act_dep_o", "outcome", "resulted", "resulted"),
    ],
  },
  {
    id: "work_cost", title: "model spend 3× on review", lifecycle: "failed", task_class: "cost_anomaly",
    problem_type: "Cost anomaly", level: "A1", severity: "low", outcome: "failed", cost: 0.12, owner: "agent:finops_sentinel", minutes: 40,
    nodes: [
      node("signal:sig_cost", "signal", "Spend 3× review", ["low"], { kind: "signal", id: "sig_cost" }),
      party(COS, "Chief of Staff"),
      party("agent:finops_sentinel", "Finops sentinel"),
      node("proposal:wim_cost", "action", "Propose", ["low"], { kind: "message", id: "wim_cost" }),
      node("outcome", "outcome", "Failed 40m", ["failed"], { kind: "work_item", id: "work_cost" }),
    ],
    edges: [
      edge("signal:sig_cost", `party:${COS}`, "raised", "raised"),
      edge(`party:${COS}`, "party:agent:finops_sentinel", "claimed", "routed"),
      edge("party:agent:finops_sentinel", "proposal:wim_cost", "proposed", "proposed"),
      edge("proposal:wim_cost", "outcome", "resulted", "resulted"),
    ],
  },
  {
    id: "work_docs", title: "README install steps drifted", lifecycle: "claimed", task_class: "doc_drift",
    problem_type: "Doc drift", level: "A1", severity: "low", outcome: "running", cost: 0.05, owner: "agent:docs_writer", minutes: 2,
    nodes: [
      node("signal:sig_docs", "signal", "README drift", ["low"], { kind: "signal", id: "sig_docs" }),
      party(COS, "Chief of Staff"),
      party("agent:docs_writer", "Docs writer"),
    ],
    edges: [
      edge("signal:sig_docs", `party:${COS}`, "raised", "raised"),
      edge(`party:${COS}`, "party:agent:docs_writer", "claimed", "routed"),
    ],
  },
];

const TAB_OF: Record<string, string[]> = {
  needs_you: ["needs_you"], running: ["running"], resolved_by_agents: ["resolved_by_agents"],
  overridden: ["overridden"], failed: ["failed"], dismissed: ["dismissed"],
};

function summary(i: Item): Wire {
  return {
    id: i.id, project_id: "demo", title: i.title, lifecycle: i.lifecycle, task_class: i.task_class, autonomy_level: i.level,
    severity: i.severity, subject_ref: null, app: null, lease_owner: i.lifecycle === "resolved" || i.lifecycle === "failed" ? null : i.owner,
    lease_kind: "agent", needs_you_decision_id: i.needs_you_decision_id ?? null, outcome: null, resolved_by: i.resolved_by ?? null,
    created_at: at(-i.minutes), updated_at: at(0), resolved_at: ["resolved", "failed"].includes(i.lifecycle) ? at(0) : null,
  };
}
function badges(i: Item): Wire {
  return { problem_type: i.problem_type, task_class: i.task_class, outcome: i.outcome, needs_you: i.lifecycle === "needs_you",
           autonomy_level: i.level, severity: i.severity, cost_usd: i.cost, dissent: !!i.dissent, overridden: !!i.overridden };
}
function strip(i: Item): Wire[] {
  const pick: N[] = [];
  for (const kind of ["signal", "agent", "person", "decision", "needs_you", "outcome"]) {
    const found = i.nodes.filter((n) => n.type === kind);
    if (!found.length) continue;
    // The owner is the agent the thumbnail shows (the read model's _strip takes the last agent
    // node today; integration note for 906 — see the PR).
    const owner = kind === "agent" ? found.find((n) => n.id === `party:${i.owner}`) : undefined;
    const chosen = owner ?? (kind === "decision" || kind === "agent" ? found[found.length - 1] : found[0]);
    if (!pick.includes(chosen)) pick.push(chosen);
  }
  return pick.slice(0, 6).map((n) => ({ type: n.type, label: n.label, badges: (n.badges ?? []).slice(0, 2) }));
}

export function demoList(filter: string): Wire {
  const items = ITEMS.filter((i) => filter === "all" || (TAB_OF[filter] ?? []).includes(i.outcome));
  const counts: Record<string, number> = { all: ITEMS.length };
  for (const t of Object.keys(TAB_OF)) counts[t] = ITEMS.filter((i) => TAB_OF[t].includes(i.outcome)).length;
  return { filter, items: items.map((i) => ({ ...summary(i), badges: badges(i), strip: strip(i) })), counts, next_before: null };
}

export function demoGraph(id: string): Wire | undefined {
  const i = ITEMS.find((x) => x.id === id);
  if (!i) return undefined;
  return { item: summary(i), badges: badges(i), nodes: i.nodes.map((n) => ({ badges: [], ...n })), edges: i.edges.map((e) => ({ dissent: false, ...e })), links: i.links ?? [] };
}

const MESSAGES: Record<string, Wire[]> = {
  work_order_api: [
    { id: "wim_route", kind: "handoff", sender: COS, body: { to: "agent:sre", summary: "5xx on order-api since 09:08; owner SRE." } },
    { id: "wim_f1", kind: "finding", sender: "agent:investigator", body: { summary: "Errors start 2 min after release 6bf2672.", evidence: [{ kind: "commit", ref: "6bf2672" }, { kind: "query", ref: "rate(http_5xx[5m])" }] } },
    { id: "wim_f2", kind: "hypothesis", sender: "agent:investigator", body: { summary: "N+1 query in POST /orders with ≥15 items.", confidence: 0.86 } },
    { id: "wim_f3", kind: "finding", sender: "agent:app_engineer", body: { summary: "Confirmed: new ORM eager-load missing.", evidence: [{ kind: "log", ref: "order-api#L4412" }] } },
    { id: "wim_rb", kind: "proposal", sender: "agent:investigator", body: { action: "staging_rollback", summary: "Roll back staging to 6bf2672~1.", undo: "redeploy 6bf2672", risk: "medium" } },
    { id: "wim_v1", kind: "vote", sender: "agent:app_engineer", reply_to: "wim_rb", body: { vote: "approve" } },
    { id: "wim_v2", kind: "vote", sender: "agent:sre", reply_to: "wim_rb", body: { vote: "approve" } },
    { id: "wim_v3", kind: "vote", sender: "agent:adversarial_reviewer", reply_to: "wim_rb", body: { vote: "reject", reason: "A forward fix is one line; rollback drops two other fixes." } },
  ],
  work_payments: [
    { id: "wim_route2", kind: "handoff", sender: COS, body: { to: "agent:sre", summary: "payments p99 up 4× after release." } },
    { id: "wim_prb", kind: "proposal", sender: "agent:investigator", body: { action: "prod_rollback", summary: "Roll back production payments.", risk: "high" } },
  ],
};

const DECISIONS: Record<string, Wire> = {
  act_rb: {
    act_id: "act_rb", work_item_id: "work_order_api", question: "Roll back order-api staging to 6bf2672~1?", decided_by: "squad:work_order_api",
    decider_kind: "squad", chosen: "rollback", options: [{ key: "rollback", label: "Roll back" }, { key: "forward_fix", label: "Forward fix" }],
    rejected: [{ option: "forward_fix", why: "Slower to verify under load." }],
    rationale: "Errors began 2 minutes after 6bf2672. Rollback is reversible and staging policy allows it.",
    evidence: [{ kind: "commit", ref: "6bf2672" }, { kind: "query", ref: "rate(http_5xx[5m])" }, { kind: "incident", ref: "inc_31" }],
    confidence: 0.86, autonomy_level: "A2", task_class: "incident_staging_mitigation", action: "staging_rollback",
    reversibility: "reversible", undo: "redeploy 6bf2672",
    consensus: { mode: "squad_vote", result: "approved", participants: ["agent:sre", "agent:app_engineer", "agent:adversarial_reviewer"],
                 votes: [{ party: "agent:sre", vote: "approve" }, { party: "agent:app_engineer", vote: "approve" }, { party: "agent:adversarial_reviewer", vote: "reject" }],
                 dissent: [{ party: "agent:adversarial_reviewer", position: "reject", reason: "A forward fix is one line; rollback drops two other fixes." }] },
    cost_usd_micros: 1_840_000, outcome: "succeeded", overrides_act_id: null, created_at: at(-1),
  },
  act_prb: {
    act_id: "act_prb", work_item_id: "work_payments", question: "Roll back production payments to the last release?", decided_by: "squad:work_payments",
    decider_kind: "squad", chosen: "recommend_rollback", options: [{ key: "recommend_rollback", label: "Recommend rollback" }, { key: "wait", label: "Wait and watch" }],
    rejected: [{ option: "wait" }], rationale: "p99 is 4× baseline since the release. Production rollback always needs a person.",
    evidence: [{ kind: "alert", ref: "alr_p99" }], confidence: 0.78, autonomy_level: "A1", task_class: "incident_response", action: "prod_rollback",
    reversibility: "compensable", undo: null, consensus: { mode: "squad_vote_and_person", result: "approved", needs_person: true },
    cost_usd_micros: 920_000, outcome: null, overrides_act_id: null, created_at: at(-2),
  },
};

const SIGNALS: Wire[] = [
  { id: "sig_5xx", kind: "alert", title: "order-api 5xx at 6.6%", severity: "high", source: "alerts", occurrences: 3, last_seen_at: at(-5) },
  { id: "sig_p99", kind: "alert", title: "payments p99 4.8s", severity: "high", source: "alerts", occurrences: 1, last_seen_at: at(-11) },
];

const CLASSES: Array<[string, string, string, string, string, string]> = [
  ["ci_fix_boss_pr", "CI fix", "A2", "A3", "low", "software_engineer"], ["red_main", "Red main", "A1", "A2", "medium", "sre"],
  ["security_patch", "Security patch", "A2", "A3", "medium", "security_reviewer"], ["dependency_upgrade", "Dependency upgrade", "A1", "A3", "low", ""],
  ["flaky_test", "Flaky test", "A2", "A3", "low", "qa"], ["missing_tests", "Missing tests", "A1", "A2", "low", "qa"],
  ["doc_drift", "Doc drift", "A1", "A2", "low", "docs_writer"], ["todo_triage", "TODO triage", "A0", "A1", "low", "chief_of_staff"],
  ["incident_response", "Incident", "A1", "A3", "high", "sre"], ["incident_staging_mitigation", "Staging mitigation", "A2", "A3", "medium", "sre"],
  ["perf_regression", "Perf regression", "A1", "A2", "medium", "sre"], ["cost_anomaly", "Cost anomaly", "A1", "A3", "low", "finops_sentinel"],
  ["iac_drift", "IaC drift", "A1", "A1", "high", "infra_steward"],
];

function autonomy(): Wire {
  return {
    classes: CLASSES.map(([k, title, level, ceiling, risk, owner]) => ({
      task_class: k, title, level, ceiling, max_ceiling: ceiling, default_level: level, risk, owner_role: owner || null,
      level_name: LEVEL_WORDS[level],
      suggested_level: k === "ci_fix_boss_pr" ? "A3" : null, source: "default",
    })),
    levels: Object.entries(LEVEL_WORDS).map(([level, name]) => ({ level, name })),
  };
}

const ENVELOPE = { cap_usd_micros: 5_000_000, spent_usd_micros: 3_350_000, held_usd_micros: 400_000, available_usd_micros: 1_250_000, resets: "00:00 UTC" };
const NOT_STOPPED = { stopped: false, scope: null, by: null, at: null, reason: "" };

function scorecard(): Wire {
  const days = ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"];
  const mix = [[4, 6, 2, 0], [3, 7, 3, 0], [5, 5, 4, 1], [2, 8, 5, 1], [4, 6, 6, 1], [3, 7, 7, 2], [2, 6, 8, 2]];
  return {
    project_id: "demo", days: 7,
    decisions_per_day: days.map((day, i) => {
      const [a0, a1, a2, a3] = mix[i];
      const total = a0 + a1 + a2 + a3;
      return { day, a0, a1, a2, a3, agent: total - 3, policy: 1, squad: 1, person: 1, total };
    }),
    by_class: [
      { task_class: "ci_fix_boss_pr", decided: 24, accepted_pct: 91.7, overridden_pct: 4.2, reverted_pct: 0 },
      { task_class: "flaky_test", decided: 11, accepted_pct: 81.8, overridden_pct: 9.1, reverted_pct: 0 },
      { task_class: "incident_staging_mitigation", decided: 6, accepted_pct: 83.3, overridden_pct: 16.7, reverted_pct: 0 },
      { task_class: "security_patch", decided: 5, accepted_pct: 60, overridden_pct: 20, reverted_pct: 0 },
      { task_class: "doc_drift", decided: 8, accepted_pct: 62.5, overridden_pct: 0, reverted_pct: 0 },
    ],
    time_to_human_decision_minutes: { n: 9, median: 38, p90: 210 },
    mttr_minutes: { n: 31, median: 22, p90: 95 }, incident_mttr_minutes: { n: 6, median: 4.2, p90: 12 },
    items_ended: { resolved: 31, failed: 2, dismissed: 5 }, noise_rate: 0.13,
    cost: { spent_usd: 19.4, per_resolved_item_usd: 0.63, envelope_today: ENVELOPE },
    guards: { guard_refused: 3 }, stopped: NOT_STOPPED,
    outcomes: {
      days: 7, task_class: null, decided: 54, with_outcome: 47, pending: 7, accepted: 39, dismissed_or_ignored: 5, overridden: 4,
      reverted: 1, merged: 18, acceptance_pct: 83, noise_pct: 10.6, override_pct: 7.4, revert_pct: 5.3,
      decisions_by_level: { A0: 23, A1: 45, A2: 35, A3: 7 }, decisions_by_decider: { agent: 99, policy: 7, squad: 7, person: 7 },
      decisions_per_day: 15.7, time_to_human_decision_minutes: { n: 9, median: 38, p90: 210 },
      incidents: { n: 6, mttm_minutes: { n: 6, median: 3.1, p90: 8 }, mttr_minutes: { n: 6, median: 4.2, p90: 12 }, agent_resolved_pct: 66.7 },
      cost: { spent_usd: 19.4, per_resolved_item_usd: 0.63, per_merged_change_usd: 1.08 },
      ambient_share: { work_items: { boss: 38, all: 71, pct: 53.5 }, builds: { boss: 12, all: 30, pct: 40 } },
      by_task_class: [],
      recommendations: { shown: 12, accepted: 6, rejected: 3, ignored: 1, snoozed: 1, resolved_by_change: 1, recurred: 1,
                         acceptance_pct: 60, noise_pct: 40, time_to_decision_minutes: { n: 10, median: 95, p90: 600 },
                         cost_per_accepted_usd: 0.42, by_detector: [] },
    },
  };
}

type Rec = {
  id: string; title: string; why: string; kind: string; kindLabel: string; detector: string; detectorTitle: string;
  value: number; effort: number; risk: number; score: number; shape: string; squad: string[];
  sources: Array<[string, number]>; evidence: Wire[];
};
const REC_SOURCE: Record<string, string> = { incident: "Incidents", code: "Code graph", review: "Review findings", cost: "Cost" };
const RECS: Rec[] = [
  { id: "rec_orders_test", title: "Regression test + index for POST /orders ≥15 items", why: "Caused INC-31 and INC-44. 2nd-hottest file. No test covers ≥15 items.",
    kind: "hardening", kindLabel: "Hardening", detector: "incident_hotspot", detectorTitle: "Incident hotspot", value: 82, effort: 2, risk: 1.05, score: 2.41,
    shape: "ticket", squad: ["qa", "software_engineer"], sources: [["incident", 2], ["code", 1]],
    evidence: [{ kind: "incident", ref: "inc_31", note: "INC-31" }, { kind: "incident", ref: "inc_44", note: "INC-44" },
               { kind: "kg_node", ref: "kgn_orders", note: "orders.py hotspot" }] },
  { id: "rec_review_lint", title: "Lint rule for unawaited promises", why: "Reviewers flagged the same thing 14 times in 30 days.",
    kind: "process", kindLabel: "Process", detector: "review_repeat", detectorTitle: "Repeated review finding", value: 55, effort: 1, risk: 1.0, score: 1.72,
    shape: "ticket", squad: ["code_reviewer"], sources: [["review", 14]],
    evidence: [{ kind: "code_task", ref: "ct_r1", note: "14 review comments" }] },
  { id: "rec_alert_gap", title: "Alert rule for /refunds endpoint", why: "New endpoint has no alert. Customers noticed INC-40 first.",
    kind: "hardening", kindLabel: "Hardening", detector: "alert_gap", detectorTitle: "Alert gap", value: 74, effort: 2, risk: 1.08, score: 1.55,
    shape: "ticket", squad: ["sre"], sources: [["code", 1], ["incident", 1]],
    evidence: [{ kind: "kg_node", ref: "kgn_refunds", note: "/refunds route" }, { kind: "incident", ref: "inc_40", note: "INC-40" }] },
  { id: "rec_model_route", title: "Route doc drift to a smaller model", why: "Doc drift spends $0.40 per item on the largest model; outcomes match on a small one.",
    kind: "cost", kindLabel: "Cost", detector: "cost_outlier", detectorTitle: "Cost outlier", value: 48, effort: 4, risk: 1.2, score: 0.61,
    shape: "prd", squad: ["finops_sentinel"], sources: [["cost", 6]],
    evidence: [{ kind: "query", ref: "llm_calls by class", note: "Cost rows" }] },
];
function recSummary(r: Rec, rank: number): Wire {
  const strip = [
    ...r.sources.map(([src, k]) => ({ type: "signal", label: REC_SOURCE[src] ?? src, badges: [`×${k}`] })),
    { type: "agent", label: r.detectorTitle, badges: [] },
    { type: "action", label: `${r.kindLabel} ${r.title.split(" ").slice(0, 2).join(" ")}`, badges: [r.kindLabel, `score ${r.score.toFixed(1)}`] },
  ].slice(0, 6);
  return {
    work_item_id: r.id, id: r.id, project_id: "demo", title: r.title, kind: r.kind, detector: r.detector, detectors: [r.detector],
    subject: null, proposed_shape: r.shape, status: "open", score: r.score, rank, snoozed_until: null, accepted_as: null, artifact: null,
    reject_category: null, resurfaced_from: null, recurred_at: null, created_at: at(-600), decided_at: null, evidence_at: at(-60),
    badges: { kind: r.kindLabel, shape: r.shape, evidence: r.evidence.length, sources: r.sources.length, single_source: r.sources.length <= 1,
              freshness: 0.9, effort: r.effort, risk: r.risk, score: r.score, value: r.value, status: "open", resurfaced: false, detector: r.detectorTitle },
    strip,
  };
}
function roadmap(): Wire {
  return { status: "open", items: RECS.map((r, i) => recSummary(r, i + 1)),
           counts: { open: RECS.length, snoozed: 1, accepted: 3, rejected: 2, resolved_by_change: 1 }, detectors: [], last_run: null };
}
function recOne(id: string): Wire | undefined {
  const i = RECS.findIndex((r) => r.id === id);
  if (i < 0) return undefined;
  const r = RECS[i];
  return { ...recSummary(r, i + 1), nodes: [], edges: [],
           detail: { why: r.why, evidence: r.evidence, squad: r.squad, value: r.value, effort_units: r.effort, risk: r.risk } };
}

/** The demo's answer to a GET of `path`, or undefined (a route the sample does not have). */
export function demoAnswer(path: string): unknown {
  const u = new URL(path, "http://demo");
  const p = u.pathname;
  if (p === "/v1/ambient/work-items") return demoList(u.searchParams.get("filter") ?? "all");
  let m = p.match(/^\/v1\/ambient\/work-items\/([^/]+)\/messages$/);
  if (m) return { messages: (MESSAGES[decodeURIComponent(m[1])] ?? []).map((x, i) => ({ reply_to: null, created_at: at(-10 + i), ...x })) };
  m = p.match(/^\/v1\/ambient\/work-items\/([^/]+)$/);
  if (m) return demoGraph(decodeURIComponent(m[1]));
  m = p.match(/^\/v1\/ambient\/decisions\/([^/]+)$/);
  if (m) {
    const d = DECISIONS[decodeURIComponent(m[1])];
    return d ? { decision: d } : { decision: { act_id: decodeURIComponent(m[1]), work_item_id: "work_dep", question: "Which fix for the lodash CVE?",
      decided_by: "agent:security_reviewer", decider_kind: "agent", chosen: "Bump to 4.17.21", options: [], rejected: [],
      rationale: "Patch release fixes the CVE with no API change.", evidence: [], confidence: 0.9, autonomy_level: "A2",
      task_class: "security_patch", action: "open_draft_pr", reversibility: "reversible", undo: "revert PR", consensus: null,
      cost_usd_micros: 480_000, outcome: "overridden", overrides_act_id: null, created_at: at(-90) } };
  }
  if (p === "/v1/ambient/signals") return { signals: SIGNALS };
  if (p === "/v1/ambient/autonomy") return autonomy();
  if (p === "/v1/ambient/settings") return { settings: { daily_cap_usd_micros: 5_000_000, max_concurrent: 2 }, envelope: ENVELOPE, stop: NOT_STOPPED, mode: { mode: "route", source: "console", pinned: false, by: "person:vee", at: null } };
  if (p === "/v1/ambient/scorecard") return scorecard();
  if (p === "/v1/ambient/roadmap") return roadmap();
  m = p.match(/^\/v1\/ambient\/roadmap\/([^/]+)$/);
  if (m) return recOne(decodeURIComponent(m[1]));
  return undefined;
}

export function demoWrite(_path: string, _body: Record<string, unknown>) {
  return Promise.resolve({ ok: true, status: 200, body: { sample: true } as Record<string, unknown>, error: null });
}

export const DEMO_ITEM_IDS = ITEMS.map((i) => i.id);
