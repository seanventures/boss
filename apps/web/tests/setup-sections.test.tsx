// @vitest-environment happy-dom
/**
 * SETUP, ONE FOLD PER TOPIC (operator, 2026-11-15: "The page is growing big, so we need to
 * collapse based on the topic - like repos, models, ticket system"): a section folded is its
 * name and one line of state; open or closed is remembered per viewer; a section that needs
 * attention or that a link points at opens regardless; and the area is called Setup.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { SECTIONS_KEY, SetupSection } from "../app/setup/setup-section";
import { modelSummary, repoSummary } from "../app/setup/setup-board";
import type { SetupView } from "../lib/setup-types";
import { $, mount, settle, unmount } from "./_helpers/live-render";

afterEach(() => { unmount(); try { window.localStorage.clear(); } catch { /* none */ } });

const section = (over: Partial<React.ComponentProps<typeof SetupSection>> = {}) => (
  <SetupSection id="ticket_systems" title="Ticket systems" icon={null} summary="Jira · shared · 2 projects mapped" {...over}>
    <p data-testid="body">inside</p>
  </SetupSection>
);

describe("a Setup section", () => {
  it("folded, says its name and one line; the body stays mounted for its own reads", async () => {
    await mount(section());
    expect($('[data-testid="su-sec-ticket_systems-toggle"]')?.getAttribute("aria-expanded")).toBe("false");
    expect($('[data-testid="su-sec-ticket_systems-summary"]')?.textContent).toBe("Jira · shared · 2 projects mapped");
    expect($('[data-testid="body"]')).not.toBeNull();
    expect(($('[data-testid="body"]')!.parentElement as HTMLElement).hidden).toBe(true);
  });

  it("remembers open or closed for this viewer", async () => {
    await mount(section());
    $('[data-testid="su-sec-ticket_systems-toggle"]')!.click();
    await settle();
    expect($('[data-testid="su-sec-ticket_systems-toggle"]')?.getAttribute("aria-expanded")).toBe("true");
    expect(JSON.parse(window.localStorage.getItem(SECTIONS_KEY)!)).toEqual({ ticket_systems: true });
    unmount();
    await mount(section());
    expect($('[data-testid="su-sec-ticket_systems-toggle"]')?.getAttribute("aria-expanded")).toBe("true");
  });

  it("opens on a deep link or when it needs attention, whatever was remembered", async () => {
    window.localStorage.setItem(SECTIONS_KEY, JSON.stringify({ ticket_systems: false }));
    await mount(section({ focused: true }));
    expect($('[data-testid="su-sec-ticket_systems-toggle"]')?.getAttribute("aria-expanded")).toBe("true");
    unmount();
    await mount(section({ attention: true }));
    expect($('[data-testid="su-sec-ticket_systems-toggle"]')?.getAttribute("aria-expanded")).toBe("true");
  });

  it("works when storage throws", async () => {
    const real = window.localStorage.getItem;
    window.localStorage.getItem = () => { throw new Error("blocked"); };
    try {
      await mount(section({ defaultOpen: true }));
      expect($('[data-testid="su-sec-ticket_systems-toggle"]')?.getAttribute("aria-expanded")).toBe("true");
    } finally { window.localStorage.getItem = real; }
  });
});

describe("the folded lines", () => {
  const view = (repos: number, models: string[]) => ({
    repository: { connection: null, repos: Array.from({ length: repos }, (_, i) => ({ full_name: `a/r${i}`, pill: { tone: "green" } })) },
    github_accounts: [{ id: "a1", name: "paramg" }],
    models: models.map((label) => ({ label, pill: { tone: "green" } })),
  }) as unknown as SetupView;
  it("say what is there, or what is missing", () => {
    expect(repoSummary(view(2, []))).toBe("2 repos · 1 GitHub account");
    expect(repoSummary(view(0, []))).toBe("1 GitHub account · no repo yet");
    expect(modelSummary(view(0, ["Claude Sonnet", "GPT", "Ollama"]))).toBe("Claude Sonnet · 2 more");
    expect(modelSummary(view(0, []))).toBe("No model yet");
  });
});

describe("the area is Setup", () => {
  it("in the rail, with what it holds", () => {
    const shell = readFileSync(join(__dirname, "..", "app", "ConsoleShell.tsx"), "utf8");
    expect(shell).toContain('{ id: "setup", label: "Setup", sub: "Repos, models and ticket systems"');
    expect(shell).not.toContain("Repo and models");
  });
});
