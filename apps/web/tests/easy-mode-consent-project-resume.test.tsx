// @vitest-environment happy-dom
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectsHome } from "../app/projects/projects-home";
import { OverviewTab } from "../app/projects/tab-overview";
import type { PxCtx } from "../app/projects/project-page";
import { FX_OVERVIEW, FX_SUMMARY } from "../lib/project-experience-fixtures";
import { parseOverview } from "../lib/project-experience";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const settle = async (n = 8) => {
  for (let i = 0; i < n; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
};
const press = async (el: Element | null) => {
  if (!el) throw new Error("nothing to press");
  await act(async () => {
    (el as HTMLElement).click();
  });
  await settle();
};

afterEach(() => unmount());

describe("Easy Mode project waiting for consent", () => {
  it("surfaces Waiting for your consent — Start on ProjectsHome and resumes via /factory/resume", async () => {
    const pid = "kept-easy-1";
    const summary = {
      ...FX_SUMMARY,
      items: [
        {
          ...(FX_SUMMARY as { items: Record<string, unknown>[] }).items[0],
          id: pid,
          name: "Kept Easy App",
          repo: "local_kept-easy-1/app",
          stage: "ready",
          status_label: "Waiting for your consent",
          onboarding_active: false,
          needs_you: 0,
          running: 0,
          start_onboarding: null,
          awaiting_consent: { href: `/v1/projects/${pid}/factory/resume`, method: "POST", body: {} },
        },
      ],
    };
    const calls = stubFetch((c: Call) => {
      if (c.method === "GET" && c.path === "/v1/projects/summary") return { body: summary };
      if (c.method === "POST" && c.path === `/v1/projects/${pid}/factory/resume`) {
        return { status: 201, body: { project_id: pid, run_id: "crun_1" } };
      }
      return undefined;
    });

    await mountAs(<ProjectsHome projectId={pid} actor="ceo" />);
    await settle();

    const btn = tid(`px-card-consent-start-${pid}`);
    expect(btn?.textContent).toContain("Waiting for your consent — Start");
    expect(tid(`px-card-onboard-${pid}`)).toBeNull();
    expect(tid(`px-card-${pid}`)?.textContent).toContain("Waiting for your consent");

    await press(btn);
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual([
      `/v1/projects/${pid}/factory/resume`,
    ]);
  });

  it("surfaces Waiting for your consent — Start on OverviewTab and refreshes overview/work", async () => {
    const pid = "kept-easy-2";
    const rawOverview = {
      ...FX_OVERVIEW,
      project: {
        ...FX_OVERVIEW.project,
        id: pid,
        name: "Kept Easy Overview",
        stage: "ready",
        status_label: "Waiting for your consent",
        awaiting_consent: { href: `/v1/projects/${pid}/factory/resume`, method: "POST", body: {} },
      },
    };
    const refreshOverview = vi.fn();
    const refreshWork = vi.fn();
    const ctx = {
      projectId: pid,
      actor: "ceo",
      overview: parseOverview(rawOverview),
      work: null,
      onboarding: null,
      open: vi.fn(),
      tab: vi.fn(),
      refreshWork,
      refreshOverview,
    } as unknown as PxCtx;

    const calls = stubFetch((c: Call) => {
      if (c.method === "GET") return { body: { seed: null } };
      if (c.method === "POST" && c.path === `/v1/projects/${pid}/factory/resume`) {
        return { status: 201, body: { project_id: pid, run_id: "crun_2" } };
      }
      return undefined;
    });

    await mountAs(<OverviewTab ctx={ctx} />);
    await settle();

    const btn = tid("px-ov-consent-start");
    expect(btn?.textContent).toContain("Waiting for your consent — Start");
    await press(btn);
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual([
      `/v1/projects/${pid}/factory/resume`,
    ]);
    expect(refreshOverview).toHaveBeenCalledTimes(1);
    expect(refreshWork).toHaveBeenCalledTimes(1);
  });
});
