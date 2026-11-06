// @vitest-environment happy-dom
/**
 * A PRESS REFUSED FOR A STALE SECOND FACTOR ALWAYS GETS "CONFIRM IT'S YOU" — AND NEVER WEDGES.
 *
 * boss-1, 2026-11-06 15:23:44Z: Approve on a workflow Deploy step's stack deployment
 * (app/stack-deploy-ui.tsx → lib/stack-deploy.ts → apiWrite) got one `401 step_up_required` in the
 * API log, no sheet appeared, and no later press of Approve reached the API.
 *
 * The cause was in the one step-up path, not the panel: the mirror apiWrite asks is installed
 * DURING RENDER, so it could belong to a provider React rendered and threw away; its stepUp put
 * the ask into a component that never mounted, the promise never settled, and the panel stayed
 * busy forever. A second ask while a sheet was open replaced the first (whose caller then waited
 * forever), and one provider leaving the page nulled the mirror for the provider still there.
 *
 * Pinned here for every apiWrite caller, through the real IdentityProvider and the real
 * StepUpSheet: the sheet opens from a live provider even when the installed mirror is dead; a
 * cancelled step-up comes back as words; the next press goes out; concurrent asks share one
 * sheet; nothing is left waiting when a provider leaves.
 */
import React, { act, Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { apiWrite, stepUpNotConfirmedSentence } from "../lib/api-write";
import { apiErrorText } from "../lib/api-error";
import { currentMirror, installMirror, liveMirror } from "../lib/identity/mirror";
import { noteGesture, resetGesturesForTest } from "../lib/user-gesture";
import { StackDeploymentView } from "../app/stack-deploy-ui";
import type { StackDeployment } from "../lib/stack-deploy";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];
function mount(node: ReactNode): { root: Root; host: HTMLElement } {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  act(() => { root.render(node); });
  return { root, host };
}

const sheet = () => document.querySelector<HTMLElement>("[data-testid=step-up-sheet]");
const press = () => {
  const b = document.createElement("button");
  document.body.appendChild(b);
  noteGesture({ type: "pointerdown", target: b });
  b.remove();
};
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const flush = async () => { await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };

class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch() { /* expected */ }
  render() { return this.state.failed ? null : this.props.children; }
}
function Throws(): ReactNode { throw new Error("this subtree fails and is thrown away"); }

beforeEach(() => {
  resetGesturesForTest();
  vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  installMirror(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the step-up sheet for every apiWrite press", () => {
  it("opens from the provider on the page even when the installed mirror belongs to a thrown-away render", async () => {
    mount(<IdentityProvider me={null} org={null} unavailable><p>console</p></IdentityProvider>);
    const live = liveMirror();
    // A provider React renders and then discards: its render installs its mirror, it never mounts.
    mount(<Boundary><IdentityProvider me={null} org={null} unavailable><Throws /></IdentityProvider></Boundary>);
    expect(currentMirror()).not.toBe(live);   // the dead one is what apiWrite would have asked

    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async () => (++n === 1 ? json(401, { error: "step_up_required" }) : json(200, { ok: true }))));
    press();
    let answer: Response | null = null;
    await act(async () => {
      void apiWrite("/v1/decisions/dec_x/approve", { method: "POST", body: "{}" },
        { press: { scope: "stack-decision-dec_x", label: "Approve" } }).then((r) => { answer = r; });
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(sheet(), "Confirm it's you must open").not.toBeNull();
    expect(answer).toBeNull();
  });

  it("cancelled: the press comes back as words, and the next press is sent and asks again", async () => {
    mount(<IdentityProvider me={null} org={null} unavailable><p>console</p></IdentityProvider>);
    const fetchMock = vi.fn(async () => json(401, { error: "step_up_required" }));
    vi.stubGlobal("fetch", fetchMock);

    for (const round of [1, 2]) {
      press();
      let answer: Response | null = null;
      await act(async () => {
        void apiWrite("/v1/decisions/dec_x/approve", { method: "POST", body: "{}" },
          { press: { scope: "stack-decision-dec_x", label: "Approve" } }).then((r) => { answer = r; });
        await new Promise((r) => setTimeout(r, 0));
      });
      expect(sheet(), `round ${round}: the sheet opens`).not.toBeNull();
      const cancel = [...sheet()!.querySelectorAll("button")].find((b) => b.textContent === "Cancel")!;
      await act(async () => { cancel.click(); });
      await flush();
      expect(sheet()).toBeNull();
      expect(answer).not.toBeNull();
      const r = answer as unknown as Response;
      expect(r.status).toBe(401);
      expect(apiErrorText(await r.json(), 401)).toBe(stepUpNotConfirmedSentence("Approve"));
      expect(fetchMock).toHaveBeenCalledTimes(round);
    }
  });

  it("two presses waiting on the sheet share it, and both go out once it is confirmed", async () => {
    mount(<IdentityProvider me={null} org={null} unavailable><p>console</p></IdentityProvider>);
    const mirror = currentMirror()!;
    press();
    let a: boolean | null = null, b: boolean | null = null;
    await act(async () => {
      void mirror.stepUp("first").then((v) => { a = v; });
      void mirror.stepUp("second").then((v) => { b = v; });
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(document.querySelectorAll("[data-testid=step-up-sheet]")).toHaveLength(1);
    const cancel = [...sheet()!.querySelectorAll("button")].find((x) => x.textContent === "Cancel")!;
    await act(async () => { cancel.click(); });
    await flush();
    expect([a, b]).toEqual([false, false]);   // both answered, neither left waiting
  });

  it("a provider leaving the page answers its open ask and hands the mirror to the one still there", async () => {
    mount(<IdentityProvider me={null} org={null} unavailable><p>console</p></IdentityProvider>);
    const stays = liveMirror();
    const leaving = mount(<IdentityProvider me={null} org={null} unavailable><p>panel</p></IdentityProvider>);
    const mirror = liveMirror()!;
    expect(mirror).not.toBe(stays);
    press();
    let answered: boolean | null = null;
    await act(async () => {
      void mirror.stepUp("x").then((v) => { answered = v; });
      await new Promise((r) => setTimeout(r, 0));
    });
    act(() => leaving.root.unmount());
    await flush();
    expect(answered).toBe(false);
    expect(currentMirror()).toBe(stays);
  });

  it("with no provider at all, the refusal is still said in words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(401, { error: "step_up_required" })));
    const r = await apiWrite("/v1/x", { method: "POST", body: "{}" }, { press: { scope: "s", label: "Approve" } });
    expect(apiErrorText(await r.json(), r.status)).toBe(stepUpNotConfirmedSentence("Approve"));
  });
});

describe("the Deploy step's stack approval (the reported panel)", () => {
  const deployment: StackDeployment = {
    id: "sdep_fixture", project_id: "p", target_id: "t", repository: "example/app", ref: "main",
    status: "awaiting_approval", decision_id: "dec_fixture", actions: ["approve", "reject"],
    manifest: { name: "app" }, steps: [],
  };

  it("Approve opens the sheet; cancelled, it says why and Approve works again", async () => {
    const approves: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") { approves.push(String(url)); return json(401, { error: "step_up_required" }); }
      return json(200, deployment);
    }));
    const { host } = mount(
      <IdentityProvider me={null} org={null} unavailable>
        <StackDeploymentView deploymentId="sdep_fixture" actor="usr_x" initial={deployment} />
      </IdentityProvider>);
    await flush();
    const approve = () => [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Approve")!;

    for (const round of [1, 2]) {
      press();
      await act(async () => { approve().click(); await new Promise((r) => setTimeout(r, 0)); });
      await flush();
      expect(sheet(), `press ${round}: Confirm it's you`).not.toBeNull();
      const cancel = [...sheet()!.querySelectorAll("button")].find((b) => b.textContent === "Cancel")!;
      await act(async () => { cancel.click(); });
      await flush(); await flush();
      expect(host.querySelector("[role=alert]")?.textContent).toContain(stepUpNotConfirmedSentence("Approve"));
      expect(approve().disabled, "Approve is not left busy").toBe(false);
      expect(approves).toHaveLength(round);
    }
  });
});
