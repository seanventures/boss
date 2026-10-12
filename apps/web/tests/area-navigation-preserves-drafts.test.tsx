// @vitest-environment happy-dom

import React, { act, useEffect, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ refresh: vi.fn(), replace: vi.fn(), query: null as string | null }));
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
import { requestConsoleRefresh } from "../lib/console-refresh";
import {
  areasToRender, loadedAreasCookie, loadedAreasCookieValue, parseLoadedAreas,
} from "../lib/area-loading";

let host: HTMLDivElement;
let root: Root;
let draftMounts: number;
let draftUnmounts: number;
let serverDelayMs: number;
let omitNextArea: boolean;
let discardRestoredResponse: boolean;
let commitServer: ((loaded: string[]) => void) | null = null;
const fixtureStatus = { apiReachable: true, budgetState: "green", deadletters: 0, pendingDecisions: 0 };

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
  commitServer = setLoaded;
  // The RSC transport is a service double. Its selection rule is the actual page's rule;
  // omitting an area removes its subtree, as the server's shows(area) gate does.
  router.refresh.mockImplementation(() => {
    const requested = new URLSearchParams(window.location.search).get("area") ?? "overview";
    const cookie = loadedAreasCookieValue(document.cookie);
    const omitted = omitNextArea;
    omitNextArea = false;
    const url = window.location.search;
    const deliver = () => {
      // Next's ACTION_RESTORE drops an older pending refresh, including a successful
      // response. A discarded response must never update this server-prop boundary.
      if (discardRestoredResponse && window.location.search !== url) return;
      setLoaded(omitted ? ["overview"] : [...areasToRender({
        requested, cookie, fullDocument: false,
      })]);
    };
    if (serverDelayMs) window.setTimeout(deliver, serverDelayMs);
    else deliver();
  });
  return <ConsoleShell initialArea="overview" loadedAreas={loaded}
    scope={{ project: "owned-navigation-fixture", actor: "owned-fixture" }}
    status={fixtureStatus}>
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
  router.replace.mockReset();
  router.query = null;
  // The document this console loaded with took as long as the server double answers: the
  // shell seeds its refresh watchdog with it (navigation timing), as on a real install.
  vi.spyOn(performance, "getEntriesByType").mockImplementation(
    ((type: string) => (type === "navigation"
      ? [{ requestStart: 0, responseEnd: serverDelayMs }] : [])) as unknown as typeof performance.getEntriesByType);
  draftMounts = 0;
  draftUnmounts = 0;
  serverDelayMs = 0;
  omitNextArea = false;
  discardRestoredResponse = true;
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
  vi.restoreAllMocks();
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

// MERGED WITH THE LATENCY FIX (scratchpad/rc9/MERGE.md, "rc9's navigation tests"). On
// integration/next an area not yet in the document opens with a page load unless a draft is on
// screen, a rail click's URL follows once its area arrives, and a refresh Next never completes is
// re-issued by a watchdog. Each case below keeps rc9's name and the guarantee it guards, asserted
// on that design; the refresh path is the one a draft takes, so each types one first.
async function typeDraft() {
  const draft = host.querySelector<HTMLTextAreaElement>("textarea[aria-label='Unsaved plan']")!;
  await act(async () => {
    draft.value = draft.value || "An unsubmitted plan";
    draft.dispatchEvent(new Event("input", { bubbles: true }));
  });
  return draft;
}
const activeArea = () => host.querySelector("[data-testid='console-area']")?.getAttribute("data-active");
const loading = () => host.querySelector("[data-testid='area-loading']");

// A router refresh is dispatched synchronously; a shell transition can settle before its
// RSC response arrives. Only actual server props may acknowledge the refresh — and a refresh
// that is only slow (the server takes 1.5 s, as the page load measured) is neither retried nor
// unstuck while it is still being answered.
it("waits for server data before retrying an unloaded area", async () => {
  serverDelayMs = 1500;
  await act(async () => root.render(<ServerBoundary />));
  const draft = await typeDraft();
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  expect(loading()).not.toBeNull();
  await act(async () => vi.advanceTimersByTime(1000));
  expect(router.refresh).toHaveBeenCalledTimes(1);
  expect(router.replace).not.toHaveBeenCalled();          // no unstick of a refresh still in flight
  await act(async () => vi.advanceTimersByTime(500));
  expect(loading()).toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(1);
  expect(window.location.search).toBe("?area=decisions");  // the deferred URL followed the area
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draftMounts).toBe(1);
  expect(draftUnmounts).toBe(0);
});

// rc9 discards the first read when the URL moves on. Here the URL waits for the area, so the
// first read is never discarded; the guarantee is the same: the area asked for LAST is the one
// that loads and is shown, with never two reads in flight and the draft kept.
it("loads a second area when the router discards the first area's response", async () => {
  serverDelayMs = 1500;
  await act(async () => root.render(<ServerBoundary />));
  const draft = await typeDraft();
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  await act(async () => vi.advanceTimersByTime(250));
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "setup" })));
  expect(router.refresh).toHaveBeenCalledTimes(1);          // owed, not alongside the first
  await act(async () => vi.advanceTimersByTime(1250));
  expect(activeArea()).toBe("setup");                       // the first read never switches back
  expect(loading()).not.toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(2);          // the owed read, once the first landed
  expect(window.location.search).toBe("?area=overview");    // no URL for an area not on screen
  await act(async () => vi.advanceTimersByTime(1500));
  expect(loading()).toBeNull();
  expect(activeArea()).toBe("setup");
  expect(window.location.search).toBe("?area=setup");
  expect(router.refresh).toHaveBeenCalledTimes(2);
  expect(router.replace).not.toHaveBeenCalled();
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draftMounts).toBe(1);
  expect(draftUnmounts).toBe(0);
});

// Back to a mounted area while another is still loading: it is shown at once, the read in
// flight is not duplicated, and when that read lands it neither switches the area nor writes
// its URL over the one the operator returned to.
it("replaces a discarded read when returning to an already mounted area", async () => {
  serverDelayMs = 1500;
  await act(async () => root.render(<ServerBoundary />));
  const draft = await typeDraft();
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  await act(async () => vi.advanceTimersByTime(250));
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "overview" })));
  expect(loading()).toBeNull();
  expect(activeArea()).toBe("overview");
  expect(router.refresh).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTime(1500));
  expect(activeArea()).toBe("overview");
  expect(window.location.search).toBe("?area=overview");
  expect(router.refresh).toHaveBeenCalledTimes(1);
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draftMounts).toBe(1);
  expect(draftUnmounts).toBe(0);
});

it("retries when newly committed server data still omits the requested area", async () => {
  serverDelayMs = 100;
  omitNextArea = true;
  await act(async () => root.render(<ServerBoundary />));
  await typeDraft();
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

// Back/Forward: the browser's URL changes before Next's router commits it. No area read starts
// until the router's URL snapshot names the area (a read issued earlier is one Next discards).
it("waits for the router URL snapshot before starting the first-area refresh", async () => {
  serverDelayMs = 1500;
  router.query = "area=overview";
  await act(async () => root.render(<ServerBoundary />));
  const draft = await typeDraft();
  await act(async () => {
    window.history.pushState(null, "", "/?area=decisions");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await act(async () => vi.advanceTimersByTime(1000));
  expect(router.refresh).not.toHaveBeenCalled();
  router.query = window.location.search;
  await act(async () => root.render(<ServerBoundary />));
  expect(activeArea()).toBe("decisions");
  expect(router.refresh).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTime(1500));
  expect(loading()).toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(1);
  expect(host.querySelector("textarea[aria-label='Unsaved plan']")).toBe(draft);
  expect(draftMounts).toBe(1);
  expect(draftUnmounts).toBe(0);
});

// URL filters and server props can commit together while a read is pending. A restore while
// the area's read is in flight makes Next discard that read: the shell cancels it and reads
// again for the restored URL, and the filter the restore brought is kept in the URL.
it("replaces a discarded first-area read after a same-area query filter restore", async () => {
  serverDelayMs = 1500;
  await act(async () => root.render(<ServerBoundary />));
  await typeDraft();
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  await act(async () => vi.advanceTimersByTime(250));
  await act(async () => {
    window.history.replaceState(null, "", "/?area=decisions&state=pending");
    root.render(<ServerBoundary />);
  });
  expect(router.refresh).toHaveBeenCalledTimes(2);
  await act(async () => vi.advanceTimersByTime(1500));
  expect(loading()).toBeNull();
  expect(router.refresh).toHaveBeenCalledTimes(2);
  expect(window.location.search).toBe("?area=decisions&state=pending");
});

// The old read's props reaching the router in the same commit as a URL restore belong to the
// previous query: they must not acknowledge the replacement read, and the deferred rail-click
// URL must not overwrite the restored one.
it("does not acknowledge a replacement read with server props committed alongside URL restore", async () => {
  serverDelayMs = 1500;
  await act(async () => root.render(<ServerBoundary />));
  await typeDraft();
  await act(async () => window.dispatchEvent(new CustomEvent("boss:navigate", { detail: "decisions" })));
  expect(router.refresh).toHaveBeenCalledTimes(1);
  await act(async () => {
    // The earlier result reached the router before RESTORE; React batches both state updates.
    // It belongs to the previous query, not the replacement started by the restore effect.
    commitServer!(["overview", "decisions"]);
    window.history.replaceState(null, "", "/?area=decisions&state=pending");
    root.render(<ServerBoundary />);
  });
  expect(router.refresh).toHaveBeenCalledTimes(2);
  await act(async () => requestConsoleRefresh(router, "now"));
  expect(router.refresh).toHaveBeenCalledTimes(2); // Current replacement should still be busy.
  expect(window.location.search).toBe("?area=decisions&state=pending");
});
