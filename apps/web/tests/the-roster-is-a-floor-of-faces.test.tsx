// @vitest-environment happy-dom
/**
 * The roster and the Passport, as the lead walked them live (1549x784) — and what was wrong:
 *
 *  (a) the roster sat in a narrow centred column: it now takes the width it is given, as a
 *      responsive grid of cards no narrower than 260px;
 *  (b) faces were ~36px: a roster face is 64px and moves (idle breathes, working sweeps), the
 *      Passport hero is 128px;
 *  (c) callsigns truncated even when short ("Ro…"): the callsign never truncates, the role and
 *      tagline give way with their full text as a tooltip;
 *  (d) pressing a card did nothing: the whole card opens the Passport; Talk is an icon button;
 *  (e) hover actions were drawn on top of the role: the actions have their own footer row,
 *      always visible, real buttons a keyboard reaches;
 *  (f) agents hid behind a "+12" tile: everyone is drawn, a quiet `Show n more` only past 30;
 *  (g) the certificate explains itself (a tooltip per row, copy for ID and Version), and an
 *      unregistered agent gets a Register action instead of red "No registered role";
 *  (h) the Profile's archetype tiles are real pickers with real faces.
 *
 * Rendered and asserted on the DOM and on the stylesheet rules that drive it — happy-dom does no
 * layout, so the rules that MAKE the layout are what is held.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { V2Agent } from "../lib/v2/workforce";
import { DETAIL, PASSPORT, installAgentWorldApi } from "./_helpers/agent-world-api";
import { overBudget } from "./_helpers/copy-budget";
import { cssOnly } from "./_helpers/source";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { RosterZones, ROSTER_FACE_PX, ROSTER_FOLD_AT } = await import("../app/agents/roster-zones");
const { AgentDetail } = await import("../app/agent-detail-ui");
const { PASSPORT_FACE_PX, CERT_TIPS } = await import("../app/agents/passport-header");
const { ARCH_FACE_PX } = await import("../app/agents/passport-persona");
const { resetLiveStores } = await import("../lib/agents-live");
const { identityOf } = await import("../lib/agent-world");

const CSS = cssOnly("app/agents/agent-world.css");
const ALL_CSS = CSS + cssOnly("app/globals.css") + cssOnly("app/design-system.css") + cssOnly("app/workflows/library.css");
/** Every declaration block whose selector list names `sel` exactly (as a whole selector). */
function rules(css: string, sel: string): string[] {
  const out: string[] = [];
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = m[1].split(",").map((x) => x.trim());
    if (selectors.includes(sel)) out.push(m[2]);
  }
  return out;
}

const agent = (id: string, over: Partial<V2Agent> = {}): V2Agent => ({
  id, name: id.replace(/_/g, " "), role: id, status: "IDLE", task: "No work in this window.", model: null,
  tokens: null, spend_usd: null, calls: 0, autonomy_level: 1, trust_score: 50, successful_runs: 3, violations: 0,
  hidden_failures: 0, last_seen_seconds: null, run_id: null, project: null, last_touched: null, registered: true,
  ...over,
} as V2Agent);

// The shipped cast — the very agents whose callsigns were cut ("Ro…", "For…", "Sent…", "Q…").
const CAST = ["adversarial_reviewer", "software_engineer", "security_reviewer", "docs_writer", "qa", "pm",
  "release_manager", "design_critic", "finops_sentinel", "chief_architect", "engineering_captain",
  "backend_engineer", "frontend_engineer", "operations_engineer", "sre", "visionary_founder",
  "first_principles_researcher", "relentless_operator", "teacher_who_simplifies", "product_manager"];

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  resetLiveStores();
  vi.unstubAllGlobals();
});
async function settle() {
  for (let i = 0; i < 12; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

async function roster(agents: V2Agent[] = CAST.map((k) => agent(k, { name: identityOf(k).name }))) {
  const opened: string[] = [];
  const asked: string[] = [];
  const registered: string[] = [];
  await render(<RosterZones agents={agents} live={new Map([["qa", { state: "working", line: "running tests" }]])}
                            onOpen={(k) => opened.push(k)} onAsk={(k) => asked.push(k)}
                            onRegister={(n) => registered.push(n)} />);
  return { opened, asked, registered };
}

describe("(a) the roster uses the width it is given", () => {
  it("is a responsive grid of cards no narrower than 260px, with nothing capping its width", () => {
    const grid = rules(CSS, ".ag-zone-grid").join(";");
    expect(grid).toMatch(/grid-template-columns:\s*repeat\(auto-fill,\s*minmax\(min\(100%,\s*260px\),\s*1fr\)\)/);
    for (const sel of [".ag-roster", ".ag-zone", ".ag-zone-grid", ".roster", ".wf-agents"]) {
      for (const body of rules(ALL_CSS, sel)) expect(body, `${sel} caps its width`).not.toMatch(/max-width|margin:\s*0 auto/);
    }
    expect(rules(CSS, ".ag-roster").join(";")).toMatch(/width:\s*100%/);
  });

  it("a card is not also a legacy .roster-card, whose 3-column row grid squeezed it", async () => {
    await roster();
    for (const card of $$("article")) {
      expect(card.className).toBe("ag-card");
      expect(card.querySelector(".roster-actions, .roster-task")).toBeNull();
    }
  });
});

describe("(b) faces are the hero", () => {
  it("a roster face is at least 56px and its idle and working motion is defined for it", async () => {
    await roster();
    expect(ROSTER_FACE_PX).toBeGreaterThanOrEqual(56);
    const faces = $$("article .ag-card-face .ag-avatar");
    expect(faces).toHaveLength(CAST.length);
    for (const f of faces) expect(Number(f.dataset.size)).toBeGreaterThanOrEqual(56);
    // Working sweeps (the kit's rule, any size); idle breathes and blinks on a roster face.
    expect($('[data-testid="wf-agent-qa"] .ag-av')?.getAttribute("data-state")).toBe("working");
    expect(CSS).toMatch(/\.ag-card-face \.ag-av\[data-state="idle"\] \.av-body \{ animation: agav-breathe/);
    expect(CSS).toMatch(/\.ag-card-face \.ag-av\[data-state="idle"\] \.av-eyes \{\s*animation: agav-blink/);
    // ...and holds still under reduced motion (the kit's blanket rule covers every .ag-av part).
    expect(CSS).toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.ag-av, \.ag-av \*/);
  });

  it("the Passport hero is at least 120px", async () => {
    installAgentWorldApi();
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    expect(PASSPORT_FACE_PX).toBeGreaterThanOrEqual(120);
    expect(Number($(".ag-hero-face .ag-avatar")?.dataset.size)).toBeGreaterThanOrEqual(120);
  });
});

describe("(c) the callsign never truncates; role and tagline do, with a tooltip", () => {
  it("in the DOM", async () => {
    await roster();
    for (const card of $$("article")) {
      const name = card.querySelector<HTMLElement>(".ag-card-name")!;
      expect(name.textContent!.length).toBeGreaterThan(0);
      for (let el: HTMLElement | null = name; el && el !== card; el = el.parentElement) {
        expect(el.classList.contains("ag-trunc"), `${card.dataset.testid}: the name sits in a truncating box`).toBe(false);
      }
      for (const sel of [".ag-card-role", ".ag-card-tagline"]) {
        const el = card.querySelector<HTMLElement>(sel);
        if (!el) continue;
        expect(el.classList.contains("ag-trunc")).toBe(true);
        expect(el.getAttribute("title")).toBe(el.textContent);
      }
    }
    expect($('[data-testid="ag-name-adversarial_reviewer"]')?.textContent).toBe("Rook");
  });

  it("in the stylesheet: the name wraps, never ellipses", () => {
    const name = rules(CSS, ".ag-card-name").join(";");
    expect(name).not.toMatch(/text-overflow|nowrap|overflow:\s*hidden/);
    expect(name).toMatch(/overflow-wrap:\s*anywhere/);
    expect(rules(CSS, ".ag-card-open").join(";")).toMatch(/max-width:\s*100%/);
    expect(rules(CSS, ".ag-trunc").join(";")).toMatch(/text-overflow:\s*ellipsis/);
  });
});

describe("(d) the whole card opens the Passport; Talk is its own icon button", () => {
  it("a press anywhere on the card opens it — face, tagline, presence line, the card itself", async () => {
    const { opened, asked } = await roster();
    const card = $('[data-testid="wf-agent-security_reviewer"]')!;
    for (const target of [card, card.querySelector(".ag-card-face")!, card.querySelector(".ag-card-tagline")!,
                          card.querySelector(".ag-card-line")!, card.querySelector(".ag-card-name")!]) {
      await act(async () => { (target as HTMLElement).click(); });
    }
    expect(opened).toEqual(Array(5).fill("security_reviewer"));
    expect(asked).toEqual([]);
  });

  it("the card's name is a real button — Tab reaches it, Enter opens — labelled with who it is", async () => {
    const { opened } = await roster();
    const open = $('[data-testid="wf-open-qa"]')!;
    expect(open.tagName).toBe("BUTTON");
    expect(open.getAttribute("tabindex")).toBeNull();
    expect(open.getAttribute("aria-label")).toBe("Open Vera, QA Lead");
    await act(async () => { open.click(); });
    expect(opened).toEqual(["qa"]);
  });

  it("Talk is an icon button that talks, and never also opens the Passport", async () => {
    const { opened, asked } = await roster();
    const talk = $('[data-testid="wf-ask-qa"]')!;
    expect(talk.tagName).toBe("BUTTON");
    expect(talk.querySelector("svg")).not.toBeNull();
    expect(talk.getAttribute("aria-label")).toBe("Talk to Vera");
    expect(talk.getAttribute("title")).toBe("Talk to Vera");
    await act(async () => { talk.click(); });
    expect(asked).toEqual(["qa"]);
    expect(opened).toEqual([]);
  });
});

describe("(e) actions have their own row and never sit on the text", () => {
  it("Talk and Register are in the footer, outside the name, role and tagline", async () => {
    const { registered, opened } = await roster([agent("qa", { name: "QA Lead" }), agent("unknown_bot", { registered: false })]);
    for (const card of $$("article")) {
      const actions = card.querySelector(".ag-card-actions")!;
      expect(actions.parentElement?.classList.contains("ag-card-foot")).toBe(true);
      for (const text of card.querySelectorAll(".ag-card-head, .ag-card-role, .ag-card-tagline, .ag-card-line")) {
        expect(text.contains(actions)).toBe(false);
        expect(text.querySelector("[data-testid^='wf-ask-'], [data-testid^='wf-register-']")).toBeNull();
      }
    }
    await act(async () => { $('[data-testid="wf-register-unknown_bot"]')!.click(); });
    expect(registered).toEqual(["unknown bot"]);
    expect(opened).toEqual([]);
  });

  it("in the stylesheet: not absolutely placed, not hidden until hover", () => {
    const actions = rules(CSS, ".ag-card-actions").join(";");
    expect(actions).not.toMatch(/position:\s*absolute|opacity:\s*0/);
    expect(CSS).not.toMatch(/\.ag-card:hover \.ag-card-actions/);
    expect(rules(CSS, ".ag-card").join(";")).toMatch(/grid-template-areas:\s*"face head" "line line" "foot foot"/);
  });
});

describe("(f) every agent is on the floor", () => {
  it("draws all of them, grouped NEEDS YOU / WORKING / IN A PANEL / IDLE, with no +n tile", async () => {
    const many = Array.from({ length: 26 }, (_, i) => agent(`role_${i}`));
    await render(<RosterZones agents={many}
                              live={new Map([["role_3", { state: "needs_you", line: "approve" }],
                                             ["role_4", { state: "working", line: "build" }],
                                             ["role_5", { state: "working", line: "PRD", inPanel: true }]])}
                              onOpen={() => {}} onAsk={() => {}} />);
    expect($$("article")).toHaveLength(26);
    expect($$("section").map((s) => s.dataset.zone)).toEqual(["needs_you", "working", "in_panel", "idle"]);
    expect($('[data-testid="ag-idle-more"]')).toBeNull();
    expect(ROSTER_FOLD_AT).toBeGreaterThanOrEqual(30);
  });
});

describe("(g) the Passport's certificate explains itself", () => {
  it("every row has a short tooltip; ID and Version copy", async () => {
    installAgentWorldApi();
    const writes: string[] = [];
    Object.defineProperty(navigator, "clipboard", { configurable: true,
      value: { writeText: async (t: string) => { writes.push(t); } } });
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    const labels = $$('[data-testid="ag-cert"] dt');
    expect(labels.map((d) => d.textContent)).toEqual(
      ["ID", "Born", "Born by", "From", "Sponsor", "Version", "Reports to", "Identity"]);
    for (const dt of labels) {
      const tip = dt.getAttribute("title") ?? "";
      expect(tip.length, `${dt.textContent} has no tooltip`).toBeGreaterThan(0);
      expect(tip.split(/\s+/).length, tip).toBeLessThanOrEqual(14);
    }
    expect(Object.keys(CERT_TIPS)).toHaveLength(8);
    expect($('[data-testid="ag-cert-identity"]')?.textContent).toBe("Local · tokens coming");
    await act(async () => { $('[data-testid="ag-copy-id"]')!.click(); });
    await act(async () => { $('[data-testid="ag-copy-version"]')!.click(); });
    expect(writes[0]).toBe(PASSPORT.agent_id);
    expect(writes[1]).toMatch(/^v4 /);
    expect($('[data-testid="ag-copy-born"]')).toBeNull();
  });

  it("an unregistered agent gets Register, not red text", async () => {
    installAgentWorldApi([(url) => (url.startsWith("/api/agents/qa")
      ? Response.json({ ...DETAIL, registered: false }) : undefined)]);
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    const box = $('[data-testid="agent-unregistered"]')!;
    expect(box.textContent).not.toContain("No registered role");
    expect(box.classList.contains("ad-bad")).toBe(false);
    const register = $('[data-testid="agent-register"]')!;
    expect(register.tagName).toBe("BUTTON");
    await act(async () => { register.click(); });
    await settle();
    // It opens New agent, already named after the agent it registers.
    expect($('[data-testid="aq-archetypes"], .aw-form, [role="dialog"][aria-label*="agent" i]')).not.toBeNull();
    expect(overBudget(box)).toEqual([]);
    expect(box.querySelector("p")).toBeNull();
  });
});

describe("(h) the archetype tiles are pickers", () => {
  it("each tile shows a real face (≥48px), its callsign and its role; the picked one says so", async () => {
    installAgentWorldApi();
    await render(<AgentDetail projectId="p1" roleKey="qa" actor="ceo" onClose={() => {}} />);
    const tiles = $$('[data-testid^="ag-arch-"]').filter((t) => t.dataset.testid !== "ag-arch-custom");
    expect(tiles.length).toBeGreaterThanOrEqual(4);
    expect(ARCH_FACE_PX).toBeGreaterThanOrEqual(48);
    for (const t of tiles) {
      expect(Number(t.querySelector<HTMLElement>(".ag-avatar")?.dataset.size)).toBeGreaterThanOrEqual(48);
      expect(t.querySelector(".ag-arch-name")?.textContent).toBeTruthy();
      expect(t.getAttribute("role")).toBe("radio");
    }
    await act(async () => { tiles[0].click(); });
    expect(tiles[0].getAttribute("aria-checked")).toBe("true");
    expect(rules(CSS, ".ag-arch-grid").join(";")).toMatch(/minmax\(132px/);
  });
});

describe("copy on the roster card", () => {
  it("stays inside the budget: labels, not sentences; every explanation is a tooltip", async () => {
    await roster([agent("qa", { name: "QA Lead" }), agent("unknown_bot", { registered: false })]);
    expect(overBudget($(".ag-roster")!)).toEqual([]);
    expect($$(".ag-roster p")).toHaveLength(0);
    const words = $$(".ag-roster button").filter((b) => !b.closest("[data-user-text]"))
      .map((b) => b.textContent?.trim()).filter(Boolean);
    expect(words.filter((w) => !/^(Register|Vera|unknown bot)$/.test(w!))).toEqual([]);
  });
});
