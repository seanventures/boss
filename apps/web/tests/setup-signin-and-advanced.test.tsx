// @vitest-environment happy-dom
/**
 * Repo and models, simplified: ① GitHub (the one sign-in) ② Repos ③ Model ④ Try it, and everything
 * else folded under Advanced — with every old row still on the page (parity), and a deep link to an
 * Advanced row opening the fold.
 */
import { afterEach, describe, expect, it } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { click, fakeApi, flush, mountBoard, q, type FakeApi, type Mounted } from "./_helpers/setup-board";
import { board as plainBoard, firstRun } from "./_helpers/setup-fixtures";
import type { SetupView } from "../lib/setup-types";

/** The board with every optional row present (build database and toolchains report here). */
const board = (): SetupView => ({
  ...plainBoard(),
  build_database: { key: "build_services", mode: "auto", enabled: true, source: "default", configured: true,
                    permission: "settings:update" },
  toolchains: [{ name: "python", label: "Python", installed: true, builtin: true, versions: {} }],
} as SetupView);

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

/** Every row the page had before the redesign, by its test id. */
const OLD_ROWS = ["su-row-repository", "su-row-github-access", "su-row-build-database", "su-row-models",
                  "su-row-capacity", "su-row-jobs", "su-row-budget", "su-row-try", "su-row-toolchains",
                  "su-row-public-url"];
const ADVANCED_ROWS = ["su-row-github-access", "su-row-build-database", "su-row-jobs",
                       "su-row-budget", "su-row-toolchains", "su-row-public-url", "su-row-ci"];

describe("the order a new person sees", () => {
  it("① GitHub, ② Repos, ③ Model, ④ Try it — then a folded Advanced", async () => {
    const api = fakeApi(firstRun());
    withSignin(api, SIGNED_OUT);
    mounted = await mountBoard(firstRun());
    await flush();
    const main = [...document.querySelectorAll("[data-testid=su-board] > section")].map((s) => s.getAttribute("data-testid"));
    expect(main).toEqual(["su-row-github", "su-row-repository", "su-row-models", "su-row-try"]);
    const steps = [...document.querySelectorAll("[data-testid=su-board] > section .su-step")].map((s) => s.textContent);
    expect(steps).toEqual(["①", "②", "③", "④"]);
    const adv = q(document.body, "su-advanced") as HTMLDetailsElement;
    expect(adv.open).toBe(false);
    expect(q(document.body, "gh-signin-start")!.textContent).toContain("Sign in with GitHub");
    expect(q(document.body, "su-signin-first")).not.toBeNull();     // Repos waits for the sign-in
    expect(overBudget(document.body)).toEqual([]);
  });

  it("says plainly when this install uses the advanced path, and opens it", async () => {
    const api = fakeApi(firstRun());
    withSignin(api, UNCONFIGURED);
    mounted = await mountBoard(firstRun());
    await flush();
    expect(q(document.body, "gh-signin-unconfigured")!.textContent).toContain("This install uses the advanced path");
    expect(q(document.body, "su-token-form")).not.toBeNull();       // the token path, as before
    await click(q(document.body, "gh-signin-advanced"));
    expect((q(document.body, "su-advanced") as HTMLDetailsElement).open).toBe(true);
  });
});

describe("parity: every capability still reachable", () => {
  it("every old row is on the page; the advanced ones inside the fold, each with one plain line", async () => {
    const api = fakeApi(board());
    withSignin(api, SIGNED_IN);
    mounted = await mountBoard(board());
    await flush();
    for (const id of OLD_ROWS) expect(q(document.body, id), id).not.toBeNull();
    const adv = q(document.body, "su-advanced")!;
    for (const id of ADVANCED_ROWS) expect(adv.querySelector(`[data-testid="${id}"]`), id).not.toBeNull();
    for (const why of ["github_app", "repositories", "ci", "build_database", "toolchains", "jobs",
                       "budget", "public_url"]) {
      const line = q(adv, `su-adv-why-${why}`)!;
      expect(line.textContent!.split(/\s+/).length, why).toBeLessThanOrEqual(6);
      expect(line.getAttribute("data-tip")).toBeTruthy();
    }
    // rc9 grouped the model sources (capacity) inside ③ Model, not the fold
    expect(q(document.body, "su-row-models")!.querySelector('[data-testid="su-row-capacity"]')).not.toBeNull();
    // the CI opt-in moved out of the repo line into Advanced › CI permission
    expect(q(document.body, "su-row-repository")!.querySelector("[data-testid^=su-ci-workflows]")).toBeNull();
  });

  for (const focus of ["budget", "jobs", "ci", "github_app", "toolchains", "public_url", "build_database"]) {
    it(`?focus=${focus} opens Advanced`, async () => {
      const api = fakeApi(board());
      withSignin(api, SIGNED_IN);
      mounted = await mountBoard(board(), `?area=setup&focus=${focus}`);
      expect((q(document.body, "su-advanced") as HTMLDetailsElement).open).toBe(true);
    });
  }

  it("GitHub sending the org admin back from the App install opens Advanced", async () => {
    const api = fakeApi(board());
    withSignin(api, SIGNED_IN);
    mounted = await mountBoard(board(), "?area=setup&focus=repository&github=app_connected");
    expect((q(document.body, "su-advanced") as HTMLDetailsElement).open).toBe(true);
  });
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
