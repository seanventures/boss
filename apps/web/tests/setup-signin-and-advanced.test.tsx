// @vitest-environment happy-dom
/**
 * Repo and models, simplified: ① GitHub (the one sign-in) ② Repos ③ Model ④ Try it, and everything
 * else moved out (BOSSAPP-898): Setup holds ONLY Repos and Models. The GitHub App, model routing, the
 * repository menus and CI toggles open in drawers from their blocks; the budget, allowed repos and the
 * embedding model live in Govern and Knowledge, and old links go there.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { click, fakeApi, flush, mountBoard, q, type FakeApi, type Mounted } from "./_helpers/setup-board";
import { board as plainBoard, firstRun } from "./_helpers/setup-fixtures";
import type { SetupView } from "../lib/setup-types";

const board = plainBoard;

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

const SIGNED_OUT = { configured: true, slug: "boss-hq", install_url: "https://github.com/apps/boss-hq/installations/new",
                     verification_uri: "https://github.com/login/device", me: null };
const SIGNED_IN = { ...SIGNED_OUT, me: { id: "vlt_1", login: "octo-dev", status: "active", linked_at: null, via_signin: true } };
const UNCONFIGURED = { ...SIGNED_OUT, configured: false, slug: null, install_url: null };

function withSignin(api: FakeApi, status: unknown, extra: (url: string, op: string | null) => unknown = () => null) {
  api.answer = (call) => {
    const special = extra(call.url, call.op);
    if (special) return { status: 200, body: special };
    if (call.url.includes("/v1/identity/github/signin")) return { status: 200, body: status };
    return null;
  };
}

describe("the order a new person sees", () => {
  // BOSSAPP-808: Try it left Setup for the Welcome (PARITY-808.md) — three steps.
  it("① GitHub, ② Repos, ③ Model — and no Advanced", async () => {
    const api = fakeApi(firstRun());
    withSignin(api, SIGNED_OUT);
    mounted = await mountBoard(firstRun());
    await flush();
    const main = [...document.querySelectorAll("[data-testid=su-board] > section")].map((s) => s.getAttribute("data-testid"));
    expect(main).toEqual(["su-row-github", "su-row-repository", "su-row-models"]);
    const steps = [...document.querySelectorAll("[data-testid=su-board] > section .su-step")].map((s) => s.textContent);
    expect(steps).toEqual(["①", "②", "③"]);
    expect(q(document.body, "su-advanced")).toBeNull();
    expect(q(document.body, "gh-signin-start")!.textContent).toContain("Sign in with GitHub");
    expect(q(document.body, "su-signin-first")).not.toBeNull();     // Repos waits for the sign-in
    expect(overBudget(document.body)).toEqual([]);
  });

  it("without sign-in on this install there is no ① GitHub row: Repos is ① and says how to connect", async () => {
    const api = fakeApi(firstRun());
    withSignin(api, UNCONFIGURED);
    mounted = await mountBoard(firstRun());
    await flush();
    expect(q(document.body, "gh-signin-unconfigured")).toBeNull();   // no "advanced path" jargon
    expect(document.body.textContent).not.toContain("advanced path");
    const steps = [...document.querySelectorAll("[data-testid=su-board] > section .su-step")].map((s) => s.textContent);
    expect(steps[0]).toBe("①");
    // GitHub access unreadable in this fake: the token path, exactly as before.
    expect(q(document.body, "su-token-form")).not.toBeNull();
  });
});

describe("parity: every capability still reachable (BOSSAPP-898)", () => {
  it("Setup's page is only Repos and Models; the moved rows are not on it and there is no Advanced", async () => {
    const api = fakeApi(board());
    withSignin(api, SIGNED_IN);
    mounted = await mountBoard(board());
    await flush();
    for (const id of ["su-row-repository", "su-row-models"]) expect(q(document.body, id), id).not.toBeNull();
    for (const id of ["su-advanced", "su-advanced-toggle", "su-advanced-all", "su-advanced-all-toggle", "su-open-advanced",
                      "su-row-github-access", "su-row-jobs", "su-row-budget", "su-row-ci"]) {
      expect(q(document.body, id), id).toBeNull();
    }
    expect(document.querySelector('[data-testid^="su-adv-why-"]')).toBeNull();
    // the CI opt-in is not on the repo line either; it is in the repository menus drawer
    expect(q(document.body, "su-row-repository")!.querySelector("[data-testid^=su-ci-workflows]")).toBeNull();
  });

  for (const [focus, drawer, row] of [["jobs", "su-routing-drawer", "su-row-jobs"],
                                      ["github_app", "su-github-app-drawer", "su-row-github-access"],
                                      ["ci", "su-manage-repos-drawer", "su-row-ci"],
                                      ["capacity", "su-manage-models-drawer", "su-row-capacity"],
                                      ["embedding_model", "su-manage-models-drawer", "su-row-embedding-model"]] as const) {
    it(`?focus=${focus} opens its drawer on the Setup board`, async () => {
      const api = fakeApi(board());
      withSignin(api, SIGNED_IN);
      mounted = await mountBoard(board(), `?area=setup&focus=${focus}`, true, "setup");
      const d = q(document.body, drawer);
      expect(d, drawer).not.toBeNull();
      // (the embedding row itself needs the server's slot; setup-embedding-model.test covers its content)
      if (focus !== "embedding_model") expect(d!.querySelector(`[data-testid="${row}"]`), row).not.toBeNull();
      expect(q(document.body, "su-advanced")).toBeNull();
    });
  }

  for (const [focus, to] of [["budget", "?area=govern&tab=limits"], ["repositories", "?area=govern&tab=repositories"]] as const) {
    it(`?focus=${focus} goes to ${to}`, async () => {
      const api = fakeApi(board());
      withSignin(api, SIGNED_IN);
      const replace = vi.spyOn(window.location, "replace").mockImplementation(() => {});
      mounted = await mountBoard(board(), `?area=setup&focus=${focus}`);
      expect(replace).toHaveBeenCalledWith(to);
      replace.mockRestore();
    });
  }

  for (const [query, say] of [["github=app_connected", "GitHub connected"],
                              ["github=app_failed", "GitHub was not connected. Start again."]] as const) {
    it(`GitHub sending the person back (${query}) says so once and drops it from the URL`, async () => {
      const api = fakeApi(board());
      withSignin(api, SIGNED_IN);
      const said: string[] = [];
      const on = (e: Event) => said.push(String((e as CustomEvent<{ message: string }>).detail.message));
      window.addEventListener("boss:toast", on);
      try {
        mounted = await mountBoard(board(), `?area=setup&focus=repository&${query}`);
        expect(said).toEqual([say]);
        expect(window.location.search).not.toContain("github=");
      } finally {
        window.removeEventListener("boss:toast", on);
      }
    });
  }
});

describe("sign in, then pick repos", () => {
  it("device flow: the code, then signed in, with no second trip for 'as me'", async () => {
    const api = fakeApi(firstRun());
    let polls = 0;
    let approved = false;              // the person, on github.com/login/device
    let status: unknown = SIGNED_OUT;
    withSignin(api, null, (url) => {
      if (url.includes("/v1/identity/github/signin")) return status;
      if (url.endsWith("/device/start")) return { user_code: "BOSS-0042", verification_uri: "https://github.com/login/device",
                                                  expires_in: 900, interval: 0, handle: "sealed" };
      if (url.endsWith("/device/poll")) {
        polls += 1;
        if (!approved) return { state: "pending", interval: 0 };
        status = SIGNED_IN;
        return { state: "connected", login: "octo-dev" };
      }
      return null;
    });
    const reach = api.answer;
    api.answer = (call) => call.op === "repo_signin"
      ? { status: 200, body: { account_login: "octo-dev", expires_at: null, connection_id: "c1", repos: [],
                               install_needed: [] } }
      : reach(call);
    mounted = await mountBoard(firstRun());
    await flush();
    await click(q(document.body, "gh-signin-start"));
    expect(q(document.body, "gh-signin-code")!.textContent).toBe("BOSS-0042");
    expect(q(document.body, "gh-signin-open")!.getAttribute("href")).toBe("https://github.com/login/device");
    expect(polls).toBeGreaterThan(0);
    expect(q(document.body, "gh-signin-done")).toBeNull();
    approved = true;
    for (let i = 0; i < 6 && !q(document.body, "gh-signin-done"); i += 1) await flush();
    expect(q(document.body, "gh-signin-done")!.textContent).toContain("@octo-dev");
    const poll = api.calls.find((c) => c.url.endsWith("/device/poll"))!;
    expect(poll.body).toEqual({ handle: "sealed" });
  });

  it("an expired code says so and offers Try again", async () => {
    const api = fakeApi(firstRun());
    withSignin(api, SIGNED_OUT, (url) => url.endsWith("/device/start")
      ? { user_code: "BOSS-1", verification_uri: "https://github.com/login/device", expires_in: 900, interval: 0, handle: "h" }
      : url.endsWith("/device/poll") ? { state: "expired", error: "expired_token" } : null);
    mounted = await mountBoard(firstRun());
    await flush();
    await click(q(document.body, "gh-signin-start"));
    for (let i = 0; i < 4 && !q(document.body, "gh-signin-stopped"); i += 1) await flush();
    expect(q(document.body, "gh-signin-stopped")!.getAttribute("data-why")).toBe("expired");
    expect(q(document.body, "gh-signin-start")!.textContent).toContain("Try again");
  });

  it("Repos lists only what the server says is yours, and names each install needed", async () => {
    const api = fakeApi(firstRun());
    withSignin(api, SIGNED_IN, (_url, op) => op === "repo_signin" ? {
      account_login: "octo-dev", expires_at: null, connection_id: "c1", source: "github_signin", read_only: 1,
      repos: [{ full_name: "acme/web", private: true, default_branch: "main", owner_type: "Organization" }],
      install_needed: [{ account: "globex", type: "Organization",
                         install_url: "https://github.com/apps/boss-hq/installations/new/permissions?target_id=9002" }],
    } : null);
    mounted = await mountBoard(firstRun());
    await flush();
    expect(q(document.body, "su-signin-picker")).not.toBeNull();
    expect(q(document.body, "su-repo-option-acme/web")).not.toBeNull();
    expect(document.querySelectorAll("[data-testid^=su-repo-option-]").length).toBe(1);
    const install = q(document.body, "gh-install-globex")!;
    expect(install.textContent).toContain("Install BOSS on globex");
    expect(install.getAttribute("href")).toContain("target_id=9002");
    expect(q(document.body, "su-signin-read-only")!.textContent).toBe("1 read-only hidden");
    await click(q(document.body, "su-repo-option-acme/web"));
    await click(q(document.body, "su-repo-connect"));
    const select = api.calls.find((c) => c.op === "repo_select")!;
    expect(select.body).toMatchObject({ connection_id: "c1", full_name: "acme/web" });
    // a token is still one click away
    expect(q(document.body, "su-use-token")).not.toBeNull();
  });
});
