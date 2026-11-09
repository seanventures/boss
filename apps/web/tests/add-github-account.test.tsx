// @vitest-environment happy-dom
/**
 * BOSSAPP-945: Add a GitHub account, guided by BOSS. The sheet against the shapes
 * engine/boss_engine/identity/github_accounts.py answers: one input, the path as data, the
 * one-time switch with no "I did it" button, the install aimed at the account, BOSS noticing the
 * installation on its own, Pick repos with "used by", Allow with results in words; Other ways.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SetupView } from "../lib/setup-types";
import { AreaShownContext } from "../lib/area-shown";
import { lookable, pollDelay, publicRepos, stepsFor, tokenPageFor } from "../lib/github-accounts";
import { click, fakeApi, flush, press, q, type, type Call, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

const SLUG = "boss-localhost-ab12";
const APP = { slug: SLUG, owner_login: "paramg", owner_type: "User", page_url: `https://github.com/apps/${SLUG}`,
              settings_url: `https://github.com/settings/apps/${SLUG}/advanced`, is_owner: false,
              install_url: `https://github.com/apps/${SLUG}/installations/new/permissions?target_id=7202` };
const KAAPPU = { login: "kaappu", type: "Organization", id: "7202", name: "Kaappu", public_repos: 14,
                 avatar_url: "https://avatars.githubusercontent.com/u/7202?v=4", html_url: "https://github.com/kaappu" };
const LOOKUP = { account: KAAPPU, connection: { covered: false, how: null, words: "You are not connected to it yet." },
                 path: "make_public", app: { ...APP, public: false }, installation: null,
                 line: "The fastest way for this account: the BOSS app you already own, installed on kaappu. It is private, so one switch first." };
const REPOS = { installation_id: "91000002", account: "kaappu", account_type: "Organization", repos: [
  { full_name: "kaappu/helpdesk", private: true, used_by: ["HelpDesk AI"], allowed: false, state: "not_allowed", words: "" },
  { full_name: "kaappu/website", private: false, used_by: [], allowed: false, state: "not_allowed", words: "" },
] };

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function open(view: SetupView): Promise<HTMLElement> {
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
  await click(q(container, "su-add-account-open"));
  return q(document.body, "su-add-account") as HTMLElement;
}

const at = (call: Call, path: string) => call.url.includes(`/v1/identity/github/accounts/${path}`);

/** The person came back from GitHub's tab (the sheet asks GitHub at once). */
async function backFromGitHub(clock: { now: number }): Promise<void> {
  clock.now += 6000;
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  await flush();
}

describe("Add a GitHub account, guided by BOSS", () => {
  it("looks the account up, asks for the one-time switch, then installs and picks repos on its own", async () => {
    const view = board();
    const api = fakeApi(view);
    let stage: "private" | "public" | "installed" = "private";
    api.answer = (call) => {
      if (at(call, "lookup")) return { status: 200, body: LOOKUP };
      if (at(call, "watch")) {
        if (stage === "private") return { status: 200, body: { ...LOOKUP, account: { login: "kaappu", id: "7202" } } };
        if (stage === "public") return { status: 200, body: { account: { login: "kaappu", id: "7202" }, path: "install",
          app: { ...APP, public: true }, installation: null, line: "The fastest way for this account: the BOSS app you already own, installed on kaappu." } };
        return { status: 200, body: { account: { login: "kaappu", id: "7202" }, path: "pick", app: { ...APP, public: true },
          installation: { installation_id: "91000002", new: true, settings_url: "https://github.com/organizations/kaappu/settings/installations/91000002" },
          line: "The BOSS app is already installed on kaappu: pick the repositories BOSS may use." } };
      }
      if (at(call, "installations/91000002/repos")) return { status: 200, body: REPOS };
      if (at(call, "allow")) return { status: 200, body: { working: 1, line: "1 of 1 working.", results: [
        { full_name: "kaappu/helpdesk", state: "working", allowed: true, checks: {},
          words: "Working: BOSS can read it, push branches and open pull requests." }] } };
      return null;
    };
    const clock = { now: 1_000_000 };
    vi.spyOn(Date, "now").mockImplementation(() => clock.now);
    const panel = await open(view);

    await type(q(panel, "ga-input"), "kaappu");
    await press(q(panel, "ga-input"), "Enter");
    expect(api.calls.find((c) => at(c, "lookup"))!.body).toEqual({ account: "kaappu" });
    expect(q(panel, "ga-found")!.textContent).toContain("Found on GitHub: kaappu · organisation · 14 public repositories · not connected yet");
    expect(q(panel, "ga-agent-actions")!.textContent).toContain("Agent actions");
    expect(q(panel, "ga-line")!.textContent).toContain("one switch first");
    expect(q(panel, "ga-gate-public")!.textContent).toContain(`Your app ${SLUG} is private to @paramg`);
    expect(q(panel, "ga-step-gate")!.getAttribute("data-s")).toBe("run");
    expect(q(panel, "ga-step-install")!.getAttribute("data-s")).toBe("todo");
    expect(q(panel, "ga-open-settings")!.getAttribute("href")).toBe(`https://github.com/settings/apps/${SLUG}/advanced`);
    expect(q(panel, "ga-open-settings")!.getAttribute("target")).toBe("_blank");
    // No "I did it": BOSS sees the change itself.
    expect(panel.textContent).not.toMatch(/I did it|I've done|Done it/i);
    expect(q(panel, "ga-waiting")!.textContent).toBe("Waiting for GitHub…");
    expect(q(panel, "ga-install")).toBeNull();
    expect(q(panel, "ga-install-later")).not.toBeNull();

    stage = "public";
    await backFromGitHub(clock);
    expect(q(panel, "ga-sheet")!.getAttribute("data-path")).toBe("install");
    expect(q(panel, "ga-step-gate")!.getAttribute("data-s")).toBe("done");
    expect(q(panel, "ga-step-install")!.getAttribute("data-s")).toBe("run");
    const install = q(panel, "ga-install")!;
    expect(install.getAttribute("href")).toBe(APP.install_url);
    expect(install.textContent).toBe("Install the BOSS app on kaappu");
    expect(q(panel, "ga-gate-install")!.textContent).toContain("Switch account");

    stage = "installed";
    await backFromGitHub(clock);
    expect(q(panel, "ga-sheet")!.getAttribute("data-path")).toBe("pick");
    await flush();
    expect(q(panel, "ga-used-kaappu/helpdesk")!.textContent).toBe("used by HelpDesk AI");
    expect(q(panel, "ga-used-kaappu/website")).toBeNull();
    await click(q(panel, "ga-pick-kaappu/helpdesk"));
    expect(q(panel, "ga-allow")!.textContent).toBe("Allow 1 repository");
    await click(q(panel, "ga-allow"));
    expect(api.calls.find((c) => at(c, "allow"))!.body).toEqual(
      { project_id: "p", installation_id: "91000002", repos: ["kaappu/helpdesk"] });
    expect(q(panel, "ga-result-kaappu/helpdesk")!.textContent).toBe(
      "Working: BOSS can read it, push branches and open pull requests.");
    expect(q(panel, "ga-step-check")!.getAttribute("data-s")).toBe("done");
    expect(q(panel, "ga-foot-note")!.textContent).toBe("1 of 1 working.");
    await click(q(panel, "ga-done"));
    expect(q(document.body, "su-add-account")).toBeNull();
  });

  it("a name GitHub does not know is said in words, and nothing else happens", async () => {
    const view = board();
    const api = fakeApi(view);
    api.answer = (call) => (at(call, "lookup") ? { status: 404, body: {
      error: "github_account_not_found", login: "nobody-here",
      message: "No GitHub account called nobody-here. Check the spelling." } } : null);
    const panel = await open(view);
    await type(q(panel, "ga-input"), "nobody-here");
    await press(q(panel, "ga-input"), "Enter");
    expect(q(panel, "ga-error")!.textContent).toBe("No GitHub account called nobody-here. Check the spelling.");
    expect(q(panel, "ga-agent-actions")).toBeNull();
    expect(api.calls.filter((c) => at(c, "watch"))).toEqual([]);
  });

  it("an account already installed goes straight to Pick repos", async () => {
    const view = board();
    const api = fakeApi(view);
    api.answer = (call) => {
      if (at(call, "lookup")) return { status: 200, body: { ...LOOKUP, path: "pick", app: { ...APP, public: true },
        installation: { installation_id: "91000002" }, line: "The BOSS app is already installed on kaappu: pick the repositories BOSS may use." } };
      if (at(call, "installations/91000002/repos")) return { status: 200, body: REPOS };
      return null;
    };
    const panel = await open(view);
    await type(q(panel, "ga-input"), "https://github.com/kaappu");
    await press(q(panel, "ga-input"), "Enter");
    await flush();
    expect(q(panel, "ga-step-gate")).toBeNull();
    expect(q(panel, "ga-step-pick")!.getAttribute("data-s")).toBe("run");
    expect(q(panel, "ga-repos")).not.toBeNull();
    expect(api.calls.filter((c) => at(c, "watch"))).toEqual([]);
  });

  it("Other ways keeps the token, Sign in with GitHub and the org's own app reachable", async () => {
    const view = board();
    fakeApi(view);
    const panel = await open(view);
    const other = q(panel, "ga-other")!;
    expect(other.tagName).toBe("DETAILS");
    expect((other as HTMLDetailsElement).open).toBe(false);
    expect(q(panel, "ga-other-token")).not.toBeNull();
    expect(q(panel, "ga-other-own-app")).not.toBeNull();
    await click(q(panel, "ga-other-token"));
    expect(q(panel, "su-how-token-form")).not.toBeNull();
    await click(q(panel, "su-account-back"));
    expect(q(panel, "ga-input")).not.toBeNull();
  });
});

describe("the rules the sheet draws from", () => {
  it("steps follow the path", () => {
    const states = (s: ReturnType<typeof stepsFor>) => s.map((x) => `${x.key}:${x.state}`).join(" ");
    expect(states(stepsFor("make_public", "kaappu", "Organization"))).toBe(
      "found:done gate:run install:todo pick:todo check:todo");
    expect(states(stepsFor("install", "kaappu", "Organization", { sawGate: true }))).toBe(
      "found:done gate:done install:run pick:todo check:todo");
    expect(states(stepsFor("install", "octo", "User"))).toBe("found:done install:run pick:todo check:todo");
    expect(states(stepsFor("pick", "kaappu", "Organization"))).toBe("found:done install:done pick:run check:todo");
    expect(states(stepsFor("pick", "kaappu", "Organization", { allowed: true }))).toBe(
      "found:done install:done pick:done check:done");
    expect(stepsFor("make_public", "kaappu", "Organization")[0].sub).toBe("kaappu · organisation");
  });

  it("GitHub is asked every 5 s at most, backing off", () => {
    expect(pollDelay(0)).toBe(5000);
    expect(pollDelay(11)).toBe(5000);
    expect(pollDelay(12)).toBe(10000);
    expect(pollDelay(40)).toBe(20000);
  });

  it("the token page names the account and the exact permissions", () => {
    const url = new URL(tokenPageFor("kaappu", "boss.example"));
    expect(url.origin + url.pathname).toBe("https://github.com/settings/personal-access-tokens/new");
    expect(url.searchParams.get("target_name")).toBe("kaappu");
    expect(url.searchParams.get("contents")).toBe("write");
    expect(url.searchParams.get("pull_requests")).toBe("write");
    expect(url.searchParams.get("metadata")).toBe("read");
  });

  it("what is worth looking up", () => {
    expect(lookable("kaappu")).toBe(true);
    expect(lookable("@kaappu")).toBe(true);
    expect(lookable("https://github.com/kaappu/helpdesk")).toBe(true);
    expect(lookable("kaa ppu")).toBe(false);
    expect(lookable("")).toBe(false);
    expect(publicRepos(1)).toBe("1 public repository");
    expect(publicRepos(14)).toBe("14 public repositories");
  });
});
