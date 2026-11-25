// @vitest-environment happy-dom
/**
 * The deploy card says when a host is shared, beside the "runs on {host}" link.
 * host_projects stays in the title only when it is greater than 1. The sentence
 * is four words, so it is a span; a longer sentence would be a paragraph.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../app/actions", () => ({
  promoteDeploymentAction: vi.fn(async () => ({ ok: true })),
}));

import { DeploymentsTab, hostShareSentence } from "../app/projects/tab-deployments";
import type { PxCtx } from "../app/projects/project-page";
import { FX_DEPLOYMENTS } from "../lib/project-experience-fixtures";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ctx = {
  projectId: "helpdesk-ai-demo", actor: "ceo", orgId: "org_1", demo: false,
  overview: { project: { live_url: "" } }, work: null, workState: "ready",
  refreshWork: () => {}, onboarding: null, tab: () => {}, open: () => {}, ask: () => {},
} as unknown as PxCtx;

function engine(environments: Record<string, unknown>[]) {
  return stubFetch((c: Call) => (c.path.endsWith("/deployments") ? { body: { ...FX_DEPLOYMENTS, environments, history: [] } } : { body: {} }));
}

function env(key: string, host: string, projects: number | null) {
  return {
    ...FX_DEPLOYMENTS.environments[0],
    key, label: key, host, host_name: host, host_projects: projects, demo_logins: [],
  };
}

async function open(environments: Record<string, unknown>[]) {
  engine(environments);
  await mountAs(<DeploymentsTab ctx={ctx} />, "owner");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

afterEach(() => { unmount(); });

describe("the deploy card says when a host is shared", () => {
  it("says how many projects share the host when more than one does", () => {
    expect(hostShareSentence(3)).toBe("Shared by 3 projects.");
    expect(hostShareSentence(2)).toBe("Shared by 2 projects.");
  });

  it("says nothing when one project, none, or the count is missing", () => {
    expect(hostShareSentence(1)).toBeNull();
    expect(hostShareSentence(0)).toBeNull();
    expect(hostShareSentence(-1)).toBeNull();
    expect(hostShareSentence(null)).toBeNull();
    expect(hostShareSentence(undefined)).toBeNull();
  });

  it("shows that sentence in a span beside the runs-on link, and shows nothing otherwise", async () => {
    await open([
      env("shared", "Staging server", 3),
      env("pair", "Pair server", 2),
      env("solo", "Solo server", 1),
      env("blank", "Blank server", null),
    ]);

    const shared = tid("px-env-host-shared")!;
    const sharedBy = shared.nextElementSibling;
    expect(shared.textContent).toContain("runs on Staging server");
    expect(shared.textContent).not.toContain("Shared by");
    expect(shared.getAttribute("title")).toContain("shared by 3 projects");
    expect(sharedBy?.textContent).toBe("Shared by 3 projects.");
    expect(sharedBy?.tagName).toBe("SPAN");
    expect(sharedBy?.querySelector("p")).toBeNull();

    const pair = tid("px-env-host-pair")!;
    expect(pair.nextElementSibling?.tagName).toBe("SPAN");
    expect(pair.nextElementSibling?.textContent).toBe("Shared by 2 projects.");
    expect(pair.getAttribute("title")).toContain("shared by 2 projects");

    const solo = tid("px-env-host-solo")!;
    expect(solo.textContent).toContain("runs on Solo server");
    expect(solo.nextElementSibling?.textContent ?? "").not.toMatch(/Shared by/);
    expect(solo.getAttribute("title")).not.toContain("shared by");
    expect(tid("px-env-solo")!.textContent).not.toMatch(/Shared by/);

    const blank = tid("px-env-host-blank")!;
    expect(blank.textContent).toContain("runs on Blank server");
    expect(blank.nextElementSibling?.textContent ?? "").not.toMatch(/Shared by/);
    expect(blank.getAttribute("title")).not.toContain("shared by");
    expect(tid("px-env-blank")!.textContent).not.toMatch(/Shared by/);
  });
});
