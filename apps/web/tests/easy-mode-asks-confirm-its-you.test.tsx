// @vitest-environment happy-dom
/**
 * EASY MODE ASKS "CONFIRM IT'S YOU", THEN SENDS THE SAME LAUNCH AGAIN.
 *
 * Operator, laptop, 2026-11-18: Command Center › Recipe box › Easy Mode printed "The build did not
 * start." The API had answered 401 step_up_required (`enforce denied POST /v1/factory/launch:
 * step_up_required project:create`): the person's second factor was only stale. The launch was a
 * raw fetch, so it went around lib/api-write.ts, whose step-up opens the sheet and resends. Now:
 *   - the launch goes through apiWrite: the sheet is asked, and the same launch is sent again
 *     with a fresh Idempotency-Key (the server's own dedupe key makes a second send reuse the
 *     project, never make another);
 *   - cancelled, the one shared not-confirmed sentence is said, never the generic line or a code;
 *   - consent and step-up compose in either order;
 *   - a started build shows what happens next and links to where it is watched.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BuildHome, projectHref, watchBuildHref } from "../app/build-home";
import { ConsentHost } from "../components/identity/consent-sheet";
import { stepUpNotConfirmedSentence } from "../lib/api-write";
import { stepUpCode } from "../lib/api-error";
import { installMirror } from "../lib/identity/mirror";
import { mountAs, settle, tid, unmount } from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

afterEach(() => { unmount(); installMirror(null); vi.unstubAllGlobals(); });

const STEP_UP = { status: 401, body: { error: "step_up_required" } };
const STARTED = {
  status: 201,
  body: { project_id: "recipe-box-fd3ab4a3", run_id: "crun_6883ec7cbd67401a9ce8", spec: { title: "Recipe box" } },
};
const CONSENT = {
  status: 409,
  body: {
    error: "consent_required", task: "Recipe box", task_run_id: "pending:R1",
    agents: [{ id: "agt_forge01", name: "Software Engineer", role_key: "software_engineer" }],
    permissions: ["code_task:read"], projects: ["recipe-box-fd3ab4a3"], github: null,
    local: { repo: "_local/recipe", repos: ["_local/recipe"], summary: "on this machine" },
    durations: [{ kind: "task", label: "For this build only", days: null }],
    max_days: 90, project_id: "recipe-box-fd3ab4a3", dedupe_key: "factory.launch:r", created: true,
    retry: { project_id: "recipe-box-fd3ab4a3", dedupe_key: "factory.launch:r" },
  },
};

type Sent = { path: string; method: string; key: string; body: string };

function server(launches: Array<{ status: number; body: unknown }>): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    sent.push({ path: url.pathname, method, key: new Headers(init?.headers).get("Idempotency-Key") ?? "",
                body: typeof init?.body === "string" ? init.body : "" });
    const reply = url.pathname === "/api/factory/templates"
      ? { status: 200, body: { templates: [] } }
      : url.pathname === "/api/factory/launch"
        ? launches.shift() ?? { status: 500, body: { error: "too many launches" } }
        : method === "POST" && url.pathname === "/v1/identity/me/delegations"
          ? { status: 201, body: { id: "dlg_1" } }
          : { status: 404, body: { error: "not_found" } };
    return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { "Content-Type": "application/json" } });
  }));
  return sent;
}

/** The step-up sheet, as api-write's own tests stand it in: answers yes or no. */
function sheet(answer: boolean) {
  const stepUp = vi.fn(async () => answer);
  installMirror({ check: () => ({ allowed: true, reason: "", needsStepUp: false }) as never, stepUp, refresh: async () => {} });
  return stepUp;
}

async function press(id: string) {
  const el = tid(id);
  expect(el, id).not.toBeNull();
  await React.act(async () => { el!.click(); });
  for (let i = 0; i < 4; i += 1) await settle();
}

const launchesOf = (sent: Sent[]) => sent.filter((s) => s.path === "/api/factory/launch");

describe("Easy Mode under a stale second factor", () => {
  it("asks Confirm it's you, then sends the same launch again and starts", async () => {
    const sent = server([STEP_UP, STARTED]);
    await mountAs(<BuildHome />, "owner");
    const stepUp = sheet(true);
    await press("build-lucky");
    expect(stepUp).toHaveBeenCalledTimes(1);
    const launches = launchesOf(sent);
    expect(launches).toHaveLength(2);
    expect(launches[1].body).toBe(launches[0].body);           // the same launch
    expect(launches[0].key).not.toBe("");
    expect(launches[1].key).not.toBe(launches[0].key);         // the refused key would replay the refusal
    expect(tid("build-launch-error")).toBeNull();
    expect(tid("build-status")?.textContent).toContain("Build started");
    expect(document.body.textContent).not.toContain("The build did not start");
  });

  it("cancelled: says the one not-confirmed sentence, sends nothing more", async () => {
    const sent = server([STEP_UP]);
    await mountAs(<BuildHome />, "owner");
    sheet(false);
    await press("build-lucky");
    expect(launchesOf(sent)).toHaveLength(1);
    const said = tid("build-launch-error")?.textContent ?? "";
    expect(said).toBe(stepUpNotConfirmedSentence("Easy Mode"));
    expect(document.body.textContent).not.toContain("step_up_required");
    expect(said).not.toContain("The build did not start");
    expect(tid("build-launch")).toBeNull();
  });

  it("consent, then step-up: both are asked, and the third send starts", async () => {
    const sent = server([CONSENT, STEP_UP, STARTED]);
    await mountAs(<><BuildHome /><ConsentHost /></>, "owner");
    const stepUp = sheet(true);
    await press("build-lucky");
    expect(tid("consent-durations")).not.toBeNull();
    await press("consent-task");
    expect(sent.some((s) => s.method === "POST" && s.path === "/v1/identity/me/delegations")).toBe(true);
    expect(stepUp).toHaveBeenCalledTimes(1);
    const launches = launchesOf(sent);
    expect(launches).toHaveLength(3);
    expect(new Set(launches.map((l) => l.body)).size).toBe(1);
    expect(tid("build-status")?.textContent).toContain("Build started");
    expect(tid("build-launch-error")).toBeNull();
  });

  it("step-up, then consent: both are asked, and the third send starts", async () => {
    const sent = server([STEP_UP, CONSENT, STARTED]);
    await mountAs(<><BuildHome /><ConsentHost /></>, "owner");
    const stepUp = sheet(true);
    await press("build-lucky");
    expect(stepUp).toHaveBeenCalledTimes(1);
    expect(tid("consent-durations")).not.toBeNull();
    await press("consent-task");
    expect(launchesOf(sent)).toHaveLength(3);
    expect(tid("build-status")?.textContent).toContain("Build started");
    expect(tid("build-launch-error")).toBeNull();
  });

  it("the launch proxy keeps the step-up code when it rewords a refusal", () => {
    expect(stepUpCode({ error: "step_up_required" })).toEqual({ code: "step_up_required" });
    expect(stepUpCode({ detail: { error: "step_up_required" } })).toEqual({ code: "step_up_required" });
    expect(stepUpCode({ error: "forbidden" })).toEqual({});
  });
});

describe("a started build says what happens next", () => {
  it("names the app, links Watch the build to its build page, and keeps the ids faint", async () => {
    server([STARTED]);
    await mountAs(<BuildHome />, "owner");
    await press("build-lucky");
    expect(tid("build-started-title")?.textContent).toBe("Recipe box is building");
    expect(tid("build-started-next")?.textContent).toMatch(/pull request.*15–25 min/);
    const watch = tid("build-watch") as HTMLAnchorElement | null;
    const params = new URLSearchParams(watch?.getAttribute("href")?.replace(/^\?/, "") ?? "");
    expect(params.get("area")).toBe("code_tasks");
    expect(params.get("build")).toBe("crun_6883ec7cbd67401a9ce8");
    expect(params.get("project_id")).toBe("recipe-box-fd3ab4a3");
    const open = new URLSearchParams(tid("build-open-project")?.getAttribute("href")?.replace(/^\?/, "") ?? "");
    expect(open.get("area")).toBe("project");
    expect(open.get("project_id")).toBe("recipe-box-fd3ab4a3");
    // The ids are there, but never the card's main words.
    expect(tid("build-run-id")?.textContent).toBe("crun_6883ec7cbd67401a9ce8");
    expect(tid("build-started-title")?.textContent).not.toMatch(/crun_|fd3ab4a3/);
    expect(tid("build-run-id")?.closest(".bh-started-ids")).not.toBeNull();
  });

  it("a reused launch says it already started, not 'is building — 15–25 min'", async () => {
    server([{ status: 201, body: { ...STARTED.body, reused: true } }]);
    await mountAs(<BuildHome />, "owner");
    await press("build-lucky");
    expect(tid("build-started-title")?.textContent).toBe("Recipe box already started");
    expect(tid("build-started-next")?.textContent).not.toMatch(/15–25 min/);
    expect(tid("build-launch")?.getAttribute("data-reused")).toBe("true");
    expect(new URLSearchParams(tid("build-watch")?.getAttribute("href")?.replace(/^\?/, "") ?? "").get("build"))
      .toBe("crun_6883ec7cbd67401a9ce8");
  });

  it("only the title is the live region; the ids are not read out", async () => {
    server([STARTED]);
    await mountAs(<BuildHome />, "owner");
    await press("build-lucky");
    expect(tid("build-started-title")?.getAttribute("role")).toBe("status");
    expect(tid("build-launch")?.getAttribute("role")).toBeNull();
    expect(tid("build-run-id")?.closest("[aria-hidden=true]")).not.toBeNull();
  });

  it("a spec-first launch says the team writes the spec first, and links to its workflow run", async () => {
    const sent = server([{ status: 201, body: { project_id: "recipe-box-fd3ab4a3", run_id: "wfr_5e1c",
                                                workflow_run_id: "wfr_5e1c", stage: "spec",
                                                spec: { title: "Recipe box" } } }]);
    await mountAs(<BuildHome />, "owner");
    await press("build-lucky");
    expect(tid("build-started-title")?.textContent)
      .toBe("Recipe box: the team is writing the spec, then building (about 20–30 min)");
    expect(tid("build-started-next")?.textContent).toMatch(/panel.*spec/);
    const params = new URLSearchParams(tid("build-watch")?.getAttribute("href")?.replace(/^\?/, "") ?? "");
    expect(params.get("area")).toBe("delivery_workflows");
    expect(params.get("wfr")).toBe("wfr_5e1c");
    // The light lane by default: the start approves the spec, so nothing asks to review it.
    expect(JSON.parse(launchesOf(sent)[0]!.body).review_plan).toBeUndefined();
  });

  it("Review the plan first is sent with the launch", async () => {
    const sent = server([STARTED]);
    await mountAs(<BuildHome />, "owner");
    const box = tid("build-review-plan") as HTMLInputElement;
    await React.act(async () => { box.click(); });
    await press("build-lucky");
    expect(JSON.parse(launchesOf(sent)[0]!.body).review_plan).toBe(true);
  });

  it("without a run, Watch the build opens the project", () => {
    expect(watchBuildHref({ projectId: "p1", runId: "" })).toBe(projectHref("p1"));
  });
});
