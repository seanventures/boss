// @vitest-environment happy-dom
/**
 * The Library (tester K12, K13, K14, K22).
 *
 * K12 granting a server from its drawer shows at once, and an older read landing late never puts
 *     the row back as it was.
 * K13 a shipped skill is named in the Kit's words; its status is its ability's; "Used in" counts
 *     its own steps, not the whole ability's.
 * K14 a read that fails is a red bar with Retry, never an empty list.
 * K22 the MCP drawer stars recommended agents first and has Try it; an empty search says so.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render, settle, type } from "./_helpers/kit-render";
import { ability, clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";
import type { KitWithCrew } from "../lib/kit-api";

vi.mock("../lib/dock-subject", () => ({ openDockAbout: () => {} }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { Library } = await import("../app/skills/library");
const { readKitParams } = await import("../app/skills/kit-url");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

async function mount(kit: KitWithCrew, search: string, on?: Parameters<typeof installKitApi>[1]) {
  window.history.replaceState(null, "", search);
  const calls = installKitApi(kit, on);
  await render(<Library projectId="p1" actor="ceo" kit={kit} crew={kit.crew ?? []} initial={readKitParams()} onKitChanged={() => {}} />);
  return calls;
}

describe("skills are named and judged by the Kit (K13)", () => {
  it("a shipped skill reads as its tool label, shows its ability's status, and counts its own steps", async () => {
    const kit = clone(KITS.fresh);
    const prs = ability(kit, "open_prs");
    prs.used_in = [
      { workflow_key: "w1", workflow_name: "W1", step_id: "build", step_title: "build", via: "capability:build" },
      { workflow_key: "w2", workflow_name: "W2", step_id: "pr", step_title: "PR", via: "skill:github.create_pr_real" },
    ];
    await mount(kit, "/?area=skills&tab=library");
    const row = $('[data-testid="kit-skill-github.create_pr_real"]')!;
    expect(row.querySelector(".kit-list-name")!.textContent).toBe("Open a PR");
    expect(row.querySelector('[data-testid="kit-pill"]')!.textContent).toBe("Needs repo");
    expect(row.textContent).toContain("Used in 1");
  });

  it("an empty search says No matches", async () => {
    await mount(clone(KITS.fresh), "/?area=skills&tab=library");
    await type('input[aria-label="Find"]', "zzzz-nothing");
    expect($('[data-testid="kit-lib-empty"]')!.textContent).toContain("No matches");
  });
});

describe("a failed read is said (K14)", () => {
  it("/api/skills answering 502 shows Can't reach BOSS API · Retry, and Retry loads", async () => {
    let down = true;
    await mount(clone(KITS.fresh), "/?area=skills&tab=library", (url) => {
      if (down && url.startsWith("/api/skills")) {
        return new Response(JSON.stringify({ error: "Could not reach the BOSS API: fetch failed" }), { status: 502 });
      }
      return undefined;
    });
    expect($('[data-testid="kit-lib-error"]')!.textContent).toContain("Can't reach BOSS API");
    expect($('[data-testid="kit-skill-list"]')).toBeNull();
    down = false;
    await click('[data-testid="kit-lib-retry"]');
    expect($('[data-testid="kit-lib-error"]')).toBeNull();
    expect($('[data-testid="kit-skill-github.create_pr_real"]')).not.toBeNull();
  });
});

describe("the MCP drawer (K12, K22)", () => {
  it("stars the agents its ability recommends, first, and offers Try it", async () => {
    const kit = clone(KITS.fresh);
    const docs = ability(kit, "library_docs");
    docs.mcp_names = ["context7"];
    docs.try = { field: { name: "libraryName", label: "Library", placeholder: "react" } };
    await mount(kit, "/?area=skills&tab=library&lib=mcp&mcp=mcp_c7");
    const agents = $$('[data-testid^="kit-mcp-agent-"][data-on]').map((li) => li.dataset.testid!.replace("kit-mcp-agent-", ""));
    const rec = docs.recommended;
    expect(agents.slice(0, rec.length).sort()).toEqual([...rec].sort());
    for (const r of rec) expect($(`[data-testid="kit-mcp-agent-rec-${r}"]`)).not.toBeNull();
    expect($('[data-testid="kit-mcp-drawer"] [data-testid="kit-try"]')).not.toBeNull();
  });

  it("a grant shows at once, and a read that lands late does not undo it", async () => {
    const kit = clone(KITS.fresh);
    let release: (() => void) | null = null;
    let lists = 0;
    const granted = JSON.parse(JSON.stringify(SERVERS));
    granted[1].references.agents = [{ role_key: "software_engineer", tools: [] }];
    await mount(kit, "/?area=skills&tab=library&lib=mcp&mcp=mcp_c7", (url, _body, method) => {
      if (!url.startsWith("/api/mcp") || method !== "GET" || url.includes("view=")) return undefined;
      lists += 1;
      if (lists === 2) {
        // The review's re-read: answered only after the grant's, with the row as it was.
        return new Promise<Response>((r) => { release = () => r(new Response(JSON.stringify({ items: SERVERS }))); }) as unknown as Response;
      }
      if (lists >= 3) return { items: granted };
      return undefined;
    });
    await click('[data-testid="kit-review-open-context7"]');
    await click('[data-testid="kit-review-confirm"]');
    await click('[data-testid="kit-mcp-agent-toggle-software_engineer"]');
    expect($('[data-testid="kit-mcp-agent-toggle-software_engineer"]')!.getAttribute("aria-checked")).toBe("true");
    release!();
    await settle();
    expect($('[data-testid="kit-mcp-agent-toggle-software_engineer"]')!.getAttribute("aria-checked")).toBe("true");
  });
});
