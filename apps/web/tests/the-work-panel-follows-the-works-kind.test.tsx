// @vitest-environment happy-dom
/**
 * THE WORK PANEL FOLLOWS WHAT THE WORK IS (operator, 2026-11-20: "why an investigation needs code
 * task pipeline?").
 *
 * northpeak-lending's "Investigate DQX-0588 v2 on COB 2026-10-05" (DQ RCA and Remediation, run
 * #36, Done) was drawn as Spec → Build → Review & tests → Merge → Staging → Production, Asked said
 * the workflow's name, Discussed "it went straight to the build", Built "Nothing built yet", an
 * empty preview card and "Pick a prod host". "Extract the rules of NorthPeak Bank" showed five
 * empty code-pipeline dots. Fixtures modelled on those rows as the engine now answers them
 * (work_outcome): the panel draws the workflow's own steps and what the run found or produced,
 * hides the code sections, and code work keeps what it had.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const RUN = "?project_id=northpeak-lending&area=delivery_workflows&wfr=wfr_50d5";
const agent = (name: string, role_key: string) => ({ name, role_key, avatar: { kit: "boss-v1", seed: role_key } });
const CAUSE = "F-01 extracted principal_outstanding before the LSP 'Update Loan Summary' COB job finished recomputing balances, because CHG-7731 removed the F-01 wait-for-completion dependency.";
const FIX = "Apply the RPAT-COB-ORDER durable fix: restore the F-01 job dependency so the F-01 extract waits for the LSP COB completion flag.";

const BASE = {
  state: "done", status: "completed", age: "18d", action_label: null, cost_usd: 0, list_usd: null, attempts: [], attempt_count: 0,
  followups: 0, retries: 0, agents: [], preview_app: null, created_at: "2026-11-02T05:22:25Z", updated_at: "2026-11-02T05:40:05Z",
  stages: [
    { key: "plan", label: "P", state: "skipped" }, { key: "design", label: "D", state: "done" }, { key: "build", label: "B", state: "done" },
    { key: "review", label: "R", state: "skipped" }, { key: "pr", label: "PR", state: "skipped" }, { key: "ship", label: "S", state: "skipped" },
  ],
};
const INVESTIGATION = {
  ...BASE, id: "wfr_50d5", label: "Investigate DQX-0588 v2 on COB 2026-10-05", title: "Investigate DQX-0588 v2 on COB 2026-10-05",
  kind: "incident", workflow: "DQ RCA and Remediation", run_number: 36, detail: "DQ RCA and Remediation", shape: "investigation",
  links: { workflow_run: RUN, build: null, log: null, pr: null, deploy: null, preview: null, decision: null, merge_pr: null, merged_pr: null },
  flow: [
    { key: "monitoring", label: "Sentinel", state: "done", agent: agent("Sentinel", "dq_monitoring_agent"), you: false, duration_s: 1,
      sub: "Opened INC-505 for DQX-0588 v2 on COB 2026-10-05", steps: ["Sentinel", "Context Hydration", "Past Incidents"] },
    { key: "rca", label: "Root Cause Analysis", state: "done", agent: agent("Tracer", "dq_rca_agent"), you: false, duration_s: 40, sub: "Candidate root causes for INC-505", steps: ["Root Cause Analysis"] },
    { key: "pipeline_orchestrator", label: "Swimlane", state: "done", agent: agent("Lane", "dq_lane_agent"), you: false, duration_s: 19, steps: ["Flow Orchestrator", "Swimlane", "Rule Proposal"] },
    { key: "remediation_advisor", label: "Corrective Action", state: "done", agent: agent("Mender", "dq_remediation_advisor"), you: false, duration_s: 22, steps: ["Corrective Action"] },
    { key: "human_approval", label: "Human approval", state: "done", agent: null, you: true, duration_s: null, steps: ["Human approval", "Human execution"],
      decided: "approved", decided_by: "Seed Owner", decided_at: "2026-11-02T05:27:30Z" },
    { key: "remediation_fix", label: "Retrospective", state: "done", agent: agent("Closer", "dq_incident_closure_agent"), you: false, duration_s: null, steps: ["Fix (design target)", "Retrospective"] },
  ],
  outcome: {
    shape: "investigation", run_href: RUN, links: [], produced: [], counts: [],
    asked: { text: "Investigate DQX-0588 v2 on COB 2026-10-05", trigger_step: "Sentinel",
             trigger: "Opened INC-505 for DQX-0588 v2 on COB 2026-10-05 (NPB-DQ-LN-003), severity Medium. Failure reason: Principal outstanding reconciles to its components failed for 412 of 61220.",
             facts: [{ label: "Exception group id", value: "EXG-20261005-0003" }] },
    found: { step: "Root Cause Analysis", text: CAUSE, short: "Source processing", confidence: 0.84, rationale: "The exact INC-2187/INC-2041 job-ordering pattern on the same rule and CDE.",
             refs: ["EXCEPTION", "CHG-7731", "INC-2187"], cited: 4, others: [{ text: "The EDW patch window made F-01 land late.", confidence: 0.18 }] },
    evidence: [{ step: "Context Hydration", text: "Context assembled for INC-505: 1 CDE, 3 owners." }, { step: "Past Incidents", text: "Matched 5 closed incident(s) for INC-505." }],
    remediation: [
      { step: "Corrective Action", person: false, text: FIX, state: "proposed", who: null, at: "2026-11-02T05:26:38Z" },
      { step: "Human approval", person: true, text: "Human approval: approved", state: "approved", who: "Seed Owner", at: "2026-11-02T05:27:30Z" },
      { step: "Human execution", person: true, text: "Human execution: approved", state: "approved", who: "Founder / CEO", at: "2026-11-02T05:39:34Z" },
      { step: "Retrospective", person: false, text: "Closed INC-505: the DQ Monitor's re-run of NPB-DQ-LN-003 grades Clean.", state: "done", who: null, at: "2026-11-02T05:40:05Z" },
    ],
    line: "Root cause: Source processing (0.84) · remediation approved",
  },
};
const JOB = {
  ...BASE, id: "wfr_f409", label: "Extract the rules of NorthPeak Bank", title: "Extract the rules of NorthPeak Bank: Loan Data Quality Rulebook v3.2",
  kind: "job", workflow: "Extract Rules from a Rulebook", run_number: 38, detail: "Extract Rules from a Rulebook", shape: "job",
  links: { workflow_run: "?project_id=northpeak-lending&area=delivery_workflows&wfr=wfr_f409", build: null, pr: null, deploy: null, preview: null, decision: null },
  flow: [
    { key: "extract", label: "Extract", state: "done", agent: agent("Rulebook Analyst", "rulebook_analyst"), you: false, duration_s: 815, steps: ["Extract"] },
    { key: "review", label: "Review", state: "done", agent: agent("Chief Architect", "chief_architect"), you: false, duration_s: null, steps: ["Review"] },
  ],
  outcome: {
    shape: "job", run_href: "", found: null, evidence: [], remediation: [],
    asked: { text: "Extract the rules of NorthPeak Bank: Loan Data Quality Rulebook v3.2", trigger: null, trigger_step: null, facts: [] },
    produced: [{ step: "Extract", text: "Read 136 of 136 sections of “NorthPeak Bank: Loan Data Quality Rulebook v3.2” and proposed 96 rule(s) with 190 threshold(s) for review." },
               { step: "Review", text: "Approved by auto approval" }],
    counts: [{ label: "Sections read", value: 136 }, { label: "Rules proposed", value: 96 }, { label: "Thresholds proposed", value: 190 }],
    links: [{ label: "Extract: review", href: "/?area=knowledge&kg_tab=review&kg_scan=rbx_ee62447aa442" }],
    line: "Read 136 of 136 sections of “NorthPeak Bank: Loan Data Quality Rulebook v3.2” and proposed 96 rule(s) with 190 threshold(s) for review.",
  },
};
// Code work as an older engine sends it: no shape, no flow — it keeps the code-delivery stations.
const CODE = {
  ...BASE, id: "wfr_c0de", label: "Dark mode", title: "Add a dark mode", kind: "feature", workflow: "Minor Feature", run_number: 4, state: "running",
  detail: "Add a dark mode", links: { workflow_run: "?wfr=wfr_c0de", build: "?build=crun_1", pr: null, deploy: null, preview: null, decision: null },
  stages: [{ key: "plan", label: "P", state: "done" }, { key: "design", label: "D", state: "done" }, { key: "build", label: "B", state: "now" },
           { key: "review", label: "R", state: "todo" }, { key: "pr", label: "PR", state: "todo" }, { key: "ship", label: "S", state: "todo" }],
};
const DEPLOYMENTS = { environments: [], production: { state: "needs_host", action: { label: "Pick host", href: "?drawer=needs:dec_prodhost" } } };

async function load(raw: Record<string, unknown>) {
  const px = await import("../lib/project-experience");
  return { w: px.parseWorkItem(raw), deployments: px.parseDeployments(DEPLOYMENTS) };
}
const ctx = { projectId: "northpeak-lending", actor: "ceo", overview: { captain: null, project: { name: "NorthPeak Lending", repos: [] } },
              refreshWork: () => {}, open: () => {}, ask: () => {} };

describe("the engine's answer is read whole", () => {
  it("shape, the workflow's own steps and the outcome", async () => {
    const { w } = await load(INVESTIGATION);
    expect(w.shape).toBe("investigation");
    expect(w.flow.map((f) => f.label)).toEqual(["Sentinel", "Root Cause Analysis", "Swimlane", "Corrective Action", "Human approval", "Retrospective"]);
    expect(w.flow[4]).toMatchObject({ you: true, decided: "approved", decided_by: "Seed Owner" });
    expect(w.outcome!.found!.confidence).toBe(0.84);
    expect(w.outcome!.remediation.map((r) => r.state)).toEqual(["proposed", "approved", "approved", "done"]);
    expect((await load(CODE)).w.shape).toBeUndefined();
  });
});

describe("who did what follows the work", () => {
  it("an investigation draws its workflow's steps, with agents and the person who decided", async () => {
    const { flowNodes } = await import("../lib/work-panel");
    const { w, deployments } = await load(INVESTIGATION);
    const nodes = flowNodes({ w, story: null, next: null, deployments, hhmm: () => "5:27 AM" });
    expect(nodes.map((n) => n.name)).toEqual(["Sentinel", "Root Cause Analysis", "Swimlane", "Corrective Action", "Human approval", "Retrospective"]);
    expect(nodes.map((n) => n.name)).not.toContain("Production");
    expect(nodes[1]).toMatchObject({ state: "done", sub: "40 s", who: "Tracer", you: false });
    expect(nodes[2].sub).toBe("19 s · 3 steps");
    expect(nodes[2].title).toContain("Flow Orchestrator → Swimlane → Rule Proposal");
    expect(nodes[4]).toMatchObject({ you: true, agent: null, sub: "Seed Owner approved 5:27 AM" });
    expect(nodes.every((n) => n.note === null)).toBe(true);
  });

  it("code work keeps Spec → … → Production", async () => {
    const { flowNodes } = await import("../lib/work-panel");
    const { w, deployments } = await load(CODE);
    expect(flowNodes({ w, story: null, next: null, deployments }).map((n) => n.name))
      .toEqual(["Spec", "Build", "Review & tests", "Merge", "Staging", "Production"]);
  });

  it("the header speaks for the kind", async () => {
    const { stateSentence, helmSuggestions } = await import("../lib/work-panel");
    const inv = (await load(INVESTIGATION)).w;
    expect(stateSentence(inv, null)).toEqual({ lead: "Done.", tone: "green", rest: "Root cause: Source processing (0.84) · remediation approved." });
    expect(helmSuggestions(inv)).toEqual(["What was the root cause?", "What evidence backs it?", "What was done about it?"]);
    const job = (await load(JOB)).w;
    expect(stateSentence(job, null).rest).toMatch(/^Read 136 of 136 sections .* for review\.$/);
    const running = (await load({ ...INVESTIGATION, state: "running", flow: INVESTIGATION.flow.map((f, i) => ({ ...f, state: i < 2 ? "done" : i === 2 ? "now" : "todo" })) })).w;
    expect(stateSentence(running, null)).toMatchObject({ lead: "BOSS is working.", rest: "On Swimlane." });
  });
});

describe("the panel draws only what the work produced", () => {
  it("an investigation: Asked the incident, Found the root cause, Remediation — never the code pipeline", async () => {
    const { WorkPanel } = await import("../app/projects/work-panel");
    const { w, deployments } = await load(INVESTIGATION);
    await mount(<WorkPanel ctx={ctx as never} w={w} story={null} deployments={deployments} testid="wp" />);
    const page = $('[data-testid="wp"]')!;
    expect($('[data-testid="wp-sentence"]')!.textContent).toBe("Done. Root cause: Source processing (0.84) · remediation approved.");
    expect($$('[data-testid="wp-flow"] .wp-nm').map((n) => n.textContent)).toEqual(["Sentinel", "Root Cause Analysis", "Swimlane", "Corrective Action", "Human approval", "Retrospective"]);
    // Asked: the incident and what triggered it — never the workflow's name.
    expect($('[data-testid="wp-asked"] .wp-quote')!.textContent).toBe("“Investigate DQX-0588 v2 on COB 2026-10-05”");
    expect($('[data-testid="wp-trigger"]')!.textContent).toContain("failed for 412 of 61220");
    expect($('[data-testid="wp-asked"]')!.textContent).toContain("EXG-20261005-0003");
    // Found: the cause, its confidence and evidence.
    expect($('[data-testid="wp-cause"]')!.textContent).toBe(CAUSE);
    expect($('[data-testid="wp-found"]')!.textContent).toContain("confidence 0.84");
    expect($('[data-testid="wp-refs"]')!.textContent).toContain("CHG-7731");
    expect($('[data-testid="wp-evidence"]')!.textContent).toContain("Past Incidents");
    // Remediation: proposed → approved (who) → done.
    const rem = $$('[data-testid="wp-rem"]');
    expect(rem.map((r) => r.getAttribute("data-s"))).toEqual(["live", "done", "done", "done"]);
    expect(rem[0].textContent).toContain("restore the F-01 job dependency");
    expect(rem[1].textContent).toContain("Seed Owner");
    // Outputs: the run, step by step.
    expect($('[data-testid="wp-out-run"]')!.getAttribute("href")).toContain("wfr=wfr_50d5");
    // Never the code pipeline's sections.
    for (const gone of ["wp-preview-card", "wp-where", "wp-comments", "wp-story", "wp-discussed", "wp-built", "wp-produced"]) {
      expect($(`[data-testid="${gone}"]`)).toBeNull();
    }
    for (const words of ["Pick a prod host", "Pick host", "Nothing built yet", "went straight to the build", "Production", "Preview of"]) {
      expect(page.textContent).not.toContain(words);
    }
  });

  it("a job: Asked → Produced with its counts and links; no empty Found or Remediation", async () => {
    const { WorkPanel } = await import("../app/projects/work-panel");
    const { w, deployments } = await load(JOB);
    await mount(<WorkPanel ctx={ctx as never} w={w} story={null} deployments={deployments} testid="wp" />);
    expect($$('[data-testid="wp-flow"] .wp-nm').map((n) => n.textContent)).toEqual(["Extract", "Review"]);
    expect(($('[data-testid="wp-flow"] .wp-lane') as HTMLElement).style.getPropertyValue("--wp-n")).toBe("2");
    expect($('[data-testid="wp-counts"]')!.textContent).toContain("Rules proposed96");
    expect($('[data-testid="wp-produced"]')!.textContent).toContain("Approved by auto approval");
    expect($('[data-testid="wp-out-link"]')!.getAttribute("href")).toContain("kg_scan=rbx_ee62447aa442");
    expect($('[data-testid="wp-found"]')).toBeNull();
    expect($('[data-testid="wp-remediation"]')).toBeNull();
    expect($('[data-testid="wp-preview-card"]')).toBeNull();
    expect($('[data-testid="wp"]')!.textContent).not.toContain("Pick a prod host");
  });

  it("code work keeps its preview, where it runs and the prod host", async () => {
    const { WorkPanel } = await import("../app/projects/work-panel");
    const { w, deployments } = await load(CODE);
    await mount(<WorkPanel ctx={ctx as never} w={w} story={null} deployments={deployments} testid="wp" />);
    expect($('[data-testid="wp-preview-card"]')).not.toBeNull();
    expect($('[data-testid="wp-where"]')).not.toBeNull();
    expect($('[data-testid="wp-story"]')).not.toBeNull();
    expect($('[data-testid="wp-outcome"]')).toBeNull();
  });
});

describe("the rows", () => {
  it("an investigation's row: its steps as dots and its one-line outcome", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const { w } = await load(INVESTIGATION);
    await mount(<WorkRow w={w} ctx={ctx as never} />);
    const dots = $$('[data-testid="px-work-flow-wfr_50d5"] i');
    expect(dots.length).toBe(6);
    expect(dots.every((d) => d.getAttribute("data-s") === "done")).toBe(true);
    expect($('[data-testid="px-work-flow-wfr_50d5"]')!.getAttribute("aria-label")).toContain("Root Cause Analysis done");
    expect($('[data-testid="px-work-line-wfr_50d5"]')!.textContent).toContain("Done. Root cause: Source processing (0.84) · remediation approved.");
  });

  it("a job's row: its own two steps, never five empty code dots", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const { w } = await load(JOB);
    await mount(<WorkRow w={w} ctx={ctx as never} />);
    expect($$('[data-testid="px-work-flow-wfr_f409"] i').length).toBe(2);
    expect($('[data-testid="px-work-line-wfr_f409"]')!.textContent).toContain("proposed 96 rule(s)");
  });

  it("the Work list has no letter legend", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const src = readFileSync(join(__dirname, "..", "app", "projects", "tab-work.tsx"), "utf8");
    // Rows and board cards draw dots with names on hover; the P · D · B · R · PR/CS · S key is gone.
    expect(src).not.toContain("px-legend");
    expect(src).not.toMatch(/\["P", "Plan"\]/);
    expect(src).not.toContain("<Stations");
  });
});
