/**
 * THE CONTROLS THE ROLE WALK PRESSED AND THE API REFUSED ARE PRE-GREYED (round 2, R2-WEB).
 *
 * FX-D's walk (scratchpad/id-fxd/role-walk.json) pressed every enabled button as each role and
 * recorded the writes the API refused. The class is held by the mirror tests
 * (every-control-mirrors-the-permission-the-api-checks: an action's declared permission is the
 * one its route checks, now for literal paths too; every-call-action-button-is-pre-greyed;
 * every-write-is-pre-greyed). This file pins each control the walk caught, by its element, to
 * the permission the API checks for what it sends (tests/identity/matrix.yaml), so none of
 * them can quietly become a plain <button> again.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const WEB = process.cwd();
const MATRIX = readFileSync(join(WEB, "..", "..", "tests/identity/matrix.yaml"), "utf8");

/** [file, a marker inside the control's opening tag, the permission, the route it sends to]. */
const CONTROLS: ReadonlyArray<readonly [string, string, string, string]> = [
  ["app/delivery-workflows-ui.tsx", 'data-testid="dw-advance"', "workflow_run:control", "POST /v1/delivery-workflows/runs/{run_id}/recheck"],
  ["app/delivery-workflows-ui.tsx", "data-testid={`${idPrefix}-retry`}", "workflow_run:control", "POST /v1/delivery-workflows/runs/{run_id}/retry-step"],
  ["app/delivery-workflows-ui.tsx", "data-testid={`${idPrefix}-skip`}", "workflow_run:control", "POST /v1/delivery-workflows/runs/{run_id}/skip-step"],
  ["app/code-tasks-ui.tsx", 'data-testid="fx-new-plan"', "meeting:convene", "POST /v1/conversations"],
  ["app/ConsoleShell.tsx", 'data-testid="topbar-new-project"', "meeting:convene", "POST /v1/conversations"],
  ["app/improve/sharing-ask.tsx", 'data-testid="su-sharing-on"', "telemetry:manage", ""],
  ["app/improve/sharing-ask.tsx", 'data-testid="su-sharing-no"', "telemetry:manage", ""],
  ["app/setup/setup-board.tsx", 'data-testid="su-test-all"', "setup:manage", "POST /v1/setup/checks"],
  ["app/shepherd-ui.tsx", "onClick={sweep}", "autopilot:operate", ""],
  ["app/agents/attention-card.tsx", "data-testid={`ag-att-option-${a.key}`}", "system:operate", "POST /v1/system-doctor/findings/{finding_id}/act"],
  ["app/agents/attention-card.tsx", "data-testid={`ag-att-infra-${a.key}`}", "deployment:rollback", "POST /v1/infra/attention/act"],
];

/** The opening tag that contains `marker`: from the nearest preceding `<Tag` to its end. */
function tagAround(src: string, marker: string): string {
  const at = src.indexOf(marker);
  expect(at, marker).toBeGreaterThan(0);
  const open = src.lastIndexOf("<", at);
  return src.slice(open, src.indexOf(">", at) + 1);
}

describe("each control the walk caught is gated on the permission the API checks", () => {
  it.each(CONTROLS)("%s %s needs %s", (file, marker, permission, route) => {
    const tag = tagAround(readFileSync(join(WEB, file), "utf8"), marker);
    expect(tag, tag).toMatch(new RegExp(`^<(GatedButton|CanButton|SubmitButton)\\b[\\s\\S]*permission="${permission}"`));
    if (route) {
      const declared = new RegExp(`^"${route.replace(/[{}]/g, (c) => `\\${c}`)}": \\["permit ${permission} @`, "m");
      expect(MATRIX, route).toMatch(declared);
    }
  });

  it("Setup does not run the stale checks for a viewer who may not (the API refuses setup:manage)", () => {
    const src = readFileSync(join(WEB, "app/setup/setup-board.tsx"), "utf8");
    expect(src).toMatch(/const mayCheck = useGate\("setup:manage"\)\.allowed;/);
    expect(src).toMatch(/if \(!view \|\| autoChecked\.current \|\| !mayCheck\) return;/);
  });
});
