// @vitest-environment happy-dom
/**
 * One status, one action (skills-mcp spec §5.2, test 2): for each of the eight states a card
 * shows exactly one pill and one button, with the §8 words.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AbilityStatus } from "../lib/kit-types";
import { $$, cleanup, click, render } from "./_helpers/kit-render";
import { ability, clone, KITS } from "./_helpers/kit-api";

const { AbilityCard } = await import("../app/skills/ability-card");

afterEach(() => cleanup());

const CASES: [AbilityStatus, string, string][] = [
  ["ready", "Ready", "Test"], ["unheld", "No one holds", "Give to agents"], ["setup", "Set up", "Connect"],
  ["needs_repo", "Needs repo", "Connect repo ↗"], ["testing", "Testing…", "Testing…"], ["failing", "Failing", "Fix"],
  ["review", "Review", "Review"], ["off", "Off", "Turn on"],
];

describe("an ability card", () => {
  it.each(CASES)("%s: one pill (%s) and one button (%s)", async (status, pill, button) => {
    const a = { ...clone(ability(KITS.fresh, "web_research")), status: status === "testing" ? "ready" as const : status };
    const onAction = vi.fn();
    await render(<AbilityCard ability={a} testing={status === "testing"} onOpen={() => {}} onAction={onAction} />);
    const pills = $$('[data-testid="kit-pill"]');
    expect(pills.map((p) => p.textContent)).toEqual([pill]);
    const actions = $$('[data-testid="kit-action-web_research"]');
    expect(actions.map((b) => b.textContent)).toEqual([button]);
    if (status === "needs_repo") expect(actions[0].getAttribute("href")).toBe("?area=setup&focus=repository");
    if (status === "testing") expect((actions[0] as HTMLButtonElement).disabled).toBe(true);
  });

  it("a failing or review card says why in one short line; the rest say nothing more", async () => {
    const failing = ability(KITS.failing, "email");
    await render(<AbilityCard ability={failing} onOpen={() => {}} onAction={() => {}} />);
    const line = $$('[data-testid="kit-card-line-email"]')[0];
    expect(line.textContent).toBe("Token rejected");
    expect(line.textContent!.split(/\s+/).length).toBeLessThanOrEqual(6);
    cleanup();
    await render(<AbilityCard ability={ability(KITS.ready, "email")} onOpen={() => {}} onAction={() => {}} />);
    expect($$('[data-testid="kit-card-line-email"]')).toEqual([]);
  });

  it("the button acts; the rest of the card opens the drawer", async () => {
    const onOpen = vi.fn(); const onAction = vi.fn();
    await render(<AbilityCard ability={ability(KITS.ready, "web_research")} onOpen={onOpen} onAction={onAction} />);
    await click('[data-testid="kit-action-web_research"]');
    expect(onAction).toHaveBeenCalledWith("test");
    expect(onOpen).not.toHaveBeenCalled();
    await click('[data-testid="kit-card-web_research"]');
    expect(onOpen).toHaveBeenCalled();
  });
});
