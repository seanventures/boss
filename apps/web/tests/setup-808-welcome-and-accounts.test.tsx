// @vitest-environment happy-dom
/**
 * BOSSAPP-808: the three screens — Welcome (3 steps), Setup on one page (GitHub accounts,
 * Repositories, Models) and Add a GitHub account (1 How · 2 Pick repos · 3 Check access) — against
 * GET /v1/setup shapes as engine/boss_engine/services/setup_accounts.py writes them.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GitHubAccount, SetupRepository, SetupView } from "../lib/setup-types";
import { AreaShownContext } from "../lib/area-shown";
import { click, fakeApi, flush, q, type, type Mounted } from "./_helpers/setup-board";
import { board, firstRun, PILLS, tryStep } from "./_helpers/setup-fixtures";

const now = "2026-11-04T12:00:00+00:00";
const ok = { ok: true, state: "working", at: now };

const ACCOUNTS: GitHubAccount[] = [
  { id: "ghc1", connection_id: "ghc1", scope: "project", kind: "token", name: "paramg", login: "paramg",
    how: "Personal · access token", personal: true, repo_count: 1, expires_at: null,
    pill: { state: "connected", tone: "green", label: "Connected", tip: "BOSS can read, push and open pull requests." },
    created_at: now },
  { id: "ghc2", connection_id: "ghc2", scope: "project", kind: "token", name: "sean-bot (org)", login: "sean-bot",
    how: "Organisation · access token", personal: false, repo_count: 1, expires_at: "2026-11-13T12:00:00+00:00",
    pill: { state: "expiring", tone: "amber", label: "Token expires in 9 days",
            tip: "Make a new token before then and press Fix.", action: "replace" }, created_at: now },
];

const REPOS: SetupRepository[] = [
  { full_name: "owner/app", binding_id: "ghb1", in_project: true, default: true, account_id: "ghc1", via: "paramg",
    projects: [{ project_id: "p", name: "Hello" }], checks: { read: ok, write: ok, pull_requests: ok },
    pill: PILLS.working },
  { full_name: "seanventures/boss", binding_id: "ghb2", in_project: true, default: false, account_id: "ghc2",
    via: "sean-bot (org)", projects: [{ project_id: "p", name: "Hello" }, { project_id: "p2", name: "BOSS" }],
    checks: { read: ok, write: { ok: false, state: "cant_write", at: now }, pull_requests: { ok: null, state: "not_run", at: null } },
    pill: PILLS.cantWrite },
];

function withAccounts(view: SetupView): SetupView {
  return { ...view, github_accounts: ACCOUNTS, repositories: REPOS };
}

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.unstubAllGlobals(); });

async function mount(view: SetupView, search = "?area=setup",
                     layout: "setup" | "welcome" = "setup"): Promise<Mounted> {
  window.history.replaceState(null, "", `/${search}`);
  const { SetupBoard } = await import("../app/setup/setup-board");
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<AreaShownContext.Provider value={true}>
      <SetupBoard projectId="p" initialView={view} layout={layout} />
    </AreaShownContext.Provider>);
  });
  await flush();
  mounted = { root, container, unmount: () => { act(() => root.unmount()); container.remove(); } };
  return mounted;
}

/** Area switches the board asks the shell for (`boss:navigate`). */
function navigations(): string[] {
  const seen: string[] = [];
  window.addEventListener("boss:navigate", (e) => seen.push(String((e as CustomEvent).detail)));
  return seen;
}

describe("Screen 1 — Get started is its own place", () => {
  it("shows the 3 steps, GitHub first, and the project buttons open New project", async () => {
    fakeApi(firstRun());
    const { container } = await mount(firstRun(), "?area=welcome", "welcome");
    expect(q(container, "su-welcome")).not.toBeNull();
    expect(container.textContent).toContain("Get BOSS working in 3 steps");
    expect(container.textContent).toContain("About 10 minutes. You can change everything later in Setup.");
    expect(q(container, "su-wl-github")!.getAttribute("data-current")).toBe("true");
    expect(q(container, "su-wl-github-go")!.className).toContain("su-btn-primary");
    expect(q(container, "su-wl-sample-go")!.getAttribute("href")).toBe("/?new_project=sample");
    expect(q(container, "su-wl-import")!.getAttribute("href")).toBe("/?new_project=import");
    expect(q(container, "su-wl-idea")!.getAttribute("href")).toBe("/?new_project=idea");
    const flow = [...container.querySelectorAll(".su-wl-flow > li.su-chip")].map((li) => li.textContent);
    expect(flow).toEqual(["Idea", "Plan (you approve)", "Build", "Review", "Pull request", "Live preview"]);
    expect(container.textContent).toContain("~8 min · included in your subscription");
    expect(q(container, "su-accounts")).toBeNull();
    expect(q(container, "su-row-repository")).toBeNull();
  });

  it("Connect GitHub and Add a model deep-link into Repo and models, in place", async () => {
    fakeApi(firstRun());
    const seen = navigations();
    const { container } = await mount(firstRun(), "?area=welcome", "welcome");
    expect(q(container, "su-wl-github-go")!.getAttribute("href")).toBe("/?area=setup&focus=repository");
    expect(q(container, "su-wl-model-go")!.getAttribute("href")).toBe("/?area=setup&focus=models");
    await click(q(container, "su-wl-github-go"));
    expect(seen).toContain("setup");
    expect(new URLSearchParams(window.location.search).get("focus")).toBe("repository");
  });

  it("done steps say who is connected and which model builds; step 3 is current", async () => {
    const view = withAccounts(tryStep());
    fakeApi(view);
    const { container } = await mount(view, "?area=welcome", "welcome");
    expect(q(container, "su-wl-github-done")!.textContent).toBe("Connected as paramg · 2 repos BOSS can use");
    expect(q(container, "su-wl-model-done")!.textContent).toBe("Claude Code signed in · builds and reviews will use it");
    expect(q(container, "su-wl-github-more")!.textContent).toBe("Add another account");
    expect(q(container, "su-wl-model-more")!.textContent).toBe("Add another model");
    expect(q(container, "su-wl-sample")!.getAttribute("data-current")).toBe("true");
  });
});

describe("Repo and models never shows the Welcome", () => {
  it("a new install's Setup is the 1 GitHub · 2 Repos · 3 Model stepper, with a way back", async () => {
    fakeApi(firstRun());
    const seen = navigations();
    const { container } = await mount(firstRun());
    expect(q(container, "su-welcome")).toBeNull();
    expect(container.querySelector("h1")!.textContent).toBe("Repo and models");
    expect(q(container, "su-row-repository")).not.toBeNull();
    expect(q(container, "su-row-models")).not.toBeNull();
    expect(q(container, "su-row-try")).toBeNull();
    await click(q(container, "su-open-welcome"));
    expect(seen).toContain("welcome");
  });

  it("once a repo and a model work it is the board; Back to Get started until a first PR", async () => {
    const ready = withAccounts(tryStep());
    fakeApi(ready);
    const { container } = await mount(ready);
    expect(q(container, "su-accounts")).not.toBeNull();
    expect(q(container, "su-open-welcome")!.textContent).toBe("Back to Get started");
    mounted?.unmount(); mounted = null;
    const done = withAccounts(board());
    fakeApi(done);
    const again = await mount(done);
    expect(q(again.container, "su-open-welcome")).toBeNull();
  });

  it("Welcome's Add another account lands on the board's Add GitHub account", async () => {
    const view = withAccounts(board());
    fakeApi(view);
    await mount(view, "?area=setup&focus=repository");
    expect(q(document.body, "su-add-account")).not.toBeNull();
  });

  it("old links (view=welcome, focus=try) open Get started", async () => {
    const view = withAccounts(board());
    fakeApi(view);
    const seen = navigations();
    await mount(view, "?area=setup&view=welcome");
    expect(seen).toContain("welcome");
  });
});

describe("Try it is not part of Setup", () => {
  it("Setup shows connections only; the sample, Quick PR and the last run live on Get started", async () => {
    const view = withAccounts(board());
    fakeApi(view);
    const { container } = await mount(view);
    expect(q(container, "su-row-try")).toBeNull();
    expect(q(container, "su-try-input")).toBeNull();
    mounted?.unmount(); mounted = null;
    const welcome = await mount(view, "?area=welcome", "welcome");
    expect(q(welcome.container, "su-wl-quick-pr")!.querySelector('[data-testid="su-try-input"]')).not.toBeNull();
    expect(q(welcome.container, "su-wl-last-pr")!.getAttribute("href")).toBe("https://github.com/owner/app/pull/12");
  });

  it("focus=try opens Get started with Quick PR open", async () => {
    const view = withAccounts(board());
    fakeApi(view);
    const { container } = await mount(view, "?area=welcome&focus=try", "welcome");
    expect((q(container, "su-wl-quick-pr") as HTMLDetailsElement).open).toBe(true);
  });
});

describe("Welcome is honest", () => {
  it("other PRs in the project do not tick the sample step", async () => {
    const view = { ...withAccounts(board()), try: { workflow_key: "setup_first_pr", last_run: null, done: true } };
    fakeApi(view);
    const { container } = await mount(view, "?area=welcome", "welcome");
    const circle = q(container, "su-wl-sample")!.querySelector(".su-wl-circle")!;
    expect(circle.getAttribute("data-done")).toBeNull();
    expect(q(container, "su-wl-github")!.querySelector(".su-wl-circle")!.getAttribute("data-done")).toBe("true");
  });

  it("the rail's Get started shows until GitHub, a model and a first PR exist", async () => {
    const { summarize } = await import("../lib/setup-stepper");
    expect(summarize(firstRun()).get_started).toBe(true);
    expect(summarize(withAccounts(tryStep())).get_started).toBe(true);
    expect(summarize(withAccounts(board())).get_started).toBe(false);
  });
});

describe("Screen 2 — Setup on one page", () => {
  it("a model whose login expired says so in Who does what", async () => {
    const view = withAccounts(board());
    view.jobs = view.jobs.map((j) => (j.job === "review" && j.resolved
      ? { ...j, resolved: { ...j.resolved, pill: PILLS.loginExpired } } : j));
    fakeApi(view);
    const { container } = await mount(view);
    expect(q(container, "su-who-pill-review")!.textContent).toContain("Login expired");
    expect(q(container, "su-who-pill-build")).toBeNull();
  });

  it("lists one row per GitHub account with how, repo count and the server's status", async () => {
    const view = withAccounts(board());
    fakeApi(view);
    const { container } = await mount(view);
    const first = q(container, "su-account-ghc1")!;
    expect(first.textContent).toContain("paramg");
    expect(first.textContent).toContain("Personal · access token");
    expect(first.textContent).toContain("Connected");
    expect(first.querySelector('[data-testid="su-account-fix"]')).toBeNull();
    const org = q(container, "su-account-ghc2")!;
    expect(org.textContent).toContain("sean-bot (org)");
    expect(org.textContent).toContain("Organisation · access token");
    expect(org.textContent).toContain("Token expires in 9 days");
    expect(org.querySelector(".su-pill")!.className).toContain("su-tone-amber");
    // Fix on an expiring token opens Replace token for THAT connection.
    await click(org.querySelector('[data-testid="su-account-fix"]'));
    expect(q(document.body, "su-fix-account")).not.toBeNull();
    expect(q(document.body, "su-token-form")).not.toBeNull();
  });

  it("lists every repo with its account, the projects that use it and its access", async () => {
    const view = withAccounts(board());
    fakeApi(view);
    const { container } = await mount(view);
    const app = q(container, "su-repo-row-owner/app")!;
    expect(app.textContent).toContain("via paramg");
    expect(app.textContent).toContain("Hello");
    expect(app.textContent).toContain("Read · Write · PRs ✓");
    const boss = q(container, "su-repo-row-seanventures/boss")!;
    expect(boss.textContent).toContain("via sean-bot (org)");
    expect(boss.textContent).toContain("Hello, BOSS");
    expect(boss.textContent).toContain("Can't write");
  });

  it("shows a card per model with what it is used for, and Who does what with Change", async () => {
    const view = withAccounts(board());
    fakeApi(view);
    const { container } = await mount(view);
    const claude = q(container, "su-model-card-anthropic_claude_code")!;
    expect(claude.textContent).toContain("Claude Code");
    expect(claude.textContent).toContain("Subscription · signed in");
    expect(claude.textContent).toContain("Used for: Build · Chat · Decisions");
    expect(q(container, "su-model-card-setup-openrouter")!.textContent).toContain("API key");
    expect(q(container, "su-who-build")!.textContent).toBe("Build → Auto · Claude Code");
    expect(q(container, "su-who-review")!.textContent).toBe("Review → OpenRouter · openai/gpt-5");
    await click(q(container, "su-who-change"));
    expect((q(container, "su-advanced-all") as HTMLDetailsElement).open).toBe(true);
  });

  it("keeps every earlier row under Advanced settings", async () => {
    const view = withAccounts(board());
    fakeApi(view);
    const { container } = await mount(view);
    const advanced = q(container, "su-advanced-all")!;
    expect((advanced as HTMLDetailsElement).open).toBe(false);
    for (const id of ["su-row-repository", "su-row-models", "su-test-all", "su-advanced", "su-row-ci"]) {
      expect(advanced.querySelector(`[data-testid="${id}"]`), id).not.toBeNull();
    }
    await click(q(container, "su-open-advanced"));
    expect((advanced as HTMLDetailsElement).open).toBe(true);
  });

  it("a deep link into a row opens Advanced settings on it", async () => {
    fakeApi(board());
    const { container } = await mount(board(), "?area=setup&focus=budget");
    expect(q(container, "su-welcome")).toBeNull();
    expect((q(container, "su-advanced-all") as HTMLDetailsElement).open).toBe(true);
  });

  it("an older API without github_accounts still shows its one connection", async () => {
    fakeApi(board());
    const { container } = await mount(board());
    expect(q(container, "su-account-ghc1")!.textContent).toContain("param");
    expect(q(container, "su-repo-row-owner/app")!.textContent).toContain("Read · Write · PRs ✓");
  });
});

describe("Screen 3 — Add a GitHub account", () => {
  it("pastes a token, picks two repos, and shows Read / Write / Open PRs for each", async () => {
    const view = withAccounts(board());
    const api = fakeApi(view);
    const added: SetupView = {
      ...view,
      repository: { ...view.repository, repos: [...view.repository.repos,
        { ...view.repository.repos[0], binding_id: "ghb3", full_name: "acme/web", default: false },
        { ...view.repository.repos[0], binding_id: "ghb4", full_name: "acme/api", default: false,
          checks: { read: ok, write: ok, pull_requests: { ok: false, state: "cant_open_pr", at: now } } }] },
    };
    api.answer = (call) => {
      if (call.op === "repo_token") {
        return { status: 200, body: { account_login: "acme-bot", expires_at: null, connection_id: "ghc9",
                                      repos: [{ full_name: "acme/web", private: true, default_branch: "main", owner_type: "Organization" },
                                              { full_name: "acme/api", private: false, default_branch: "main", owner_type: "Organization" }] } };
      }
      if (call.op === "repo_select") return { status: 200, body: added };
      return null;
    };
    const { container } = await mount(view);
    await click(q(container, "su-add-account-open"));
    const panel = q(document.body, "su-add-account")!;
    expect(panel.textContent).toContain("How to connect");
    expect(panel.textContent).toContain("Pick repos");
    expect(panel.textContent).toContain("Check access");
    expect(q(panel, "su-how-own")!.textContent).toContain("My own account");
    // Sign-in is not set up here: own account says why (tooltip) and is not the recommended one.
    expect(q(panel, "su-how-own")!.textContent).not.toContain("Recommended");
    expect(q(panel, "su-how-own")!.getAttribute("title")).toContain("not set up on this install");
    expect(q(panel, "su-how-org")!.textContent).toContain("An organisation");
    expect(q(panel, "su-how-token")!.textContent).toContain("Paste an access token");
    // No "Sign in with GitHub" and no App here: a token is the way, preselected and recommended.
    expect((q(panel, "su-how-token")!.querySelector("input") as HTMLInputElement).checked).toBe(true);
    expect(q(panel, "su-how-token")!.textContent).toContain("Recommended");
    expect(q(panel, "su-account-cancel")).not.toBeNull();
    expect(q(panel, "su-account-continue")!.textContent).toBe("Continue");
    await type(q(panel, "su-account-token"), "github_pat_not_a_real_one_0000000000");
    await click(q(panel, "su-account-continue"));
    expect(q(panel, "su-add-account-body")!.getAttribute("data-step")).toBe("2");
    await click(q(panel, "su-account-repo-acme/web"));
    await click(q(panel, "su-account-repo-acme/api"));
    expect(q(panel, "su-account-continue")!.textContent).toBe("Add 2 repos");
    await click(q(panel, "su-account-continue"));
    const selects = api.calls.filter((c) => c.op === "repo_select");
    expect(selects.map((c) => [c.body.connection_id, c.body.full_name])).toEqual([["ghc9", "acme/web"], ["ghc9", "acme/api"]]);
    expect(q(panel, "su-add-account-body")!.getAttribute("data-step")).toBe("3");
    expect(q(panel, "su-account-access-acme/web")!.textContent).toContain("Read ✓Write ✓Open PRs ✓");
    expect(q(panel, "su-account-access-acme/api")!.textContent).toContain("Open PRs ✗");
    // No token value is ever echoed back into the page.
    expect(document.body.innerHTML).not.toContain("github_pat_not_a_real_one");
    await click(q(panel, "su-account-continue"));
    expect(q(document.body, "su-add-account")).toBeNull();
  });

  it("with the org's GitHub App installed, An organisation is recommended and Continue lists its repos", async () => {
    const appRow: GitHubAccount = { ...ACCOUNTS[0], id: "org_app", connection_id: null, scope: "org", kind: "org_app",
                                    name: "paramg", how: "Personal · BOSS GitHub App" };
    const view = { ...withAccounts(board()), github_accounts: [ACCOUNTS[0], appRow] };
    const api = fakeApi(view);
    api.answer = (call) => (call.op === "repo_app" ? { status: 200, body: {
      account_login: "paramg", expires_at: null, connection_id: "ghc-app", source: "github_app",
      repos: [{ full_name: "paramg/boss", private: true, default_branch: "main", owner_type: "User" }] } } : null);
    const { container } = await mount(view);
    await click(q(container, "su-add-account-open"));
    const panel = q(document.body, "su-add-account")!;
    expect((q(panel, "su-how-org")!.querySelector("input") as HTMLInputElement).checked).toBe(true);
    expect(q(panel, "su-how-org")!.textContent).toContain("Recommended");
    expect(q(panel, "su-how-org")!.textContent).toContain("@paramg");
    await click(q(panel, "su-account-continue"));
    expect(api.calls.some((c) => c.op === "repo_app")).toBe(true);
    expect(q(panel, "su-add-account-body")!.getAttribute("data-step")).toBe("2");
    expect(q(panel, "su-account-continue")!.textContent).toBe("Add 1 repo");
  });

  it("Cancel closes the panel and nothing is written", async () => {
    const view = withAccounts(board());
    const api = fakeApi(view);
    const { container } = await mount(view);
    await click(q(container, "su-add-account-open"));
    await click(q(document.body, "su-account-cancel"));
    expect(q(document.body, "su-add-account")).toBeNull();
    expect(api.calls.filter((c) => c.method !== "GET")).toEqual([]);
  });
});
