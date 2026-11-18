// @vitest-environment happy-dom
/**
 * Get started's buttons open Setup's Add form, through the real shell (review of #204).
 *
 * The shell drops the previous area's one-shot params when the area changes (moveToArea). Get
 * started wrote `focus=…&open=add` under `area=welcome` and then announced the switch, so the
 * params were dropped and Connect GitHub / Add a model landed on Setup with nothing open.
 * Breaks if a button's instructions are lost on the way to Setup.
 */
import React, { act, useEffect, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ refresh: vi.fn(), replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
vi.mock("../lib/use-agent-identities", () => ({ useLoadIdentities: () => undefined }));
vi.mock("../lib/agents-live", () => ({ useAttention: () => null }));
vi.mock("../app/agent-ui", () => ({ AgentChat: () => null }));
vi.mock("../app/project-strip", () => ({ ProjectStrip: () => null }));
vi.mock("../app/agents/presence-pill", () => ({ PresencePill: () => null, reviewPatch: () => ({}) }));
vi.mock("../app/agents/converse-host", () => ({ ConverseHost: () => null }));
vi.mock("../components/identity/consent-sheet", () => ({ ConsentHost: () => null }));
vi.mock("../components/identity/account-menu", () => ({
  AccountMenu: () => null, IdentityBanners: () => null, signOut: vi.fn(),
}));
vi.mock("../components/identity/identity-provider", () => {
  const allowed = { allowed: true, reason: "", needsStepUp: false };
  const identity = { me: null, check: () => allowed };
  return {
    IdentityProvider: ({ children }: { children: ReactNode }) => children,
    useIdentity: () => identity, useCan: () => allowed, useGuardedCall: () => (_l: string, f: () => unknown) => f(),
  };
});

import ConsoleShell from "../app/ConsoleShell";
import { AreaShownContext } from "../lib/area-shown";
import { SetupBoard as Board } from "../app/setup/setup-board";
import { loadedAreasCookie } from "../lib/area-loading";
import { fakeApi } from "./_helpers/setup-board";
import { firstRun } from "./_helpers/setup-fixtures";

let host: HTMLDivElement;
let root: Root;

/** The page renders the area the URL names; a new area mounts fresh, as a page load would. */
function Areas() {
  const [area, setArea] = useState(() => new URLSearchParams(window.location.search).get("area"));
  useEffect(() => {
    const read = () => setArea(new URLSearchParams(window.location.search).get("area"));
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  return (
    <AreaShownContext.Provider value={true}>
      <section data-area={area ?? ""}>
        {area === "welcome" ? <Board key="welcome" projectId="p" initialView={firstRun()} layout="welcome" /> : null}
        {area === "setup" ? <Board key="setup" projectId="p" initialView={firstRun()} layout="setup" /> : null}
      </section>
    </AreaShownContext.Provider>
  );
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  window.history.replaceState(null, "", "/?area=welcome");
  document.cookie = loadedAreasCookie("welcome,setup");
  fakeApi(firstRun());
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

it.each([["su-wl-model-go", "models", "su-model-drawer"]])(
  "%s opens Setup with its Add form", async (button, focus, opened) => {
    await act(async () => root.render(
      <ConsoleShell initialArea="welcome" loadedAreas={["welcome", "setup"]}
                    scope={{ project: "p", actor: "ceo" }}
                    status={{ apiReachable: true, budgetState: "green", deadletters: 0, pendingDecisions: 0 }}>
        <Areas />
      </ConsoleShell>));
    await settle();
    const go = host.querySelector<HTMLAnchorElement>(`[data-testid='${button}']`);
    expect(go, "Get started's button").not.toBeNull();
    await act(async () => { go!.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 })); });
    await settle();
    const now = new URLSearchParams(window.location.search);
    expect(now.get("area")).toBe("setup");
    expect(now.get("focus")).toBe(focus);
    expect(now.get("open")).toBe("add");
    expect(document.querySelector(`[data-testid='${opened}']`), "Setup's Add form").not.toBeNull();
  });

it("an old link (area=setup&focus=try) still opens Get started with Quick PR open", async () => {
  // The board switches areas itself here (no link click for the shell to carry): the switch must
  // name the new area in the URL first, or the shell drops `focus=try` on the way.
  window.history.replaceState(null, "", "/?area=setup&focus=try");
  await act(async () => root.render(
    <ConsoleShell initialArea="setup" loadedAreas={["welcome", "setup"]}
                  scope={{ project: "p", actor: "ceo" }}
                  status={{ apiReachable: true, budgetState: "green", deadletters: 0, pendingDecisions: 0 }}>
      <Areas />
    </ConsoleShell>));
  await settle();
  const now = new URLSearchParams(window.location.search);
  expect(now.get("area")).toBe("welcome");
  expect(now.get("focus")).toBe("try");
  expect((document.querySelector("[data-testid='su-wl-quick-pr']") as HTMLDetailsElement | null)?.open).toBe(true);
});
