// @vitest-environment happy-dom
/**
 * HONEST STAGE LABELS (operator, 2026-10-13). The run strip (PLAN · TESTS · BUILD · CHECK ·
 * REVIEW · PR · PREVIEW · SHIP) said "Stuck · 29s" on a Check that had FAILED (the app did not
 * start) and "Stuck · 3m" on a Review whose act never closed because the build was BLOCKED.
 *
 *   an act that errored            → Failed, the reason in the tooltip
 *   an act left open by a stop     → Stopped (no clock: it would only time the stop)
 *   stations after a stopped build → Not reached, never Next, no hollow "next" face
 *   Stuck                          → only a live build's station gone quiet (the server's
 *                                    QUIET_AFTER_MINUTES, factory_view.py)
 *
 * The words are the server's (engine/tests/test_stations_come_from_acts.py pins them); this
 * pins that the strip shows them, and that every surface deriving a face from an act's status
 * (chain-model, Activity's river, Live's lanes, the roster) says the same thing.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseStation } from "../lib/factory-floor";
import { at, stations } from "./_helpers/factory-fixtures";
import { $, mount, unmount } from "./_helpers/live-render";
import type { Act } from "../lib/agent-world-types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { StationLine, activeStation } = await import("../app/factory/station-line");
const { actState } = await import("../app/agents/chain-model");
const { riverState } = await import("../app/agents/activity-river");
const { laneHops, currentHopIndex } = await import("../app/agents/mission-row");
const { presenceFromStatus, presenceLine } = await import("../app/agents/roster-zones");
const { toAvatarState, zoneOf } = await import("../lib/agent-world");

afterEach(() => unmount());

/** The blocked build the operator saw: Check failed (app didn't start), Review left open. */
function blockedAtReview() {
  return stations("review", "stopped", [
    { key: "check", status: "failed", word: "Failed", face: "boss", tip: "app didn't start",
      started_at: at(6), ended_at: at(6.5) },
    { key: "review", word: "Stopped", tip: "Left open when the build stopped", started_at: at(8), ended_at: null },
    ...["pr", "preview", "ship"].map((key) => ({ key, word: "Not reached", not_reached: true, face: "none",
                                                 hollow_role: null, tip: "The build stopped before this" })),
  ]).map(parseStation);
}

describe("the run strip says what happened", () => {
  it("Failed with its reason, Stopped with no clock, and Not reached after the stop", async () => {
    const now = Date.parse(at(11));
    await mount(<StationLine stations={blockedAtReview()} density="header" now={now} />);
    const check = $("[data-key=check]")!;
    expect(check.getAttribute("data-status")).toBe("failed");
    expect(check.querySelector(".fx-bay-word")?.textContent).toBe("Failed, 30s");
    expect(check.querySelector(".fx-bay-word")?.getAttribute("title")).toBe("app didn't start");

    const review = $("[data-key=review]")!;
    expect(review.getAttribute("data-status")).toBe("stopped");
    // A stopped bay's act was left open: its clock would only time how long ago it stopped.
    expect(review.querySelector(".fx-bay-word")?.textContent).toBe("Stopped");
    expect(review.querySelector(".fx-bay-word")?.getAttribute("title")).toBe("Left open when the build stopped");

    for (const key of ["pr", "preview", "ship"]) {
      const bay = $(`[data-key=${key}]`)!;
      expect(bay.querySelector(".fx-bay-word")?.textContent, key).toBe("Not reached");
      expect(bay.hasAttribute("data-not-reached"), key).toBe(true);
      expect(bay.getAttribute("data-face"), key).toBe("none");
    }
    const words = [...document.querySelectorAll(".fx-bay-word")].map((w) => w.textContent ?? "");
    expect(words.filter((w) => /Stuck|Next/.test(w))).toEqual([]);
  });

  it("the Floor row's tooltip says it too", async () => {
    await mount(<StationLine stations={blockedAtReview()} density="row" />);
    expect($("[data-key=check]")?.getAttribute("title")).toContain("Check, Failed");
    expect($("[data-key=check]")?.getAttribute("title")).toContain("app didn't start");
    expect($("[data-key=pr]")?.getAttribute("title")).toContain("PR, Not reached");
  });

  it("a failed or stopped face is never drawn stuck", async () => {
    await mount(<StationLine stations={blockedAtReview()} density="header" />);
    expect(document.querySelector('[data-key=check] [data-state="stuck"]')).toBeNull();
    expect(document.querySelector('[data-key=review] [data-state="stuck"]')).toBeNull();
  });

  it("where the build ended is the active bay: the failure before the stop", () => {
    expect(activeStation(blockedAtReview())?.key).toBe("check");
  });

  it("Stuck is a live station gone quiet, drawn stuck", async () => {
    const line = stations("build", "stuck", [{ key: "build", word: "Stuck", tip: "No progress for 24m. It may be stuck." }])
      .map(parseStation);
    await mount(<StationLine stations={line} density="header" />);
    const build = $("[data-key=build]")!;
    expect(build.querySelector(".fx-bay-word")?.textContent).toMatch(/^Stuck/);
    expect(build.querySelector('[data-state="stuck"]')).not.toBeNull();
    // A live build's later stations are still next.
    expect($("[data-key=review] .fx-bay-word")?.textContent).toBe("Next");
  });

  it("parses the server's not_reached flag, and only a true one", () => {
    expect(parseStation({ key: "pr", status: "pending", word: "Not reached", not_reached: true }).notReached).toBe(true);
    expect(parseStation({ key: "pr", status: "pending", word: "Next" }).notReached).toBeUndefined();
    expect(parseStation({ key: "pr", status: "failed" }).status).toBe("failed");
    expect(parseStation({ key: "pr", status: "stopped" }).status).toBe("stopped");
  });
});

const act = (over: Partial<Act>): Act => ({
  actId: "a1", roleKey: "qa", kind: "step", status: "active", rootActId: "r", handedBy: { kind: "boss" },
  how: "named", edgeLabel: "", considered: [], scope: { skills: [], mcp: [] }, accountable: "", label: "Check",
  links: {}, artifacts: [], tokensIn: 0, tokensOut: 0, costUsd: 0, reconstructed: false, startedAt: at(0), ...over,
} as Act);

describe("every surface says the same ending", () => {
  it("chain-model: errored is failed, stale is stopped, never stuck", () => {
    expect(actState(act({ status: "error" }))).toBe("failed");
    expect(actState(act({ status: "stale" }))).toBe("stopped");
    expect(actState(act({ status: "active" }))).toBe("working");
    // The person who started a run is never the failure.
    expect(actState(act({ kind: "run", status: "complete" }))).toBe("idle");
  });

  it("Activity's river: a step failed or stopped, a run stopped", () => {
    expect(riverState("error", { kind: "step", verb: "Failed" })).toBe("failed");
    expect(riverState("stale", { kind: "step", verb: "Stopped" })).toBe("stopped");
    expect(riverState("error", { kind: "run", verb: "Working" })).toBe("stopped");
  });

  it("Live's lane: the failed hop is the current one; the starter never failed", () => {
    const hops = [
      { kind: "hop", actId: "r", roleKey: null, state: "failed", label: "", edgeLabel: "" },
      { kind: "hop", actId: "a", roleKey: "qa", state: "done", label: "", edgeLabel: "" },
      { kind: "hop", actId: "b", roleKey: "software_engineer", state: "failed", label: "", edgeLabel: "" },
      { kind: "hop", actId: "c", roleKey: "adversarial_reviewer", state: "idle", label: "", edgeLabel: "" },
    ] as never[];
    const lane = laneHops({ hops, state: "stopped" } as never);
    expect((lane[0] as { state: string }).state).toBe("done");
    expect(currentHopIndex(lane)).toBe(2);
  });

  it("the roster: a failed step is Failed; only no progress is Stuck", () => {
    const failed = presenceFromStatus({ status: "STUCK", task: "serve_check — failed" } as never);
    expect(failed.state).toBe("failed");
    expect(presenceLine(failed)).toBe("Failed: serve_check");
    const quiet = presenceFromStatus({ status: "STUCK", task: "implement — running 31m with no progress" } as never);
    expect(quiet.state).toBe("stuck");
    expect(presenceLine(quiet)).toBe("Stuck: implement — running 31m with no progress");
  });

  it("the live board's wire words, and a failure still asks for the operator", () => {
    expect([toAvatarState("error"), toAvatarState("stale"), toAvatarState("failed"), toAvatarState("stopped")])
      .toEqual(["failed", "stopped", "failed", "stopped"]);
    expect(zoneOf({ state: "failed" } as never)).toBe("needs_you");
    expect(zoneOf({ state: "stopped" } as never)).toBe("idle");
  });
});
