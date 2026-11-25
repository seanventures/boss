// @vitest-environment happy-dom
/**
 * The deploy card says where p95 came from, in words next to the number.
 * The label's title can keep the long note. App and probe each have one sentence.
 * Anything else adds none, and a blank source invents none.
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

import { DeploymentsTab, envP95SourceSentence } from "../app/projects/tab-deployments";
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

function env(key: string, source: string | null, ms: number | null) {
  return { ...FX_DEPLOYMENTS.environments[0], key, label: key, p95_ms: ms, p95_source: source, window: "24 h", demo_logins: [] };
}

async function open(environments: Record<string, unknown>[]) {
  engine(environments);
  await mountAs(<DeploymentsTab ctx={ctx} />, "owner");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

afterEach(() => { unmount(); });

describe("the deploy card says where p95 came from", () => {
  it("says the app's requests when the source is the app", () => {
    expect(envP95SourceSentence("app")).toBe("From the app's requests.");
  });

  it("says health probes when the source is a probe", () => {
    expect(envP95SourceSentence("probe")).toBe("From health probes.");
  });

  it("says nothing when the source is missing or anything else", () => {
    expect(envP95SourceSentence(null)).toBeNull();
    expect(envP95SourceSentence(undefined)).toBeNull();
    expect(envP95SourceSentence("")).toBeNull();
    expect(envP95SourceSentence("gateway")).toBeNull();
  });

  it("shows that sentence in a small next to the p95 number, and shows nothing otherwise", async () => {
    await open([
      env("live", "app", 207),
      env("stage", "probe", 180),
      env("blank", null, 90),
      env("other", "gateway", 40),
    ]);
    const liveNum = tid("px-env-p95-live")!;
    const liveSrc = tid("px-env-p95-source-live")!;
    expect(liveNum.textContent).toBe("207ms");
    expect(liveSrc.tagName).toBe("SMALL");
    expect(liveSrc.textContent).toBe("From the app's requests.");
    expect(liveNum.nextElementSibling).toBe(liveSrc);
    expect(liveNum.previousElementSibling?.getAttribute("title")).toBe("p95 from the app's own requests, last 24 h");

    const stageNum = tid("px-env-p95-stage")!;
    const stageSrc = tid("px-env-p95-source-stage")!;
    expect(stageNum.textContent).toBe("180ms");
    expect(stageSrc.tagName).toBe("SMALL");
    expect(stageSrc.textContent).toBe("From health probes.");
    expect(stageNum.nextElementSibling).toBe(stageSrc);
    expect(stageNum.previousElementSibling?.getAttribute("title")).toBe("p95 of BOSS's health probes that answered, last 24 h — the same number as Observability");

    expect(tid("px-env-p95-source-blank")).toBeNull();
    expect(tid("px-env-p95-source-other")).toBeNull();
    expect(tid("px-env-p95-blank")!.textContent).toBe("90ms");
    expect(tid("px-env-p95-other")!.textContent).toBe("40ms");
    expect(tid("px-env-blank")!.textContent).not.toMatch(/From the app's requests|From health probes/);
    expect(tid("px-env-other")!.textContent).not.toMatch(/From the app's requests|From health probes/);
  });
});
