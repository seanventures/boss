// @vitest-environment happy-dom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { missingWord } from "../lib/build-detail";
import { parseWorkItem, waitingSinceWords, workWhen } from "../lib/project-experience";
import { whenLocal } from "../lib/local-time";
import { needsYou, personWait, stepClock, waitsOnPersonOutput } from "../lib/step-timing";
import { toAvatarState } from "../lib/agent-world";
import { summarize, SETUP_MODEL_READY_EVENT } from "../lib/setup-stepper";
import { repoSummary } from "../app/setup/setup-board";
import { teamSummary } from "../app/command-center-ui";
import { needRow } from "../app/command-center/cards";
import { EditionChip } from "../app/edition-ui";
import { Timeline, timelineLabels } from "../app/projects/kit";
import { FailedActions } from "../app/projects/build-failed";
import { statusChip } from "../app/projects/tab-work";
import { InfraCriticalCardView } from "../app/infra/infra-critical-card";
import { openIncident } from "../lib/infra-storage";
import { demoIncident } from "../lib/infra-storage-demo";
import { actorVerb, stepLogSentence } from "../app/workflows/step-run";
import { bucketOf, laneStateOf, phaseOf, ReviewAndDecide, runNeedsYou } from "../app/delivery-workflows-ui";
import { StartRefusal, type PendingRefusal, type ReadinessNeed } from "../app/workflow-readiness-ui";
import { Enrol } from "../app/(auth)/enrol/enrol";
import { ConsentSheet, type ConsentRequest } from "../components/identity/consent-sheet";
import { BoardContext, type SetupCtx } from "../app/setup/parts";
import { Welcome } from "../app/setup/welcome";
import type { SetupView } from "../lib/setup-types";
import { primeCsrf } from "../app/(auth)/auth-flow";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("../app/(auth)/auth-flow", () => ({
  primeCsrf: vi.fn(async () => ({ csrf_token: "t", sign_in: { password: true } })),
  registerPasskey: vi.fn(async () => []),
  sayAuth: vi.fn((e: unknown) => String(e)),
}));

afterEach(() => {
  unmount();
  try { window.sessionStorage.clear(); } catch { /* noop */ }
});

beforeEach(() => {
  try { window.sessionStorage.clear(); } catch { /* noop */ }
});

describe("BOSSAPP-1401: console words, counts, layout, and hydration", () => {
  it("missingWord says build failed unless gates actually failed", () => {
    const base = {
      live: false,
      local: true,
      outputs: {},
      state: { state: "failed" },
    } as unknown as Parameters<typeof missingWord>[1];
    expect(missingWord("pr", { ...base, summary: { gates: { failed: [] } } as never })).toEqual({
      word: "Not opened · build failed",
    });
    expect(missingWord("pr", { ...base, summary: { gates: { failed: ["pytest"] } } as never })).toEqual({
      word: "Not opened · gates failed",
      tip: "pytest",
    });
  });

  it("repoSummary never reports 1 GitHub account when accounts === 0", () => {
    const viewWithLocal = (repos: number, accounts: number, local: boolean) => ({
      github_accounts: Array.from({ length: accounts }, (_, i) => ({
        id: `gh_${i}`, login: `u${i}`, name: `u${i}`, pill: { tone: "green", word: "Connected" },
      })),
      repository: {
        connection: null,
        repos: Array.from({ length: repos }, (_, i) => ({
          id: `r_${i}`, full_name: `org/r${i}`, pill: { tone: "green", word: "Connected" },
        })),
        local: local ? { full_name: "local_x/app", project_id: "x", project_name: "App" } : null,
      },
    }) as unknown as SetupView;
    expect(repoSummary(viewWithLocal(2, 0, true))).toBe("2 repos · on this machine");
    expect(repoSummary(viewWithLocal(0, 0, true))).toBe("On this machine · no GitHub account yet");
    expect(repoSummary(viewWithLocal(1, 1, false))).toBe("1 repo · 1 GitHub account");
  });

  it("EditionChip does not hide behind hide-sm inside menus", async () => {
    await mount(<EditionChip editionId="community" />);
    const el = $('[data-testid="edition-chip"]');
    expect(el?.className).not.toContain("hide-sm");
    expect(el?.textContent).toBe("Community");
  });

  it("teamSummary disambiguates background loops when no assigned agent is working", () => {
    expect(teamSummary(7, 0, true, 3)).toBe("Team: 7 agents · 3 background loops running");
    expect(teamSummary(7, 0, true, 1)).toBe("Team: 7 agents · 1 background loop running");
    expect(teamSummary(7, 0, true, 0)).toBe("Team: 7 agents · none working now");
    expect(teamSummary(7, 2, true, 3)).toBe("Team: 7 agents · 2 working now");
  });

  it("InfraCriticalCardView says helper unavailable and trims trailing period before appending one", async () => {
    const base = openIncident(demoIncident("1"))!;
    await mount(
      <InfraCriticalCardView
        incident={{ ...base, helper: { ...base.helper, reachable: false }, causeText: "logs filled /var." }}
        projectId="p1"
        actor="alice"
      />,
    );
    expect($('[data-testid="icc-helper"]')?.textContent).toContain("helper unavailable");
    expect($('[data-testid="icc-cause"]')?.textContent).toContain("logs filled /var.");
    expect($('[data-testid="icc-cause"]')?.textContent).not.toContain("..");
  });

  it("ConsentSheet formats local_<project>-<hex>/<name> via localName even when passed in github", async () => {
    const req: ConsentRequest = {
      scope: "workflow_run",
      task: "My App",
      task_run_id: null,
      agents: [{ id: "ag_1", name: "Helm", role_key: "engineering_captain" }],
      permissions: ["code_task:run"],
      projects: ["proj_1"],
      github: {
        repo: "local_my-app-abcdef12/my-app",
        repos: ["local_my-app-abcdef12/my-app"],
        permissions: { contents: "write" },
        summary: "this machine",
      },
      local: null,
      durations: [{ kind: "task", label: "For this run", days: null }],
      max_days: 30,
    };
    await mount(<ConsentSheet request={req} onClose={() => {}} />);
    expect(document.body.textContent).toContain("My app, this machine");
    expect(document.body.textContent).not.toContain("local_my-app-abcdef12/my-app");
  });

  it("FailedActions invokes onSee in-place when seeHref points to Setup", async () => {
    const onSee = vi.fn();
    await mount(
      <FailedActions
        retry={null}
        fix={null}
        seeHref="/?area=setup&focus=models"
        onSee={onSee}
        projectId="p1"
        actor="alice"
        testid="fa"
      />,
    );
    const btn = $('[data-testid="fa-see"]')!;
    expect(btn.tagName).toBe("BUTTON");
    await click(btn);
    expect(onSee).toHaveBeenCalledTimes(1);
  });

  it("hydration helpers produce identical UTC output across SSR timezones before mount", () => {
    const prevTz = process.env.TZ;
    const iso = "2026-11-05T15:23:00Z";
    try {
      process.env.TZ = "UTC";
      const utcWait = waitingSinceWords(iso);
      const utcWork = workWhen({ state: "done", updated_at: iso });
      const utcWhen = whenLocal(iso);
      const utcTl = renderToStaticMarkup(<Timeline points={[{ at: iso, label: "PR #1", kind: "pr" }]} />);
      const utcLabels = timelineLabels([iso], null);

      process.env.TZ = "America/Los_Angeles";
      expect(waitingSinceWords(iso)).toBe(utcWait);
      expect(workWhen({ state: "done", updated_at: iso })).toBe(utcWork);
      expect(whenLocal(iso)).toBe(utcWhen);
      expect(renderToStaticMarkup(<Timeline points={[{ at: iso, label: "PR #1", kind: "pr" }]} />)).toBe(utcTl);
      expect(timelineLabels([iso], null)).toEqual(utcLabels);
    } finally {
      if (prevTz === undefined) delete process.env.TZ;
      else process.env.TZ = prevTz;
    }
  });
});

describe("BOSSAPP-1406: waiting on person, phaseOf, step logs, and presence", () => {
  it("needsYou, personWait, stepClock, and phaseOf treat waiting_on_person as waiting on you", () => {
    const step = {
      step_id: "panel",
      kind: "capability",
      capability: "meeting",
      status: "waiting",
      started_at: "2026-11-06T15:20:00Z",
      completed_at: null,
      decision_id: null,
      output: { waiting_on_person: true },
      waiting_on_person: true,
      evidence: [],
      error: null,
      approval: {},
      due_at: null,
      timeout_applied: null,
      attempt: 1,
    } as unknown as Parameters<typeof phaseOf>[0];

    expect(needsYou(step)).toBe(true);
    expect(personWait(step)).not.toBeNull();
    expect(stepClock(step, Date.parse("2026-11-06T16:00:00Z"))?.kind).toBe("waiting");
    expect(phaseOf(step)).toBe("wait");
    expect(actorVerb("wait", "waiting")).toBe("Waiting on");
  });

  it("handles real /v1/delivery-workflows/runs string output.waiting_on_person without top-level waiting_on_person", () => {
    const now = Date.parse("2026-11-06T16:00:00Z");
    const step = {
      step_id: "meeting",
      kind: "capability",
      capability: "meeting",
      status: "waiting",
      started_at: "2026-11-06T15:20:00Z",
      completed_at: null,
      decision_id: null,
      output: {
        waiting_on_person: "Your model at http://10.0.0.7:8080 is plain http; BOSS only sends keys over https.",
        meeting_run_id: "mrun_1",
      },
      evidence: [],
      error: null,
      approval: {},
      due_at: null,
      timeout_applied: null,
      attempt: 1,
    } as unknown as Parameters<typeof phaseOf>[0];

    expect(waitsOnPersonOutput(step)).toBe(true);
    expect(needsYou(step)).toBe(true);
    expect(personWait(step)).not.toBeNull();
    expect(stepClock(step, now)?.kind).toBe("waiting");
    expect(phaseOf(step)).toBe("wait");
    expect(laneStateOf(step).phase).toBe("needs_you");
    expect(runNeedsYou({ status: "running", steps: [step] })).toBe(true);
    expect(bucketOf({ status: "running", standing: "active", steps: [step] })).toBe("needs_you");
  });

  it("resolved decision_status takes precedence over output.waiting_on_person and waiting_on_person", () => {
    const now = Date.parse("2026-11-06T16:00:00Z");
    const makeStep = (decision_status: string | null | undefined, topLevel?: boolean) => ({
      step_id: "meeting",
      kind: "capability",
      capability: "meeting",
      status: "waiting",
      started_at: "2026-11-06T15:20:00Z",
      completed_at: null,
      decision_id: "dec_1",
      decision_status,
      ...(topLevel !== undefined ? { waiting_on_person: topLevel } : {}),
      output: { waiting_on_person: "Your model at http://10.0.0.7:8080 is plain http; BOSS only sends keys over https." },
      evidence: [],
      error: null,
      approval: {},
      due_at: null,
      timeout_applied: null,
      attempt: 1,
    }) as unknown as Parameters<typeof phaseOf>[0];

    for (const resolved of ["approved", "rejected", "resolved", "expired"]) {
      for (const s of [makeStep(resolved), makeStep(resolved, true)]) {
        expect(needsYou(s)).toBe(false);
        expect(personWait(s)).toBeNull();
        expect(stepClock(s, now)?.kind).toBe("working");
        expect(phaseOf(s)).toBe("now");
      }
    }

    for (const open of ["pending", "delegated", null, undefined]) {
      const s = makeStep(open);
      expect(needsYou(s)).toBe(true);
      expect(phaseOf(s)).toBe("wait");
    }
  });

  it("ignores blank/false/null waiting_on_person and non-waiting step statuses", () => {
    for (const badVal of ["", "   ", null, false]) {
      const s = {
        step_id: "meeting",
        kind: "capability",
        capability: "meeting",
        status: "waiting",
        started_at: "2026-11-06T15:20:00Z",
        completed_at: null,
        decision_id: null,
        output: { waiting_on_person: badVal },
        evidence: [],
        error: null,
        approval: {},
        due_at: null,
        timeout_applied: null,
        attempt: 1,
      } as unknown as Parameters<typeof phaseOf>[0];
      expect(waitsOnPersonOutput(s)).toBe(false);
      expect(needsYou(s)).toBe(false);
      expect(personWait(s)).toBeNull();
      expect(phaseOf(s)).toBe("now");
    }

    for (const status of ["succeeded", "failed", "running", "skipped"]) {
      const s = {
        step_id: "meeting",
        kind: "capability",
        capability: "meeting",
        status,
        started_at: "2026-11-06T15:20:00Z",
        completed_at: status === "succeeded" ? "2026-11-06T15:25:00Z" : null,
        decision_id: null,
        output: { waiting_on_person: "stale" },
        evidence: [],
        error: null,
        approval: {},
        due_at: null,
        timeout_applied: null,
        attempt: 1,
      } as unknown as Parameters<typeof phaseOf>[0];
      expect(waitsOnPersonOutput(s)).toBe(false);
      expect(needsYou(s)).toBe(false);
      expect(personWait(s)).toBeNull();
      expect(phaseOf(s)).not.toBe("wait");
    }
  });

  it("ReviewAndDecide does not render dw-step-review when decision_id is null even with string output.waiting_on_person", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({}), { status: 404 }),
    );
    try {
      const waitingNoDecision = {
        step_id: "meeting",
        kind: "capability",
        capability: "meeting",
        status: "waiting",
        started_at: "2026-11-06T15:20:00Z",
        completed_at: null,
        decision_id: null,
        output: { waiting_on_person: "Your model at http://10.0.0.7:8080 is plain http; BOSS only sends keys over https." },
        evidence: [],
        error: null,
        approval: {},
        due_at: null,
        timeout_applied: null,
        attempt: 1,
      } as unknown as Parameters<typeof ReviewAndDecide>[0]["step"];

      await mount(<ReviewAndDecide step={waitingNoDecision} projectId="p1" actor="alice" />);
      expect($('[data-testid="dw-step-review"]')).toBeNull();
      expect($('[data-testid="dw-step-decide"]')).toBeNull();
      unmount();

      await mount(
        <ReviewAndDecide
          step={{ ...waitingNoDecision, decision_id: "dec_1", decision_status: "pending" }}
          projectId="p1"
          actor="alice"
        />,
      );
      await settle();
      expect($('[data-testid="dw-step-review"]')).not.toBeNull();
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it("statusChip marks a work item with waiting step as Needs you rather than Running", () => {
    const item = parseWorkItem({
      id: "w1",
      label: "Easy Mode app",
      kind: "feature",
      state: "running",
      steps: [{ id: "s1", name: "Panel", state: "waiting", waiting_on_person: true }],
    });
    expect(statusChip(item).word).toBe("Needs you");
  });

  it("stepLogSentence renders plain English for workflow.panel_waits_on_person and internal service keys", () => {
    expect(stepLogSentence({
      source: "workflow", kind: "workflow.panel_waits_on_person", level: "info", title: "Panel Waits On Person", data: {},
    })).toBe("Panel waiting on you");
    expect(stepLogSentence({
      source: "workflow", kind: "workflow.spec_fallback", level: "info", title: "Spec Fallback", data: {},
    })).toBe("Built from brief after panel stopped");
    expect(stepLogSentence({
      source: "workflow", kind: "workflow.custom", level: "info", title: "Governed Code Task Service started", data: {},
    })).toBe("Build runner started");
    expect(toAvatarState("waiting_on_person")).toBe("needs_you");
    expect(toAvatarState("waiting_you")).toBe("needs_you");
  });
});

describe("BOSSAPP-1392: Setup welcome scope, readiness later split, Enrol reload, BuildHome", () => {
  it("command center readiness card uses ⚙ instead of overflowing Settings text", () => {
    const row = needRow({
      id: "r1",
      kind: "readiness",
      title: "Model missing",
      detail: "Add a model",
      at: "2026-11-06T15:00:00Z",
      roleKey: null,
      href: "/?area=setup",
    } as never, Date.parse("2026-11-06T15:05:00Z"));
    expect(row.icon).toBe("⚙");
  });

  it("Welcome names local project only when viewing that project", async () => {
    const view = {
      ready: false,
      next: "model",
      problems: 0,
      verdict: { tone: "amber", word: "1 to go", tip: "Add a model" },
      github_accounts: [],
      try: { done: false, last_run: null, pill: { tone: "grey", word: "Ready", tip: "" } },
      repository: {
        connection: null,
        repos: [],
        local: { full_name: "local_alpha-1234/alpha", project_id: "proj_alpha", project_name: "Alpha App" },
        pill: { tone: "green", word: "Connected", tip: "" },
      },
      models: [],
    } as unknown as SetupView;
    const ctxOther: SetupCtx = {
      projectId: "proj_beta",
      view,
      apply: () => {},
      checking: new Set(),
      setChecking: () => {},
    };
    await mount(
      <BoardContext.Provider value={ctxOther}>
        <Welcome onSetup={() => {}} />
      </BoardContext.Provider>,
    );
    expect($('[data-testid="su-wl-github-local"]')?.textContent).toContain("Projects keep their code on this machine.");
    expect($('[data-testid="su-wl-github-local"]')?.textContent).not.toContain("Alpha App keeps its code here.");
    unmount();

    await mount(
      <BoardContext.Provider value={{ ...ctxOther, projectId: "proj_alpha" }}>
        <Welcome onSetup={() => {}} />
      </BoardContext.Provider>,
    );
    expect($('[data-testid="su-wl-github-local"]')?.textContent).toContain("Alpha App keeps its code here.");
  });

  it("StartRefusal separates after_a_person items from immediate missing count", async () => {
    const makeNeed = (key: string, after: boolean): ReadinessNeed => ({
      key,
      label: key === "model" ? "AI model" : "Deployment host",
      fix: "Add in Setup",
      area: "setup",
      ok: false,
      detail: "",
      needed_by: [{ step_id: key, label: key }],
      first_step_label: "Deploy",
      after_a_person: after,
    });
    const m = makeNeed("model", false);
    const d = makeNeed("deployment_target", true);
    const refusal: PendingRefusal = {
      label: "Build",
      message: "Missing prerequisites",
      readiness: {
        ready: false,
        checked: [m, d],
        missing: [m, d],
      },
      retry: () => {},
    };
    await mount(<StartRefusal refusal={refusal} projectId="p1" />);
    expect($(".dw-new-error")?.textContent).toContain("1 thing missing · 1 needed later.");
    expect($('[data-testid="dw-readiness-later-group"]')?.textContent).toContain("Needed later");
  });

  it("Enrol shows protected state on reload when alreadyEnrolled is true and restores codes from sessionStorage", async () => {
    await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} alreadyEnrolled />);
    expect(document.body.textContent).toContain("Your account is protected");
    expect(document.body.textContent).not.toContain("Protect your account");
    unmount();

    window.sessionStorage.setItem("boss.enrol.state", JSON.stringify({ stage: "codes", codes: ["AAA-BBB"] }));
    await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} alreadyEnrolled />);
    expect(document.body.textContent).toContain("Save your recovery codes");
    expect(document.body.textContent).toContain("AAA-BBB");
    unmount();

    window.sessionStorage.clear();
    vi.mocked(primeCsrf).mockResolvedValueOnce({ csrf_token: "t", sign_in: { password: false } } as never);
    await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} alreadyEnrolled initialNeedsPassword />);
    await settle();
    expect(document.body.textContent).toContain("Choose a password");
    unmount();

    window.sessionStorage.setItem("boss.enrol.state", JSON.stringify({ stage: "password" }));
    await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} alreadyEnrolled />);
    expect(document.body.textContent).toContain("Choose a password");
    unmount();

    window.sessionStorage.setItem("boss.enrol.state", JSON.stringify({ stage: "codes", codes: ["STALE-CODE"] }));
    await mount(<Enrol host="boss.acme.dev" next="/identity" who={null} alreadyEnrolled={false} />);
    expect(document.body.textContent).toContain("Protect your account");
    expect(document.body.textContent).not.toContain("STALE-CODE");
  });

  it("BuildHome fails fast with needs_model when modelReady is false without calling launch", async () => {
    const { BuildHome } = await import("../app/build-home");
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    await mount(<BuildHome modelReady={false} />);
    await click($('[data-testid="build-lucky"]')!);
    await settle();
    expect(document.body.textContent).toContain("No model is connected");
    expect(fetchSpy).not.toHaveBeenCalledWith("/api/factory/launch", expect.anything());
    fetchSpy.mockRestore();
  });

  it("summarize exposes has_model and SETUP_MODEL_READY_EVENT name", () => {
    expect(SETUP_MODEL_READY_EVENT).toBe("boss:setup-model-ready");
    const s = summarize({
      ready: false,
      next: "model",
      problems: 0,
      repository: { repos: [], local: { full_name: "local_x/x" } },
      models: [{ pill: { tone: "green" } }],
    } as never);
    expect(s.has_model).toBe(true);
  });
});
