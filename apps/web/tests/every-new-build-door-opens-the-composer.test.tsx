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
 * Then the doors that matter are pressed: the Floor's empty state opens the composer and starts
 * no conversation.
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

/** Each "New build" / "New project" label and the opening tag of the control that holds it. */
function doors(): { where: string; tag: string }[] {
  const out: { where: string; tag: string }[] = [];
  for (const f of [...files(join(ROOT, "app")), ...files(join(ROOT, "components"))]) {
    const s = readFileSync(f, "utf8");
    for (const m of s.matchAll(/>\s*(?:<span aria-hidden>\+<\/span>\s*|[+＋]\s*)?New (?:build|project)\s*</g)) {
      const before = s.slice(0, m.index! + 1);
      const open = Math.max(before.lastIndexOf("<button"), before.lastIndexOf("<GatedButton"));
      if (open < 0) continue;
      out.push({ where: `${f.slice(ROOT.length + 1)}:${before.split("\n").length}`, tag: before.slice(open) });
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
    expect([url.get("area"), url.get("compose")]).toEqual(["delivery_workflows", "new"]);
  });
});

describe("New build starts at the document origin", () => {
  it("resets both axes after announcing the composer, without changing its route", () => {
    const frames: FrameRequestCallback[] = [];
    const order: string[] = [];
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      order.push("frame queued");
      frames.push(callback);
      return frames.length;
    });
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    const onPop = () => order.push("composer announced");
    window.addEventListener("popstate", onPop);
    window.history.replaceState(null, "", "/?org=org_boss&area=setup&compose=add&app=old&build=old");
    try {
      openNewProject();
      const params = new URLSearchParams(window.location.search);
      expect([params.get("org"), params.get("area"), params.get("compose")])
        .toEqual(["org_boss", "delivery_workflows", "new"]);
      expect([params.get("app"), params.get("build")]).toEqual([null, null]);
      expect(order).toEqual(["composer announced", "frame queued"]);
      expect(scroll).not.toHaveBeenCalled();
      expect(frames).toHaveLength(1);
      frames[0](0);
      expect(scroll.mock.calls).toEqual([[{ top: 0, left: 0, behavior: "instant" }]]);
    } finally {
      window.removeEventListener("popstate", onPop);
      scroll.mockRestore();
      raf.mockRestore();
    }
  });
});
