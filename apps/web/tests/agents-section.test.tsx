// @vitest-environment happy-dom
/**
 * Grants from the drawer (skills-mcp spec §5.3 3, test 5): a toggle is ONE PUT holders carrying
 * the whole new set; an ability that is not set up cannot be given, only taken away; the toast
 * names the agent by its callsign.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render } from "./_helpers/kit-render";
import { ability, clone, installKitApi, KITS } from "./_helpers/kit-api";

const { AgentsSection } = await import("../app/skills/agents-section");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("an ability's agents", () => {
  it("a toggle is one PUT with the whole new set", async () => {
    const calls = installKitApi();
    const kit = clone(KITS.fresh);
    const research = ability(kit, "web_research");
    const toasts: string[] = [];
    window.addEventListener("boss:toast", (e) => toasts.push((e as CustomEvent).detail.message));
    await render(<AgentsSection ability={research} crew={kit.crew ?? []} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await click('[data-testid="kit-agent-toggle-backend_engineer"]');
    const puts = calls.filter((c) => c.body?.action === "holders");
    expect(puts).toHaveLength(1);
    expect(new Set(puts[0].body!.roles as string[])).toEqual(new Set([...research.holders, "backend_engineer"]));
    expect(toasts.at(-1)).toBe("Gave Web research to Strata");
  });

  it("an ability not set up cannot be given, only taken away", async () => {
    installKitApi();
    const kit = clone(KITS.fresh);
    const email = ability(kit, "email");
    await render(<AgentsSection ability={email} crew={kit.crew ?? []} projectId="p1" actor="ceo" onChanged={() => {}} />);
    const off = $('[data-testid="kit-agent-toggle-qa"]') as HTMLButtonElement;
    expect(off.disabled).toBe(true);
    expect(off.dataset.tip).toBe("Set up Email (SMTP) first");
    const held = $(`[data-testid="kit-agent-toggle-${email.holders[0]}"]`) as HTMLButtonElement;
    expect(held.disabled).toBe(false);
  });

  it("the role's recommended agents carry a star", async () => {
    installKitApi();
    const kit = clone(KITS.fresh);
    await render(<AgentsSection ability={ability(kit, "web_search")} crew={kit.crew ?? []} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-agent-rec-backend_engineer"]')).not.toBeNull();
    expect($('[data-testid="kit-agent-rec-qa"]')).toBeNull();
  });
});
