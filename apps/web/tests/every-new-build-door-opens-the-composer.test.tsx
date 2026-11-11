// @vitest-environment happy-dom
/**
 * EVERY "NEW BUILD" DOOR OPENS THE NEW PROJECT COMPOSER, NEVER AN AGENT CHAT (operator,
 * 2026-10-07; it superseded the 2026-10-04 decision that New build meant "Plan it with Juno").
 *
 * The composer is the one screen with the big "What should it do?" box and its "Add to an
 * existing app" mode (lib/compose-links.ts openNewProject → area=delivery_workflows,
 * compose=new). Planning with an agent is an option INSIDE the composer.
 *
 * The class, by label: every control in app/ or components/ that reads "New build" or
 * "New project" names no planner (openPlan, onPlan, a `plan` callback) in its opening tag.
 * Then the doors that matter are pressed: the Floor's empty state opens Add a project (v2: one
 * screen that absorbed New build's new app) and starts no conversation.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { openNewProject } from "../lib/compose-links";
import { parseFloor } from "../lib/factory-floor";
import { emptyFloor } from "./_helpers/factory-fixtures";
import { $, click, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { FloorView } = await import("../app/factory/floor");
const ROOT = process.cwd();
function files(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...files(path));
    else if (/\.tsx$/.test(name)) out.push(path);
  }
  return out;
}

const SOURCES = () => [...files(join(ROOT, "app")), ...files(join(ROOT, "components"))];
const rel = (f: string) => f.slice(ROOT.length + 1);

/** Each "New build" label and the opening tag of the control that holds it. */
function doors(): { where: string; tag: string }[] {
  const out: { where: string; tag: string }[] = [];
  for (const f of SOURCES()) {
    const s = readFileSync(f, "utf8");
    for (const m of s.matchAll(/>\s*(?:<span aria-hidden>\+<\/span>\s*|[+＋]\s*)?New build\s*</g)) {
      const before = s.slice(0, m.index! + 1);
      const open = Math.max(before.lastIndexOf("<button"), before.lastIndexOf("<GatedButton"));
      if (open < 0) continue;
      out.push({ where: `${rel(f)}:${before.split("\n").length}`, tag: before.slice(open) });
    }
  }
  return out;
}

/** Every place the console SHOWS "New project": JSX text, or a string literal (a heading, a
 *  tooltip, a label prop). Comments are not shown, so they are not read. */
function newProjectLabels(): string[] {
  const out: string[] = [];
  for (const f of SOURCES()) {
    const s = readFileSync(f, "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "))
      .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
    const shown = [/>\s*(?:<span aria-hidden>\+<\/span>\s*|[+＋]\s*)?New project\s*</g,
      /["'`][^"'`\n]*\bNew project\b[^"'`\n]*["'`]/g];
    for (const re of shown) {
      for (const m of s.matchAll(re)) out.push(`${rel(f)}:${s.slice(0, m.index!).split("\n").length}`);
    }
  }
  return out;
}

afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });

describe("no New build door opens the agent chat", () => {
  it("finds the doors (top bar, palette, Factory header, its empty states, Workflows)", () => {
    const where = doors().map((d) => d.where.split(":")[0]);
    for (const f of ["app/ConsoleShell.tsx", "app/code-tasks-ui.tsx", "app/factory/floor.tsx",
      "app/factory/apps-tab.tsx", "app/delivery-workflows-ui.tsx"]) expect(where).toContain(f);
  });

  it("calls the composer New build everywhere: New project is only the project menu's and its sheet's", () => {
    // Operator-approved rename: the build composer's doors, the Workflows tab and the composer's
    // own heading read "New build". "New project" is the top bar's PROJECT MENU door and the
    // sheet it opens (new-project-sheet.tsx, BOSSAPP-768 round 2: the form moved out of the
    // dropdown into a sheet) — it makes a BOSS project in the org (POST /v1/projects,
    // project:create), not a build (tests/new-project-in-the-org.test.tsx holds it).
    // BOSSAPP-810: the menu's New project opens the start flow's sheet (three doors), whose
    // "New app from an idea" is the project sheet.
    // BOSSAPP-913: the start flow's sheets moved into one host (start-project/start-host.tsx) that
    // the menu, the Projects page and its first run all mount; the menu opens it, it opens the
    // project sheet.
    const PROJECT_DOORS = ["app/project-menu.tsx", "app/new-project-sheet.tsx", "app/start-project/start-sheet.tsx",
                           "app/start-project/start-host.tsx"];
    const labels = newProjectLabels();
    expect(labels.map((l) => l.split(":")[0]).every((f) => PROJECT_DOORS.includes(f)), labels.join(", "))
      .toBe(true);
    expect(labels.some((l) => l.startsWith("app/project-menu.tsx:"))).toBe(true);
    const menu = readFileSync(join(ROOT, "app", "project-menu.tsx"), "utf8");
    // The menu's New project door opens the start host, whose idea door is the project sheet —
    // never the build composer.
    expect(menu).toMatch(/<StartProjectHost\b/);
    expect(readFileSync(join(ROOT, "app", "start-project", "start-host.tsx"), "utf8")).toMatch(/<NewProjectSheet\b/);
    for (const f of PROJECT_DOORS) {
      expect(readFileSync(join(ROOT, f), "utf8"), f).not.toMatch(/openNewProject|compose=/);
    }
    // ONE WIZARD (BOSSAPP-989): the Workflows tab row's New build is a door to Add a project,
    // not a tab and not the composer.
    const workflows = readFileSync(join(ROOT, "app", "delivery-workflows-ui.tsx"), "utf8");
    expect(workflows).toContain('data-testid="dw-tab-new"');
    expect(workflows).toMatch(/data-testid="dw-tab-new"[\s\S]{0,200}openNewProject\("idea"\)/);
    const composer = readFileSync(join(ROOT, "app", "workflows", "new-project-composer.tsx"), "utf8");
    expect(composer).toMatch(/:\s*"New build";/);
  });

  it("names no planner in any door's control", () => {
    const bad = doors().filter((d) => /openPlan|onPlan|\bplan\b|openPicker|openTalk/.test(d.tag)).map((d) => d.where);
    expect(bad).toEqual([]);
  });

  it("gates every door on what the composer's Start needs, with the same tooltip", () => {
    const gated = doors().filter((d) => d.tag.startsWith("<GatedButton"));
    expect(gated.length).toBeGreaterThanOrEqual(4);
    for (const d of gated) {
      expect(d.tag, d.where).toContain('permission="workflow_run:start"');
      expect(d.tag, d.where).toContain("NEW_BUILD_TIP");
    }
  });

  it("the Floor's empty state opens the composer and starts no conversation", async () => {
    const noop = () => {};
    const talks: unknown[] = [];
    const on = (e: Event) => talks.push((e as CustomEvent).detail);
    window.addEventListener("boss:converse", on);
    window.history.replaceState(null, "", "/?area=code_tasks");
    await mount(<FloorView projectId="p1" actor="ceo" floor={parseFloor(emptyFloor())} error={null} loading={false}
                           refresh={noop} onOpen={noop} onPlan={noop} onDirect={noop} onApps={noop} band="wide" />);
    await click($("[data-testid=fx-empty-new]"));
    window.removeEventListener("boss:converse", on);
    expect(talks).toEqual([]);
    const url = new URLSearchParams(window.location.search);
    // Add a project v2: a new app is the one Add a project screen, opened where you are.
    expect([url.get("area"), url.get("new_project"), url.get("compose")]).toEqual(["code_tasks", "idea", null]);
  });
});

describe("a New build door opens Add a project where you are (Add a project v2)", () => {
  it("keeps the org and the area, asks for the sheet, and drops the old composer's command", () => {
    const order: string[] = [];
    const onPop = () => order.push("sheet announced");
    window.addEventListener("popstate", onPop);
    window.history.replaceState(null, "", "/?org=org_boss&area=setup&compose=add&app=old");
    try {
      openNewProject();
      const params = new URLSearchParams(window.location.search);
      expect([params.get("org"), params.get("area"), params.get("new_project")]).toEqual(["org_boss", "setup", "idea"]);
      expect([params.get("compose"), params.get("app")]).toEqual([null, null]);
      expect(order).toEqual(["sheet announced"]);
    } finally {
      window.removeEventListener("popstate", onPop);
    }
  });
});
