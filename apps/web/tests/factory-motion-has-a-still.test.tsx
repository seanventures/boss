// @vitest-environment happy-dom
/**
 * Factory motion has a still (factory spec §5.2): at most eight rings move on the Floor, a new
 * edge draws in only for a station that starts while you watch, and every motion has its
 * reduced-motion rule.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseFloor, parseStation } from "../lib/factory-floor";
import { codeOnly } from "./_helpers/source";
import { ROWS, floor, stations } from "./_helpers/factory-fixtures";
import { $, $$, mount, rerender, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { FloorView } = await import("../app/factory/floor");
const { StationLine } = await import("../app/factory/station-line");
const CSS = codeOnly("app/factory/factory.css");

afterEach(() => unmount());
const noop = () => {};

describe("the ring budget", () => {
  it("twelve building rows: at most eight move", async () => {
    const rows = Array.from({ length: 12 }, (_, i) => ({ ...ROWS.implementing, run_id: `crun_r${i}`, app: `App ${i}` }));
    await mount(<FloorView projectId="p1" actor="ceo" floor={parseFloor(floor({ attention: [], builds: rows }))}
                           error={null} loading={false} refresh={noop} onOpen={noop} onPlan={noop} onDirect={noop}
                           onApps={noop} band="wide" />);
    const shown = $$(".fx-row[data-testid^=fx-build-row-]");
    expect(shown.length).toBeGreaterThan(0);
    const moving = shown.filter((r) => !r.hasAttribute("data-still") && r.querySelector(".ag-avatar[data-state=working]"));
    expect(moving.length).toBeLessThanOrEqual(8);
  });
});

describe("a handoff draws in once, only while you watch", () => {
  it("no edge is new at first paint; the next station's edge is when it starts", async () => {
    await mount(<StationLine stations={stations("build").map(parseStation)} density="header" />);
    expect($$(".fx-edge[data-new]")).toHaveLength(0);
    await rerender(<StationLine stations={stations("check", "working", [{ key: "check", face: "agent", role_key: "qa" }]).map(parseStation)} density="header" />);
    expect($$(".fx-edge[data-new]").map((e) => e.closest(".fx-bay")?.getAttribute("data-key"))).toEqual(["check"]);
  });
});

describe("every motion has a still", () => {
  it("held-still rows and lines stop the face loops", () => {
    expect(CSS).toContain(".fx-row[data-still] .ag-av");
    expect(CSS).toContain(".fx-line[data-still] .ag-av");
  });

  it("reduced motion stops the edge draw-in", () => {
    const reduced = CSS.slice(CSS.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(reduced).toContain(".fx .ag-chain-edge[data-new=\"true\"] .ag-chain-line { animation: none; }");
  });

  it("quiet is a dashed ring, never a sweep", () => {
    expect(CSS).toMatch(/\.fx-row\[data-state="quiet"\][^{]*\{[^}]*dashed/);
  });
});
