// @vitest-environment happy-dom
/**
 * AN AREA NOBODY IS LOOKING AT DOES NO WORK (2026-10-10).
 *
 * Every area the session opened stays mounted and the CSS reveal only hides it, so its panels
 * kept polling: sitting on the Command Center after opening each area once, the laptop console
 * sent 126 requests a minute, ~80% of them from areas off screen. The frame now tells each
 * area's sections whether they are on screen, and every poll asks before it fetches.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";

import { ActiveAreaContext, AreaFrame, holdsArea } from "../app/area-frame";
import { useAreaShown } from "../lib/area-shown";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function Probe({ name, seen }: { name: string; seen: Record<string, boolean> }) {
  seen[name] = useAreaShown();
  return null;
}

async function frame(active: string | null, seen: Record<string, boolean>) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(
      <ActiveAreaContext.Provider value={active}>
        <AreaFrame>
          <section data-area="overview"><Probe name="overview" seen={seen} /></section>
          <>
            <section data-area="code_tasks"><Probe name="code_tasks" seen={seen} /></section>
          </>
          <section data-area="ops"><span data-area="org" /><Probe name="ops-holding-org" seen={seen} /></section>
          <div><Probe name="no-area" seen={seen} /></div>
        </AreaFrame>
      </ActiveAreaContext.Provider>,
    );
  });
  return { host, root };
}

describe("the frame says which area is on screen", () => {
  it("only the active area's sections are shown; anything without an area is left alone", async () => {
    const seen: Record<string, boolean> = {};
    const { root } = await frame("code_tasks", seen);
    expect(seen).toEqual({ overview: false, code_tasks: true, "ops-holding-org": false, "no-area": true });
    act(() => root.unmount());
  });

  it("a section holding the active area counts as shown (the visibility CSS's :has rule)", async () => {
    const seen: Record<string, boolean> = {};
    const { root } = await frame("org", seen);
    expect(seen["ops-holding-org"]).toBe(true);
    expect(seen.overview).toBe(false);
    act(() => root.unmount());
  });

  it("outside the shell (a route page, a unit test) everything counts as shown", async () => {
    const seen: Record<string, boolean> = {};
    const { root } = await frame(null, seen);
    expect(Object.values(seen).every(Boolean)).toBe(true);
    act(() => root.unmount());
  });

  it("holdsArea looks through the element tree", () => {
    expect(holdsArea(<div><p><b data-area="x" /></p></div>, "x")).toBe(true);
    expect(holdsArea(<div><p /></div>, "x")).toBe(false);
  });
});

describe("every poll asks before it fetches", () => {
  it("the shared polling hooks", () => {
    expect(codeOnly("lib/factory-floor.ts")).toContain("shownRef.current");
    expect(codeOnly("lib/log-stream.ts")).toMatch(/export function useVisible\(\): boolean \{\s*return useOnScreen\(\);/);
    expect(codeOnly("lib/agents-live-board.ts")).toContain("shownRef.current");
    expect(codeOnly("app/use-console-refresh.ts")).toMatch(/if \(kind === "poll" && !onScreenRef\.current\) return;/);
  });

  it("each panel's own interval", () => {
    for (const file of [
      "app/delivery-workflows-ui.tsx", "app/coordination-ui.tsx", "app/meeting-room-ui.tsx",
      "app/discoveries-ui.tsx", "app/host-advice.tsx", "app/stack-deploy-ui.tsx", "app/why-this-model.tsx",
      "app/setup/setup-board.tsx", "app/multirepo/deploy-plan.tsx", "app/multirepo/change-set-cockpit.tsx",
      "app/factory/legacy-build-parts.tsx", "app/factory/apps-tab.tsx", "components/v2/DeployControls.tsx",
      "app/shepherd-ui.tsx", "app/agents/run-acts.ts",
    ]) {
      expect(codeOnly(file), file).toContain("useOnScreen()");
    }
  });

  it("the page wraps its areas in the frame, and the shell says which is active", () => {
    expect(codeOnly("app/page.tsx")).toContain("<AreaFrame>");
    expect(codeOnly("app/ConsoleShell.tsx")).toContain("<ActiveAreaContext.Provider value={routeMode ? null : active}>");
  });
});
