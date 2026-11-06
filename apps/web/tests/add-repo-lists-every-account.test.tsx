// @vitest-environment happy-dom
/**
 * Setup › Repositories › Add repo lists the repositories of EVERY connected account, grouped and
 * searchable (operator, 2026-11-06: only the personal token's repos were listed, a repository
 * newly granted to the BOSS GitHub App never appeared, and an install whose only account was the
 * org's App got the whole Add-account wizard). Picking one binds it through repo_select, with the
 * group's connection (`org_app` = connect the App to this project first).
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AvailableRepoGroup, GitHubAccount, SetupView } from "../lib/setup-types";
import { AreaShownContext } from "../lib/area-shown";
import { visibleGroups } from "../app/setup/add-repo-any-account";
import { click, fakeApi, flush, q, type, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

const now = "2026-11-06T12:00:00+00:00";
const pill = { state: "connected", tone: "green" as const, label: "Connected", tip: "" };

const TOKEN: GitHubAccount = {
  id: "ghc1", connection_id: "ghc1", scope: "project", kind: "token", name: "paramg", login: "paramg",
  how: "Personal · access token", personal: true, repo_count: 1, expires_at: null, pill, created_at: now };
const ORG_APP: GitHubAccount = {
  id: "org_app", connection_id: null, scope: "org", kind: "org_app", name: "paramg", login: "paramg",
  how: "Personal · BOSS GitHub App", personal: true, repo_count: 3, expires_at: null, pill, created_at: null };

const GROUPS: AvailableRepoGroup[] = [
  { id: "ghc1", connection_id: "ghc1", kind: "token", account: "paramg", error: null, repos: [
    { full_name: "owner/app", private: true, default_branch: "main", owner_type: "User", in_project: true },
    { full_name: "paramg/tool", private: false, default_branch: "main", owner_type: "User", in_project: false },
  ] },
  { id: "org_app", connection_id: "org_app", kind: "org_app", account: "paramg", error: null, repos: [
    { full_name: "paramg/helpdesk-ai-demo", private: true, default_branch: "main", owner_type: "User", in_project: false },
  ] },
];

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.unstubAllGlobals(); });

async function mount(view: SetupView): Promise<Mounted> {
  window.history.replaceState(null, "", "/?area=setup");
  const { SetupBoard } = await import("../app/setup/setup-board");
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<AreaShownContext.Provider value={true}>
      <SetupBoard projectId="p" initialView={view} layout="setup" />
    </AreaShownContext.Provider>);
  });
  await flush();
  mounted = { root, container, unmount: () => { act(() => root.unmount()); container.remove(); } };
  return mounted;
}

function withAccounts(accounts: GitHubAccount[]): SetupView {
  return { ...board(), github_accounts: accounts, repositories: [] };
}

describe("Add repo lists every connected account", () => {
  it("groups the token's and the App's repositories, hides ones already here, and binds through the App", async () => {
    const view = withAccounts([TOKEN, ORG_APP]);
    const api = fakeApi(view);
    api.answer = (call) => (call.method === "GET" && call.url.includes("available=1")
      ? { status: 200, body: { groups: GROUPS } } : null);
    await mount(view);
    await click(q(document.body, "su-add-repo-open"));
    await flush();
    const drawer = q(document.body, "su-add-repo-drawer")!;
    expect(drawer).not.toBeNull();
    expect(q(document.body, "su-add-account")).toBeNull();
    expect(api.calls.some((c) => c.url.includes("available=1"))).toBe(true);
    expect(api.calls.some((c) => c.url.includes("&repos="))).toBe(false);
    expect(q(drawer, "su-repo-group-ghc1")!.textContent).toContain("Access token");
    expect(q(drawer, "su-repo-group-org_app")!.textContent).toContain("BOSS GitHub App");
    expect(q(drawer, "su-repo-option-paramg/helpdesk-ai-demo")).not.toBeNull();
    expect(q(drawer, "su-repo-option-paramg/tool")).not.toBeNull();
    expect(q(drawer, "su-repo-option-owner/app")).toBeNull();

    await type(q(drawer, "su-repo-search") as HTMLInputElement, "helpdesk");
    expect(q(drawer, "su-repo-option-paramg/tool")).toBeNull();
    await click(q(drawer, "su-repo-option-paramg/helpdesk-ai-demo"));
    await click(q(drawer, "su-repo-connect"));
    await flush();
    const select = api.calls.find((c) => c.op === "repo_select");
    expect(select?.body).toMatchObject({ connection_id: "org_app", full_name: "paramg/helpdesk-ai-demo",
                                         make_default: false });
  });

  it("an install whose only account is the org's App gets the picker, not the Add-account wizard", async () => {
    const view = withAccounts([ORG_APP]);
    const api = fakeApi(view);
    api.answer = (call) => (call.method === "GET" && call.url.includes("available=1")
      ? { status: 200, body: { groups: [GROUPS[1]] } } : null);
    await mount(view);
    await click(q(document.body, "su-add-repo-open"));
    await flush();
    expect(q(document.body, "su-add-account")).toBeNull();
    expect(q(document.body, "su-repo-option-paramg/helpdesk-ai-demo")).not.toBeNull();
  });

  it("an account GitHub refused says so in its own group while the others still list", () => {
    const refused: AvailableRepoGroup = { ...GROUPS[0], repos: [],
      error: { state: "token_rejected", headline: "Token rejected", fix: "Paste a new token." } };
    const out = visibleGroups([refused, GROUPS[1]], "");
    expect(out.map((g) => g.id)).toEqual(["ghc1", "org_app"]);
    expect(out[0].error?.headline).toBe("Token rejected");
    expect(out[1].repos.map((r) => r.full_name)).toEqual(["paramg/helpdesk-ai-demo"]);
  });
});

describe("GitHub's return after an access change", () => {
  it("says Access updated and names what the App reaches now", async () => {
    const { accessUpdatedSentence } = await import("../app/setup/github-access-row");
    expect(accessUpdatedSentence([], 0)).toBe("Access updated");
    expect(accessUpdatedSentence(["paramg/helpdesk-ai-demo"], 1))
      .toBe("Access updated · BOSS sees 1 repo: paramg/helpdesk-ai-demo");
    expect(accessUpdatedSentence(["a/1", "a/2", "a/3", "a/4", "a/5", "a/6"], 9))
      .toBe("Access updated · BOSS sees 9 repos: a/1, a/2, a/3, a/4, a/5 and 4 more");
  });
});
