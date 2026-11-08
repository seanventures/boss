// @vitest-environment happy-dom
/**
 * EVERY IDENTITY WRITE GETS "CONFIRM IT'S YOU", NEVER A SILENT WAIT (lib/identity/api.ts idFetch).
 *
 * boss-1, 2026-11-08: Enter on an idea in Add a project sent POST …/add-project/understand, the API
 * answered `401 step_up_required` (project:create, a step-up permission, with a lapsed passkey),
 * and the screen just waited. idFetch told the one write path "the caller handles the step-up"
 * for EVERY call, and this caller (like most idPost callers) never wrapped it in useGuardedCall.
 *
 * Now any identity write refused for a stale second factor opens the provider's step-up sheet;
 * confirmed, it is sent once more; cancelled, it fails with words (`step_up_cancelled`). Inside
 * useGuardedCall the sheet says the guarded call's own reason, and the guard never asks twice.
 */
import React, { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { addProjectApi } from "../lib/add-project";
import { IdentityError, idPost, withStepUpReason } from "../lib/identity/api";
import { installMirror, type Mirror } from "../lib/identity/mirror";
import { noteGesture, resetGesturesForTest } from "../lib/user-gesture";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];
function mount(node: ReactNode): void {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  act(() => { root.render(node); });
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
const UNDERSTOOD = { status: "ok", brief: { title: "HelpDesk AI" }, picks: { stack: null, connectors: [], workflow: null } };

function fakeMirror(answer: boolean, reasons: string[]): Mirror {
  return { check: () => ({ allowed: true, reason: "", needsStepUp: false }), refresh: () => undefined, projectId: null,
           stepUp: async (reason: string) => { reasons.push(reason); return answer; } } as unknown as Mirror;
}

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

describe("a 401 step_up_required on an identity write", () => {
  it("opens the real step-up sheet for Add a project's understand, and cancelled it fails with words (never hangs)", async () => {
    mount(<IdentityProvider me={null} org={null} unavailable><p>console</p></IdentityProvider>);
    const fetchMock = vi.fn(async () => json(401, { error: "step_up_required", permission: "project:create" }));
    vi.stubGlobal("fetch", fetchMock);
    press();
    let failed: unknown = null;
    await act(async () => {
      void addProjectApi.understand("p1", { words: "A helpdesk for our support team" }).catch((e) => { failed = e; });
      await new Promise((r) => setTimeout(r, 0));
    });
    await flush();
    expect(sheet(), "Confirm it's you opens").not.toBeNull();
    const cancel = [...sheet()!.querySelectorAll("button")].find((b) => b.textContent === "Cancel")!;
    await act(async () => { cancel.click(); });
    await flush();
    expect(sheet()).toBeNull();
    expect(failed).toBeInstanceOf(IdentityError);
    expect((failed as IdentityError).code).toBe("step_up_cancelled");
    expect((failed as IdentityError).stepUp, "a guard around it never asks a second time").toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("confirmed, the same write is sent once more and its answer comes back", async () => {
    const reasons: string[] = [];
    installMirror(fakeMirror(true, reasons));
    let n = 0;
    const fetchMock = vi.fn(async () => (++n === 1 ? json(401, { error: "step_up_required" }) : json(200, UNDERSTOOD)));
    vi.stubGlobal("fetch", fetchMock);
    const got = await addProjectApi.understand("p1", { words: "A helpdesk for our support team" });
    expect(got.brief.title).toBe("HelpDesk AI");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(reasons).toEqual(["Confirm it's you to continue."]);
  });

  it("inside a guarded call the sheet says the guard's own reason", async () => {
    const reasons: string[] = [];
    installMirror(fakeMirror(true, reasons));
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async () => (++n === 1 ? json(401, { error: "step_up_required" }) : json(200, { ok: true }))));
    await withStepUpReason("Create the project and start it.", () => idPost("/v1/add-project", {}));
    expect(reasons).toEqual(["Create the project and start it."]);
  });
});
