// @vitest-environment happy-dom
/**
 * APPROVALS: THREE PLAIN LINES PER CARD (approvals wireframe B).
 *
 * The operator, on Approvals: "Do you think these things are really readable ... if not why do
 * we even display like this?" A merge card was an eyebrow ("Control plane proposes · merge"), a
 * commit sha, a build id and five P0–P3 boxes before its buttons; the page was headed by a
 * paragraph on deadlines and its tabs carried "+67" for other projects.
 *
 * Now: tabs Needs you · Expired (the paragraph is its tooltip) · Resolved; one "Also waiting"
 * line that opens a short list by project name; and each card says (1) what deciding does, (2)
 * one status line, (3) the answers, primary + one secondary, the rest behind "…", with the
 * Workflow / Change / PR / Meeting links on the right. Everything else is behind Checks /
 * Details, still in the page. Every answer still sends what it sent before: the tests below
 * build the form the browser would send for each button and run it through the real server
 * action against a mocked API client.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";
import type { ProjectCard, ProjectsSummary } from "../lib/project-experience";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
  redirect: vi.fn(),
}));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("next/headers", () => ({
  headers: async () => new Headers(),
  cookies: async () => ({ get: () => undefined }),
}));
vi.mock("../lib/client", () => ({
  PROJECT_ID: "northgate-gateway",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "northgate-gateway",
  API_URL: "http://127.0.0.1:8000",
  authHeaders: (extra?: Record<string, string>) => extra ?? {},
  client: {
    approveDecision: vi.fn(async () => ({})), rejectDecision: vi.fn(async () => ({})),
    reviseDecision: vi.fn(async () => ({})), delegateDecision: vi.fn(async () => ({})),
    settleDecision: vi.fn(async () => ({})), executeDecision: vi.fn(async () => ({})),
  },
}));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) =>
    React.createElement("button", { ...props, type: props.type ?? "submit" }, props.children),
}));
vi.mock("../app/client-relative-time", () => ({
  ClientRelativeTime: ({ iso }: { iso?: string }) => React.createElement("time", null, iso ?? ""),
}));
vi.mock("../lib/agents-live", () => ({ useAttention: () => ({ count: 0, setup: 0, items: [] }) }));

const project = (id: string, name: string, needs: number, archived = false) =>
  ({ id, name, needs_you: needs, archived } as unknown as ProjectCard);
const SUMMARY: ProjectsSummary = {
  week_cap_usd: null,
  items: [
    project("northgate-gateway", "Northgate gateway", 3),
    project("boss-dogfood", "BOSS dogfood", 14), project("helpdesk", "HelpDesk", 7),
    project("helpdesk-laptop", "HelpDesk Laptop", 7), project("recipe-box", "Recipe box", 6),
    project("hello-world", "Hello World", 5), project("lumen-pay", "Lumen Pay sandbox", 2),
    project("todo", "A tiny to-do", 1), project("old", "Old", 9, true),
  ],
};
vi.mock("../lib/project-experience", async (orig) => {
  const real = await orig<typeof import("../lib/project-experience")>();
  return { ...real, usePxRead: () => ({ state: "ready", data: SUMMARY }) };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DecisionInbox, DecisionCard, decisionCardLines, splitQuestion, heldWords } = await import("../app/decision-inbox-ui");
const { PACKET_READY } = await import("../lib/validation-review-fixtures");
const { client } = await import("../lib/client");
const { resolveDecisionAction } = await import("../app/actions");

const PID = "northgate-gateway";
const BASE = { riskTier: 2, options: [], costSummary: {}, createdAt: "2026-11-21T10:00:00Z", updatedAt: "2026-11-21T10:00:00Z" };
const MERGE_OFFERS = [
  { action: "approve", label: "Approve and merge", tone: "approve", effect: "Merges it.", needs: null },
  { action: "reject", label: "Keep blocked", tone: "reject", effect: "Keeps it open.", needs: null },
  { action: "revise", label: "Request changes", tone: "neutral", effect: "Sends it back.", needs: "note" },
  { action: "delegate", label: "Hand to someone else", tone: "neutral", effect: "Hands it on.", needs: "delegate_to" },
];

const READY = { ...BASE, id: "dec_ready", status: "pending",
  question: "Merge change #9?",
  recommendation: { action: "review_pull_request", pull_request_url: "http://boss-local/local_ng/gateway/pull/9",
                    workflow_run_id: "wfr_40229c5c963c4f599c51", packet: PACKET_READY },
  allowedActions: MERGE_OFFERS } as unknown as DecisionFeedItem;

const BLOCKED_PACKET = {
  ...PACKET_READY,
  work: { ...PACKET_READY.work, change: 11, run_no: 18 },
  verdict: { state: "blocked", reasons: ["open:V-03", "open:V-07"] },
  findings: PACKET_READY.findings.map((f) => (f.id === "V-03" || f.id === "V-07" ? { ...f, status: "open", override: null } : f)),
};
const BLOCKED = { ...BASE, id: "dec_blocked", status: "pending", question: "Merge change #11?",
  recommendation: { action: "review_pull_request", pull_request_url: "http://boss-local/local_ng/gateway/pull/11",
                    packet: BLOCKED_PACKET },
  allowedActions: MERGE_OFFERS } as unknown as DecisionFeedItem;

const DEPLOY = { ...BASE, id: "dec_deploy", status: "pending", question: "Deploy HelpDesk?",
  recommendation: { action: "deploy_host", app_name: "HelpDesk", host: "lab-host", environment: "staging",
                    workflow_run_id: "wfr_43", run_no: 43, commit_ref: "75949e3aa1b2c3",
                    pull_request_url: "https://github.com/sample-org/helpdesk/pull/5" },
  allowedActions: [
    { action: "approve", label: "Approve the deployment", tone: "approve", effect: "Ships it.", needs: null },
    { action: "reject", label: "Not now", tone: "reject", effect: "Holds it.", needs: null },
  ] } as unknown as DecisionFeedItem;

const SPEC = { ...BASE, id: "dec_spec", status: "pending", question: "Publish the PRD and start the build?",
  recommendation: { action: "execute_agent_tool", tool: "publish_document", start_code_task: true, workflow_run_id: "wfr_44", run_no: 44 },
  prd: { title: "Visual refresh", markdown: "# Visual refresh\n\n- R1 one\n- R2 two", fullPage: null, repo: null,
         meeting: { runId: "mtg_1", title: "Visual refresh", href: "/?area=meetings&meeting=mtg_1",
                    agenda: { brief: "Refresh the look", participants: [] },
                    positions: [{ seat: "pm", agent: "PM", position: "Ship it", points: [], debate: "", debatePoints: [] },
                                { seat: "design", agent: "Designer", position: "Ship it", points: [], debate: "", debatePoints: [] },
                                { seat: "eng", agent: "Engineer", position: "Ship it", points: [], debate: "", debatePoints: [] }],
                    dissent: [] } },
  allowedActions: [
    { action: "approve", label: "Approve and build", tone: "approve", effect: "Publishes it.", needs: null },
    { action: "revise", label: "Request changes", tone: "neutral", effect: "Sends it back.", needs: "note" },
    { action: "reject", label: "Reject", tone: "reject", effect: "Drops it.", needs: null },
  ] } as unknown as DecisionFeedItem;

const RESOLVED = { ...BASE, id: "dec_done", status: "approved", approvedBy: "ceo", approvedAt: "2026-11-20T09:00:00Z",
  question: "Merge change #7?", attention: "resolved",
  recommendation: { action: "review_pull_request", pull_request_url: "http://boss-local/local_ng/gateway/pull/7", title: "Tenant quota" },
  allowedActions: [] } as unknown as DecisionFeedItem;

const EXPIRED = { ...BASE, id: "dec_old", status: "expired", attention: "expired", deadlineAt: "2026-11-01T00:00:00Z",
  question: "Merge change #3?", recommendation: { action: "review_pull_request", pull_request_url: "http://boss-local/local_ng/gateway/pull/3" },
  allowedActions: MERGE_OFFERS.slice(0, 2) } as unknown as DecisionFeedItem;

let host: HTMLElement;
let root: Root;
beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  window.history.replaceState(null, "", "/?area=decisions");
  vi.clearAllMocks();
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function inbox(items: DecisionFeedItem[]) {
  act(() => root.render(
    <DecisionInbox feed={{ projectId: PID, apiReachable: true, items }}
                   tasks={[]} roles={[]} projectId={PID} actorId="ceo" orgId="org_1" />));
  return host;
}
function one(item: DecisionFeedItem) {
  act(() => root.render(<DecisionCard decision={item} projectId={PID} actorId="ceo" orgId="org_1" />));
  return host.querySelector('[data-testid="decision-row"]') as HTMLElement;
}
const q = (el: ParentNode, sel: string) => el.querySelector(sel) as HTMLElement | null;
const text = (el: ParentNode, sel: string) => q(el, sel)?.textContent ?? "";
const click = (el: HTMLElement | null) => { if (!el) throw new Error("nothing to click"); act(() => { el.click(); }); };

/** What the browser sends when `button` submits its form: the form's fields + the button's. */
function pressed(button: HTMLButtonElement): FormData {
  const fd = new FormData(button.form!);
  if (button.name) fd.append(button.name, button.value);
  return fd;
}

describe("the tabs and the line under them", () => {
  it("are Needs you · Expired · Resolved; no All, no +N, no paragraph (it is Expired's tooltip)", () => {
    const page = inbox([READY, EXPIRED, RESOLVED]);
    const tabs = [...page.querySelectorAll('[role="tab"]')].map((t) => t.textContent);
    expect(tabs).toEqual(["Needs you1", "Expired1", "Resolved1"]);
    expect(q(page, '[data-testid="decision-filter-all"]')).toBeNull();
    expect(q(page, '[data-testid="decision-filter-elsewhere"]')).toBeNull();
    expect(q(page, ".status-meta")).toBeNull();
    expect(q(page, '[data-testid="decision-filter-expired"]')!.getAttribute("title"))
      .toContain("can still be answered");
  });

  it("each tab shows only its own cards", () => {
    const page = inbox([READY, EXPIRED, RESOLVED]);
    const ids = () => [...page.querySelectorAll('[data-testid="decision-row"]')].map((r) => r.id);
    expect(ids()).toEqual(["decision-dec_ready"]);
    click(q(page, '[data-testid="decision-filter-expired"]'));
    expect(ids()).toEqual(["decision-dec_old"]);
    click(q(page, '[data-testid="decision-filter-resolved"]'));
    expect(ids()).toEqual(["decision-dec_done"]);
  });

  it("a link naming a decision opens the tab it is under", () => {
    window.history.replaceState(null, "", "/?area=decisions&decision=dec_done");
    const page = inbox([READY, RESOLVED]);
    expect(q(page, '[data-testid="decision-filter-resolved"]')!.getAttribute("aria-selected")).toBe("true");
    expect(q(page, "#decision-dec_done")!.getAttribute("data-highlighted")).toBe("true");
  });

  it("Also waiting: one line, then the projects by name, most waiting first, top five + more", () => {
    const page = inbox([READY]);
    const line = q(page, '[data-testid="decision-inbox-elsewhere"]')!;
    // The rail's number (approvalCountsOf): every other project's needs_you, archived ones out.
    expect(line.textContent).toBe("Also waiting: 42 in 7 other projects ›");
    expect(q(page, '[data-testid="decision-also-list"]')).toBeNull();
    click(q(page, '[data-testid="decision-also-toggle"]'));
    const rows = () => [...page.querySelectorAll('[data-testid="decision-also-list"] li')].map((li) => li.textContent);
    expect(rows()).toEqual(["BOSS dogfood · 14", "HelpDesk · 7", "HelpDesk Laptop · 7", "Recipe box · 6", "Hello World · 5", "+ 2 more"]);
    expect(q(page, '[data-testid="decision-inbox-elsewhere-boss-dogfood"]')!.getAttribute("href"))
      .toBe("/?org=org_1&area=decisions&project_id=boss-dogfood&decision_filter=needs_action");
    click(q(page, '[data-testid="decision-also-more"]'));
    expect(rows().slice(-2)).toEqual(["Lumen Pay sandbox · 2", "A tiny to-do · 1"]);
    expect(rows().join(" ")).not.toContain("Old");
  });
});

describe("a card is three lines", () => {
  it("merge, ready: the action, a green status in counts, two answers and the links", () => {
    const row = one(READY);
    expect(row.getAttribute("data-variant")).toBe("merge_ready");
    expect(text(row, '[data-testid="decision-title"]')).toBe("Merge change #9 · Lumen Pay tenant onboarding");
    const status = q(row, '[data-testid="decision-status"]')!;
    expect(status.className).toBe("dq-status ok");
    expect(status.textContent).toBe("✓ Ready · 9 issues found, all fixed or waived · 1 follow-up for later · Checks ›");
    expect(q(status, '[data-testid="decision-status-more"]')!.getAttribute("href")).toBe("/changes/northgate-gateway/9?tab=checks");
    // Line 3: primary + one secondary drawn, the rest behind "…".
    const act3 = q(row, ".dq-card-act")!;
    const top = [...act3.querySelectorAll("form > button")].map((b) => b.textContent);
    expect(top).toEqual(["Approve and merge", "Keep blocked"]);
    const more = q(act3, '[data-testid="decision-more-actions"]')!;
    expect(more.tagName).toBe("DETAILS");
    expect(q(more, '[data-testid="decision-why"]')).not.toBeNull();
    expect(q(more, '[data-testid="decision-revise"]')).not.toBeNull();
    expect(q(more, '[data-testid="decision-delegate"]')).not.toBeNull();
    // Links: the run by its number, the change.
    expect(text(row, '[data-testid="decision-link-run"]')).toBe("Workflow #16");
    expect(q(row, '[data-testid="decision-link-run"]')!.getAttribute("href"))
      .toBe("/?project_id=northgate-gateway&area=delivery_workflows&wfr=wfr_40229c5c963c4f599c51");
    expect(text(row, '[data-testid="decision-link-change"]')).toBe("Change #9");
    expect(q(row, '[data-testid="decision-link-change"]')!.getAttribute("href")).toBe("/changes/northgate-gateway/9");
  });

  it("the eyebrow, sha, build id and P0–P3 boxes are gone from the face, kept behind Details", () => {
    const row = one(READY);
    expect(row.textContent).not.toContain("proposes");
    const details = q(row, '[data-testid="decision-details"]')!;
    expect(details.hidden).toBe(true);
    // Everything outside Details: no sha, no build id, no severity boxes.
    const face = row.cloneNode(true) as HTMLElement;
    face.querySelector('[data-testid="decision-details"]')!.remove();
    expect(face.textContent).not.toMatch(/9f2c1d0|crun_|P0|P1/);
    expect(q(details, '[data-testid="vr-sevbar"]')).not.toBeNull();
    expect(text(details, '[data-testid="vr-prov-head"]')).toBe("head 9f2c1d0");
    expect(q(details, '[data-testid="vr-prov-build"]')!.getAttribute("href")).toContain("build=crun_58aecc16a1b2");
    expect(q(details, '[data-testid="review-packet-title"]')).toBeNull();
    // The Details toggle opens it in place.
    const toggle = q(row, '[data-testid="decision-details-toggle"]')!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    click(toggle);
    expect(q(row, '[data-testid="decision-details"]')!.hidden).toBe(false);
    expect(q(row, '[data-testid="decision-details-toggle"]')!.getAttribute("aria-expanded")).toBe("true");
  });

  it("merge, blocked: red, the open must-fix issues in words, Keep blocked + Merge anyway…", () => {
    const row = one(BLOCKED);
    expect(row.getAttribute("data-variant")).toBe("merge_blocked");
    expect(row.className).toContain("dq-card-bad");
    const status = q(row, '[data-testid="decision-status"]')!;
    expect(status.className).toBe("dq-status bad");
    expect(status.textContent).toBe("2 must-fix issues open: no inlined secrets, no sensitive paths exposed · Checks ›");
    const top = [...row.querySelectorAll(".dq-card-act form > button")].map((b) => b.textContent);
    expect(top).toEqual(["Keep blocked"]);
    const anyway = q(row, '[data-testid="decision-reasoned-approve"]')!;
    expect(anyway.tagName).toBe("DETAILS");
    expect(text(anyway, "summary")).toBe("Merge anyway…");
    expect((q(anyway, 'input[name="force"]') as HTMLInputElement).value).toBe("true");
    expect((q(anyway, 'textarea[name="note"]') as HTMLTextAreaElement).placeholder).toBe("Why merge with these open? Recorded with the merge.");
    expect([...anyway.querySelectorAll('[data-testid="decision-blocked-rows"] li')].length).toBe(2);
  });

  it("deploy: where it goes in the title, the host and the cost note on line 2, Details", () => {
    const row = one(DEPLOY);
    expect(row.getAttribute("data-variant")).toBe("deploy");
    expect(text(row, '[data-testid="decision-title"]')).toBe("Deploy HelpDesk to staging");
    expect(text(row, '[data-testid="decision-status"]')).toBe("lab-host · your own host, no provider cost · Details ›");
    expect(text(row, '[data-testid="decision-link-run"]')).toBe("Workflow #43");
    expect(text(row, '[data-testid="decision-link-pr"]')).toBe("PR #5");
    const top = [...row.querySelectorAll(".dq-card-act form > button")].map((b) => b.textContent);
    expect(top).toEqual(["Approve the deployment", "Not now"]);
    // The commit is behind Details, with the full sentence on what approving does.
    const more = q(row, '[data-testid="decision-status-more"]')!;
    expect(more.tagName).toBe("BUTTON");
    click(more);
    expect(q(row, '[data-testid="decision-details"]')!.hidden).toBe(false);
    expect(text(row, '[data-testid="decision-details"]')).toContain("75949e3aa1b2");
  });

  it("spec: the panel in a phrase, Read spec opens the PRD, the Meeting link", () => {
    const row = one(SPEC);
    expect(row.getAttribute("data-variant")).toBe("spec");
    expect(text(row, '[data-testid="decision-title"]')).toBe("Approve the spec · Visual refresh");
    expect(text(row, '[data-testid="decision-status"]')).toBe("Panel agreed · 3 agents · build starts when you approve · Read spec ›");
    expect(q(row, '[data-testid="decision-link-meeting"]')!.getAttribute("href")).toBe("/?area=meetings&meeting=mtg_1");
    expect(text(row, '[data-testid="decision-link-run"]')).toBe("Workflow #44");
    click(q(row, '[data-testid="decision-status-more"]'));
    expect(q(row, '[data-testid="decision-details"]')!.hidden).toBe(false);
    expect(q(row, '[data-testid="decision-prd"]')).not.toBeNull();
    // A spec's secondary answer is Request changes (wireframe B); Reject is behind "…".
    const top = [...row.querySelectorAll(".dq-card-act form > button")].map((b) => b.textContent);
    expect(top).toEqual(["Approve and build"]);
    expect(text(row, '[data-testid="decision-revise-fold"] > summary')).toBe("Request changes…");
    expect(q(row, '[data-testid="decision-more-actions"] [data-testid="decision-reject"]')).not.toBeNull();
  });

  it("a deadline takes the time's place on line 1", () => {
    const row = one(EXPIRED);
    expect(text(row, '[data-testid="decision-deadline"]')).toMatch(/^Deadline passed /);
    expect(q(row, '[data-testid="decision-when"]')).toBeNull();
  });

  it("resolved: one line, who answered, no answers", () => {
    const row = one(RESOLVED);
    expect(row.className).toContain("dq-card-resolved");
    expect(text(row, '[data-testid="decision-title"]')).toBe("Merge change #7 · Tenant quota");
    expect(text(row, '[data-testid="decision-resolved-note"]')).toMatch(/^Approved by the owner at /);
    expect(q(row, '[data-testid="decision-approve"]')).toBeNull();
    expect(q(row, '[data-testid="decision-status"]')).toBeNull();
  });

  it("the cost note and the re-raises are one phrase each", () => {
    const lines = decisionCardLines({ ...DEPLOY, costSummary: { estimated_usd_micros: 1_250_000 },
      recommendation: { ...DEPLOY.recommendation, occurrences: 3 } } as DecisionFeedItem, PID, "org_1");
    expect(lines.phrases).toContain("est. $1.25");
    expect(lines.phrases).toContain("same cause re-raised 3×");
  });

  it("every other kind still has a plain title and a status line", () => {
    const of = (rec: Record<string, unknown>, extra: Partial<DecisionFeedItem> = {}) =>
      decisionCardLines({ ...BASE, id: "d", status: "pending", question: "Q?", recommendation: rec, ...extra } as DecisionFeedItem, PID);
    expect(of({ action: "review_plan_draft", title: "Search filters" }).title).toBe("Review the plan · Search filters");
    expect(of({ action: "resume_code_task", title: "Search filters" }).title).toBe("Resume the work · Search filters");
    expect(of({ action: "review_architecture", app_name: "HelpDesk" }).title).toBe("Approve the architecture · HelpDesk");
    expect(of({ action: "resume_workflow", workflow_run_id: "wfr_9", gate: { workflow_name: "AML Alert Investigation", subject: "ALERT-3" } }).title)
      .toBe("AML Alert Investigation, ALERT-3");
    expect(of({ capability: "preview", workflow_run_id: "wfr_9", step_label: "Preview" }, { question: "review required" }).title)
      .toBe("Approve the “Preview” step");
    const refusal = of({}, { kind: "refusal", guidance: { what_happened: "Model capacity ran out", what_to_do: "Add a model in Setup",
                                                         area: "setup", link_label: "Open Setup" } });
    expect(refusal.title).toBe("Model capacity ran out");
    expect(refusal.tone).toBe("bad");
    expect(refusal.links[0]).toMatchObject({ label: "Open Setup", testid: "decision-link-fix" });
    expect(of({ action: "grant_tool_access" }, { question: "Let the builder use the browser?" }).title).toBe("Let the builder use the browser?");
  });
});

describe("the words on real cards (review of PR #298)", () => {
  it("a question is titled at the colon that ends a name, never inside quotes or a name", () => {
    expect(splitQuestion("Background job · Docs update: No GitHub credentials for /: the App has none"))
      .toEqual({ title: "Background job · Docs update", rest: "No GitHub credentials for /: the App has none" });
    expect(splitQuestion("Workflow run · KAN-9 Northgate gateway + gwlint: automate tenant onboarding: BOSS couldn't save this").title)
      .toBe("Workflow run · KAN-9 Northgate gateway + gwlint: automate tenant onboarding");
    expect(splitQuestion("CI squad used its $1.50 on “Hot file without tests: internal/lint/rules.go” — give it $1 more?").rest).toBe("");
    expect(splitQuestion("Approve the plan: KAN-9 Northgate gateway")).toEqual({ title: "Approve the plan", rest: "KAN-9 Northgate gateway" });
  });

  it("a CI hold says it once, in words, and never beside CI passed or ✓ Ready", () => {
    const reason = "CI could not be read: HTTPStatusError: Client error '403 Forbidden' for url 'https://api.github.com/repos/x/y/commits/abc/status'";
    expect(heldWords(reason)).toBe("CI could not be read (403)");
    const held = { ...BASE, id: "dec_held", status: "pending", question: "Merge PR #246?",
      recommendation: { action: "review_pull_request", pull_request_url: "https://github.com/sample-org/orders/pull/246", validation_status: "passed" },
      held: { state: "ci_unreadable", label: "Checks not read", reason },
      allowedActions: MERGE_OFFERS.slice(0, 2) } as unknown as DecisionFeedItem;
    const lines = decisionCardLines(held, PID);
    expect(lines.phrases).toEqual(["CI could not be read (403)"]);
    expect(lines.variant).toBe("merge_waiting");
    const row = one(held);
    expect(q(row, '[data-testid="decision-held"]')).toBeNull();
    expect((q(row, '[data-testid="decision-approve"]') as HTMLButtonElement).title).toBe(reason);
    // A GitHub PR has no BOSS Checks page: line 2 opens Details and says so.
    expect(text(row, '[data-testid="decision-status-more"]')).toBe("Details ›");
  });

  it("follow-ups are the engine's count, and a panel with dissent never says agreed", () => {
    const fu = decisionCardLines({ ...READY, recommendation: { ...READY.recommendation,
      followups: { count: 2, p1: 0, queued: false, bar: "standard", items: [] } } } as DecisionFeedItem, PID);
    expect(fu.phrases).toContain("2 follow-ups for later");
    const prd = (SPEC as unknown as { prd: { meeting: Record<string, unknown> } }).prd;
    const dissent = decisionCardLines({ ...SPEC, prd: { ...prd, meeting: { ...prd.meeting,
      dissent: [{ role: "eng", position: "Not yet", reason: "scope" }] } } } as unknown as DecisionFeedItem, PID);
    expect(dissent.phrases[0]).toBe("Panel: 2 for, 1 dissented");
  });
});

describe("every answer sends what it sent before", () => {
  async function send(button: HTMLButtonElement) {
    return resolveDecisionAction(pressed(button));
  }
  const mocked = (fn: unknown) => fn as ReturnType<typeof vi.fn>;

  it("Approve and merge, Keep blocked (with Why no? words), Request changes, hand to someone else", async () => {
    const row = one(READY);
    await send(q(row, '[data-testid="decision-approve"]') as HTMLButtonElement);
    expect(mocked(client.approveDecision)).toHaveBeenCalledWith("dec_ready", expect.objectContaining({ actor: "ceo" }));
    expect(mocked(client.approveDecision).mock.calls[0][1].force).toBeUndefined();

    (q(row, '[data-testid="decision-reject-note"]') as HTMLTextAreaElement).value = "Wrong tenant";
    await send(q(row, '[data-testid="decision-reject"]') as HTMLButtonElement);
    expect(mocked(client.rejectDecision)).toHaveBeenCalledWith("dec_ready", expect.objectContaining({ note: "Wrong tenant" }));

    const notes = row.querySelectorAll('textarea[name="note"]');
    (notes[notes.length - 1] as HTMLTextAreaElement).value = "Cover the quota too";
    await send(q(row, '[data-testid="decision-revise"]') as HTMLButtonElement);
    expect(mocked(client.reviseDecision)).toHaveBeenCalledWith("dec_ready", expect.objectContaining({ note: "Cover the quota too" }));

    (q(row, '[data-testid="decision-delegate-target"]') as HTMLInputElement).value = "release_manager";
    await send(q(row, '[data-testid="decision-delegate"]') as HTMLButtonElement);
    expect(mocked(client.delegateDecision)).toHaveBeenCalledWith("dec_ready", expect.objectContaining({ delegate_to: "release_manager" }));
  });

  it("Merge anyway… sends the forced merge with the reason; without one it is refused", async () => {
    const row = one(BLOCKED);
    const merge = q(row, '[data-testid="decision-approve"]') as HTMLButtonElement;
    expect(merge.textContent).toBe("Merge anyway");
    await expect(send(merge)).resolves.toMatchObject({ ok: false });
    expect(mocked(client.approveDecision)).not.toHaveBeenCalled();
    (q(row, '[data-testid="decision-reasoned-approve"] textarea[name="note"]') as HTMLTextAreaElement).value = "Partner accepted the risk";
    await send(merge);
    expect(mocked(client.approveDecision)).toHaveBeenCalledWith("dec_blocked",
      expect.objectContaining({ force: true, note: "Partner accepted the risk" }));
    await send(q(row, '[data-testid="decision-reject"]') as HTMLButtonElement);
    expect(mocked(client.rejectDecision)).toHaveBeenCalledWith("dec_blocked", expect.any(Object));
  });

  it("Approve and build carries starts_work and the run, as before", async () => {
    const row = one(SPEC);
    const fd = pressed(q(row, '[data-testid="decision-approve"]') as HTMLButtonElement);
    expect(fd.get("starts_work")).toBe("1");
    expect(fd.get("workflow_run_id")).toBe("wfr_44");
    await expect(resolveDecisionAction(fd)).resolves.toMatchObject({ ok: true, href: expect.stringContaining("wfr_44") });
    expect(mocked(client.approveDecision)).toHaveBeenCalledWith("dec_spec", expect.any(Object));
  });
});
