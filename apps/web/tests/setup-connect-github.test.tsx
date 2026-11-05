// @vitest-environment happy-dom
/**
 * BOSSAPP-815 — Repos opens on ONE "Connect GitHub": this install's own GitHub App, created and
 * installed in two clicks on GitHub, whose access never expires (egress mints a fresh token per
 * call). Only a fresh setup gets it; an install already on a token keeps the token path, and an
 * unreadable GitHub status is never taken for a fresh one.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { NEXT_STEPS } from "../app/setup/connect-github";
import { click, fakeApi, flush, mountBoard, q, type, type FakeApi, type Mounted } from "./_helpers/setup-board";
import { board, firstRun } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); document.body.innerHTML = ""; });

const NONE = {
  mode: "none", verdict: { tone: "red", label: "Builds can't reach GitHub", tip: "t" },
  app: { configured: false, installed: false, slug: null, html_url: null, account: null, source: null },
  token: null, people_connected: 0, env_token: { present: false, read_by_builds: false },
};
const MANIFEST = { post_url: "https://github.com/settings/apps/new?state=s1", manifest: { name: "BOSS x" }, state: "s1" };

function withGitHub(api: FakeApi, status: unknown) {
  api.answer = (call) => {
    if (call.method === "POST" && call.url.endsWith("/app/manifest")) return { status: 200, body: MANIFEST };
    if (call.url.includes("/v1/identity/github/signin")) return { status: 200, body: { configured: false, me: null } };
    if (call.url.includes("/v1/identity/github")) return { status: 200, body: status };
    return null;
  };
}

async function withNavigation(run: (assign: ReturnType<typeof vi.fn>) => Promise<void>) {
  const assign = vi.fn();
  const real = window.location;
  Object.defineProperty(window, "location", { configurable: true, value: { ...real, assign, href: real.href, origin: real.origin } });
  try { await run(assign); } finally {
    Object.defineProperty(window, "location", { configurable: true, value: real });
    window.sessionStorage.removeItem("boss:github-app-manifest");
  }
}

describe("a fresh install: Connect GitHub", () => {
  it("says what happens on GitHub, then hands the manifest for the person's own account", async () => {
    const api = fakeApi(firstRun());
    withGitHub(api, NONE);
    await withNavigation(async (assign) => {
      mounted = await mountBoard(firstRun());
      await flush();
      const start = q(document.body, "gh-connect-start")!;
      expect(start.textContent).toContain("My GitHub account");
      expect([...q(start, "gh-connect-steps")!.querySelectorAll("li")].map((l) => l.textContent)).toEqual([...NEXT_STEPS]);
      expect(q(document.body, "su-token-input")).toBeNull();          // no token on the first screen
      expect(q(document.body, "gh-connect-go")!.getAttribute("title")).toContain("never expires");
      await click(q(document.body, "gh-connect-go"));
      await flush();
      const call = api.calls.find((c) => c.method === "POST" && c.url.endsWith("/app/manifest"))!;
      expect(call.body).toEqual({});
      expect(assign).toHaveBeenCalledWith("/github-app");
    });
  });

  it("an organisation needs its name, and the App is created there", async () => {
    const api = fakeApi(firstRun());
    withGitHub(api, NONE);
    await withNavigation(async () => {
      mounted = await mountBoard(firstRun());
      await flush();
      await click(q(document.body, "gh-where-org")!.querySelector("input"));
      await click(q(document.body, "gh-connect-go"));
      expect(q(document.body, "su-refusal")!.textContent).toContain("organisation's name");
      expect(api.calls.some((c) => c.url.endsWith("/app/manifest"))).toBe(false);
      await type(q(document.body, "gh-connect-org"), "acme");
      await click(q(document.body, "gh-connect-go"));
      await flush();
      expect(api.calls.find((c) => c.url.endsWith("/app/manifest"))!.body).toEqual({ owner: "acme" });
    });
  });

  it("created but not installed: one button to install it on GitHub", async () => {
    const api = fakeApi(firstRun());
    withGitHub(api, { ...NONE, app: { ...NONE.app, configured: true, slug: "boss-x", source: "setup",
                                      links: { install: "https://github.com/apps/boss-x/installations/new" } } });
    mounted = await mountBoard(firstRun());
    await flush();
    expect(q(document.body, "gh-connect-install-go")!.getAttribute("href")).toBe("https://github.com/apps/boss-x/installations/new");
  });
});

describe("never a new App over an existing connection", () => {
  it("an install on a token keeps the token path", async () => {
    const api = fakeApi(firstRun());
    withGitHub(api, { ...NONE, mode: "token", token: { repos: ["o/r"], login: "me", migrated: false } });
    mounted = await mountBoard(firstRun());
    await flush();
    expect(q(document.body, "gh-connect")).toBeNull();
    expect(q(document.body, "su-token-input")).not.toBeNull();
  });

  it("an unreadable GitHub status is not a fresh setup", async () => {
    const api = fakeApi(firstRun());
    api.answer = (call) => (call.url.includes("/v1/identity/github") ? { status: 500, body: { error: "x" } } : null);
    mounted = await mountBoard(firstRun());
    await flush();
    expect(q(document.body, "gh-connect-start")).toBeNull();
    expect(q(document.body, "su-token-input")).not.toBeNull();
  });

  it("Add a GitHub account with accounts already connected keeps the token as the way", async () => {
    const api = fakeApi(board());
    withGitHub(api, NONE);
    mounted = await mountBoard(board());
    await flush();
    expect(document.body.textContent).not.toContain("advanced path");
  });
});
