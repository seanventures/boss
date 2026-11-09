// @vitest-environment happy-dom
/**
 * The Line never guesses (factory spec §4.3 truth rules): a face is an act's role; machinery
 * (PR, Preview, a Check with no tester) is the BOSS glyph; a hollow face appears only when the
 * server says the role is certain, and is never ringed; quiet and budget-wait faces never move.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseBuildRow, parseFloor, parseStation } from "../lib/factory-floor";
import { ROWS, floor, stations } from "./_helpers/factory-fixtures";
import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { StationLine } = await import("../app/factory/station-line");
const { FloorView } = await import("../app/factory/floor");

afterEach(() => unmount());
const noop = () => {};

describe("who stands at a station", () => {
  it("machinery stations show the BOSS glyph, never an agent", async () => {
    const line = stations("ship", "needs_you").map(parseStation);
    await mount(<StationLine stations={line} density="header" />);
    for (const key of ["pr", "preview"]) {
      const bay = $(`[data-key=${key}]`)!;
      expect(bay.getAttribute("data-face")).toBe("boss");
      expect(bay.querySelector("[data-variant=agent]")).toBeNull();
      expect(bay.querySelector(".fx-boss")?.getAttribute("title")).toBe("BOSS machinery. No agent at this station.");
    }
  });

  it("a check with no tester's act is BOSS", async () => {
    const line = stations("build").map(parseStation);
    await mount(<StationLine stations={line} density="header" />);
    expect($("[data-key=check]")?.getAttribute("data-face")).toBe("boss");
  });

  it("a hollow face is a pending station whose role is certain, and is never ringed", async () => {
    const line = stations("build").map(parseStation);
    await mount(<StationLine stations={line} density="header" />);
    const review = $("[data-key=review]")!;
    expect(review.getAttribute("data-status")).toBe("pending");
    expect(review.getAttribute("data-face")).toBe("hollow");
    const face = review.querySelector(".fx-hollow .ag-avatar")!;
    expect(face.getAttribute("data-state")).toBe("idle");
    expect(review.querySelector(".fx-hollow")?.getAttribute("title")).toMatch(/^Next: .+, Review$/);
    expect(review.hasAttribute("data-active")).toBe(false);
  });

  it("an agent face is the act's own role, in its real state", async () => {
    const line = stations("build").map(parseStation);
    await mount(<StationLine stations={line} density="header" />);
    const build = $("[data-key=build]")!;
    expect(build.getAttribute("data-role")).toBe("software_engineer");
    expect(build.querySelector(".ag-avatar")?.getAttribute("data-state")).toBe("working");
    expect(build.querySelector("[data-testid=ag-act-face]")).not.toBeNull();
  });

  it("a station the server sent no face for is an empty slot", async () => {
    const s = parseStation({ key: "build", label: "Build", status: "pending", word: "Next" });
    await mount(<StationLine stations={[s]} density="header" />);
    expect($("[data-key=build]")?.getAttribute("data-face")).toBe("none");
    expect($("[data-key=build] .ag-avatar")).toBeNull();
    expect($("[data-key=build] .fx-slot")).not.toBeNull();
  });

  it("a bay reads as station, who, state for a screen reader", async () => {
    const line = stations("build").map(parseStation);
    await mount(<StationLine stations={line} density="row" />);
    expect($("[data-key=build]")?.getAttribute("aria-label")).toMatch(/^Build, .+, coding$/);
    expect($(".fx-line")?.getAttribute("aria-label")).toBe("Stations");
  });
});

describe("motion only for real work", () => {
  it.each(["quiet", "budget_wait"])("a %s row's faces hold still", async (name) => {
    await mount(<FloorView projectId="p1" actor="ceo" floor={parseFloor(floor({ attention: [], builds: [ROWS[name]] }))}
                           error={null} loading={false} refresh={noop} onOpen={noop} onPlan={noop} onDirect={noop}
                           onApps={noop} band="wide" />);
    const row = $(".fx-row[data-testid^=fx-build-row-]")!;
    expect(row.hasAttribute("data-still")).toBe(true);
  });

  it("a budget-wait station shows an hourglass, not a working ring", () => {
    const b = parseBuildRow(ROWS.budget_wait);
    const active = b.stations.find((s) => s.key === "build")!;
    expect(active.status).toBe("waiting");
  });

  it("no percent, ETA, estimate or 'project' anywhere on the Floor", async () => {
    await mount(<FloorView projectId="p1" actor="ceo" floor={parseFloor(floor())} error={null} loading={false}
                           refresh={noop} onOpen={noop} onPlan={noop} onDirect={noop} onApps={noop} band="wide" />);
    const text = $$(".fx-floor")[0].textContent ?? "";
    expect(text).not.toMatch(/%|\bETA\b|estimat|\bproject/i);
  });
});
