// @vitest-environment happy-dom
/**
 * What the independent walk of Agents › Live found (ah/home @1c42a84c), each as the rule it
 * broke. Every case here failed before its fix.
 *
 *   D1  Skip on a failed step sent no reason, the engine refused it (422), and the refusal
 *       surfaced as a raw lowercase server fragment.
 *   D2  "need you" counted setup gaps as well as decisions, so Live disagreed with Approvals.
 *   D3  A reject fails the step: the toast says so.
 *   D6  "Why" repeated the approve effect.
 *   D10 Searching an agent dimmed only the team faces, not cards and lanes.
 *   D11 A gate was drawn twice (the captain ringed amber AND the You hop); a failed run's
 *       starter read "You, stuck".
 *   D12 A failed run's card could be folded under "+6 more".
 *   D15 A phone row said the step twice (2/3 and step 2 of 3).
 *   D16 A plain decision was called "Gate"; an unattributed card wore the person glyph.
 *   D18 A lane had no names without hovering.
 *   D20 A failed row's tooltip ran past 14 words.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ActionResult } from "../lib/action-result";
import { board, failed, gate, mission } from "./_helpers/live-board-fixture";
import { $, $$, click, mount, settle, stateOf, unmount } from "./_helpers/live-render";
import { act } from "react";

const decide = vi.fn<(form: FormData) => Promise<ActionResult>>();
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  resolveDecisionAction: (form: FormData) => decide(form),
}));
const post = vi.fn<(action: string, payload: Record<string, unknown>, key?: string) => Promise<unknown>>();
vi.mock("../app/workflows/delivery-api", async (original) => ({
  ...(await original<typeof import("../app/workflows/delivery-api")>()),
  post: (action: string, payload: Record<string, unknown>, key?: string) => post(action, payload, key),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { LiveBoardView } = await import("../app/agents/live-board");
const { MissionRow, laneHops, clipWords } = await import("../app/agents/mission-row");
const { parseMission } = await import("../lib/agents-live-board");

const noop = () => {};
const view = (body = board(), search?: string) => (
  <LiveBoardView projectId="p1" actor="ceo" live={stateOf(body)} onOpenMap={noop} onActivity={noop}
                 onDirectory={noop} onNewAgent={noop} search={search} />
);
const skippable = () => failed({
  answers: [
    { key: "retry_step", label: "Retry", enabled: true, reason: null, step_id: "build" },
    { key: "skip_step", label: "Skip", enabled: true, reason: null, step_id: "build" },
  ],
});
const ready = (key: string, title: string) => ({ id: `ready:${key}`, kind: "readiness", title, detail: "",
                                                 role_key: null, actions: [], answers: [], decide_inline: false,
                                                 fix: { area: "setup", label: "Fix" } });

let toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
beforeEach(() => {
  decide.mockReset(); post.mockReset(); toasts = [];
  window.addEventListener("boss:toast", onToast);
  window.history.replaceState(null, "", "/?area=workforce");
});
afterEach(() => { window.removeEventListener("boss:toast", onToast); unmount(); });

async function type(el: HTMLInputElement, value: string) {
  await act(async () => {
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}

describe("D1 Skip asks why, and sends it", () => {
  it("does not post until a reason is given, then posts it and says Skipped", async () => {
    post.mockResolvedValue({ ok: true, data: {} });
    await mount(view(board({ attention: [skippable()] })));
    await click($('[data-testid="ag-att-answer-skip_step"]'));
    expect(post).not.toHaveBeenCalled();
    expect($('[data-testid="ag-att-skip"]')?.textContent).toContain("Why skip?");
    await type($('[data-testid="ag-att-skip-why"]') as HTMLInputElement, "Known flake, shipping anyway");
    await click($('[data-testid="ag-att-skip-confirm"]'));
    expect(post).toHaveBeenCalledTimes(1);
    const [action, body] = post.mock.calls[0];
    expect(action).toBe("skip_step");
    expect(body.reason).toBe("Known flake, shipping anyway");
    expect(toasts).toEqual(["Skipped step 3"]);
  });

  it("a refused standalone gate offers Retry and Stop, never Skip; Stop asks first", async () => {
    post.mockResolvedValue({ ok: true, data: {} });
    const refusedGate = failed({
      title: "“Approve release” was rejected on Ship it #182",
      answers: [
        { key: "retry_step", label: "Retry", enabled: true, reason: null, step_id: "approve" },
        { key: "stop", label: "Stop this run", enabled: true, reason: null, step_id: "approve" },
      ],
    });
    await mount(view(board({ attention: [refusedGate] })));
    expect($('[data-testid="ag-att-answer-skip_step"]')).toBeNull();
    await click($('[data-testid="ag-att-answer-stop"]'));
    expect(post).not.toHaveBeenCalled();
    expect($('[data-testid="ag-att-stop"]')?.textContent).toContain("Stop this run?");
    await click($('[data-testid="ag-att-stop-confirm"]'));
    expect(post.mock.calls[0][0]).toBe("stop");
    expect(String(post.mock.calls[0][1].reason ?? "")).not.toBe("");
    expect(toasts).toEqual(["Run stopped"]);
  });

  it("Retry needs no reason", async () => {
    post.mockResolvedValue({ ok: true, data: {} });
    await mount(view(board({ attention: [skippable()] })));
    await click($('[data-testid="ag-att-answer-retry_step"]'));
    expect(post.mock.calls[0][0]).toBe("retry_step");
    expect(toasts).toEqual(["Retrying step 3"]);
  });

  it("a refusal is a short sentence of ours; the server's words are only the tooltip", async () => {
    post.mockResolvedValue({ ok: false, error: "reason is required" });
    await mount(view(board({ attention: [skippable()] })));
    await click($('[data-testid="ag-att-answer-retry_step"]'));
    const err = $('[data-testid="ag-att-error"]')!;
    expect(err.textContent).toBe("Couldn't retry step 3");
    expect(err.title).toBe("reason is required");
    expect(toasts).toEqual([]);
  });
});

describe("D3 a reject fails the step", () => {
  it("says Rejected, step failed", async () => {
    decide.mockResolvedValue({ ok: true, message: "Rejected." });
    await mount(view(board({ attention: [gate()] })));
    await click($('[data-testid="ag-att-reject"]'));
    await click($('[data-testid="ag-att-confirm-reject"]'));
    await act(async () => { await new Promise((r) => setTimeout(r, 250)); });
    expect(toasts).toEqual(["Rejected, step failed"]);
  });
});

describe("D2 the number is decisions; setup is a line, not a card", () => {
  it("heads NEEDS YOU with the decisions, notes what stopped, and lists setup as links", async () => {
    await mount(view(board({ attention: [gate(), failed(), ready("github", "Connect a repository")] })));
    expect($('[data-testid="ag-live-needs-h-count"]')?.textContent).toBe("1");
    expect($('[data-testid="ag-live-needs-h-extra"]')?.textContent).toBe("· 1 stopped");
    expect($$("[data-att-card]").map((c) => c.dataset.attId)).toEqual(["run:wfr_ship:build", "dec_gate"]);
    expect($('[data-testid="ag-live-setup-ready:github"]')?.textContent).toBe("Setup: Connect a repository");
  });

  it("setup alone is not 'needs you'", async () => {
    await mount(view(board({ attention: [ready("model", "Connect a model")] })));
    expect($('[data-testid="ag-live-nothing-needs"]')).not.toBeNull();
    expect($$("[data-att-card]")).toHaveLength(0);
  });
});

describe("D12 a stopped run's card is never folded away", () => {
  it("shows the failed step even behind six gates", async () => {
    const gates = Array.from({ length: 6 }, (_, i) => gate({ id: `dec_${i}`, decision_id: `dec_${i}` }));
    await mount(view(board({ attention: [...gates, failed()] })));
    const shown = $$("[data-att-card]").map((c) => c.dataset.attId);
    expect(shown[0]).toBe("run:wfr_ship:build");
    expect(shown).toHaveLength(4);
    expect($('[data-testid="ag-att-more"]')?.textContent).toBe("+3 more");
  });
});

describe("D6 / D16 the card says the right things", () => {
  it("Why is the server's detail, and is dropped when it only repeats the approve effect", async () => {
    await mount(view(board({ attention: [gate({ detail: "Releases the step and the workflow continues from it." })] })));
    await click($('[data-testid="ag-att-details"]'));
    expect($('[data-testid="ag-att-details-body"]')?.textContent).not.toContain("Why");
  });

  it("a plain decision is a Decision, and an unattributed card wears the BOSS glyph", async () => {
    await mount(view(board({ attention: [gate({ id: "dec_g", decision_id: "dec_g", decision_kind: "general",
                                                role_key: null, run: null })] })));
    const card = $('[data-testid="ag-att-dec_g"]')!;
    expect(card.querySelector(".ag-att-context")?.textContent).toBe("Decision");
    expect(card.querySelector('[data-variant="boss"]')).not.toBeNull();
    expect(card.querySelector('[data-variant="you"], [data-variant="person"]')).toBeNull();
  });
});

describe("D10 the search dims the faces that don't match, on cards and lanes", () => {
  it("dims everyone but Forge", async () => {
    await mount(view(board(), "forge"));
    expect($('[data-testid="ag-att-dec_gate"] .ag-att-face')?.dataset.dim).toBe("true");
    expect($('[data-testid="ag-att-run:wfr_ship:build"] .ag-att-face')?.dataset.dim).toBeUndefined();
    const lane = $$('[data-testid="ag-mission-wfr_182"] .ag-mission-hop');
    expect(lane.map((h) => h.dataset.dim ?? "")).toEqual(["true", "true", ""]);
  });
});

describe("D11 a lane draws a gate once", () => {
  it("never rings an agent amber for a wait, and a starter is never stuck", () => {
    const m = parseMission(mission({ state: "waiting_you", attention_id: "dec_gate", hops: [
      { act_id: "a0", role_key: null, person: "ceo", state: "stuck", label: "Started", edge_label: "" },
      { act_id: "a1", role_key: "release_manager", state: "needs_you", label: "Gate", edge_label: "" },
    ] }));
    expect(laneHops(m).map((h) => (h.kind === "hop" ? h.state : h.kind))).toEqual(["done", "idle"]);
  });

  it("the drawn lane has one amber face: the You hop", async () => {
    await mount(view(board({ missions: [mission({ run_id: "wfr_g", state: "waiting_you", attention_id: "dec_gate", hops: [
      { act_id: "a1", role_key: "release_manager", state: "needs_you", label: "Gate", edge_label: "" },
    ] })] })));
    const amber = $$('[data-testid="ag-mission-wfr_g"] [data-state="needs_you"]');
    expect(amber.length).toBeGreaterThan(0);
    expect(amber.every((e) => e.closest(".ag-mission-you-hop"))).toBe(true);
    expect($('[data-testid="ag-mission-you-wfr_g"] [data-state="needs_you"]')).not.toBeNull();
  });
});

describe("D15 / D18 / D20 the row", () => {
  it("names the faces in the lane on a wide screen", async () => {
    const m = parseMission(mission());
    await mount(<ul><MissionRow mission={m} band="wide" now={null} /></ul>);
    expect($$(".ag-mission-hop-name").map((n) => n.textContent)).toEqual(["You", "Juno", "Forge"]);
  });

  it("says the step once on a phone", async () => {
    const m = parseMission(mission());
    await mount(<ul><MissionRow mission={m} band="phone" now={null} /></ul>);
    const text = $('[data-testid="ag-mission-wfr_182"]')!.textContent ?? "";
    expect(text).toContain("step 4 of 7");
    expect(text).not.toContain("4/7");
    expect($$(".ag-mission-hop-name")).toHaveLength(0);
  });

  it("keeps a failed row's tooltip to 14 words", async () => {
    const long = "Set up monitoring failed because the probe could not reach the service on any of its ports after five tries";
    expect(clipWords(long)!.split(" ").length).toBe(14);
    const m = parseMission(mission({ state: "stopped", step_index: 3, error: long }));
    await mount(<ul><MissionRow mission={m} band="wide" now={null} /></ul>);
    const tip = $(".ag-mission-failed")!.title;
    expect(tip.replace("…", "").split(" ").length).toBe(14);
  });
});

describe("D7 the header stays on one row from 640px up", () => {
  it("does not wrap, and its buttons never break", async () => {
    const css = (await import("node:fs")).readFileSync("app/agents/agent-world.css", "utf8");
    const rule = css.slice(css.indexOf("@media (min-width: 640px) {\n  .ag-home-head { flex-wrap: nowrap; }"));
    expect(rule).toContain(".ag-home-head > .wf-btn { flex: none; white-space: nowrap; }");
    // At desktop width the summary ("8 need you") is never clipped; the search shrinks instead.
    expect(css).toMatch(/@media \(min-width: 1024px\) \{\s*\.ag-home-sum \{ flex: none; overflow: visible; \}/);
  });
});
