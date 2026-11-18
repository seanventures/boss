// @vitest-environment happy-dom
/**
 * FIRST RUN FOR A STRANGER (stranger test, rc10.27). What the report found, each as a rule:
 *
 *   · Try a sample lists EVERY need before anything is created (model, repository, deploy host),
 *     from the start's own check — never one at a time across 409s.
 *   · Ticking "Start anyway" starts the sample: no second press.
 *   · A deployment host (needed only after a person, at release) never stops a sample building
 *     and opening its PR; the sheet says where to add this machine as a host.
 *   · A button waiting on the checklist names the ONE tick there is ("Tick the 2 items above"
 *     over a single checkbox).
 *   · The idea flow says one truthful state: never "Helm rewrote it" over a fallback.
 *   · Code kept on this machine satisfies Setup's repository step; GitHub is optional.
 *   · "included in your subscription" only with a subscription connected.
 *   · The journey strip never breaks a word.
 */
import { readFileSync } from "node:fs";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { ProjectMenu } from "../app/project-menu";
import { briefNote } from "../app/start-project/add-project";
import { sampleCostLine, welcomeState } from "../app/setup/welcome";
import { startAnywayHint, type Readiness } from "../app/workflow-readiness-ui";
import { laterNeeds } from "../lib/start-project";
import { hasCode, summarize } from "../lib/setup-stepper";
import type { SetupView } from "../lib/setup-types";
import { click, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });

const need = (key: string, label: string, after = false) => ({
  key, label, fix: "Fix it.", area: "setup", ok: false, detail: "missing", needed_by: [{ step_id: "s", label: "Step" }],
  first_step: "s", first_step_label: after ? "Deploy" : "Plan", after_a_person: after,
});
const MODEL = need("model", "A connected model");
const REPO = need("repository", "A repository BOSS can push to");
const DEPLOY = need("deployment_target", "Somewhere to deploy", true);

async function openSample() {
  window.history.replaceState(null, "", "/?org=org_1&new_project=sample");
  await mountAs(<ProjectMenu projects={[{ id: "p1", name: "Acme Shop" }]} activeId="p1" homeId="p1" />);
}

describe("Try a sample: every need up front, and the tick is the answer", () => {
  it("lists model, repository and deploy host BEFORE creating anything, read for local code", async () => {
    const calls = stubFetch((c) => {
      if (c.path === "/v1/delivery-workflows/minor_feature/readiness") {
        return { body: { ready: false, checked: [MODEL, REPO, DEPLOY], missing: [MODEL, REPO, DEPLOY] } };
      }
      return undefined;
    });
    await openSample();
    await click(tid("ap-sample"));
    const asked = calls.find((c) => c.path === "/v1/delivery-workflows/minor_feature/readiness")!;
    expect(new URLSearchParams(asked.search).get("code_location")).toBe("local");
    for (const k of ["model", "repository", "deployment_target"]) expect(tid(`dw-readiness-missing-${k}`)).not.toBeNull();
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/projects")).toBe(false);
    // The later need is said as later, with where this machine becomes a host.
    expect(tid("sample-later-deploy")!.textContent).toMatch(/not needed to build and open the PR/);
    expect(tid("sample-add-host")!.getAttribute("href")).toContain("area=infra");
  });

  it("ticking Start anyway creates and starts it at once, sending exactly what was listed", async () => {
    const calls = stubFetch((c) => {
      if (c.path === "/v1/delivery-workflows/minor_feature/readiness") {
        return { body: { ready: false, checked: [MODEL, DEPLOY], missing: [MODEL, DEPLOY] } };
      }
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "hello-world", name: "Hello World" } };
      if (c.method === "POST" && c.path === "/v1/templates/hello-world/apply") return { status: 201, body: { workflow_run_id: "wfr_1" } };
      return undefined;
    });
    await openSample();
    await click(tid("ap-sample"));
    expect(calls.filter((c) => c.path.endsWith("/apply"))).toHaveLength(0);
    await click(tid("dw-readiness-start-anyway"));
    const apply = calls.find((c) => c.path.endsWith("/apply"))!;
    expect((apply.body as { start_anyway: string[] }).start_anyway.sort()).toEqual(["deployment_target", "model"]);
    expect(calls.filter((c) => c.method === "POST" && c.path === "/v1/projects")).toHaveLength(1);
  });

  it("with only a deploy host missing it starts straight away (no checklist to answer)", async () => {
    const calls = stubFetch((c) => {
      if (c.path === "/v1/delivery-workflows/minor_feature/readiness") return { body: { ready: false, checked: [DEPLOY], missing: [DEPLOY] } };
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "hello-world", name: "Hello World" } };
      if (c.method === "POST" && c.path === "/v1/templates/hello-world/apply") return { status: 201, body: { workflow_run_id: "wfr_1" } };
      return undefined;
    });
    await openSample();
    await click(tid("ap-sample"));
    const apply = calls.find((c) => c.path.endsWith("/apply"));
    expect(apply).toBeDefined();
    expect((apply!.body as { start_anyway: string[] }).start_anyway).toEqual(["deployment_target"]);
    expect(tid("ap-sample-readiness")).toBeNull();
  });

  it("laterNeeds is only what the run stops for a person before", () => {
    expect(laterNeeds({ missing: [MODEL, REPO, DEPLOY] })).toEqual(["deployment_target"]);
    expect(laterNeeds(null)).toEqual([]);
  });
});

describe("the hint names the one tick there is", () => {
  const r: Readiness = { ready: false, checked: [MODEL, DEPLOY] as never, missing: [MODEL, DEPLOY] as never };
  it("counts the items the single Start anyway covers, never 'the 2 items' to tick", () => {
    expect(startAnywayHint(r, [])).toBe("Tick “Start anyway” above to start without the 2 missing items");
    expect(startAnywayHint(r, ["model"])).toBe("Tick “Start anyway” above to start without the missing item");
    expect(startAnywayHint(r, ["model", "deployment_target"])).toBe("");
  });
  it("Add a project's button uses it", () => {
    expect(readFileSync("app/start-project/add-project.tsx", "utf8")).toContain("startAnywayHint(refusal, startAnyway)");
  });
});

describe("the idea flow says one truthful state", () => {
  const brief = { title: "A", problem: "", who_uses: "", must_haves: [], out_of_scope: [], done_when: "", open_questions: [], added: [], pinned: [] };
  it("only a brief Helm wrote says it rewrote the words", () => {
    expect(briefNote({ status: "ok", brief })).toBe("Helm rewrote it below ↓");
    for (const status of ["no_model", "timeout"] as const) {
      expect(briefNote({ status, brief: { ...brief, raw: true } })).not.toMatch(/rewrote/);
    }
    expect(briefNote(null)).toBeNull();
  });
});

describe("code on this machine is a repository (Setup)", () => {
  const base = {
    ready: false, next: "repository", problems: 0, github_accounts: [], try: { done: false, last_run: null },
    repository: { connection: null, repos: [], local: null }, models: [],
  } as unknown as SetupView;
  const local = { ...base, repository: { ...base.repository, local: { full_name: "local_x/tiny", project_id: "tiny", project_name: "A tiny static page" } } } as SetupView;
  it("the badge and Get started count the repository step done", () => {
    expect(hasCode(base)).toBe(false);
    expect(hasCode(local)).toBe(true);
    expect(summarize(base).badge?.count).toBe(2);
    expect(summarize(local).badge?.count).toBe(1);
  });
  it("the Welcome says GitHub is optional, not 'Connect GitHub' as a step to do", () => {
    const s = welcomeState(local);
    expect(s.github.done).toBe(true);
    expect(s.github.local?.project_name).toBe("A tiny static page");
    expect(welcomeState(base).github.done).toBe(false);
  });
});

describe("the sample's cost line is only as true as what is connected", () => {
  it("never says subscription before one is connected", () => {
    expect(sampleCostLine({ done: false, label: null, kind: null })).not.toMatch(/subscription/);
    expect(sampleCostLine({ done: true, label: "OpenAI", kind: "metered" })).not.toMatch(/subscription/);
    expect(sampleCostLine({ done: true, label: "Claude Code", kind: "subscription" })).toMatch(/included in your subscription/);
  });
});

describe("the journey strip never breaks a word", () => {
  it("captions wrap between words only, and a node never shrinks below its longest word", () => {
    const css = readFileSync("app/projects/px.css", "utf8");
    const rules = [...css.matchAll(/([^{}]*\.px-jr-(?:cap|meta|node)[^{}]*)\{([^}]*)\}/g)];
    expect(rules.length).toBeGreaterThan(0);
    for (const [, , body] of rules) {
      expect(body).not.toMatch(/overflow-wrap:\s*anywhere|word-break:\s*break-all/);
    }
    expect(css).toMatch(/\.px-jr-node \{ flex: 0 1 92px; min-width: min-content; \}/);
  });
});
