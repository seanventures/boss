// @vitest-environment happy-dom
/**
 * Setup › GitHub access: the server's pill, the three ways, the App's manifest leaving for GitHub as
 * a form POST, the installation picker, and the one-click move of BOSS_GITHUB_TOKEN into the vault.
 * No response the row reads carries a credential, and the row never asks for one back.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { click, fakeApi, flush, mountBoard, type FakeApi, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); });

const NONE = {
  mode: "none", verdict: { tone: "red", label: "Builds can't reach GitHub", tip: "Connect a GitHub App below." },
  app: { configured: false, installed: false, slug: null, html_url: null, account: null, source: null },
  token: null, people_connected: 0, env_token: { present: false, read_by_builds: false },
};

function withGitHub(api: FakeApi, status: unknown, extra: (url: string, method: string) => unknown = () => null) {
  api.answer = (call) => {
    if (!call.url.includes("/v1/identity/github")) return null;
    const special = extra(call.url, call.method);
    if (special) return { status: 200, body: special };
    return { status: 200, body: status };
  };
}

describe("GitHub access", () => {
  it("shows the server's pill and all three ways, the App marked recommended and the token less safe", async () => {
    const api = fakeApi(board());
    withGitHub(api, NONE);
    mounted = await mountBoard(board());
    const pill = document.querySelector('[data-testid="gh-mode"]')!;
    expect(pill.textContent).toBe("Builds can't reach GitHub");
    expect(pill.className).toContain("su-tone-red");
    expect(document.querySelector('[data-testid="gh-way-app"]')!.textContent).toContain("Recommended");
    expect(document.querySelector('[data-testid="gh-way-token"]')!.textContent).toContain("Less safe");
    expect(document.querySelector('[data-testid="gh-as-me-link"]')!.getAttribute("href")).toBe("/account/agents");
    expect(document.querySelector('[data-testid="gh-env-token"]')).toBeNull();
  });

  it("Create the App POSTs the manifest to GitHub as a form", async () => {
    const api = fakeApi(board());
    withGitHub(api, NONE, (url, method) => method === "POST" && url.endsWith("/app/manifest")
      ? { post_url: "https://github.com/settings/apps/new?state=s1", manifest: { name: "BOSS x" }, state: "s1" } : null);
    const submitted: HTMLFormElement[] = [];
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (this: HTMLFormElement) { submitted.push(this); });
    mounted = await mountBoard(board());
    await click(document.querySelector('[data-testid="gh-app-create"]'));
    await flush();
    expect(submitted).toHaveLength(1);
    expect(submitted[0].method.toUpperCase()).toBe("POST");
    expect(submitted[0].action).toBe("https://github.com/settings/apps/new?state=s1");
    const field = submitted[0].querySelector('input[name="manifest"]') as HTMLInputElement;
    expect(JSON.parse(field.value)).toEqual({ name: "BOSS x" });
  });

  it("a created App offers Install and the picker, and choosing posts the installation", async () => {
    const api = fakeApi(board());
    const created = { ...NONE, app: { ...NONE.app, configured: true, slug: "boss-x", html_url: "https://github.com/apps/boss-x", source: "setup" } };
    withGitHub(api, created, (url) => url.endsWith("/app/installations")
      ? { installations: [{ id: "42", account: "acme", repository_selection: "selected" }] } : null);
    mounted = await mountBoard(board());
    expect(document.querySelector('[data-testid="gh-app-install"]')!.getAttribute("href"))
      .toBe("https://github.com/apps/boss-x/installations/new");
    await click(document.querySelector('[data-testid="gh-app-pick"]'));
    const option = document.querySelector('[data-testid="gh-app-installations"] button')!;
    expect(option.textContent).toContain("@acme");
    await click(option);
    const posted = api.calls.find((c) => c.method === "POST" && c.url.endsWith("/app/installation"));
    expect(posted?.body).toEqual({ installation_id: "42" });
  });

  it("BOSS_GITHUB_TOKEN gets one button that moves it into the vault for the bound repos", async () => {
    const api = fakeApi(board());
    withGitHub(api, { ...NONE, env_token: { present: true, read_by_builds: false } });
    mounted = await mountBoard(board());
    const move = document.querySelector('[data-testid="gh-env-move"]')!;
    await click(move);
    const posted = api.calls.find((c) => c.method === "POST" && c.url.endsWith("/token/migrate-env"));
    expect(posted?.body.repos).toEqual(board().repository.repos.map((r) => r.full_name));
    expect(JSON.stringify(api.calls.map((c) => c.body))).not.toMatch(/token":/);
  });

  it("an answer that is not a status reads as Not reported, never a broken row", async () => {
    const api = fakeApi(board());
    withGitHub(api, { something: "else" });
    mounted = await mountBoard(board());
    expect(document.querySelector('[data-testid="su-row-github-access"]')!.textContent).toContain("Not reported");
  });
});
