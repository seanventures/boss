// @vitest-environment happy-dom
/**
 * The Self-fixes page (docs/product/BOSS_FIXES_ITSELF.md §10.3): the list with status chips, the
 * area, the run link and the upstream column with Send (only for a verified fix not yet sent);
 * the autonomy trend (sparkline, "<n>% this week", the metric in its tooltip); one fix's lineage
 * problem → diagnosis → fix → tests → review → approved → installed → resumed → upstream, opened
 * by ?sfx=, with Diff, Output, Remove and Send to BOSS team; recorded bugs with "Fix it".
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { setConsoleLive } from "../../lib/console-live";
import { AREA_IDS } from "../../lib/area-ids";
import { AREA_ACCESS } from "../../lib/identity/areas";
import { $$, click, mountAs, tid, unmount } from "../_helpers/multirepo-fixtures";
import { LINEAGE_KEYS, SFX, fakeSelfFixApi, newState, opsOf } from "./_fixtures";

const { SelfFixesHome } = await import("../../app/self-fix/self-fixes-home");

afterEach(() => { unmount(); window.history.replaceState({}, "", "/"); });

async function open(search = "?area=self_fixes", state = newState()) {
  const calls = fakeSelfFixApi(state);
  setConsoleLive(true);
  window.history.replaceState({}, "", `/${search}`);
  await mountAs(<SelfFixesHome projectId="p1" actor="ceo" />, "owner");
  return calls;
}

describe("the Self-fixes page", () => {
  it("is a console area, read with improvement:read", () => {
    expect(AREA_IDS).toContain("self_fixes");
    expect(AREA_ACCESS.self_fixes).toEqual({ permission: "improvement:read" });
  });

  it("lists each fix with its chip, area, run and upstream", async () => {
    await open();
    const rows = $$('[data-testid="sfx-row"]');
    expect(rows.map((r) => r.dataset.chip)).toEqual(["Verified", "Needs you", "Refused"]);
    for (const chip of $$('[data-testid="sfx-row-chip"]')) expect(chip.getAttribute("title")).toBeTruthy();
    expect(rows[0].textContent).toContain("retire app from the old shared project");
    expect(rows[0].textContent).toContain("deploy");
    expect(rows[0].querySelector('a[href*="wfr=wfr_f383649a8b024d97a80c"]')).toBeTruthy();
    expect(rows[0].querySelector('[data-testid="sfx-row-upstream"]')?.textContent).toContain("Not sent");
    // Send only on a verified fix that has not been sent.
    expect($$('[data-testid="sfx-row-send"]')).toHaveLength(1);
    expect(rows[2].textContent).toContain("(protected path)");
  });

  it("shows the autonomy trend with the metric in its tooltip", async () => {
    await open();
    expect(tid("sfx-autonomy-rate")?.textContent).toBe("63%");
    expect(tid("sfx-autonomy")?.textContent).toContain("this week");
    const tip = tid("sfx-autonomy")?.getAttribute("title") ?? "";
    expect(tip).toContain("BOSS 9");
    expect(tip).toContain("people 6");
    expect(tip).toContain("outside agents 4");
    expect(tip).toContain("open 2");
    expect(tid("sfx-spark")?.children).toHaveLength(4);
  });

  it("opens one fix from ?sfx= with its whole lineage, in order", async () => {
    await open(`?area=self_fixes&sfx=${SFX}`);
    expect(tid("sfx-one")).toBeTruthy();
    expect($$('[data-testid="sfx-lineage"] li').map((li) => li.dataset.testid?.replace("sfx-lineage-", ""))).toEqual(LINEAGE_KEYS);
    expect(tid("sfx-lineage-problem")?.querySelector("a")?.getAttribute("href")).toContain("wfr=");
    expect(tid("sfx-lineage-upstream")?.dataset.state).toBe("next");
    expect(tid("sfx-one-chip")?.textContent).toBe("Verified");
  });

  it("Diff reads the diff and marks the new test red→green; Output lists every event", async () => {
    await open(`?area=self_fixes&sfx=${SFX}`);
    await click(tid("sfx-one-diff"));
    expect(tid("sfx-one-diff-view")?.textContent).toContain("✗→✓ engine/tests/test_new.py");
    await click(tid("sfx-one-output"));
    expect(tid("sfx-one-events")?.textContent).toContain("reading BOSS's own code first");
  });

  it("Remove asks for removal; Send to BOSS team opens the preview sheet", async () => {
    const calls = await open(`?area=self_fixes&sfx=${SFX}`);
    await click(tid("sfx-one-remove"));
    expect(opsOf(calls, "remove")).toEqual([expect.objectContaining({ id: SFX, project_id: "p1", actor: "ceo" })]);
    await click(tid("sfx-one-send"));
    expect(tid("sfx-send")).toBeTruthy();
  });

  it("← All goes back to the list and drops ?sfx=", async () => {
    await open(`?area=self_fixes&sfx=${SFX}`);
    await click(tid("sfx-back"));
    expect(tid("sfx-list")).toBeTruthy();
    expect(window.location.search).not.toContain("sfx=");
  });

  it("lists recorded bugs BOSS did not fix, and Fix it starts a self-fix", async () => {
    const calls = await open();
    expect($$('[data-testid="sfx-rec"]')).toHaveLength(1);
    await click(tid("sfx-rec-start"));
    expect(opsOf(calls, "start")).toEqual([expect.objectContaining({ recommendation_id: "brec_open1" })]);
  });

  it("says so when there are none", async () => {
    await open("?area=self_fixes", newState({ list: { self_fixes: [], count: 0 } }));
    expect(tid("sfx-empty")?.textContent).toBe("No self-fixes yet");
  });
});
