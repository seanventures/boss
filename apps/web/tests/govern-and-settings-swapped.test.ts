import { describe, expect, it } from "vitest";

import { communityNavCopy } from "../lib/edition";
import { codeOnly } from "./_helpers/source";

/**
 * "Let's rename Govern to Settings and Settings to Govern. Let's also move SSO under Settings.
 * Rename SSO to Identity."
 *
 * It read backwards. "Govern" was the GROUP heading and "Settings" was one area inside it —
 * but those knobs (concurrency, turn budgets, spend) ARE the governing, and everything else in
 * the group is a setting. And "SSO" sat second in the whole sidebar, above everything an
 * operator actually does, named after one protocol rather than after the thing it configures.
 */
const SHELL = codeOnly("app/ConsoleShell.tsx");

describe("the group and the area swapped names", () => {
  it("the group is Settings", () => {
    expect(SHELL).toContain('group: "Run" | "Settings" | "Platform"');
    expect(SHELL).toContain('["Platform", "Run", "Settings"]');
  });

  it("and the governing knobs are Govern", () => {
    expect(SHELL).toContain('id: "budget", label: "Govern"');
  });

  it("the id did not move, because ids are routes and bookmarks", () => {
    // A rename that breaks `?area=budget` is a rename that loses everybody's links.
    expect(SHELL).toContain('id: "budget"');
  });

  it("no area still claims the old group", () => {
    expect(SHELL).not.toContain('group: "Govern"');
  });
});

describe("identity is a setting, not a platform headline", () => {
  it("it sits with the settings now", () => {
    const at = SHELL.indexOf('id: "enterprise"');
    const block = SHELL.slice(at, at + 400);
    expect(block).toContain('label: "Identity"');
    expect(block).toContain('group: "Settings"');
  });

  it("and says what it configures rather than which protocol it speaks", () => {
    // REWRITTEN AGAIN (identity S4): Identity is people, access roles, agents and audit now — the
    // installer password is gone — so the subtitle says what the area answers, not a protocol.
    const at = SHELL.indexOf('id: "enterprise"');
    expect(SHELL.slice(at, at + 400)).toMatch(/Who can do what, and for whom/);
    expect(SHELL.slice(at, at + 400)).not.toMatch(/installer password|SSO/);
  });

  it("the community relabel follows", () => {
    expect(communityNavCopy("enterprise").label).toBe("Identity");
  });

  it("but the badge still says SSO", () => {
    // That is the thing Community is being told it INCLUDES, and the word somebody scanning
    // for it would look for.
    expect(communityNavCopy("enterprise").badge).toBe("SSO");
  });
});

describe("what these panels show is not a snapshot from page load", () => {
  it("the console refreshes on its own, backing off to what a render costs", () => {
    // The cost is measured start-to-landing by the refresh coordinator (lib/console-refresh.ts).
    expect(SHELL).toContain("Math.max(20_000, refreshCoordinator.lastDurationMs() * 3)");
  });

  it("and immediately when something actually changed", () => {
    // The poll alone can leave a change unshown for up to three minutes — every panel here is
    // a server-rendered snapshot, and an operator who has just changed one expects the page to
    // know.
    expect(SHELL).toContain('onChanged(() => {');
    expect(SHELL).toContain('"console-shell"');
  });

  it("with the same guards the poll has", () => {
    const at = SHELL.indexOf("onChanged(() => {");
    const block = SHELL.slice(at, at + 400);
    expect(block).toContain("document.hidden");
    // Never stacked: asked of the coordinator, which owes it until the one in flight lands.
    expect(block).toContain('refreshCoordinator.request("now")');
    expect(block).toContain('tag === "INPUT"');
  });
});
