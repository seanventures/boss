// @vitest-environment happy-dom

import React, { act, useEffect, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ refresh: vi.fn(), query: null as string | null }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(router.query ?? window.location.search),
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
let serverDelayMs: number;
let omitNextArea: boolean;

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
  router.refresh.mockImplementation(() => {
    const requested = new URLSearchParams(window.location.search).get("area") ?? "overview";
    const cookie = loadedAreasCookieValue(document.cookie);
    const omitted = omitNextArea;
    omitNextArea = false;
    const deliver = () => setLoaded(omitted ? ["overview"] : [...areasToRender({
      requested, cookie, fullDocument: false,
    })]);
    if (serverDelayMs) window.setTimeout(deliver, serverDelayMs);
    else deliver();
  });
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
  router.query = null;
  draftMounts = 0;
  draftUnmounts = 0;
  serverDelayMs = 0;
  omitNextArea = false;
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

// A router refresh is dispatched synchronously; a shell transition can settle before its
// RSC response arrives. Only actual server props may acknowledge the refresh.
it("waits for server data before retrying an unloaded area", async () => {
  serverDelayMs = 1500;
  await act(async () => root.render(<ServerBoundary />));
  const draft = host.querySelector<HTMLTextAreaElement>("textarea[aria-label='Unsaved plan']")!;
  await act(async () => {
    draft.value = "Keep this unsubmitted plan";
    draft.dispatchEvent(new Event("input", { bubbles: true }));
    window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" }));
  });
  expect(host.querySelector("[data-testid='area-loading']")).not.toBeNull();
  await act(async () => vi.advanceTimersByTime(1000));
  expect(router.refresh).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTime(500));
  expect(host.querySelector("[data-testid='area-loading']")).toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(1);
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draft.value).toBe("Keep this unsubmitted plan");
  expect(draftMounts).toBe(1);
  expect(draftUnmounts).toBe(0);
});

it("coalesces a second area until the first server response commits", async () => {
  serverDelayMs = 1500;
  await act(async () => root.render(<ServerBoundary />));
  const draft = host.querySelector<HTMLTextAreaElement>("textarea[aria-label='Unsaved plan']")!;
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  await act(async () => vi.advanceTimersByTime(250));
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "setup" })));
  expect(router.refresh).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTime(1250));
  expect(host.querySelector("[data-testid='area-loading']")).not.toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(2);
  await act(async () => vi.advanceTimersByTime(1500));
  expect(host.querySelector("[data-testid='area-loading']")).toBeNull();
  expect(host.querySelector("[data-testid='console-area']")?.getAttribute("data-active")).toBe("setup");
  expect(router.refresh).toHaveBeenCalledTimes(2);
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draftMounts).toBe(1);
  expect(draftUnmounts).toBe(0);
});

it("retries when newly committed server data still omits the requested area", async () => {
  serverDelayMs = 100;
  omitNextArea = true;
  await act(async () => root.render(<ServerBoundary />));
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  await act(async () => vi.advanceTimersByTime(100));
  expect(host.querySelector("[data-testid='area-loading']")).not.toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTime(250));
  expect(router.refresh).toHaveBeenCalledTimes(2);
  await act(async () => vi.advanceTimersByTime(100));
  expect(host.querySelector("[data-testid='area-loading']")).toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(2);
});

it("waits for the router URL snapshot before starting the first-area refresh", async () => {
  serverDelayMs = 1500;
  router.query = "area=overview";
  await act(async () => root.render(<ServerBoundary />));
  const draft = host.querySelector<HTMLTextAreaElement>("textarea[aria-label='Unsaved plan']")!;
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  expect(host.querySelector("[data-testid='console-area']")?.getAttribute("data-active")).toBe("decisions");
  await act(async () => vi.advanceTimersByTime(1000));
  expect(router.refresh).not.toHaveBeenCalled();
  router.query = window.location.search;
  await act(async () => root.render(<ServerBoundary />));
  expect(router.refresh).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTime(1500));
  expect(host.querySelector("[data-testid='area-loading']")).toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(1);
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draftMounts).toBe(1);
  expect(draftUnmounts).toBe(0);
});
