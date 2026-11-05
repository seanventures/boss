// @vitest-environment happy-dom

// `/?org=x` reopens the area this browser last showed (localStorage), which the server did not
// render. The console must fetch it; it used to wait for `?area=` to name it, and sat on
// "Loading Get started…" until the operator reloaded or went to another page (2026-11-05).

import React, { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ refresh: vi.fn(), replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
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

let host: HTMLDivElement;
let root: Root;
const assign = vi.fn();
const replace = vi.fn();
const realLocation = window.location;

function stubLocation(path: string) {
  const url = new URL(path, "https://boss.test");
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...realLocation, href: url.toString(), search: url.search, pathname: url.pathname, hash: "",
      assign, replace },
  });
}

function Shell() {
  return <ConsoleShell initialArea="overview" loadedAreas={["overview"]}
    scope={{ project: "bare-url-fixture", actor: "owned-fixture" }}
    status={{ apiReachable: true, budgetState: "green", deadletters: 0, pendingDecisions: 0 }}>
    <section data-area="overview">overview</section>
  </ConsoleShell>;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  assign.mockReset();
  replace.mockReset();
  window.localStorage.clear();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  Object.defineProperty(window, "location", { configurable: true, value: realLocation });
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("fetches the remembered area when the URL names none", async () => {
  window.localStorage.setItem("boss.area", "welcome");
  stubLocation("/?org=org_boss");
  await act(async () => root.render(<Shell />));
  await act(async () => { vi.advanceTimersByTime(500); });
  expect(host.querySelector("[data-testid='console-area']")?.getAttribute("data-active")).toBe("welcome");
  expect(assign).toHaveBeenCalledWith("https://boss.test/?org=org_boss&area=welcome");
});

it("asks nothing when the remembered area is already on screen", async () => {
  window.localStorage.setItem("boss.area", "overview");
  stubLocation("/?org=org_boss");
  await act(async () => root.render(<Shell />));
  await act(async () => { vi.advanceTimersByTime(500); });
  expect(host.querySelector("[data-testid='area-loading']")).toBeNull();
  expect(assign).not.toHaveBeenCalled();
});
