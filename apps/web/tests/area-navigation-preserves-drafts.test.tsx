// @vitest-environment happy-dom

import React, { act, useEffect, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ refresh: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

// Keep the actual shell, area selection, cookies, coordinator and React reconciliation.
// These unrelated chrome/service leaves must not fetch or require a signed-in API fixture.
vi.mock("../lib/use-agent-identities", () => ({ useLoadIdentities: () => undefined }));
vi.mock("../lib/agents-live", () => ({ useAttention: () => null }));
vi.mock("../app/agent-ui", () => ({ AgentChat: () => null }));
vi.mock("../app/project-strip", () => ({ ProjectStrip: () => null }));
vi.mock("../app/agents/presence-pill", () => ({ PresencePill: () => null }));
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
    useIdentity: () => identity,
    useCan: () => allowed,
  };
});

import ConsoleShell from "../app/ConsoleShell";
import {
  areasToRender, loadedAreasCookie, loadedAreasCookieValue, parseLoadedAreas,
} from "../lib/area-loading";

let host: HTMLDivElement;
let root: Root;
let draftMounts: number;
let draftUnmounts: number;

function Draft() {
  const [draft, setDraft] = useState("");
  useEffect(() => {
    draftMounts += 1;
    return () => { draftUnmounts += 1; };
  }, []);
  return <textarea aria-label="Unsaved plan" value={draft}
    onInput={(event) => setDraft(event.currentTarget.value)} />;
}

function ServerBoundary() {
  const [loaded, setLoaded] = useState(() => [...areasToRender({
    requested: "overview", cookie: loadedAreasCookieValue(document.cookie), fullDocument: true,
  })]);
  // The RSC transport is a service double. Its selection rule is the actual page's rule;
  // omitting an area removes its subtree, as the server's shows(area) gate does.
  router.refresh.mockImplementation(() => setLoaded([...areasToRender({
    requested: new URLSearchParams(window.location.search).get("area") ?? "overview",
    cookie: loadedAreasCookieValue(document.cookie), fullDocument: false,
  })]));
  return <ConsoleShell initialArea="overview" loadedAreas={loaded}
    scope={{ project: "owned-navigation-fixture", actor: "owned-fixture" }}
    status={{ apiReachable: true, budgetState: "green", deadletters: 0, pendingDecisions: 0 }}>
    {loaded.map((area) => <section key={area} data-area={area}>
      {area === "overview" ? <Draft /> : <span>{area}</span>}
    </section>)}
  </ConsoleShell>;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  router.refresh.mockReset();
  draftMounts = 0;
  draftUnmounts = 0;
  window.localStorage.clear();
  window.history.replaceState(null, "", "/?area=overview");
  document.cookie = loadedAreasCookie("");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.clearAllTimers();
  vi.useRealTimers();
});

it.each(["", "setup"])("retains the initial area's mounted draft with remembered areas %j", async (cookie) => {
  document.cookie = loadedAreasCookie(cookie);
  await act(async () => root.render(<ServerBoundary />));
  const draft = host.querySelector<HTMLTextAreaElement>("textarea[aria-label='Unsaved plan']")!;
  await act(async () => {
    draft.value = "A plan that has not been submitted";
    draft.dispatchEvent(new Event("input", { bubbles: true }));
  });

  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  expect(host.querySelector("[data-testid='console-area']")?.getAttribute("data-active")).toBe("decisions");
  expect(host.querySelector("[data-testid='area-loading']")).toBeNull();
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draft.value).toBe("A plan that has not been submitted");
  expect(draftMounts).toBe(1);
  expect(draftUnmounts).toBe(0);
  expect(router.refresh).toHaveBeenCalledTimes(1);
  expect(parseLoadedAreas(loadedAreasCookieValue(document.cookie))).toEqual(
    expect.arrayContaining(["overview", "decisions", ...(cookie ? [cookie] : [])]),
  );

  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "overview" })));
  expect(host.querySelector("[data-testid='console-area']")?.getAttribute("data-active")).toBe("overview");
  expect(host.querySelector("[data-testid='area-loading']")).toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(1);
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draft.value).toBe("A plan that has not been submitted");
  expect(draftUnmounts).toBe(0);
});
