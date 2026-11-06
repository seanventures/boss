// @vitest-environment happy-dom
/**
 * Govern › Autonomy (spec §7 autonomy/settings/stop; design §4 L5): the class × level matrix with
 * each class's track record; a level changes only after a confirmation and only for an owner or
 * admin (autonomy:manage); BOSS's earned promotion is a one-press "Raise"; today's agent budget
 * is a meter; the kill switch stops after one confirmation and is open to operators (the safe
 * direction), while Resume needs autonomy:manage.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { setConsoleLive } from "../../lib/console-live";
import { demoAnswer } from "../../lib/ambient-demo";
import { $$, click, mountAs, settle, tid, type, unmount, type Role } from "../_helpers/multirepo-fixtures";
import type { Call } from "../_helpers/multirepo-fixtures";
import { fakeAmbientApi, writes } from "./_fixtures";

const { AutonomyPanel } = await import("../../app/agent-ops/autonomy-panel");

afterEach(() => { unmount(); window.history.replaceState({}, "", "/"); });

async function open(role: Role = "owner", override?: (c: Call) => { status?: number; body: unknown } | null | undefined) {
  const calls = fakeAmbientApi(override);
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget&tab=autonomy");
  await mountAs(<AutonomyPanel projectId="p1" />, role);
  await settle();
  return calls;
}
const row = (cls: string) => $$('[data-testid="aa-class"]').find((r) => r.dataset.class === cls)!;
const cell = (cls: string, level: string) => [...row(cls).querySelectorAll<HTMLElement>('[data-testid="aa-cell"]')].find((c) => c.dataset.level === level)!;

describe("Autonomy & safety", () => {
  it("draws every class against A0–A3, with its level, its ceiling and its track record", async () => {
    await open();
    expect($$('[data-testid="aa-class"]').length).toBeGreaterThanOrEqual(10);
    expect(cell("flaky_test", "A2").dataset.on).toBe("true");
    expect(cell("iac_drift", "A2").dataset.locked).toBe("true");
    expect(row("ci_fix_boss_pr").querySelector('[data-testid="aa-track"]')?.textContent).toContain("92%");
  });

  it("changes a level only after a confirmation, as PUT autonomy/{class}", async () => {
    const calls = await open();
    await click(cell("flaky_test", "A3"));
    expect(writes(calls, /autonomy/)).toHaveLength(0);
    expect(tid("aa-level-confirm")?.textContent).toContain("Raise Flaky test to A3");
    await click(tid("aa-level-apply"));
    const put = writes(calls, "/v1/ambient/autonomy/flaky_test");
    expect(put).toHaveLength(1);
    expect(put[0].method).toBe("PUT");
    expect(put[0].body).toMatchObject({ project_id: "p1", level: "A3" });
  });

  it("never offers a level above the class's ceiling", async () => {
    const calls = await open();
    await click(cell("iac_drift", "A3"));
    expect(tid("aa-level-confirm")).toBeNull();
    expect(writes(calls, /autonomy/)).toHaveLength(0);
  });

  it("turns BOSS's earned promotion into one Raise press", async () => {
    await open();
    const raise = row("ci_fix_boss_pr").querySelector<HTMLElement>('[data-testid="aa-raise"]')!;
    expect(raise.textContent).toBe("Raise to A3");
    await click(raise);
    expect(tid("aa-level-confirm")?.textContent).toContain("CI fix");
  });

  it("does not let an operator change levels (autonomy:manage)", async () => {
    const calls = await open("operator");
    expect(cell("flaky_test", "A3").getAttribute("aria-disabled")).toBe("true");
    await click(cell("flaky_test", "A3"));
    expect(tid("aa-level-confirm")).toBeNull();
    expect(writes(calls, /autonomy/)).toHaveLength(0);
  });

  it("shows today's agent budget as a meter", async () => {
    await open();
    expect(tid("aa-envelope")?.textContent).toContain("$3.35");
    expect(tid("aa-envelope")?.textContent).toContain("$5");
  });

  it("stops all agent work after one confirmation — an operator may", async () => {
    const calls = await open("operator");
    await click(tid("aa-kill-btn"));
    expect(writes(calls, /stop/)).toHaveLength(0);
    expect(tid("aa-kill-confirm")).toBeTruthy();
    // Stopping every project is the owner's: the box is off for an operator.
    expect((tid("aa-kill-everywhere") as HTMLInputElement).disabled).toBe(true);
    await type(tid("aa-kill-confirm")!.querySelector('input[type="text"]'), "bad release");
    await click(tid("aa-kill-confirm-go"));
    expect(writes(calls, "/v1/ambient/stop")[0].body).toMatchObject({ project_id: "p1", reason: "bad release" });
  });

  it("a viewer cannot stop; the switch says why", async () => {
    await open("viewer");
    expect(tid("aa-kill-btn")?.getAttribute("aria-disabled")).toBe("true");
    await click(tid("aa-kill-btn"));
    expect(tid("aa-kill-confirm")).toBeNull();
  });

  it("when stopped, says by whom and why, and Resume needs autonomy:manage", async () => {
    const stopped = (c: Call) => (c.path === "/v1/ambient/settings" && c.method === "GET"
      ? { body: { ...(demoAnswer("/v1/ambient/settings") as object), stop: { stopped: true, scope: "project", by: "person:vee", at: null, reason: "bad release" } } }
      : undefined);
    await open("operator", stopped);
    expect(tid("aa-kill")?.textContent).toContain("stopped");
    expect(tid("aa-kill")?.textContent).toContain("bad release");
    expect(tid("aa-resume")?.getAttribute("aria-disabled")).toBe("true");
    unmount();
    const calls = await open("owner", stopped);
    await click(tid("aa-resume"));
    expect(writes(calls, "/v1/ambient/resume")).toHaveLength(1);
  });
});
