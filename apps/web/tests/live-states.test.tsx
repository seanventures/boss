// @vitest-environment happy-dom
/**
 * Every state Agents › Live can be in (agents-home spec §4.7), each drawn from a fixture and
 * read by its exact words. A section with nothing in it says so in one line and keeps its place.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { board, failed, gate, mission } from "./_helpers/live-board-fixture";
import { $, $$, click, mount, stateOf, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { LiveBoardView } = await import("../app/agents/live-board");
type Props = React.ComponentProps<typeof LiveBoardView>;

const noop = () => {};
const view = (live: Props["live"], over: Partial<Props> = {}) => (
  <LiveBoardView projectId="p1" actor="ceo" live={live} onOpenMap={noop} onActivity={noop}
                 onDirectory={noop} onNewAgent={noop} {...over} />
);
const text = (sel: string) => $(sel)?.textContent ?? "";

beforeEach(() => { window.history.replaceState(null, "", "/?area=workforce"); });
afterEach(() => unmount());

describe("every Live state, by its words", () => {
  it("loading: a skeleton, busy, no words but Loading for a screen reader", async () => {
    await mount(view({ board: null, error: null, loading: true, refresh: noop }));
    expect($('[data-testid="ag-live-loading"]')?.getAttribute("aria-busy")).toBe("true");
    expect(text('[data-testid="ag-live-loading"]')).toBe("Loading");
  });

  it("unreachable: one line, the reason as its tooltip, and Retry", async () => {
    const refresh = vi.fn();
    await mount(view({ board: null, error: "Postgres is not reachable.", loading: false, refresh }));
    const alert = $('[data-testid="ag-live-unreachable"]')!;
    expect(alert.textContent).toBe("Can't reach BOSSRetry");
    expect(alert.querySelector("[title]")?.getAttribute("title")).toBe("Postgres is not reachable.");
    await click([...alert.querySelectorAll("button")].find((b) => b.textContent === "Retry")!);
    expect(refresh).toHaveBeenCalled();
  });

  it("nothing needs you", async () => {
    await mount(view(stateOf(board({ attention: [] }))));
    const quiet = $('[data-testid="ag-live-nothing-needs"]')!;
    expect(quiet.textContent).toBe("Nothing needs you");
    expect(quiet.querySelector("[title]")?.getAttribute("title")).toBe("Agents will ask here before anything risky.");
  });

  it("nothing running: Start work ↗ and Plan it with Juno, which opens the plan talk with the PM", async () => {
    await mount(view(stateOf(board({ missions: [] }))));
    expect(text('[data-testid="ag-live-nothing-running"]')).toBe("Nothing runningStart work ↗Plan it with Juno");
    const opened: { kind: string; roleKey?: string }[] = [];
    const on = (e: Event) => opened.push((e as CustomEvent<(typeof opened)[number]>).detail);
    window.addEventListener("boss:converse", on);
    await click($('[data-testid="ag-live-plan"]'));
    window.removeEventListener("boss:converse", on);
    expect(opened).toEqual([expect.objectContaining({ kind: "plan", roleKey: "pm" })]);
  });

  it("no agents: all of Live is one line and + New agent", async () => {
    const onNewAgent = vi.fn();
    await mount(view(stateOf(board({ counts: { agents: 0, working: 0, needs_you: 0, running: 0, stopped: 0 },
                                     agents: [], teams: [], attention: [], missions: [] })), { onNewAgent }));
    expect(text('[data-testid="ag-live-no-agents"]')).toBe("No agents yet+ New agent");
    expect($('[data-testid="ag-live-needs"]')).toBeNull();
    await click($('[data-testid="ag-live-no-agents"] button'));
    expect(onNewAgent).toHaveBeenCalled();
  });

  it("nothing finished today", async () => {
    await mount(view(stateOf(board({ outcomes: [] }))));
    expect(text('[data-testid="ag-live-nothing-done"]')).toBe("Nothing finished today");
  });

  it("many runs: six rows, then +n more running", async () => {
    const many = Array.from({ length: 8 }, (_, i) => mission({ run_id: `wfr_${i}` }));
    await mount(view(stateOf(board({ missions: many }))));
    expect($$(".ag-mission")).toHaveLength(6);
    expect(text('[data-testid="ag-mission-more"]')).toBe("+2 more running");
    await click($('[data-testid="ag-mission-more"]'));
    expect($$(".ag-mission")).toHaveLength(8);
  });

  it("very many runs: grouped by workflow, each group opens", async () => {
    const many = Array.from({ length: 22 }, (_, i) => mission({ run_id: `wfr_${i}` }));
    await mount(view(stateOf(board({ missions: many, attention: [] }))));
    const group = $('[data-testid="ag-mission-group-ship_it"]')!;
    expect(group.textContent).toBe("Ship it ×22");
    expect(group.getAttribute("title")).toBe("22 runs of Ship it");
    expect($$(".ag-mission")).toHaveLength(0);
    await click(group);
    expect($$(".ag-mission")).toHaveLength(22);
  });

  it("a run stopped at a failed step: its card says Failed and offers the run's answers; its row says Failed · step 3", async () => {
    await mount(view(stateOf(board())));
    const c = $('[data-testid="ag-att-run:wfr_ship:build"]')!;
    expect(c.querySelector(".ag-att-context")?.textContent).toBe("Ship it #180 · step 3 of 7 · Failed");
    const retry = c.querySelector<HTMLButtonElement>('[data-testid="ag-att-answer-retry_step"]')!;
    const skip = c.querySelector<HTMLButtonElement>('[data-testid="ag-att-answer-skip_step"]')!;
    expect([retry.textContent, retry.disabled]).toEqual(["Retry", false]);
    expect([skip.textContent, skip.disabled, skip.title]).toEqual(["Skip", true, "A required step cannot be skipped."]);
    expect($('[data-testid="ag-mission-wfr_ship"] .ag-mission-now')?.textContent).toBe("Failed · step 3");
    expect($('[data-testid="ag-mission-wfr_ship"]')?.dataset.state).toBe("stopped");
  });

  it("a blocked run: Blocked, with the reason as a tooltip", async () => {
    await mount(view(stateOf(board({ missions: [mission({ state: "blocked", blocked_reason: "No model has credit." })] }))));
    const word = [...$$(".ag-mission-now span")].find((s) => s.textContent === "Blocked")!;
    expect(word.title).toBe("No model has credit.");
  });

  it("waiting on a timer or a sub-run: Waiting · what", async () => {
    await mount(view(stateOf(board({ missions: [mission({ state: "waiting", waiting_on: "build to finish" })] }))));
    expect($(".ag-mission-now")?.textContent).toContain("Waiting · build to finish");
  });

  it("waiting on a named person: Waiting on <role>", async () => {
    await mount(view(stateOf(board({ missions: [mission({ state: "waiting_person", waiting_on_role: "release_manager" })] }))));
    expect($(".ag-mission-now")?.textContent).toMatch(/^Waiting on Release Manager/);
  });

  it("just finished: Finished · n steps · time", async () => {
    await mount(view(stateOf(board({ missions: [mission({ state: "done", step_index: 7, elapsed_seconds: 2520 })] }))));
    expect($(".ag-mission-now")?.textContent).toBe("Finished · 7 steps · 42m");
  });

  it("a run waiting on you: Waiting on you, and its You hop focuses the card — opening the overflow first", async () => {
    const cards = [...Array.from({ length: 5 }, (_, i) => failed({ id: `run:x${i}:b`, run: { id: `x${i}`, subject: `X${i}` } })),
                   gate()];
    await mount(view(stateOf(board({ attention: cards }))));
    expect($('[data-testid="ag-mission-wfr_video"] .ag-mission-now')?.textContent).toMatch(/^Waiting on you · step 5 of 7/);
    expect($('[data-testid="ag-att-dec_gate"]')).toBeNull();
    await click($('[data-testid="ag-mission-you-wfr_video"]'));
    const c = $('[data-testid="ag-att-dec_gate"]');
    expect(c).not.toBeNull();
    expect(document.activeElement).toBe(c);
    expect(c?.dataset.pulse).toBe("true");
  });

  it("paused: a muted Paused chip that says how to resume", async () => {
    await mount(view(stateOf(board()), { paused: true }));
    const chip = $('[data-testid="ag-live-paused"]')!;
    expect(chip.textContent).toBe("Paused");
    expect(chip.title).toBe("Live updates are paused. Press Live in the top bar.");
  });

  it("?focus= opens on that card", async () => {
    await mount(view(stateOf(board()), { focusId: "run:wfr_ship:build" }));
    expect(document.activeElement).toBe($('[data-testid="ag-att-run:wfr_ship:build"]'));
  });
});
