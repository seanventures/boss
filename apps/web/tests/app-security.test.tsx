// @vitest-environment happy-dom
/**
 * App › Security, the review checklist and the before/after report
 * (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.6), against a stubbed `/v1/security/*`:
 *   - the last scan reads as counts by tool and severity, with tool versions;
 *   - findings filter by tool, severity and status; a row opens its evidence and triage;
 *   - the checklist pre-ticks P0/P1, and "Fix selected" records the ticks THEN approves the gate;
 *   - the report says, in words, what was targeted and what became of it, and what is new;
 *   - ROLE GATING: a viewer sees every button but cannot scan, triage or choose (aria-disabled,
 *     the reason in the tooltip, a press sends nothing); an operator can.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppSecurity } from "../app/factory/security/app-security";
import { ReviewChecklist } from "../app/factory/security/review-checklist";
import { SecurityReport } from "../app/factory/security/security-report";
import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { needsWords } from "../lib/identity/words";
import { parseReport, parseReview, reportScans, stepWord, targetRows } from "../lib/security-review";
import { CONSENT_EVENT } from "../lib/identity/consent";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const FP = { sqli: "a".repeat(64), jwt: "b".repeat(64), lodash: "c".repeat(64), csp: "d".repeat(64), newx: "e".repeat(64) };
const tools = [
  { name: "semgrep", status: "done", version: "1.180.0", rules_digest: "sha256:1ebc" },
  { name: "osv", status: "done", version: "2.6.0" },
  { name: "zap", status: "done", version: "2.17.0" },
];
const SCAN = {
  id: "scan_before", app_key: "vulnapp", status: "done", commit_sha: "d1a1af20aaaa", started_at: "2026-10-08T10:00:00Z",
  ended_at: "2026-10-08T10:01:00Z", tools, target: { zap: { mode: "baseline" } },
  counts: { total: 4, by_severity: { critical: 0, high: 2, medium: 2, low: 0, info: 0 },
            by_tool: { semgrep: { critical: 0, high: 1, medium: 1, low: 0, info: 0 }, osv: { critical: 0, high: 1, medium: 0, low: 0, info: 0 },
                       zap: { critical: 0, high: 0, medium: 1, low: 0, info: 0 } } },
};
const AFTER = { ...SCAN, id: "scan_after", commit_sha: "cab05493bbbb" };
const finding = (id: string, fp: string, tool: string, title: string, severity: string, location: Record<string, unknown>) =>
  ({ id, fingerprint: fp, tool, rule_id: `rule.${id}`, title, severity, location, evidence: { snippet: "db.exec(`SELECT * FROM users WHERE name = '${name}'`)" }, status: "open" });
const FINDINGS = [
  finding("sf_sqli", FP.sqli, "semgrep", "SQL injection in findUser", "high", { file: "app.js", line: 12, function: "findUser" }),
  finding("sf_jwt", FP.jwt, "semgrep", "Hardcoded JWT secret", "medium", { file: "app.js", line: 30, function: "issueToken" }),
  finding("sf_lodash", FP.lodash, "osv", "Prototype pollution in lodash", "high", { package: "lodash", version: "4.17.20", lockfile: "package-lock.json" }),
  finding("sf_csp", FP.csp, "zap", "CSP header not set", "medium", { method: "GET", path: "/" }),
];
const REVIEW = {
  run_id: "wfr_1", status: "waiting", app_key: "vulnapp", created_at: "2026-10-08T10:00:00Z",
  steps: [{ id: "scan", status: "succeeded", scan_id: "scan_before", note: "4 findings" }, { id: "panel", status: "succeeded" },
          { id: "decide", status: "waiting", decision_id: "dec_1" }],
  awaiting_choice: true, decision_id: "dec_1", report_artifact_id: null, scan_id: "scan_before",
  recommendations: [
    { id: "srec_1", title: "Parameterise the user lookup", fix: "Use a prepared statement.", priority: "P0", effort: "S", fingerprints: [FP.sqli], source: "panel", selected: true, risk_if_unfixed: "Every account readable." },
    { id: "srec_2", title: "Upgrade lodash (1 advisory)", fix: "Upgrade lodash.", priority: "P1", effort: "S", fingerprints: [FP.lodash], source: "derived", selected: true },
    { id: "srec_3", title: "Move the JWT secret to the vault", fix: "Read it from the environment.", priority: "P2", effort: "M", fingerprints: [FP.jwt], source: "panel", selected: false },
    { id: "srec_4", title: "Set security headers", fix: "helmet()", priority: "P3", effort: "S", fingerprints: [FP.csp], source: "derived", selected: false },
  ],
  findings: Object.fromEntries(FINDINGS.map((f) => [f.fingerprint, { id: f.id, title: f.title, severity: f.severity, tool: f.tool, status: "open", where: "app.js" }])),
};
const REPORT = {
  app_key: "vulnapp", before: SCAN, after: AFTER, rules_differ: false, differences: [],
  severity_before: { critical: 0, high: 2, medium: 2, low: 0, info: 0 }, severity_after: { critical: 0, high: 0, medium: 2, low: 1, info: 0 },
  fixed: [{ fingerprint: FP.sqli, tool: "semgrep", title: "SQL injection in findUser", severity: "high", where: "app.js (findUser)" },
          { fingerprint: FP.lodash, tool: "osv", title: "Prototype pollution in lodash", severity: "high", where: "lodash@4.17.20" }],
  still_open: [{ fingerprint: FP.jwt, tool: "semgrep", title: "Hardcoded JWT secret", severity: "medium", where: "app.js (issueToken)" },
               { fingerprint: FP.csp, tool: "zap", title: "CSP header not set", severity: "medium", where: "GET /" }],
  moved: [], removed: [], not_rescanned: [],
  new: [{ fingerprint: FP.newx, tool: "zap", title: "Cookie without SameSite", severity: "low", where: "GET /token" }],
  summary: { fixed: 2, still_open: 2, moved: 0, removed: 0, not_rescanned: 0, new: 1, new_high_or_critical: 0 },
};

type Call = { method: string; path: string; body: unknown };
let calls: Call[] = [];
let reviewBody: unknown = REVIEW;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  reviewBody = REVIEW;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (path.startsWith("/v1/security/scans?")) return json({ scans: [SCAN] });
    if (path.startsWith("/v1/security/scans/scan_before/findings")) return json({ scan_id: "scan_before", status: "done", findings: FINDINGS });
    if (path.startsWith("/v1/security/reviews?")) return json({ reviews: [reviewBody] });
    if (path.startsWith("/v1/security/reviews/wfr_1/selection")) return json({ recommendations: [], accepted: [] });
    if (path.startsWith("/v1/security/reviews/wfr_1")) return json(reviewBody);
    if (path.startsWith("/v1/security/reports/vulnapp")) return json(REPORT);
    if (path.startsWith("/v1/decisions/dec_1/approve")) return json({ id: "dec_1", status: "approved" });
    if (path.startsWith("/v1/security/findings/")) return json({ ...FINDINGS[0], status: "accepted" });
    if (path === "/v1/security/scans" || path === "/v1/delivery-workflows/start") return json({ scan_id: "scan_new", run_id: "wfr_2" }, 202);
    return json({ error: "not_stubbed", path }, 404);
  }));
});

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; vi.unstubAllGlobals(); });

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}
const ME = {
  principal_id: "usr_x", kind: "user", display_name: "Vee", email: "v@acme.dev", org: { id: "org_1", name: "Acme" }, status: "active",
  access_roles: ["viewer"], sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
} as MeResponse;

async function mount(node: React.ReactElement, role: keyof typeof ROLE_PERMISSIONS) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={ME} org={perms(role)}>{node}</IdentityProvider>));
  await settle();
  return host;
}
async function settle() { for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
const $ = (sel: string) => host!.querySelector(sel) as HTMLElement;
const writes = () => calls.filter((c) => c.method !== "GET");

describe("App › Security", () => {
  it("shows the last scan by tool and severity, with versions", async () => {
    await mount(<AppSecurity projectId="p1" appKey="vulnapp" appName="Vuln app" />, "operator");
    const code = $('[data-testid="sec-tool-semgrep"]');
    expect(code.textContent).toContain("Code");
    expect(code.textContent).toContain("1.180.0");
    expect($('[data-testid="sec-tool-zap"]').textContent).toContain("Running app");
    expect($('[data-testid="sec-summary"]').textContent).toContain("d1a1af2");
  });

  it("filters findings and opens one to its evidence", async () => {
    await mount(<AppSecurity projectId="p1" appKey="vulnapp" />, "operator");
    expect(host!.querySelectorAll(".sec-finding").length).toBe(4);
    const tool = $('[data-testid="sec-filter-tool"]') as HTMLSelectElement;
    await act(async () => { tool.value = "osv"; tool.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(host!.querySelectorAll(".sec-finding").length).toBe(1);
    expect($('[data-testid="sec-finding-sf_lodash"]').textContent).toContain("lodash@4.17.20");
    await act(async () => { tool.value = ""; tool.dispatchEvent(new Event("change", { bubbles: true })); });
    const sev = $('[data-testid="sec-filter-severity"]') as HTMLSelectElement;
    await act(async () => { sev.value = "high"; sev.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(host!.querySelectorAll(".sec-finding").length).toBe(2);
    await act(async () => ($('[data-testid="sec-finding-sf_sqli"] button') as HTMLButtonElement).click());
    expect($('[data-testid="sec-finding-sf_sqli"] .sec-evidence').textContent).toContain("SELECT * FROM users");
  });

  it("an operator triages with a reason; without one nothing is sent", async () => {
    await mount(<AppSecurity projectId="p1" appKey="vulnapp" />, "operator");
    await act(async () => ($('[data-testid="sec-finding-sf_csp"] button') as HTMLButtonElement).click());
    await act(async () => $('[data-testid="sec-accept-sf_csp"]').click());
    expect(writes()).toEqual([]);
    const reason = $('[data-testid="sec-reason-sf_csp"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(reason, "static pages only");
      reason.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => $('[data-testid="sec-accept-sf_csp"]').click());
    await settle();
    expect(writes()[0]).toMatchObject({ method: "POST", path: "/v1/security/findings/sf_csp/triage",
                                        body: { status: "accepted", reason: "static pages only" } });
  });

  it("a viewer sees the buttons but cannot scan, start a review or triage", async () => {
    await mount(<AppSecurity projectId="p1" appKey="vulnapp" />, "viewer");
    for (const id of ["sec-scan-now", "sec-review-start"]) {
      const b = $(`[data-testid="${id}"]`);
      expect(b.getAttribute("aria-disabled")).toBe("true");
      expect(b.getAttribute("title")).toBe(needsWords("security_scan:run"));
      await act(async () => b.click());
    }
    await act(async () => ($('[data-testid="sec-finding-sf_csp"] button') as HTMLButtonElement).click());
    const accept = $('[data-testid="sec-accept-sf_csp"]');
    expect(accept.getAttribute("aria-disabled")).toBe("true");
    expect(accept.getAttribute("title")).toBe(needsWords("security_finding:triage"));
    expect(writes()).toEqual([]);
  });

  it("an operator starts a scan and a security review", async () => {
    await mount(<AppSecurity projectId="p1" appKey="vulnapp" />, "operator");
    reviewBody = { ...REVIEW, status: "completed", awaiting_choice: false };
    await act(async () => $('[data-testid="sec-scan-now"]').click());
    await settle();
    expect(writes()[0]).toMatchObject({ path: "/v1/security/scans", body: { project_id: "p1", app_key: "vulnapp" } });
  });

  it("a security review that needs consent opens the consent sheet, then starts once granted", async () => {
    const CONSENT = {
      error: "consent_required", scope: "workflow_run", task: "Security review", task_run_id: "pending:abc",
      agents: [{ id: "agt_sec", name: "Security reviewer", role_key: "security_reviewer" }],
      permissions: ["code_task:create"], projects: ["p1"], github: null,
      durations: [{ kind: "task", label: "For this run only", days: null }, { kind: "standing", label: "For 30 days", days: 30 }],
      max_days: 90,
    };
    let starts = 0;
    const inner = (globalThis.fetch as unknown as (i: RequestInfo | URL, n?: RequestInit) => Promise<Response>);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/v1/delivery-workflows/start")) {
        calls.push({ method: "POST", path: "/v1/delivery-workflows/start", body: JSON.parse(String(init?.body)) });
        starts += 1;
        return starts === 1 ? json(CONSENT, 409) : json({ run_id: "wfr_2" }, 202);
      }
      return inner(input, init);
    }));
    const asked: unknown[] = [];
    const onAsk = (e: Event) => {
      const d = (e as CustomEvent).detail as { onDecided?: (g: string | null) => void; agents: unknown };
      asked.push(d.agents);
      d.onDecided?.("dlg_1");
    };
    window.addEventListener(CONSENT_EVENT, onAsk);
    try {
      await mount(<AppSecurity projectId="p1" appKey="vulnapp" />, "operator");
      reviewBody = { ...REVIEW, status: "completed", awaiting_choice: false };
      await act(async () => $('[data-testid="sec-review-start"]').click());
      await settle();
    } finally {
      window.removeEventListener(CONSENT_EVENT, onAsk);
    }
    expect(asked).toEqual([[{ id: "agt_sec", name: "Security reviewer", role_key: "security_reviewer" }]]);
    expect(starts).toBe(2);
    expect(host!.textContent).not.toContain("consent_required");
  });

  it("shows the review's steps and, while it waits for a person, the checklist", async () => {
    await mount(<AppSecurity projectId="p1" appKey="vulnapp" />, "operator");
    expect($('[data-testid="sec-review-word"]').textContent).toBe("Needs your choice");
    expect($('[data-testid="sec-review"]').textContent).toContain("Choose");
    expect($('[data-testid="sec-checklist"]')).toBeTruthy();
  });
});

describe("The checklist", () => {
  it("pre-ticks P0 and P1; Fix selected records the ticks AND approves the gate in one server action", async () => {
    await mount(<ReviewChecklist projectId="p1" runId="wfr_1" review={parseReview(REVIEW)} />, "operator");
    const ticked = (id: string) => ($(`[data-testid="sec-tick-${id}"]`) as HTMLInputElement).checked;
    expect([ticked("srec_1"), ticked("srec_2"), ticked("srec_3"), ticked("srec_4")]).toEqual([true, true, false, false]);
    expect($('[data-testid="sec-rec-srec_2"]').textContent).toContain("Derived");
    await act(async () => $('[data-testid="sec-tick-srec_2"]').click());          // untick lodash
    await act(async () => $('[data-testid="sec-tick-srec_3"]').click());          // tick the JWT fix
    expect($('[data-testid="sec-fix-selected"]').textContent).toBe("Fix selected (2)");
    const reason = $('[data-testid="sec-accept-reason"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(reason, "internal tool");
      reason.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => $('[data-testid="sec-fix-selected"]').click());
    await settle();
    const all = writes();
    expect(all.length).toBe(1);                                   // ONE action: never half of one
    const [choose] = all;
    expect(choose.path).toBe("/v1/security/reviews/wfr_1/selection");
    expect((choose.body as { selected: string[] }).selected.sort()).toEqual(["srec_1", "srec_3"]);
    expect((choose.body as { accept: { finding_id: string }[] }).accept.map((a) => a.finding_id).sort()).toEqual(["sf_csp", "sf_lodash"]);
    expect((choose.body as { approve: boolean }).approve).toBe(true);
    expect($('[data-testid="sec-chosen"]')).toBeTruthy();
  });

  it("a viewer cannot choose: Fix selected is pre-greyed and sends nothing", async () => {
    await mount(<ReviewChecklist projectId="p1" runId="wfr_1" review={parseReview(REVIEW)} />, "viewer");
    const b = $('[data-testid="sec-fix-selected"]');
    expect(b.getAttribute("aria-disabled")).toBe("true");
    expect(b.getAttribute("title")).toBe(needsWords("security_finding:triage"));
    await act(async () => b.click());
    expect(writes()).toEqual([]);
  });
});

describe("The before/after report", () => {
  const done = parseReview({ ...REVIEW, status: "completed", awaiting_choice: false,
    steps: [...REVIEW.steps, { id: "rescan", status: "succeeded", scan_id: "scan_after" }, { id: "rereview", status: "succeeded", passed: true }] });

  it("says what was targeted and what became of it, what is new, and exports Markdown", async () => {
    await mount(<SecurityReport projectId="p1" appKey="vulnapp" review={done} />, "viewer");
    expect($('[data-testid="sec-report-shas"]').textContent).toBe("d1a1af2 → cab0549");
    const head = $('[data-testid="sec-report-headline"]').textContent ?? "";
    expect(head).toContain("Passed");
    expect(head).toContain("2 of 2 targeted fixed");
    expect($('[data-testid="sec-targets"]').textContent).toContain("SQL injection in findUser");
    expect($('[data-testid="sec-new"]').textContent).toContain("Cookie without SameSite");
    expect($('[data-testid="sec-bar-high"]').textContent).toContain("2 → 0");
    expect(($('[data-testid="sec-report-md"]') as HTMLAnchorElement).getAttribute("href"))
      .toBe("/v1/security/reports/vulnapp?project_id=p1&before=scan_before&after=scan_after&format=md");
  });

  it("the rules the screens share", () => {
    const rep = parseReport(REPORT);
    expect(targetRows(rep, [FP.sqli, FP.jwt, "f".repeat(64)]).map((t) => t.outcome)).toEqual(["Fixed", "Still open", "Not re-scanned"]);
    expect(reportScans(done)).toEqual({ before: "scan_before", after: "scan_after" });
    expect(stepWord({ id: "fix_2", status: "succeeded", note: "Round 1 passed its re-review, so there is nothing left to fix.", error: "", scanId: "", passed: null, codeTaskRunId: "", afterScanId: "" })).toBe("Not needed");
    expect(stepWord({ id: "rereview", status: "succeeded", note: "", error: "", scanId: "", passed: false, codeTaskRunId: "", afterScanId: "" })).toBe("Changes asked");
  });
});
