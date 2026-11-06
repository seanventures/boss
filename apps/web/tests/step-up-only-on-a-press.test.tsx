// @vitest-environment happy-dom
/**
 * "CONFIRM IT'S YOU" IS FOR A PRESS, NEVER FOR A PAGE VIEW (operator, 2026-10-10, ~10 reports).
 *
 * A plain refresh of the Command Center or the Factory opened the step-up sheet: Repo and models'
 * automatic re-check of stale rows (an effect, POST /api/setup op=checks) was refused
 * step_up_required once the second factor was ten minutes old, and the one write path opened the
 * sheet for it. Every area the session opened stays mounted, so it ran on every page view.
 *
 * The rule is central (components/identity/identity-provider.tsx + lib/user-gesture.ts): the sheet
 * opens only for a write the person started with a press on a control; a background write is
 * answered "not confirmed" and shows its own inline state.
 */
import { describe, expect, it, beforeEach } from "vitest";

import { GESTURE_WINDOW_MS, isActionTarget, isInteractive, noteGesture, resetGesturesForTest }
  from "../lib/user-gesture";
import { codeOnly } from "./_helpers/source";
import { PERMISSION_META } from "../lib/identity/permissions.gen";

function el(html: string, pick: string): Element {
  const host = document.createElement("div");
  host.innerHTML = html;
  document.body.appendChild(host);
  return host.querySelector(pick)!;
}

describe("what counts as the person asking", () => {
  beforeEach(() => { resetGesturesForTest(); document.body.innerHTML = ""; });

  it("nothing pressed: not interactive (a page view, an effect, a timer)", () => {
    expect(isInteractive()).toBe(false);
  });

  it("a press on a button is", () => {
    noteGesture({ type: "pointerdown", target: el("<button id=b>Test</button>", "#b") });
    expect(isInteractive()).toBe(true);
  });

  it("a form's submit is, wherever it came from", () => {
    noteGesture({ type: "submit", target: el("<form id=f><input></form>", "#f") });
    expect(isInteractive()).toBe(true);
  });

  it("choosing in a menu is a press: Pin a job saves on a select change (boss-1, 2026-11-03)", () => {
    noteGesture({ type: "change", target: el("<select id=s><option>a</option></select>", "#s") });
    expect(isInteractive()).toBe(true);
  });

  it("a script's change event is never the person", () => {
    noteGesture({ type: "change", isTrusted: false,
                  target: el("<select id=s><option>a</option></select>", "#s") });
    expect(isInteractive()).toBe(false);
  });

  it("a change on something that is not a form control is not a press", () => {
    noteGesture({ type: "change", target: el("<div id=d></div>", "#d") });
    expect(isInteractive()).toBe(false);
  });

  it("opening an area is a view, not a request to act", () => {
    for (const [html, pick] of [
      ['<nav><button id=x data-area-nav="setup">Repo and models</button></nav>', "#x"],
      ['<a id=x href="?area=setup">Repo and models</a>', "#x"],
      ['<div role=tablist><button id=x role=tab>Runs</button></div>', "#x"],
      ['<p id=x>text</p>', "#x"],
    ] as const) {
      expect(isActionTarget(el(html, pick)), html).toBe(false);
    }
  });

  it("only Enter or Space on a control is a key press", () => {
    const b = el("<button id=b>Go</button>", "#b");
    noteGesture({ type: "keydown", key: "Tab", target: b });
    expect(isInteractive()).toBe(false);
    noteGesture({ type: "keydown", key: "Enter", target: b });
    expect(isInteractive()).toBe(true);
  });

  it("the window is short: a timer firing later is not the press", () => {
    expect(GESTURE_WINDOW_MS).toBeLessThanOrEqual(15_000);
  });
});

describe("every step-up path goes through the one gate", () => {
  it("the provider's stepUp refuses without a press, before any sheet is shown", () => {
    const provider = codeOnly("components/identity/identity-provider.tsx");
    // The gesture test comes before the sheet is asked for (and a joined ask is still a press's).
    expect(provider).toMatch(/if \(!isInteractive\(\)\) return Promise\.resolve\(false\);\s*return new Promise<boolean>\(\(resolve\) => \{[\s\S]{0,200}setAsk\(/);
    // apiWrite, callAction, useGuardedCall: all reach the sheet through the provider's stepUp.
    expect(codeOnly("lib/api-write.ts")).toContain("mirror.stepUp(");
    expect(codeOnly("lib/call-action.ts")).toContain("mirror.stepUp(");
    // No other component renders the sheet.
    expect(provider).toContain("<StepUpSheet");
  });

  it("a background setup write never asks", () => {
    expect(codeOnly("lib/setup-client.ts")).toContain("background ? { noStepUp: true }");
    expect(codeOnly("app/setup/setup-board.tsx")).toMatch(/"checks", \{ target: "stale" \}, \{ label: "Test all", background: true \}/);
  });
});

/**
 * WHICH ACTIONS ASK AT ALL, PINNED IN ONE PLACE. The API decides (packages/identity/catalogue.yaml,
 * the `step_up` flag); this list is what the console may ever show the sheet for. Changing the
 * catalogue changes this list on purpose, in review — never silently.
 */
const STEP_UP_ACTIONS = [
  "access_role:manage", "activity:export", "activity:prune", "agent:lifecycle", "agent:publish",
  "agent:update", "agent_ability:assign", "agent_passport:manage", "agent_passport:revoke",
  "agent_passport:suspend", "api_key:manage_own", "api_key:revoke", "approval_rule:manage",
  "audit:export", "autonomy:manage", "autopilot:configure", "budget:manage", "changeset:approve",
  "cloud_account:manage", "code_task:override", "comms:manage", "connection:revoke_any",
  "connection:use_own", "decision:approve", "delegation:grant", "delegation:revoke_any",
  "deployment:production", "deployment:rollback", "deployment_target:manage", "entitlement:manage",
  "github:connect", "github:manage", "github:merge", "group:manage", "group:manage_members",
  "host:exec", "host:manage", "identity_provider:manage", "improvement:manage", "infra:provision",
  "infra:terminate", "integration:manage", "kms_key:manage", "mcp_server:review",
  "model_provider:manage", "model_provider:rotate", "oidc:admin", "oidc_client:manage",
  "oidc_pool_user:manage", "org:delete", "org:update", "owner:assign", "pack:install",
  "pipeline:manage", "policy:manage", "project:create", "project:delete", "project:update",
  "reliability:manage", "repository:manage", "repository:register", "role_binding:assign",
  "sandbox:exec", "secret:manage", "security_policy:manage", "service_account:manage",
  "service_account_key:manage", "session:revoke", "settings:update", "setup:manage",
  "skill:manage", "system:operate", "telemetry:manage", "telemetry:share", "template:approve",
  "user:delete", "user:disable", "user:invite", "user:reset_mfa", "user:revoke_sessions",
  "user:update", "workflow:delete", "workflow:publish", "workflow_run:compensate",
  "workflow_run:delete", "app_secret:live", "app_secret:write",
  // The knowledge graph: forgetting/promoting facts (BOSSAPP-622) and Rules review (BOSSAPP-768).
  "knowledge:manage", "knowledge:review",
].sort();

describe("the step-up list", () => {
  it("is exactly the catalogue's, and changes only on purpose", () => {
    const fromCatalogue = Object.values(PERMISSION_META).filter((p) => p.stepUp).map((p) => p.key).sort();
    expect(fromCatalogue).toEqual(STEP_UP_ACTIONS);
  });
});
