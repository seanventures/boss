// @vitest-environment happy-dom
/**
 * BOSSAPP-1012: one consent sheet for every review entry point.
 *
 * A direct change whose start answers 409 used to print the raw word "consent_required".
 * A pushed change whose review could not queue keeps that request, and opening the change
 * opens the same sheet. A benchmark (a BOSS-built change) does too.
 */
import { BossApiError } from "@boss/api-client";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsentHost } from "../components/identity/consent-sheet";
import { consentFromBody } from "../lib/identity/consent";
import { mountAs, settle, stubFetch, unmount } from "./_helpers/multirepo-fixtures";

vi.mock("next/cache", () => ({ revalidatePath: () => undefined }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(""),
  usePathname: () => "/",
  redirect: () => { throw new Error("redirect"); },
}));

const createCodeTask = vi.fn();
const startBenchmark = vi.fn();
vi.mock("../lib/client", () => ({
  client: {
    createCodeTask: (...args: unknown[]) => createCodeTask(...args),
    startQaBenchmarkApp: (...args: unknown[]) => startBenchmark(...args),
  },
  PROJECT_ID: "p1",
  resolveProjectId: (id?: string) => id || "p1",
  API_URL: "",
  authHeaders: () => ({}),
}));

const LOCAL = "local_prj_desk/desk";
const CONSENT = {
  error: "consent_required", task: "Review change #1: Add an SLA timer", task_run_id: "pending:Review1",
  agents: [{ id: "agt_forge", name: "Software Engineer", role_key: "forge" }],
  permissions: ["code_task:read", "code_task:steer", "repository:read"], projects: ["prj_desk"],
  github: null, local: { repo: LOCAL, repos: [LOCAL], summary: "on this machine" },
  durations: [{ kind: "task", label: "For this build only", days: null },
              { kind: "standing", label: "For 30 days", days: 30 }],
  max_days: 90,
};

const tid = (id: string) => document.querySelector(`[data-testid='${id}']`);
async function click(el: Element | null) {
  expect(el, "element to click").not.toBeNull();
  await React.act(async () => { (el as HTMLElement).click(); });
  await settle();
}
function setField(sel: string, value: string) {
  const el = document.querySelector(sel) as HTMLInputElement | HTMLTextAreaElement;
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

afterEach(() => { unmount(); createCodeTask.mockReset(); startBenchmark.mockReset(); });

describe("a direct change that needs consent opens the sheet", () => {
  it("does not print consent_required", async () => {
    createCodeTask.mockRejectedValue(new BossApiError("consent_required", 409, CONSENT));
    stubFetch(() => undefined);
    const { CodeTasksHome } = await import("../app/code-tasks-ui");
    await mountAs(<><ConsentHost /><CodeTasksHome projectId="p1" codeTasks={[]} apiReachable actor="ada" /></>, "owner");
    await click(tid("fx-new-build"));
    await click(document.querySelector("[data-testid=fx-new-direct]"));
    await React.act(async () => {
      setField("[data-testid=fx-direct-title]", "Fix the empty state");
      setField("[data-testid=fx-direct-what]", "Say what is empty");
    });
    await click(tid("fx-direct-start"));
    await settle();
    const sheet = tid("consent-sheet");
    expect(sheet).not.toBeNull();
    expect(sheet?.textContent).toContain("Review change #1: Add an SLA timer");
    expect(sheet?.textContent).toContain("on this machine");
    expect(document.body.textContent).not.toContain("consent_required");
    expect(consentFromBody(CONSENT)?.local?.repos).toEqual([LOCAL]);
  });
});

describe("a pushed change whose review needs consent opens the sheet", () => {
  it("opens it from the request kept on the change, before anyone hunts for a button", async () => {
    const calls = stubFetch(() => undefined);
    const { PushedReviewConsent } = await import("../app/changes/[projectId]/[number]/change-set-view");
    const { parseChangeSet } = await import("../lib/repo-links");
    const cs = parseChangeSet({
      number: 1, state: "open", source: "local", title: "Add an SLA timer", repository: LOCAL,
      branch: "fix/sla", base_branch: "main", labels: ["pushed"],
      review_consent: CONSENT,
    });
    expect(cs.review_consent?.task_run_id).toBe("pending:Review1");
    const reread = vi.fn(async () => cs);
    await mountAs(<><ConsentHost /><PushedReviewConsent cs={cs} at={{ projectId: "prj_desk", number: "1" }} reread={reread} /></>, "owner");
    await settle();
    expect(tid("consent-sheet")?.textContent).toContain("Add an SLA timer");
    expect(document.body.textContent).not.toContain("consent_required");
    expect(calls.filter((c) => c.method === "POST")).toEqual([]);
  });
});

describe("a BOSS-built change that needs consent opens the sheet", () => {
  it("a benchmark start returns the request instead of the raw word", async () => {
    startBenchmark.mockRejectedValue(new BossApiError("consent_required", 409, CONSENT));
    const { startQaBenchmarkAppAction } = await import("../app/actions");
    const data = new FormData();
    data.set("project_id", "p1");
    data.set("benchmark_id", "bugbox");
    data.set("actor", "ada");
    const out = await startQaBenchmarkAppAction(data);
    expect(out.ok).toBe(false);
    expect(out.error).toBe("Needs your consent");
    expect(consentFromBody(out.data?.consent)?.task).toBe(CONSENT.task);
    expect(out.error).not.toBe("consent_required");
  });
});
