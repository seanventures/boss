import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { DrawerHost, drawerP95SourceSentence } from "../app/projects/drawers";
import type { PxCtx } from "../app/projects/project-page";
import { mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** What production change this catches: the environment drawer hides where p95 came from, or invents a source. */
function deployments(p95_ms: number | null, p95_source?: string) {
  return {
    environments: [{
      key: "staging", label: "Staging", target_id: "dept_1", health: "healthy",
      url: "https://staging.example", p95_ms, uptime_pct: 99.9, series: [], policy: "auto",
      ...(p95_source === undefined ? {} : { p95_source }),
    }],
    history: [],
  };
}

const ctx = {
  projectId: "proj_1", actor: "ceo", orgId: null,
  overview: { project: { stage: "working" } },
  demo: false, work: null, workState: "ready", refreshWork: () => {}, onboarding: null,
  tab: () => {}, open: () => {}, ask: () => {},
} as unknown as PxCtx;

async function show(p95_ms: number | null, p95_source?: string) {
  unmount();
  stubFetch((c) => (c.path.endsWith("/deployments") ? { body: deployments(p95_ms, p95_source) } : { body: {} }));
  await mountAs(<DrawerHost ctx={ctx} drawer={{ kind: "env", id: "dept_1" }} onClose={() => {}} />);
  for (let i = 0; i < 8; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return tid("px-drawer-env-p95-source");
}

function deployDrawerBody(): string {
  const src = readFileSync(join(__dirname, "../app/projects/drawers.tsx"), "utf8");
  const start = src.indexOf("function DeployDrawer");
  expect(start).toBeGreaterThan(-1);
  const rest = src.slice(start + 1);
  const next = rest.search(/\nfunction |\nexport /);
  return next === -1 ? src.slice(start) : src.slice(start, start + 1 + next);
}

describe("the env drawer says where p95 came from", () => {
  afterEach(() => { unmount(); });

  it("names the app's requests and health probes, and nothing else", () => {
    expect(drawerP95SourceSentence("app")).toBe("From the app's requests.");
    expect(drawerP95SourceSentence("probe")).toBe("From health probes.");
    expect(drawerP95SourceSentence(null)).toBeNull();
    expect(drawerP95SourceSentence(undefined)).toBeNull();
    expect(drawerP95SourceSentence("")).toBeNull();
    expect(drawerP95SourceSentence("gateway")).toBeNull();
  });

  it("shows the sentence in the p95 row when the number is there", async () => {
    const app = await show(207, "app");
    expect(app).not.toBeNull();
    expect(app!.textContent).toBe("From the app's requests.");
    expect(app!.textContent).not.toContain("207");
    expect(app!.closest("dd")!.textContent).toContain("207ms");

    const probe = await show(180, "probe");
    expect(probe).not.toBeNull();
    expect(probe!.textContent).toBe("From health probes.");
    expect(probe!.textContent).not.toContain("180");
  });

  it("says nothing when the source is missing, anything else, or there is no p95", async () => {
    expect(await show(207)).toBeNull();
    expect(document.body.textContent ?? "").toContain("207ms");
    expect(document.body.textContent ?? "").not.toMatch(/From the app's requests|From health probes/);
    expect(await show(207, "gateway")).toBeNull();
    expect(await show(null, "app")).toBeNull();
    expect(await show(null, "probe")).toBeNull();
    expect(document.body.textContent ?? "").not.toMatch(/From the app's requests|From health probes/);
  });

  it("calls drawerP95SourceSentence inside DeployDrawer", () => {
    const body = deployDrawerBody();
    expect(body).toContain("drawerP95SourceSentence(");
    const at = body.indexOf("env.p95_ms");
    expect(at).toBeGreaterThan(-1);
    expect(body.slice(Math.max(0, at - 400), at + 400)).toContain('data-testid="px-drawer-env-p95-source"');
  });
});
