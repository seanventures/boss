// @vitest-environment happy-dom
/**
 * Setup › GitHub access: the server's pill, the three ways, the App's manifest leaving for GitHub as
 * a form POST, the installation picker, and the one-click move of BOSS_GITHUB_TOKEN into the vault.
 * No response the row reads carries a credential, and the row never asks for one back.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { click, fakeApi, flush, mountBoard, mountInBoard, type FakeApi, type Mounted } from "./_helpers/setup-board";
import { GitHubAccessRow } from "../app/setup/github-access-row";
import { board, firstRun } from "./_helpers/setup-fixtures";

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
    mounted = await mountInBoard(board(), <GitHubAccessRow />);
    const pill = document.querySelector('[data-testid="gh-mode"]')!;
    expect(pill.textContent).toBe("Builds can't reach GitHub");
    expect(pill.className).toContain("su-tone-red");
    expect(document.querySelector('[data-testid="gh-way-app"]')!.textContent).toContain("Recommended");
    expect(document.querySelector('[data-testid="gh-way-token"]')!.textContent).toContain("Less safe");
    expect(document.querySelector('[data-testid="gh-as-me-link"]')!.getAttribute("href")).toBe("/account/agents");
    expect(document.querySelector('[data-testid="gh-env-token"]')).toBeNull();
  });

  it("Create the App hands the manifest to /github-app, the one page whose CSP lets a form reach GitHub", async () => {
    const api = fakeApi(board());
    withGitHub(api, NONE, (url, method) => method === "POST" && url.endsWith("/app/manifest")
      ? { post_url: "https://github.com/settings/apps/new?state=s1", manifest: { name: "BOSS x" }, state: "s1" } : null);
    const submitted: HTMLFormElement[] = [];
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (this: HTMLFormElement) { submitted.push(this); });
    const assign = vi.fn();
    const real = window.location;
    Object.defineProperty(window, "location", { configurable: true, value: { ...real, assign, href: real.href, origin: real.origin } });
    try {
      mounted = await mountInBoard(board(), <GitHubAccessRow />);
      await click(document.querySelector('[data-testid="gh-app-create"]'));
      await flush();
      // Not posted from Setup (its CSP is form-action 'self'): kept for this tab, then the hand-off opens.
      expect(submitted).toHaveLength(0);
      expect(assign).toHaveBeenCalledWith("/github-app");
      expect(JSON.parse(window.sessionStorage.getItem("boss:github-app-manifest") ?? "null"))
        .toEqual({ post_url: "https://github.com/settings/apps/new?state=s1", manifest: { name: "BOSS x" } });
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: real });
      window.sessionStorage.removeItem("boss:github-app-manifest");
    }
  });

  it("a created App offers Install and the picker, and choosing posts the installation", async () => {
    const api = fakeApi(board());
    const created = { ...NONE, app: { ...NONE.app, configured: true, slug: "boss-x", html_url: "https://github.com/apps/boss-x", source: "setup",
                                      links: { install: "https://github.com/apps/boss-x/installations/new" } } };
    withGitHub(api, created, (url) => url.endsWith("/app/installations")
      ? { installations: [{ id: "42", account: "acme", repository_selection: "selected" }] } : null);
    mounted = await mountInBoard(board(), <GitHubAccessRow />);
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
    mounted = await mountInBoard(board(), <GitHubAccessRow />);
    const move = document.querySelector('[data-testid="gh-env-move"]')!;
    await click(move);
    const posted = api.calls.find((c) => c.method === "POST" && c.url.endsWith("/token/migrate-env"));
    expect(posted?.body.repos).toEqual(board().repository.repos.map((r) => r.full_name));
    expect(JSON.stringify(api.calls.map((c) => c.body))).not.toMatch(/token":/);
  });

  it("an answer that is not a status reads as Not reported, never a broken row", async () => {
    const api = fakeApi(board());
    withGitHub(api, { something: "else" });
    mounted = await mountInBoard(board(), <GitHubAccessRow />);
    expect(document.querySelector('[data-testid="su-row-github-access"]')!.textContent).toContain("Not reported");
  });

  it("with the App installed, Repository lists what it can reach at once — no token, nothing to press", async () => {
    const view = firstRun();
    const api = fakeApi(view);
    const installed = { ...NONE, mode: "app",
      verdict: { tone: "green", label: "GitHub App · @acme", tip: "t" },
      app: { configured: true, installed: true, slug: "boss-x", html_url: "https://github.com/apps/boss-x",
             account: "acme", source: "setup" } };
    api.answer = (call) => {
      if (call.url.includes("/v1/identity/github")) return { status: 200, body: installed };
      if (call.op === "repo_app") {
        return { status: 200, body: { account_login: "acme", expires_at: null, connection_id: "ghconn_app",
                                      source: "github_app", rechecked: [],
                                      repos: [{ full_name: "acme/web", private: true, default_branch: "main",
                                                owner_type: "Organization" }] } };
      }
      return null;
    };
    mounted = await mountBoard(view);
    await flush();
    expect(api.calls.some((c) => c.op === "repo_app")).toBe(true);
    expect(document.querySelector('[data-testid="gh-connect-pick"]')!.textContent).toContain("GitHub connected · @acme");
    expect(document.querySelector('[data-testid="su-repo-option-acme/web"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="su-token-input"]')).toBeNull();
    expect(document.body.textContent).toContain("acme/web");
  });

  // VERIFY, DON'T TRUST: what GitHub says the installation was granted, each gap with its page.
  const PERSONAL_LINKS = { app: "https://github.com/apps/boss-x", install: "https://github.com/apps/boss-x/installations/new",
                           permissions: "https://github.com/settings/apps/boss-x/permissions",
                           installation: "https://github.com/settings/installations/42" };
  const installedPersonal = (verified: unknown) => ({ ...NONE, mode: "app",
    verdict: { tone: "green", label: "GitHub App · @paramg", tip: "t" },
    app: { configured: true, installed: true, slug: "boss-x", html_url: "https://github.com/apps/boss-x", account: "paramg",
           account_type: "User", owner: "paramg", owner_type: "User", source: "setup", links: PERSONAL_LINKS, verified } });

  it("shows what GitHub accepted: Workflows waiting with the installation's page, a repo the App is not on", async () => {
    const api = fakeApi(board());
    const verified = { state: "ok", ok: false, account: { login: "paramg", type: "User" }, repository_selection: "selected",
      permissions: [{ name: "contents", wanted: "write", granted: "write", state: "accepted", fix_url: null },
                    { name: "workflows", wanted: "write", granted: null, state: "waiting", fix_url: PERSONAL_LINKS.installation }],
      repos: [{ full_name: "paramg/boss-sandbox", installed: true, fix_url: null },
              { full_name: "paramg/other", installed: false, fix_url: PERSONAL_LINKS.installation }],
      links: PERSONAL_LINKS };
    // A stored answer with a gap is asked again on open; GitHub still says the same.
    withGitHub(api, installedPersonal(verified), (url, method) => (method === "POST" && url.endsWith("/app/verify") ? verified : null));
    mounted = await mountInBoard(board(), <GitHubAccessRow />);
    expect(api.calls.some((c) => c.url.endsWith("/app/verify"))).toBe(true);
    const wf = document.querySelector('[data-testid="gh-perm-workflows"]')!;
    expect(wf.getAttribute("data-state")).toBe("waiting");
    expect(wf.textContent).toContain("Workflows: waiting for you to accept");
    expect(document.querySelector('[data-testid="gh-perm-workflows-fix"]')!.getAttribute("href"))
      .toBe("https://github.com/settings/installations/42");
    const repos = [...document.querySelectorAll('[data-testid="gh-repo-verified"]')];
    expect(repos.map((r) => r.getAttribute("data-installed"))).toEqual(["true", "false"]);
    expect(repos[0].textContent).toContain("paramg/boss-sandbox ✓");
    expect(document.querySelector('[data-testid="gh-app-manage"]')!.getAttribute("href"))
      .toBe("https://github.com/settings/installations/42");
    expect(document.body.innerHTML).not.toContain("/organizations/paramg");
  });

  it("asks GitHub when it was never asked, and Check again asks again", async () => {
    const api = fakeApi(board());
    const answer = { state: "ok", ok: true, account: { login: "paramg", type: "User" },
      permissions: [{ name: "workflows", wanted: "write", granted: "write", state: "accepted", fix_url: null }],
      repos: [], links: PERSONAL_LINKS };
    withGitHub(api, installedPersonal(null), (url, method) => (method === "POST" && url.endsWith("/app/verify") ? answer : null));
    mounted = await mountInBoard(board(), <GitHubAccessRow />);
    await flush();
    expect(api.calls.filter((c) => c.url.endsWith("/app/verify")).length).toBe(1);
    expect(document.querySelector('[data-testid="gh-perm-workflows"]')!.textContent).toContain("Workflows: accepted ✓");
    await click(document.querySelector('[data-testid="gh-app-verify"]'));
    expect(api.calls.filter((c) => c.url.endsWith("/app/verify")).length).toBe(2);
  });

  it("never asks on its own while Setup is hidden, and an unasked failure is never a toast (KG round 6)", async () => {
    // "Reload and retry" over the Knowledge Graph: this card, mounted hidden in Setup, asked
    // GitHub on its own, the write was refused, and the refusal became a toast on another area.
    const api = fakeApi(board());
    api.answer = (call) => {
      if (!call.url.includes("/v1/identity/github")) return null;
      if (call.method === "POST" && call.url.endsWith("/app/verify")) return { status: 403, body: { error: "csrf_failed" } };
      return { status: 200, body: installedPersonal(null) };
    };
    const toasts: string[] = [];
    const on = (e: Event) => toasts.push(String((e as CustomEvent<{ message: string }>).detail.message));
    window.addEventListener("boss:toast", on);
    try {
      mounted = await mountInBoard(board(), <GitHubAccessRow />, false);
      await flush();
      expect(api.calls.filter((c) => c.url.endsWith("/app/verify"))).toEqual([]);
      mounted.unmount();
      mounted = await mountInBoard(board(), <GitHubAccessRow />);
      await flush();
      expect(api.calls.filter((c) => c.url.endsWith("/app/verify")).length).toBe(1);
      expect(toasts).toEqual([]);
      // A press is answered, refusal and all.
      await click(document.querySelector('[data-testid="gh-app-verify"]'));
      expect(toasts.length).toBe(1);
    } finally {
      window.removeEventListener("boss:toast", on);
    }
  });
});


describe("simple GitHub entry", () => {
  // Merged with github-simple (rc9 merge, scratchpad/rc9/MERGE.md): the one GitHub sign-in is
  // step ①, and the org's GitHub App lives in Advanced, so `focus=repository` lands on Repository
  // and `focus=github_app` on the App. What rc9 added inside the App row holds: the App first,
  // the other ways folded on a fresh install.
  it("keeps the App first and both alternatives intact in closed Other ways on a fresh install", async () => {
    const view = firstRun();
    view.secrets.needs_consent = true;
    const api = fakeApi(view);
    withGitHub(api, NONE);
    mounted = await mountInBoard(view, <GitHubAccessRow focused />);
    const alternatives = document.querySelector('[data-testid="gh-other-ways"]') as HTMLDetailsElement;
    expect(alternatives.open).toBe(false);
    expect(alternatives.querySelector("summary")?.textContent).toBe("Other ways");
    expect(document.querySelector('[data-testid="gh-way-app"]')!.textContent).toContain("A token per build, only for its repos, gone when it ends.");
    await click(alternatives.querySelector("summary"));
    expect(alternatives.open).toBe(true);
    expect(alternatives.textContent).toContain("Less safe");
    expect(alternatives.querySelector('[data-testid="gh-as-me-link"]')).not.toBeNull();
    mounted.unmount();
    // The Repository row is on the page without the App row (BOSSAPP-898: the App moved to a drawer).
    mounted = await mountBoard(view, "?area=setup&focus=repository");
    expect(document.querySelector('[data-testid="su-row-github-access"]')).toBeNull();
    const repo = document.querySelector('[data-testid="su-row-repository"]')!;
    // Repos opens on Connect GitHub; the token (and its storage consent) is under its Other ways.
    expect(repo.querySelector('[data-testid="gh-connect-start"]')).not.toBeNull();
    await click(repo.querySelector('[data-testid="su-use-token"]'));
    expect(repo.querySelector('[data-testid="su-consent"]')?.getAttribute("title")).toContain("Encrypted in Postgres");
    expect(api.calls.filter(x => x.method !== "GET")).toEqual([]);
  });

  for (const patch of [
    { people_connected: undefined }, { token: undefined }, { token: {} },
    { verdict: { tone: "grey", label: "Unknown", tip: "Not reported" } },
    { people_connected: 1 }, { mode: "as_me" }, { mode: "token", token: { repos: [], login: "me", migrated: false } },
    { app: { ...NONE.app, configured: true } }, { app: { ...NONE.app, configured: undefined } },
    { app: { ...NONE.app, verified: { state: "unreachable" } } },
    { env_token: { present: true, read_by_builds: false } },
  ]) {
    it(`keeps alternatives exposed for ${JSON.stringify(patch)}`, async () => {
      const api = fakeApi(firstRun());
      withGitHub(api, { ...NONE, ...patch });
      mounted = await mountInBoard(firstRun(), <GitHubAccessRow />);
      const alternatives = document.querySelector('[data-testid="gh-other-ways"]') as HTMLDetailsElement;
      expect(alternatives.open).toBe(true);
      expect(document.querySelector('[data-testid="gh-way-token"]')).not.toBeNull();
    });
  }

  it("connected repository deep links still land on Repository", async () => {
    const api = fakeApi(board()); withGitHub(api, NONE);
    const landed: HTMLElement[] = [];
    vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(function (this: HTMLElement) { landed.push(this); });
    mounted = await mountBoard(board(), "?area=setup&focus=repository");
    expect(landed[0]).toBe(document.querySelector('[data-testid="su-row-repository"]'));
    expect(document.querySelector('[data-testid="su-row-repository"]')?.getAttribute("data-focused")).toBe("true");
    // The App row is no longer on Setup's page at all; it opens from the GitHub accounts block.
    expect(document.querySelector('[data-testid="su-row-github-access"]')).toBeNull();
  });
});
