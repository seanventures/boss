// @vitest-environment happy-dom
/**
 * The pill counts what needs you, not who (agents-home spec §4.10, §6.3). Its number is
 * `attention.count` from /v1/agents/live — the Command Center's and Approvals' number — so three
 * gates on one agent say 3 and a pull request nobody is attributed to is counted. The crew lists
 * those items first, each with Review; each new item is said once, by its id.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { parseAttentionSummary } from "../lib/agents-live-board";
import { $, $$, click, mount, rerender, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { PresencePill, pillText, reviewPatch } = await import("../app/agents/presence-pill");
const { parseLive, resetLiveStores } = await import("../lib/agents-live");

const gates = (n: number, role: string | null = "media_publisher") => parseAttentionSummary({
  count: n, items: Array.from({ length: n }, (_, i) => ({ id: `dec_${i}`, role_key: role, title: `Gate ${i}`,
                                                          decide_inline: i === 0, at: "2026-10-04T09:00:00Z" })),
});
const pillWordsShown = () => $('[data-testid="ag-presence-pill"]')?.textContent ?? "";

beforeEach(() => { document.title = "BOSS"; window.history.replaceState(null, "", "/?area=observability"); });
afterEach(() => { unmount(); resetLiveStores(); vi.unstubAllGlobals(); });

describe("the pill counts what needs you", () => {
  it("one agent with three gates says 3 need you", async () => {
    const live = parseLive([{ role_key: "media_publisher", state: "needs_you", line: "Waiting on you: Publish" },
                            { role_key: "software_engineer", state: "working", line: "Build" }]);
    await mount(<PresencePill projectId="p1" live={live} attention={gates(3)} />);
    expect(pillWordsShown()).toBe("1 working · 3 need you");
    // The title's `(n)` is the shell's now (tests/the-tab-title-has-one-owner.test.tsx).
  });

  it("an agent that is live but not working (queued, waiting) is not counted as working", async () => {
    const live = parseLive([{ role_key: "release_manager", state: "waiting", line: "Queued behind a deploy" }]);
    await mount(<PresencePill projectId="p1" live={live} attention={gates(1)} />);
    expect(pillWordsShown()).toBe("1 needs you");
  });

  it("a pull request with no agent is counted, and shows the pill on its own", async () => {
    await mount(<PresencePill projectId="p1" live={new Map()} attention={gates(1, null)} />);
    expect(pillWordsShown()).toBe("1 needs you");
  });

  it("the crew lists Needs you rows, each with Review, above Working", async () => {
    const live = parseLive([{ role_key: "software_engineer", state: "working", line: "Build" },
                            { role_key: "media_publisher", state: "needs_you", line: "Waiting on you" }]);
    await mount(<PresencePill projectId="p1" live={live} attention={gates(3)} />);
    await click($('[data-testid="ag-presence-pill"]'));
    const crew = $('[data-testid="ag-crew"]')!;
    expect([...crew.querySelectorAll(".ag-crew-sub")].map((s) => s.textContent)).toEqual(["Needs you", "Working"]);
    expect($$('[data-testid^="ag-crew-need-"]').map((r) => r.dataset.testid))
      .toEqual(["ag-crew-need-dec_0", "ag-crew-need-dec_1", "ag-crew-need-dec_2"]);
    expect($$('[data-testid^="ag-crew-review-"]').map((b) => b.textContent)).toEqual(["Review", "Review", "Review"]);
    // The agent waiting is one of the items above, not a second row below.
    expect($('[data-testid="ag-crew-media_publisher"]')).toBeNull();
    expect($('[data-testid="ag-crew-software_engineer"]')).not.toBeNull();
    expect($('[data-testid="ag-crew-roster"]')?.textContent).toBe("Agents ↗");
  });

  it("Review goes to the card on Live when it can be decided there, otherwise to Approvals", async () => {
    const [inline, other] = gates(2).items;
    expect(reviewPatch(inline)).toMatchObject({ area: "workforce", focus: "dec_0", view: null });
    expect(reviewPatch(other)).toMatchObject({ area: "decisions", decision: "dec_1" });
    expect(reviewPatch({ id: "run:wfr_1:build", roleKey: null, title: "x", decideInline: false }))
      .toMatchObject({ area: "workforce", focus: "run:wfr_1:build" });
    await mount(<PresencePill projectId="p1" live={new Map()} attention={gates(2)} />);
    await click($('[data-testid="ag-presence-pill"]'));
    await click($('[data-testid="ag-crew-review-dec_0"]'));
    const url = new URLSearchParams(window.location.search);
    expect([url.get("area"), url.get("focus")]).toEqual(["workforce", "dec_0"]);
  });

  it("a new item is announced once, by id — even a second gate on the same agent", async () => {
    await mount(<PresencePill projectId="p1" live={new Map()} attention={gates(1)} />);
    const said = () => $('.ag-pill-wrap [aria-live="polite"]')?.textContent;
    expect(said()).toBe("Needs you: Gate 0");
    await rerender(<PresencePill projectId="p1" live={new Map()} attention={gates(2)} />);
    expect(said()).toBe("Needs you: Gate 1");
    await rerender(<PresencePill projectId="p1" live={new Map()} attention={gates(2)} />);
    expect(said()).toBe("Needs you: Gate 1");
  });

  it("reads attention from the same /v1/agents/live poll", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      items: [{ role_key: "qa", state: "working", line: "tests" }],
      attention: { count: 2, items: [{ id: "dec_a", role_key: "qa", title: "A", decide_inline: false },
                                     { id: "dec_b", role_key: null, title: "B", decide_inline: false }] },
    })));
    await mount(<PresencePill projectId="p1" />);
    await settle();
    expect(pillWordsShown()).toBe("1 working · 2 need you");
  });

  it("words: either half left out at zero, 1 needs you, n need you", () => {
    expect(pillText(4, 2)).toBe("4 working · 2 need you");
    expect(pillText(0, 1)).toBe("1 needs you");
    expect(pillText(3, 0)).toBe("3 working");
  });

  it("the crew shows five items and says how many more (walk D19), with the BOSS glyph for none", async () => {
    const seven = parseAttentionSummary({ count: 7, items: Array.from({ length: 5 }, (_, i) => (
      { id: `dec_${i}`, role_key: null, title: `Gate ${i}`, decide_inline: false })) });
    await mount(<PresencePill projectId="p1" live={new Map()} attention={seven} />);
    await click($('[data-testid="ag-presence-pill"]'));
    expect($$('[data-testid^="ag-crew-need-"]')).toHaveLength(5);
    expect($('[data-testid="ag-crew-needs-more"]')?.textContent).toBe("+2 more");
    expect($('[data-testid="ag-crew-need-dec_0"] [data-variant="you"]')).toBeNull();
    expect($('[data-testid="ag-crew-need-dec_0"] [data-variant="boss"]')).not.toBeNull();
  });
});
