// @vitest-environment happy-dom
/**
 * A team card says only what the board recorded (org-view spec §4.2): each row from its field,
 * a row with nothing true to say hidden, a plan-only spend with no dollars, `Gaps unknown` when
 * the Kit could not be read, and a `Missing` seat only where a missing role belongs. No `%`,
 * capacity or score anywhere.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { orgFetch, orgWire } from "./_helpers/org-board-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { OrgView } = await import("../app/agents/org-view");

let root: Root | null = null;
let host: HTMLElement | null = null;
beforeEach(() => { stubMatchMedia(["min-width: 1024px"]); window.history.replaceState(null, "", "/?area=workforce&view=org"); });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});
async function render(wire: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(orgFetch(() => wire).fn));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<OrgView projectId="p1" live={new Map()} actor="ceo" lines={() => <div />} />));
  for (let i = 0; i < 12; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const card = (k: string) => document.querySelector<HTMLElement>(`[data-testid="ag-orgteam-${k}"]`)!;
const row = (k: string, r: string) => card(k).querySelector<HTMLElement>(`[data-row="${r}"]`);
const ago = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

describe("a team card", () => {
  it("renders each row from its field", async () => {
    await render(orgWire({ teams: { engineering: {
      work: { done: 23, failed: 2, open: 3 },
      waiting: [{ role_key: "software_engineer", act_id: "a1", status: "active", label: "Build · Ship it #182",
                  since: ago(72), run_id: "wfr_182", attention_id: null }], waiting_total: 2,
      members_load: [{ role_key: "software_engineer", busy_seconds: 9 * 3600 }, { role_key: "backend_engineer", busy_seconds: 3 * 3600 },
                     { role_key: "frontend_engineer", busy_seconds: 0 }, { role_key: "engineering_captain", busy_seconds: 0 }],
      spend: { metered_usd: 4.1, plan_calls: 120 },
      models: [{ model: "sonnet-4.6", calls: 88 }, { model: "haiku", calls: 32 }, { model: "opus", calls: 1 }],
      gaps: { abilities: [{ ability: "tests_ci", label: "Run tests", roles: ["backend_engineer"] }], step_gaps: [],
              missing_roles: 1, kit_ok: true, kit_reason: null },
    } } }));
    expect(row("engineering", "work")!.querySelector("dd")!.textContent).toBe("Done 23Failed 2Open 3");
    const waiting = row("engineering", "waiting")!;
    expect(waiting.textContent).toContain("Waiting");
    expect(waiting.textContent).toContain("1h 12m");
    expect(waiting.textContent).toContain("Build · Ship it #182");
    expect(waiting.querySelector("a")!.getAttribute("href")).toContain("area=delivery_workflows");
    expect(waiting.querySelector("a")!.getAttribute("href")).toContain("wfr=wfr_182");
    expect(waiting.textContent).toContain("+1 more waiting");
    expect(row("engineering", "load")!.textContent).toContain("9h");
    expect(row("engineering", "with")!.textContent).toBe("Hands toQuality ×31Operations ×6");
    expect(row("engineering", "gets")!.textContent).toBe("Gets fromLeadership ×19");
    expect(row("engineering", "spend")!.textContent).toBe("Spend$4.10 metered120 on plan");
    expect(row("engineering", "models")!.textContent).toContain("+1");
    const gaps = row("engineering", "gaps")!;
    expect(gaps.textContent).toContain("Run tests");
    expect(gaps.textContent).toContain("1 missing role");
    expect(gaps.textContent).toContain("Give");
  });

  it("hides a row with nothing true to say, and collapses an idle team with no gaps", async () => {
    await render(orgWire());
    expect(row("engineering", "work")).toBeNull();
    expect(row("engineering", "spend")).toBeNull();
    expect(row("engineering", "waiting")).toBeNull();
    // Engineering hands work in the window, so it is not "no work".
    expect(row("engineering", "none")).toBeNull();
    // Thinkers: no work, no gaps, nobody live → head and faces only.
    expect(card("thinkers").dataset.collapsed).toBe("true");
    expect(card("thinkers").querySelector("dl")).toBeNull();
  });

  it("a team with no work but a gap says No work in 7d and keeps its Gaps", async () => {
    await render(orgWire({ teams: { thinkers: { gaps: { abilities: [{ ability: "web_research", label: "Web research",
      roles: ["visionary_founder"] }], step_gaps: [], missing_roles: 0, kit_ok: true, kit_reason: null } } } }));
    expect(row("thinkers", "none")!.querySelector("dd")!.textContent).toBe("No work in 7d");
    expect(row("thinkers", "gaps")!.textContent).toContain("Web research");
  });

  it("plan-only spend shows calls, never $0.00", async () => {
    await render(orgWire({ teams: { quality: { spend: { metered_usd: 0, plan_calls: 120 } } } }));
    expect(row("quality", "spend")!.textContent).toBe("Spend120 on plan");
    expect(row("quality", "spend")!.textContent).not.toContain("$");
  });

  it("says Gaps unknown when the Kit could not be read", async () => {
    await render(orgWire({ teams: { media: { gaps: { abilities: [], step_gaps: [], missing_roles: 0, kit_ok: false,
                                                       kit_reason: "kit file unreadable" } } } }));
    const g = row("media", "gaps")!;
    expect(g.textContent).toContain("Gaps unknown");
    expect(g.querySelector("[title]")!.getAttribute("title")).toBe("Couldn't read the Kit: kit file unreadable");
  });

  it("draws a Missing seat only on the team the missing role belongs to", async () => {
    await render(orgWire({ missing_roles: [{ role_key: "data_engineer", team: "engineering",
      steps: [{ workflow_key: "ship_it", workflow_name: "Ship it", step_id: "load", step_title: "Load" }] }] }));
    const seat = document.querySelector<HTMLElement>('[data-testid="ag-orgteam-missing-data_engineer"]')!;
    expect(card("engineering").contains(seat)).toBe(true);
    expect(seat.textContent).toBe("Missing");
    expect(seat.getAttribute("title")).toBe("data_engineer is named by 1 step. Nobody fills it.");
    expect(document.querySelectorAll(".ag-orgteam-missing")).toHaveLength(1);
  });

  it("an unregistered agent doing work wears Not registered, and Yours offers Register", async () => {
    await render(orgWire({ teams: { yours: { members: ["ghost_writer"] } },
                           agents: {}, }));
    // The fixture's agents list has no ghost_writer: it is not registered.
    const yours = card("yours");
    expect(yours.textContent).toContain("Not registered");
  });

  it("never says %, capacity or score, and the totals line says metered, plan and untied", async () => {
    await render(orgWire());
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/%|capacity|score|utilisation|productivity/i);
    const totals = document.querySelector('[data-testid="ag-org-totals"]')!;
    expect(totals.textContent).toContain("$12.40 metered");
    expect(totals.textContent).toContain("310 on plan");
    expect(totals.textContent).toContain("$0.84 not tied to an agent");
  });
});
