// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs — on the cockpit (factory spec §5.1). Rendered from fixtures, every
 * fold, Outputs row, menu, the Logs drawer and the Change it sheet opened, and walked with the
 * Studio's walker: no run over six words outside a tooltip, no <p>, none of the Agent World
 * banned words nor the Factory's own (`%`, ETA, estimated, almost, project, code task, pipeline,
 * fail-closed, governed, ratchet; `machinery` only inside the BOSS glyph's tooltip).
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";
import { stubFetch, visibleRuns } from "./_helpers/cockpit-render";
import { cockpit, cockpitCancelled, cockpitChecksFailed, cockpitFailed, cockpitPrOpen } from "./_helpers/factory-fixtures";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span data-testid="live-deploy" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Cockpit } = await import("../app/factory/cockpit");

const BANNED = /(\b(persona|fingerprint|ledger|act|acts|sentient|alive|conscious|impersonate|signed|tokens?|eta|estimated|almost|projects?|code task|pipeline|fail-closed|governed|ratchet|machinery)\b|%)/i;

function audit() {
  const roots = $$(".fx-cockpit, .fx-logs, .fx-sheet");
  return {
    surfaces: roots.length,
    over: [...new Set(roots.flatMap((r) => overBudget(r)))],
    paragraphs: roots.flatMap((r) => [...r.querySelectorAll("p")].map((p) => p.textContent ?? "")),
    banned: [...new Set(roots.flatMap(visibleRuns).filter((t) => BANNED.test(t)))],
  };
}
const clean = () => { const r = audit(); expect(r.surfaces).toBeGreaterThan(0); expect([r.over, r.paragraphs, r.banned], JSON.stringify([r.over, r.paragraphs, r.banned])).toEqual([[], [], []]); };
const clickAll = async (sel: string) => { for (const b of $$(sel)) await click(b); };

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the cockpit copy budget", () => {
  for (const [name, fixture] of [["building", cockpit()], ["checks failed", cockpitChecksFailed()], ["PR open", cockpitPrOpen()],
                                 ["failed", cockpitFailed()], ["cancelled", cockpitCancelled()]] as const) {
    it(`${name}: every fold, every Outputs row, the menu`, async () => {
      stubFetch(fixture);
      await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
      await clickAll(".fx-fold-btn");
      clean();
      for (const head of $$(".fx-out-head")) {
        if (head.getAttribute("aria-expanded") !== "true") await click(head);
        await clickAll('[data-testid="fx-tests-output"]');
        clean();
      }
      await click($('[data-testid="fx-more"]'));
      if ($('[data-testid="fx-attempts"]')) await click($('[data-testid="fx-attempts"]'));
      if ($('[data-testid="fx-steer"]')) await click($('[data-testid="fx-steer"]'));
      clean();
    });
  }

  it("the Logs drawer and the Change it sheet", async () => {
    stubFetch(cockpit());
    await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    await click($('[data-testid="fx-logs-toggle"]'));
    clean();
    for (const t of ["preview", "deploy"]) { await click($(`[data-testid="fx-logs-tab-${t}"]`)); clean(); }
    await click($('[data-testid="fx-more"]'));
    await click($('[data-testid="fx-menu-change"]'));
    await act(async () => { await settle(); });
    expect($('[data-testid="fx-change-it"]')).not.toBeNull();
    expect($$('[data-testid^="fx-change-chip-"]').map((b) => b.textContent)).toEqual(
      ["New screen", "API endpoint", "Store data", "Access", "Polish", "Something's wrong"]);
    clean();
  });
});
