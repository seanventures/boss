// @vitest-environment happy-dom
/**
 * A run from another project is read from ITS project (walk of rc10.36, Workflows › Runs).
 *
 * The Runs list asks with scope=org, so it holds runs of every project in the org. The run that
 * opened by default belonged to another project, and each of its panels asked under the page's
 * project: /api/activity, /v1/decisions/<id>, /api/self-fix?view=run_autonomy,
 * /api/workflow-report and /api/run-acts?view=chain all answered 404, five console errors on
 * load. Every read about a run carries the run's own project.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { installMirror } from "../lib/identity/mirror";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { standingOf } from "./_helpers/run-standing";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));
const { DeliveryWorkflows, RunsView, projectForPayload, runProject } = await import("../app/delivery-workflows-ui");
const { forgetRunDefinitions } = await import("../app/workflows/flow/run-lane");

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
  installMirror(null);
  delete (window as { __bossReadGate?: true }).__bossReadGate;
});

function step(id: string, capability: string, status: string, extra: Record<string, unknown> = {}) {
  return { step_id: id, kind: "capability", status, capability, output: {}, evidence: [], error: null,
           approval: {}, due_at: null, timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
           decision_id: null, ...extra };
}
const RUN = {
  id: "wfr_other", project_id: "northgate", project_name: "Northgate Gateway",
  status: "waiting", standing: standingOf("waiting"), workflow_key: "onboard", workflow_name: "Onboard a config file",
  version: 1, step_count: 2, steps_done: 1, current_step: "merge", waiting_on: "You", blocked_reason: null, error: null,
  updated_at: null, actions: [], input: { title: "Tenant onboarding" }, finished: false,
  steps: [
    step("fix", "code", "succeeded"),
    step("merge", "merge", "waiting", { decision_id: "dec_1", approval: { required: true } }),
  ],
};

describe("a run of another project", () => {
  it("asks every panel under the run's project, never the page's", async () => {
    forgetRunDefinitions();
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = new URL(String(input), "http://console.test");
      asked.push(`${url.pathname}?${url.searchParams.toString()}`);
      return Response.json({});
    }));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const noop = () => {};
    await act(async () => root!.render(
      <RunsView runs={[RUN as never]} openRun="wfr_other" setOpenRun={noop} onAdvance={noop} busy={null}
                capabilities={[]} onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
                onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop} onPrepareTarget={noop}
                onRunPreview={noop} onBound={noop} projectId="boss-dogfood" actor="ceo" roles={[]} targets={[]} hosts={[]} />));
    for (let i = 0; i < 25; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });

    const aboutRun = asked.filter((u) => u.includes("wfr_other") || u.includes("dec_1"));
    expect(aboutRun.length).toBeGreaterThan(2);
    for (const u of aboutRun) {
      expect(u, u).not.toContain("project_id=boss-dogfood");
    }
    expect(aboutRun.some((u) => u.startsWith("/api/run-acts?") && u.includes("project_id=northgate"))).toBe(true);
  });

  it("falls back to the page's project for a run that names none", () => {
    expect(runProject({ project_id: "northgate" }, "p1")).toBe("northgate");
    expect(runProject({ project_id: null }, "p1")).toBe("p1");
    expect(runProject({}, "p1")).toBe("p1");
  });

});

/* ── Every press sends the run's project (review of #342) ── */

/** A run of `northgate`, stopped at a step whose blocker has cleared, offering Try again, Re-run
 *  and Stop and run again, listed on the boss-dogfood page (scope=org). */
const STOPPED = {
  id: "wfr_ng", project_id: "northgate", project_name: "Northgate Gateway",
  status: "deadlettered", workflow_key: "onboard", workflow_name: "Onboard a config file", version: 1,
  step_count: 1, steps_done: 0, current_step: "fix", waiting_on: null, blocked_reason: null, error: null,
  updated_at: null, input: { title: "Tenant onboarding" }, finished: false,
  actions: [
    { key: "retry_step", label: "Try again", enabled: true, reason: null, step_id: "fix" },
    { key: "rerun", label: "Run again", enabled: true, reason: null },
  ],
  steps: [{ step_id: "fix", kind: "capability", status: "deadlettered", capability: "code", output: {},
            evidence: [], error: "checker failed", approval: {}, due_at: null, timeout_applied: null,
            attempt: 1, started_at: null, completed_at: null, blocker: { cleared: true } }],
};
const VIEWS: Record<string, unknown> = {
  capabilities: { capabilities: [], blocks: [], manual_approval_risk_floor: 2 },
  catalogue: { items: [] }, installed: { items: [] },
  custom_steps: { steps: [], bindable: { skills: [], mcp_servers: [] } }, schedules: { schedules: [] },
};
type Sent = { method: string; url: string; body: Record<string, unknown> };
let sent: Sent[] = [];
function fakeApi(refuse = 0) {
  sent = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET") {
      const view = new URL(url, "http://console.test").searchParams.get("view") ?? "";
      if (view === "runs") return Response.json({ runs: [STOPPED] });
      return Response.json(VIEWS[view] ?? {});
    }
    sent.push({ method, url, body: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown> });
    return refuse ? Response.json({ error: "forbidden" }, { status: refuse }) : Response.json({ ok: true, requeued: [] });
  }));
}
async function settle() {
  for (let i = 0; i < 25; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mountPanel(wrap: (n: React.ReactElement) => React.ReactElement = (n) => n) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(wrap(<DeliveryWorkflows projectId="boss-dogfood" actor="ceo" />)));
  await settle();
}
const posts = (action: string) => sent.filter((s) => s.body.action === action);
async function press(testid: string) {
  const el = host!.querySelector<HTMLButtonElement>(`[data-testid="${testid}"]`);
  expect(el, testid).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
}

describe("every press about a run of another project goes to that project", () => {
  it("the automatic recheck, Try again and Re-run each send project_id=northgate", async () => {
    fakeApi();
    vi.stubGlobal("confirm", () => true);
    await mountPanel();
    expect(posts("recheck").map((p) => p.body.project_id)).toEqual(["northgate"]);
    await press("dw-run-stopped-retry");
    await press("dw-run-stopped-retry-confirm");
    expect(posts("retry_step").map((p) => [p.body.project_id, p.body.run_id])).toEqual([["northgate", "wfr_ng"]]);
    await press("dw-rerun");
    const starts = posts("start");
    expect(starts.map((p) => [p.body.project_id, p.body.rerun_of])).toEqual([["northgate", "wfr_ng"]]);
    expect(sent.every((s) => s.body.project_id !== "boss-dogfood")).toBe(true);
  });

  it("a refused background recheck (403) is not sent again on the next poll", async () => {
    fakeApi(403);
    await mountPanel();
    expect(posts("recheck").length).toBe(1);
    await act(async () => { window.dispatchEvent(new CustomEvent("boss:changed", { detail: { source: "workflows" } })); });
    await settle();
    expect(posts("recheck").length).toBe(1);
  });

  it("buttons are greyed on what the viewer may do in the RUN's project, and nothing is rechecked there", async () => {
    // Control in boss-dogfood (the page), but not org-wide, so not in northgate.
    const noControl: MePermissionsResponse = { epoch: 1, scope: "org", step_up_fresh_until: null, catalogue_fingerprint: "x",
      permissions: [...ROLE_PERMISSIONS.operator].filter((p) => p !== "workflow_run:control") };
    const page: MePermissionsResponse = { ...noControl, scope: "project:boss-dogfood", permissions: [...ROLE_PERMISSIONS.operator] };
    const ME = { principal_id: "usr_o", kind: "user", display_name: "Op", email: "o@example.test", org: { id: "org_1", name: "Acme" },
      status: "active", access_roles: ["operator"], sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
      mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false,
      banners: [], avatar_seed: null } as MeResponse;
    fakeApi();
    await mountPanel((n) => <IdentityProvider me={ME} org={noControl} project={page} projectId="boss-dogfood">{n}</IdentityProvider>);
    expect(posts("recheck")).toEqual([]);
    const retry = host!.querySelector<HTMLButtonElement>('[data-testid="dw-run-stopped-retry"]')!;
    expect(retry.getAttribute("data-can")).toBe("no");
  });
});

describe("projectForPayload", () => {
  const runs = [{ id: "wfr_ng", project_id: "northgate" }];
  it("finds the run by run_id, rerun_of, replaces_run or from_run", () => {
    for (const k of ["run_id", "rerun_of", "replaces_run", "from_run"]) {
      expect(projectForPayload({ [k]: "wfr_ng" }, runs, "p1"), k).toBe("northgate");
    }
    expect(projectForPayload({ run_id: "crun_x" }, runs, "p1")).toBe("p1");
    expect(projectForPayload({}, runs, "p1")).toBe("p1");
  });
});
