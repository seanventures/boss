// @vitest-environment happy-dom
/**
 * WAITING FOR WHAT, AND WHAT THE AGENT FOUND (engine stuck_work). The operator, 2026-11-07: "When I
 * click on Build it takes to Code task page where it does not show anything just waiting (and
 * waiting for what ???)". Four CVE builds said "Queued 0 of 7 · 1h 19m" and "Nothing yet" while every
 * attempt crashed; the Needs-you card said only "CVE squad is stuck on …".
 *
 * Pinned here, through the real poll, parsers and components:
 *   - the build page says the reason under its header and in its Now card, and draws the Shepherd's
 *     diagnosis as five nodes (stuck → checked → cause → tried → next) whose detail opens on press;
 *   - Project › Work rows say the reason instead of a bare stage/age word;
 *   - Agents at work says it on an agent's item;
 *   - a Needs-you card's drawer carries the diagnosis.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, mount, settle, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit, stations } from "./_helpers/factory-fixtures";
import { parseDiagnosis, parseWaitReason } from "../lib/stuck-work";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span /> }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span /> }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CRASH_LOOP = {
  code: "crash_loop", who: "stuck", words: "Stuck: every attempt crashed (30×)",
  detail: "ObjectNotExecutableError: Not an executable object", since: "2026-11-07T15:16:16Z", eta_minutes: null,
};
const DIAGNOSIS = {
  at: "2026-11-07T17:50:00Z", headline: "BOSS crashed pausing it for a grant; every attempt died in seconds.",
  cause: "BOSS crashes running this build at person_wait.py:125 _load_run", chosen: "stop_crash_loop", acted: true,
  tried: "Stopped re-running it and marked it failed.", next: "Install the update, then press Retry.", who: "you",
  needs_person: true, card_id: "dec_1", by: { agent: "Shepherd", model: "claude-sonnet" },
  steps: [
    { k: "stuck", label: "No progress 2h 30m", detail: "Stuck: every attempt crashed (30×)" },
    { k: "checked", label: "Job, events, log", detail: "job running (attempt 30 of 3); 30 crashes" },
    { k: "cause", label: "Crash in person_wait", detail: "30 attempts, each ended in seconds" },
    { k: "tried", label: "Stop retrying, report it", detail: "Stopped re-running it and marked it failed." },
    { k: "next", label: "You", detail: "Install the update, then press Retry.", who: "you" },
  ],
};

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the shapes", () => {
  it("parses a reason and a diagnosis, and refuses an empty one", () => {
    expect(parseWaitReason(CRASH_LOOP)).toMatchObject({ code: "crash_loop", who: "stuck", words: CRASH_LOOP.words });
    expect(parseWaitReason({ code: "x" })).toBeNull();
    expect(parseWaitReason({ words: "w", who: "nonsense" })?.who).toBe("boss");
    const d = parseDiagnosis(DIAGNOSIS)!;
    expect(d.steps.map((s) => s.k)).toEqual(["stuck", "checked", "cause", "tried", "next"]);
    expect(d.needsPerson).toBe(true);
    expect(parseDiagnosis({})).toBeNull();
  });
});

describe("the build page says what it waits for", () => {
  it("header and Now card carry the reason; the diagnosis is a five-node flow", async () => {
    const { Cockpit } = await import("../app/factory/cockpit");
    stubFetch(cockpit({
      state: { state: "quiet", word: "Stuck", tip: CRASH_LOOP.words, verb: "Stopped", station: null, k: 0, n: 7, rounds: 0, preview: null },
      stations: stations(null, "waiting"), why: CRASH_LOOP, diagnosis: DIAGNOSIS,
      summary: { phase: "queued", attempt: 1, files: [], files_count: 0 },
    }));
    await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    // With a diagnosis the header shows the diagnosis (cause + what to do), not the reason twice.
    expect($('[data-testid="fx-why"]')).toBeNull();
    expect($('[data-testid="fx-diagnosis-cause-line"]')?.textContent).toContain("person_wait.py:125");
    const nodes = [...document.querySelectorAll('[data-testid^="fx-diagnosis-"][data-k]')].map((n) => n.getAttribute("data-k"));
    expect(nodes).toEqual(["stuck", "checked", "cause", "tried", "next"]);
    expect($('[data-testid="fx-diagnosis-headline"]')?.textContent).toContain("every attempt died");
    expect($('[data-testid="fx-diagnosis"]')?.textContent).toContain("Shepherd · claude-sonnet");
    await act(async () => { ($('[data-testid="fx-diagnosis-cause"] button') as HTMLButtonElement).click(); });
    expect($('[data-testid="fx-diagnosis-detail"]')?.textContent).toBe("30 attempts, each ended in seconds");
    expect($('[data-testid="fx-nowband"]')).not.toBeNull();
    expect($('[data-testid="fx-now-why"]')?.textContent).toContain("every attempt crashed");
  });

  it("a waiting build with no diagnosis says its reason under the header", async () => {
    const { Cockpit } = await import("../app/factory/cockpit");
    stubFetch(cockpit({ why: { code: "slot", who: "queue", words: "Waiting for a build slot: 2 in use by A and B" } }));
    await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    expect($('[data-testid="fx-why"]')?.textContent).toContain("2 in use by A and B");
  });

  it("a build an agent is working on shows no reason line and no diagnosis", async () => {
    const { Cockpit } = await import("../app/factory/cockpit");
    stubFetch(cockpit());
    await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    expect($('[data-testid="fx-why"]')).toBeNull();
    expect($('[data-testid="fx-diagnosis"]')).toBeNull();
  });
});

describe("the words are never bare", () => {
  it("WhyLine says the words and whose move it is; never 'Queued' alone", async () => {
    const { WhyLine } = await import("../components/stuck-work");
    const slot = parseWaitReason({ code: "slot", who: "queue", words: "Waiting for a build slot: 2 in use by Add SSO and Fix login · next in about 4 min", detail: "Place 1 of 3 in the queue." });
    await mount(<WhyLine why={slot} testId="w" />);
    const el = $('[data-testid="w"]')!;
    expect(el.textContent).toContain("2 in use by Add SSO and Fix login");
    expect(el.textContent).toContain("In the queue");
    expect(el.getAttribute("data-who")).toBe("queue");
    expect(el.textContent?.trim()).not.toBe("Queued");
  });
});

describe("Project › Work and Agents at work say it too", () => {
  it("a work row shows its build's reason in place of the bare stage word", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const { parseWorkItem } = await import("../lib/project-experience");
    const w = parseWorkItem({
      id: "wfr_1", label: "Bump esbuild", kind: "fix", state: "stalled", age: "2h", stages: [], agents: [],
      links: { build: "?project_id=p1&area=code_tasks&view=apps&build=crun_de53", log: "?project_id=p1&area=code_tasks&build=crun_de53&logs=build" },
      why: CRASH_LOOP, diagnosis: DIAGNOSIS, attempts: [],
    });
    const ctx = { projectId: "p1", refreshWork: () => {}, open: () => {} } as unknown as Parameters<typeof WorkRow>[0]["ctx"];
    await mount(<WorkRow w={w} ctx={ctx} />);
    expect($('[data-testid="px-work-why-wfr_1"]')?.textContent).toContain("Stuck: every attempt crashed (30×)");
  });

  it("an agent's item on Agents at work says what its build waits for", async () => {
    const { parseList } = await import("../lib/ambient");
    const view = parseList({ filter: "all", counts: { running: 1 }, items: [{
      id: "work_42", title: "esbuild has a known security hole", lifecycle: "in_progress", task_class: "dependency_cve",
      badges: { outcome: "needs_you", problem_type: "Security" }, strip: [],
      why: { code: "person_grant", who: "you", words: "Waiting for you: let Helm build for you (a standing grant)" },
      diagnosis: DIAGNOSIS, run_id: "crun_de53",
    }] });
    expect(view.items[0].why?.who).toBe("you");
    const { AmbientNeedRows } = await import("../app/command-center/agents-at-work");
    await mount(<AmbientNeedRows view={view} listedDecisionIds={new Set()} />);
    expect($('[data-testid="cc-need-why-work_42"]')?.textContent).toContain("let Helm build for you");
  });
});

describe("a Needs-you card carries the diagnosis", () => {
  it("the card's drawer draws the flow", async () => {
    const { NeedsBody } = await import("../app/projects/drawers-more");
    const { parseOverview } = await import("../lib/project-experience");
    const ov = parseOverview({ needs_you: [{ id: "dec_1", kind: "question", label: "CVE squad is stuck",
      detail: "CVE squad is stuck on esbuild — BOSS crashed pausing it for a grant.", mini: [], diagnosis: DIAGNOSIS }] });
    const n = ov.needs_you[0];
    expect(n.diagnosis?.steps.length).toBe(5);
    const ctx = { projectId: "p1", work: { items: [] }, overview: ov, open: () => {} } as unknown as Parameters<typeof NeedsBody>[0]["ctx"];
    await mount(<NeedsBody ctx={ctx} n={n} />);
    expect(document.body.textContent).toContain("BOSS crashed pausing it for a grant");
    expect(document.querySelectorAll('[data-testid="px-drawer-needs-diagnosis"] .sw-node').length).toBe(5);
    expect($('[data-testid="px-drawer-needs-diagnosis-next"]')?.getAttribute("data-who")).toBe("you");
    await settle();
  });
});

describe("concise, with the rest behind Details (operator, 2026-11-07)", () => {
  it("one line of cause, one line of what to do, the diagram; Details holds why-not and the log", async () => {
    const { DiagnosisFlow } = await import("../components/stuck-work");
    const d = parseDiagnosis({ ...DIAGNOSIS, why_not_fixed: "It is BOSS's own bug: another attempt runs the same code.",
                               links: { log: "?area=code_tasks&build=crun_de53&logs=build", run: "?area=code_tasks&build=crun_de53" },
                               fixes: 1, max_fixes: 3 });
    await mount(<DiagnosisFlow d={d} testId="dg" />);
    expect($('[data-testid="dg-cause-line"]')?.textContent).toContain("person_wait.py:125");
    expect($('[data-testid="dg-next-line"]')?.textContent).toContain("Install the update, then press Retry.");
    const more = $('[data-testid="dg-more"]') as HTMLDetailsElement;
    expect(more.tagName).toBe("DETAILS");
    expect(more.open).toBe(false);
    expect($('[data-testid="dg-why-not"]')?.textContent).toContain("BOSS's own bug");
    expect(($('[data-testid="dg-log"]') as HTMLAnchorElement).getAttribute("href")).toContain("logs=build");
  });
});

describe("Agents at work › Stalled work", () => {
  it("lists what the Shepherd fixed, is trying, and needs a person for — with why", async () => {
    const entry = (id: string, extra: Record<string, unknown> = {}) => ({
      run_id: id, title: `Build ${id}`, status: "running", at: "2026-11-07T17:50:00Z", cause: "Its worker stopped answering",
      next: "BOSS handed it back to the queue.", who: "boss", tried: "Handed it back", why_not_fixed: "", fixes: 1, max_fixes: 3,
      links: { log: `?area=code_tasks&build=${id}&logs=build` }, steps: DIAGNOSIS.steps, ...extra });
    vi.stubGlobal("fetch", vi.fn(async (u: unknown) => String(u).includes("/v1/ambient/stalled-work")
      ? Response.json({ owner: "Shepherd", max_fixes: 3, fixed: [entry("crun_f")], trying: [entry("crun_t")],
                        needs_person: [entry("crun_p", { who: "you", next: "Give Helm a standing grant.",
                                                          why_not_fixed: "Only a person can let an agent act for them." })] })
      : Response.json({})));
    const { StalledWork } = await import("../app/agent-ops/stalled-work");
    await mount(<StalledWork projectId="p1" />);
    await settle();
    expect($('[data-testid="sw-stalled-owner"]')?.textContent).toContain("every project");
    expect($('[data-testid="sw-stalled-sec-fixed"]')?.textContent).toContain("Build crun_f");
    expect($('[data-testid="sw-stalled-sec-trying"]')?.textContent).toContain("Build crun_t");
    const np = $('[data-testid="sw-stalled-sec-needsPerson"]')!;
    expect(np.textContent).toContain("Give Helm a standing grant.");
    // Concise: the diagram and why-not sit behind Details until asked for.
    expect(np.textContent).not.toContain("Only a person can let an agent act for them.");
    const { act } = await import("react");
    await act(async () => { ($('[data-testid="sw-stalled-more-crun_p"]') as HTMLButtonElement).click(); });
    expect($('[data-testid="sw-stalled-sec-needsPerson"]')!.textContent).toContain("Only a person can let an agent act for them.");
    expect(($('[data-testid="sw-stalled-log-crun_p"]') as HTMLAnchorElement).getAttribute("href")).toContain("logs=build");
  });
});
