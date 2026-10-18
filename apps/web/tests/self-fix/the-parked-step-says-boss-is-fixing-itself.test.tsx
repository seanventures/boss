// @vitest-environment happy-dom
/**
 * The parked step's "Fixing BOSS" strip (docs/product/BOSS_FIXES_ITSELF.md §10.1): the eight live
 * stages with a tooltip each, the live line, the facts, Open self-fix and Stop (which posts the
 * cancel op), "Review and approve" when it needs you, the waiting-on sentence, and — when no
 * self-fix started — one line that says why, with the recommendation. Never "waiting" without why.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { overBudget } from "../_helpers/copy-budget";
import { $, $$, click, mountAs, tid, unmount } from "../_helpers/multirepo-fixtures";
import { SFX, fakeSelfFixApi, newState, opsOf, stepView } from "./_fixtures";

const { SelfFixStrip } = await import("../../app/self-fix/strip");
const { stepOutput } = await import("../../app/workflows/step-run");

afterEach(unmount);

const mount = (output: Record<string, unknown>) =>
  mountAs(<SelfFixStrip output={output} projectId="p1" actor="ceo" />, "owner");

describe("the parked step says BOSS is fixing itself", () => {
  it("draws the eight stages with where it is, each with a tooltip", async () => {
    fakeSelfFixApi(newState());
    await mount({ self_fix: stepView(), boss_recommendation_id: "brec_1" });
    expect(tid("sfx-strip-label")?.textContent).toBe("Fixing BOSS");
    expect(tid("sfx-strip-label")?.getAttribute("title")).toContain("Nothing changes here until you approve");
    const stages = $$('[data-testid="sfx-stages"] li');
    expect(stages.map((s) => s.textContent?.replace(/^[✓●○]\s*/, ""))).toEqual(
      ["Found", "Reproduced", "Fixing", "Checks", "Review", "You", "Install", "Resume"]);
    expect(stages.map((s) => s.dataset.state)).toEqual(["done", "done", "now", "next", "next", "next", "next", "next"]);
    for (const s of stages) expect(s.getAttribute("title")).toBeTruthy();
    expect(tid("sfx-stage-reproduced")?.getAttribute("title")).toBe("BOSS wrote a test that fails because of this bug");
    expect(tid("sfx-stage-you")?.getAttribute("title")).toBe("Waiting for your approval");
    expect(tid("sfx-strip-line")?.textContent).toContain("Making it pass");
    expect(tid("sfx-strip-facts")?.textContent).toBe("stack_deploy.py · 1 test added · $1.84 of $10.00 · 6 min");
    expect(tid("sfx-strip-open")?.getAttribute("href")).toContain(`area=self_fixes`);
    expect(tid("sfx-strip-open")?.getAttribute("href")).toContain(`sfx=${SFX}`);
    expect(tid("sfx-strip-approve")).toBeNull();
    expect(overBudget(tid("sfx-strip")!.querySelector(".sfx-strip-head")!)).toEqual([]);
  });

  it("Stop posts the cancel op for this self-fix", async () => {
    const calls = fakeSelfFixApi(newState());
    await mount({ self_fix: stepView() });
    await click(tid("sfx-strip-stop"));
    expect(opsOf(calls, "cancel")).toEqual([expect.objectContaining({ id: SFX, project_id: "p1", actor: "ceo" })]);
    expect(tid("sfx-strip-stop")?.textContent).toBe("Stopped");
  });

  it("at You, the one action is Review and approve, to the decision", async () => {
    fakeSelfFixApi(newState());
    const order = ["found", "reproduced", "fixing", "checks", "review", "you", "install", "resume"];
    await mount({ self_fix: stepView({ status: "awaiting_approval", chip: "Needs you", stage: "you", needs_you: true,
      decision_id: "dec_sfx1", line: `BOSS fixed itself and needs your approval to install it (${SFX}).`,
      stages: order.map((s, i) => ({ stage: s, state: i < 5 ? "done" : i === 5 ? "now" : "next" })) }) });
    const approve = tid("sfx-strip-approve")!;
    expect(approve.textContent).toBe("Review and approve");
    expect(approve.getAttribute("href")).toContain("area=decisions");
    expect(approve.getAttribute("href")).toContain("decision=dec_sfx1");
    expect(tid("sfx-strip-chip")?.textContent).toBe("Needs you");
  });

  it("says which fix it waits on when another run holds the cause; no Stop", async () => {
    fakeSelfFixApi(newState());
    await mount({ self_fix: stepView({ waiting_on: true }) });
    expect(tid("sfx-strip-line")?.textContent).toBe(`BOSS is already fixing this (${SFX}); this step waits for it.`);
    expect(tid("sfx-strip-stop")).toBeNull();
  });

  it("offers no Stop once the fix is on the box (an install finishes or rolls back)", async () => {
    fakeSelfFixApi(newState());
    await mount({ self_fix: stepView({ status: "installing", chip: "Installed", stage: "install" }) });
    expect(tid("sfx-strip-stop")).toBeNull();
  });

  it("when no self-fix started, one line says why and links the recommendation", async () => {
    fakeSelfFixApi(newState());
    await mount({ self_fix_skipped: "BOSS was not sure enough this is its own bug to change itself (confidence low; it needs medium)",
                  boss_recommendation_id: "brec_1" });
    expect(tid("sfx-strip")).toBeNull();
    expect(tid("sfx-strip-skipped")?.textContent).toContain("not sure enough");
    expect(tid("sfx-strip-recommendation")?.getAttribute("href")).toContain("rec=brec_1");
  });

  it("draws nothing for a step that has nothing to do with a self-fix", async () => {
    fakeSelfFixApi(newState());
    await mount({ summary: "deployed" });
    expect($(".sfx-strip")).toBeNull();
  });

  it("the step's Details never repeat what the strip says, and a self-fix id is a link", () => {
    const out = stepOutput({ self_fix: stepView(), self_fix_skipped: "x", self_fix_id: SFX }, { noteShownAbove: true, status: "waiting" });
    const all = JSON.stringify(out);
    expect(all).not.toContain("Fixing BOSS");
    expect(all).toContain(`?area=self_fixes&sfx=${SFX}`);
  });
});
