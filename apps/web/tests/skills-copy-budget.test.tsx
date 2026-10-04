// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs — on every Skills & MCP surface (skills-mcp spec §8, §10.2 test 1).
 *
 * The Kit (fresh, ready, failing), every ability drawer, Who can, the Library (both lists), Add
 * MCP (both tabs), the skill drawer, an MCP drawer, Test crew and Import are RENDERED from the
 * fixtures and their visible text walked: no run over six words outside a tooltip, no <p> inside
 * a kit- surface, and none of the words the spec bans. Text an operator or the API wrote (a
 * workflow name, a pasted value) is `data-user-text`; the data boundary is allowed under More.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { $, $$, cleanup, click, render } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { KitHome } = await import("../app/skills/kit-home");
const { AddMcpSheet } = await import("../app/skills/add-mcp-sheet");
const { McpDrawer } = await import("../app/skills/mcp-drawer");
const { TestCrewModal } = await import("../app/skills/test-crew-modal");
const { ImportSheet } = await import("../app/skills/import-sheet");

const SURFACES = [".kit-home", ".kit-drawer", ".kit-who", ".kit-lib", ".kit-modal"];
const SKIP = "[data-tip], [role='tooltip'], [title], textarea, script, style, option, [hidden], "
  + "[aria-hidden='true'], .ag-sr, [data-user-text], .kit-more";
const BANNED = /\b(capabilit(y|ies)|binding|executor|behaviou?r kind|json-rpc|data boundary|tiers?|governed|runtime|specpack|provenance|machinery|allowed callers|role key)\b/i;

afterEach(() => { cleanup(); vi.unstubAllGlobals(); try { localStorage.clear(); } catch { /* none */ } });

function visibleRuns(root: Element): string[] {
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("").trim();
    if (own) out.push(own);
  }
  return out;
}

function audit() {
  const roots = SURFACES.flatMap((s) => $$(s));
  return {
    surfaces: roots.length,
    over: [...new Set(roots.flatMap((r) => overBudget(r)))],
    paragraphs: roots.flatMap((r) => [...r.querySelectorAll("p")].map((p) => p.textContent ?? "")),
    banned: [...new Set(roots.flatMap(visibleRuns).filter((t) => BANNED.test(t)))],
  };
}

function clean(r: ReturnType<typeof audit>) {
  expect(r.surfaces).toBeGreaterThan(0);
  expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
}

describe("the Skills & MCP copy budget", () => {
  it.each(["fresh", "ready", "failing"] as const)("the Kit, %s", async (which) => {
    installKitApi(clone(KITS[which]));
    window.history.replaceState(null, "", "/?area=skills&setup=1");
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($$('[data-testid^="kit-card-"]').length).toBeGreaterThanOrEqual(10);
    clean(audit());
  });

  it("every ability drawer, with its menus open", async () => {
    const kit = clone(KITS.failing);
    for (const a of kit.abilities) {
      installKitApi(kit);
      window.history.replaceState(null, "", `/?area=skills&ability=${a.key}`);
      await render(<KitHome projectId="p1" actor="ceo" />);
      expect($(`[data-testid="kit-drawer-${a.key}"]`), a.key).not.toBeNull();
      await click('[data-testid="kit-drawer-more"]');
      if ($('[data-testid="kit-disconnect"]')) await click('[data-testid="kit-disconnect"]');
      const r = audit();
      expect([a.key, r.over, r.paragraphs, r.banned]).toEqual([a.key, [], [], []]);
      cleanup();
    }
  }, 20_000); // twelve drawers rendered in turn: allow for a loaded machine

  it("Who can", async () => {
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills&tab=who&role=pm");
    await render(<KitHome projectId="p1" actor="ceo" />);
    clean(audit());
  });

  it("the Library: skills, a new skill, internal shown; MCP and Import", async () => {
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills&tab=library");
    await render(<KitHome projectId="p1" actor="ceo" />);
    await click('[data-testid="kit-lib-internal"]');
    await click('[data-testid="kit-lib-new-skill"]');
    clean(audit());
    await click('[data-testid="kit-lib-mcp"]');
    await click('[data-testid="kit-lib-more"]');
    clean(audit());
  });

  it("Add MCP (both tabs), an MCP drawer, Test crew and Import", async () => {
    installKitApi();
    await render(<div>
      <AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />
      <div className="kit-lib"><McpDrawer projectId="p1" actor="ceo" server={SERVERS[1] as never} crew={[]}
                                          onClose={() => {}} onChanged={() => {}} onRemoved={() => {}} /></div>
      <TestCrewModal projectId="p1" actor="ceo" abilities={KITS.fresh.abilities} onClose={() => {}} onDone={() => {}}
                     onOpenAbility={() => {}} />
      <ImportSheet projectId="p1" actor="ceo" onClose={() => {}} onImported={() => {}} />
    </div>);
    await click('[data-testid="kit-cat-add-linear"]');
    clean(audit());
    await click('[data-testid="kit-add-custom"]');
    await click('[data-testid="kit-custom-auth-header"]');
    clean(audit());
  });
});
