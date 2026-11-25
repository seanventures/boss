// @vitest-environment happy-dom
/**
 * Deploy history names who ran it, as words on the row, not only a tooltip on the face.
 * An agent run still shows that name. A row with no name invents none, and the face stays.
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

import { DeploymentsTab, historyWho } from "../app/projects/tab-deployments";
import type { PxCtx } from "../app/projects/project-page";
import { FX_DEPLOYMENTS } from "../lib/project-experience-fixtures";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ctx = {
  projectId: "helpdesk-ai-demo", actor: "ceo", orgId: "org_1", demo: false,
  overview: { project: { live_url: "" } }, work: null, workState: "ready",
  refreshWork: () => {}, onboarding: null, tab: () => {}, open: () => {}, ask: () => {},
} as unknown as PxCtx;

function engine(body: unknown) {
  return stubFetch((c: Call) => (c.path.endsWith("/deployments") ? { body } : { body: {} }));
}

async function open(history: Record<string, unknown>[]) {
  engine({ ...FX_DEPLOYMENTS, environments: [], history });
  await mountAs(<DeploymentsTab ctx={ctx} />, "owner");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

afterEach(() => { unmount(); });

describe("the deploy history shows who", () => {
  it('shows Ada when by is "Ada" and by_agent is false', () => {
    expect(historyWho({ by: "Ada", by_agent: false })).toBe("Ada");
  });

  it('still shows Ada when by is "Ada" and by_agent is true', () => {
    expect(historyWho({ by: "Ada", by_agent: true })).toBe("Ada");
  });

  it("adds no name when by is null or blank", () => {
    expect(historyWho({ by: null, by_agent: true })).toBe("");
    expect(historyWho({ by: "", by_agent: true })).toBe("");
    expect(historyWho({ by: "  ", by_agent: false })).toBe("");
  });

  it("renders that name as text on the row and leaves the face and the link", async () => {
    await open([
      { id: "ada-person", label: "Staging · person", sub: "3m", state: "done", at: "",
        href: "?area=infra&view=deploys&deploy=ada-person", by: "Ada", by_agent: false },
      { id: "ada-agent", label: "Staging · agent", sub: "3m", state: "done", at: "",
        href: "?area=infra&view=deploys&deploy=ada-agent", by: "Ada", by_agent: true },
      { id: "no-name", label: "Staging · unnamed", sub: "3m", state: "done", at: "",
        href: "?area=infra&view=deploys&deploy=no-name", by: null, by_agent: true },
    ]);
    const person = tid("px-dep-h-ada-person")!;
    const agent = tid("px-dep-h-ada-agent")!;
    const none = tid("px-dep-h-no-name")!;
    expect(person.textContent).toContain("Ada");
    expect(person.querySelector("[title='by Ada']")).not.toBeNull();
    expect(person.getAttribute("href")).toContain("deploy=ada-person");
    expect(agent.textContent).toContain("Ada");
    expect(agent.querySelector("[title='by Ada']")).toBeNull();
    expect(agent.querySelector("[data-role-key='sre']")).not.toBeNull();
    expect(agent.getAttribute("href")).toContain("deploy=ada-agent");
    const unnamed = (none.textContent ?? "").replace(/\s+/g, " ").trim();
    expect(unnamed).toContain("Staging · unnamed");
    expect(unnamed).not.toMatch(/Ada|Beacon/);
    expect(none.querySelector("[data-role-key='sre']")).not.toBeNull();
    expect(none.getAttribute("href")).toContain("deploy=no-name");
  });
});
