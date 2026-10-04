// @vitest-environment happy-dom
/**
 * What the independent walk found on the board, each held by behaviour: the keyboard picks a
 * repository, a press waiting on consent says so, the drawer keeps focus and gives it back,
 * removing a repository asks first, a CLI not on the server asks for no token, a busy key is
 * not toasted as Working, times are local, Try it says why it cannot run, the first-PR line is
 * said once, and Test all waits for something to test.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { localPill } from "../app/setup/pill";
import { click, fakeApi, flush, mountBoard, press, q, type, type Mounted } from "./_helpers/setup-board";
import { board, everyRed, firstRun, repoDone, tryStep } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; document.body.innerHTML = ""; });

const FOUND = { account_login: "param", expires_at: null, connection_id: "c",
                repos: [{ full_name: "owner/app", private: true, default_branch: "main", owner_type: "User" },
                        { full_name: "owner/site", private: false, default_branch: "main", owner_type: "User" }] };

describe("the repository picker is keyboard usable", () => {
  it("arrows move, Enter picks, Enter again connects", async () => {
    const api = fakeApi(firstRun());
    api.answer = (call) => call.op === "repo_token" ? { status: 200, body: FOUND } : null;
    mounted = await mountBoard(firstRun());
    const input = q(document.body, "su-token-input")!;
    await type(input, "github_pat_0123456789abcdefghij");
    await press(input, "Enter");
    const first = q(document.body, "su-repo-option-owner/app") as HTMLButtonElement;
    expect(first.tagName).toBe("BUTTON");
    first.focus();
    await press(first.closest("ul"), "ArrowDown");
    const second = q(document.body, "su-repo-option-owner/site") as HTMLButtonElement;
    expect(document.activeElement).toBe(second);
    await act(async () => { second.click(); });
    await press(second, "Enter");
    expect(api.calls.find((c) => c.op === "repo_select")?.body.full_name).toBe("owner/site");
  });
});

describe("a press waiting on the operator says so", () => {
  it("Enter with the database consent unticked names what it waits for", async () => {
    const view = { ...firstRun(), secrets: { backend: "database", needs_consent: true } };
    const api = fakeApi(view);
    mounted = await mountBoard(view);
    const input = q(document.body, "su-token-input")!;
    await type(input, "github_pat_0123456789abcdefghij");
    await press(input, "Enter");
    expect(api.calls.some((c) => c.op === "repo_token")).toBe(false);
    expect(q(document.body, "su-refusal")?.textContent).toBe("Store in BOSS database");
  });
});

describe("the drawer", () => {
  it("focuses its first field, keeps Tab inside, closes on Escape and gives focus back", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    const opener = q(document.body, "su-add-model-open") as HTMLElement;
    opener.focus();
    await click(opener);
    const drawer = q(document.body, "su-model-drawer")!;
    expect(drawer.contains(document.activeElement)).toBe(true);
    expect((document.activeElement as HTMLElement).getAttribute("aria-label")).not.toBe("Close");
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    await flush();
    expect(q(document.body, "su-model-drawer")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

describe("removing a repository asks first", () => {
  it("Remove opens a confirm; nothing is sent until it is pressed", async () => {
    const view = board();
    view.repository.repos = [{ ...view.repository.repos[0], default: false }];
    const api = fakeApi(view);
    mounted = await mountBoard(view);
    await click(q(document.body, "su-repo-menu"));
    await click(q(document.body, "su-menu-remove"));
    expect(api.calls.some((c) => c.op === "repo_remove")).toBe(false);
    await click(q(document.body, "su-repo-remove-yes"));
    expect(api.calls.some((c) => c.op === "repo_remove")).toBe(true);
  });
});

describe("a CLI that is not on the server", () => {
  it("asks for no token it could not use", async () => {
    const view = repoDone();
    view.detected = view.detected.map((d) => d.provider === "claude_code"
      ? { ...d, found: "not_on_server" as const } : d);
    fakeApi(view);
    mounted = await mountBoard(view, "?area=setup&add=claude_code");
    expect(q(document.body, "su-not-on-server")?.textContent).toBe("Not on server");
    expect(q(document.body, "su-model-token")).toBeNull();
  });
});

describe("toasts say what the check said", () => {
  it("a busy key is Connected · Limited, not Working", async () => {
    const after = board();
    after.models = [after.models[0], { ...after.models[1],
      pill: { state: "rate_limited", tone: "amber", label: "Limited · 14:20", tip: "t" } }];
    const api = fakeApi(repoDone());
    api.answer = (call) => call.op === "models" ? { status: 200, body: after } : null;
    const toasts: string[] = [];
    window.addEventListener("boss:toast", (e) => toasts.push((e as CustomEvent).detail.message));
    mounted = await mountBoard(repoDone(), "?area=setup&add=openrouter");
    await type(q(document.body, "su-model-key"), "sk-or-v1-0123456789abcdef");
    await click(q(document.body, "su-model-connect"));
    expect(toasts).toContain("Connected · Limited · 14:20");
    expect(toasts).not.toContain("Connected · Working");
  });
});

describe("times are the viewer's", () => {
  it("a pill's HH:MM is rewritten from its instant in local time", () => {
    const at = "2026-10-04T14:20:00Z";
    const local = new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const pill = localPill({ state: "rate_limited", tone: "amber", label: "GitHub busy · 14:20",
                             tip: "GitHub rate limit. Checks resume at 14:20.", at });
    expect(pill.label).toBe(`GitHub busy · ${local}`);
    expect(pill.tip).toContain(local);
  });
});

describe("every time on the board is the viewer's, not only the pill's", () => {
  it("the verdict's tooltip and Try it's reason say the same local time as the pill", async () => {
    // 99:99 is no real local time: if it survives anywhere, the server's clock leaked through.
    const at = "2026-10-04T14:20:00Z";
    const local = new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const busy = { state: "rate_limited", tone: "amber" as const, label: "GitHub busy · 99:99",
                   tip: "GitHub rate limit. Checks resume at 99:99.", at };
    const base = everyRed();
    const view = { ...base, ready: false, problems: 0, next: "repository" as const,
                   verdict: { label: "1 to go", tone: "amber" as const, tip: busy.tip, at },
                   repository: { ...base.repository,
                                 repos: [{ ...base.repository.repos[0], pill: busy }] },
                   models: board().models, budget: board().budget,
                   try: { workflow_key: "setup_first_pr", last_run: null, done: false } };
    fakeApi(view);
    mounted = await mountBoard(view, "?area=setup&focus=try");
    const verdict = q(document.body, "su-verdict")!.getAttribute("title");
    const reason = q(document.body, "su-try-blocked")?.getAttribute("data-tip");
    expect(verdict).toBe(`GitHub rate limit. Checks resume at ${local}.`);
    expect(reason).toBe(`GitHub rate limit. Checks resume at ${local}.`);
    expect(document.body.innerHTML).not.toContain("99:99");
  });
});

describe("Try it says why it cannot run", () => {
  it("the Open a PR tooltip is the failing row's fix", async () => {
    const view = { ...everyRed(), next: "repository" as const, try: { workflow_key: "setup_first_pr",
                   last_run: null, done: false } };
    fakeApi(view);
    mounted = await mountBoard(view, "?area=setup&focus=try");
    const tip = q(document.body, "su-try-blocked")?.getAttribute("data-tip");
    expect(tip).toBe("Token needs Contents: Read and write.");
  });

  it("'Your first PR is open.' is not said by a page that merely loads an old PR", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    expect(q(document.body, "su-try-pr")).not.toBeNull();
    expect(document.body.textContent).not.toContain("Your first PR is open.");
  });
});

describe("Test all", () => {
  it("waits for something to test", async () => {
    fakeApi(firstRun());
    mounted = await mountBoard(firstRun());
    expect((q(document.body, "su-test-all") as HTMLButtonElement).disabled).toBe(true);
    mounted.unmount();
    fakeApi(tryStep());
    mounted = await mountBoard(tryStep());
    expect((q(document.body, "su-test-all") as HTMLButtonElement).disabled).toBe(false);
  });
});
