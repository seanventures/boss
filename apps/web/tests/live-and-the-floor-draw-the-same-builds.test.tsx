// @vitest-environment happy-dom
/**
 * Factory spec §10, decision 2: a build appears in Agents › Live and on the Floor with the same
 * row and the same count. Live draws the board's `builds` with the Floor's BuildRow.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { board } from "./_helpers/live-board-fixture";
import { ROWS } from "./_helpers/factory-fixtures";
import { $, $$, mount, stateOf, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { LiveBoardView } = await import("../app/agents/live-board");
const noop = () => {};

afterEach(() => unmount());

describe("Live and the Floor draw the same builds", () => {
  it("each building build is the Floor's row, counted under LIVE WORK", async () => {
    const builds = [ROWS.implementing, ROWS.checks_failed];
    const body = board({ builds, missions: [] });
    (body.counts as Record<string, unknown>).building = builds.length;
    await mount(<LiveBoardView projectId="p1" actor="ceo" live={stateOf(body)} onOpenMap={noop}
                               onActivity={noop} onDirectory={noop} onNewAgent={noop} />);
    const rows = $$('[data-testid="ag-live-builds"] > li');
    expect(rows).toHaveLength(2);
    expect($('[data-testid="ag-live-builds"]')?.textContent).toContain("Checks failed");
    expect($('[data-testid="ag-live-nothing-running"]')).toBeNull();
    expect($('#ag-live-work-h')?.parentElement?.querySelector(".ag-zone-count")?.textContent).toBe("2");
  });

  it("no builds and no runs: Nothing running", async () => {
    await mount(<LiveBoardView projectId="p1" actor="ceo" live={stateOf(board({ builds: [], missions: [] }))}
                               onOpenMap={noop} onActivity={noop} onDirectory={noop} onNewAgent={noop} />);
    expect($('[data-testid="ag-live-nothing-running"]')).not.toBeNull();
    expect($('[data-testid="ag-live-builds"]')).toBeNull();
  });
});
