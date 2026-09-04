/** The controls an operator needs must survive the fields the API leaves out.
 *
 *  Three separate reports of "I added the host and the workflow still says there is none" and
 *  "the preview is open in another tab and the step says nothing is serving it" came down to
 *  two absent fields — a response key read under the wrong name, and a `capability` that is
 *  null on any step that has not run yet. Nothing here needed a browser to catch. It needed
 *  somebody to assert the shape.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import {
  verifiedHostsFrom, isDeployStep, isPreviewStep,
} from "../app/delivery-workflows-ui";

type Step = Parameters<typeof isPreviewStep>[0];

/** A step as the API actually sends it, including the fields it omits. */
function step(over: Partial<Step>): Step {
  return {
    step_id: "deploy", kind: "capability", status: "pending",
    capability: null, output: {}, evidence: [], error: null, decision_id: null,
    approval: {}, attempt: 1, started_at: null, completed_at: null,
    ...over,
  } as Step;
}

describe("hosts come back under `hosts`, not `items`", () => {
  const verified = { id: "dh_1", name: "hetzner-ci-01", status: "verified" };

  it("reads the key the deployment-hosts endpoint actually uses", () => {
    // THE BUG. Verbatim shape of GET /v1/deployment-hosts. Reading `items` here returned []
    // and the deploy step said "this project has no verified host" about a VERIFIED host.
    const body = { hosts: [verified], preconditions: {} };
    expect(verifiedHostsFrom(body).map((h) => h.name)).toEqual(["hetzner-ci-01"]);
  });

  it("still reads `items`, so a unified shape would not break it", () => {
    expect(verifiedHostsFrom({ items: [verified] })).toHaveLength(1);
  });

  it("offers no host that cannot be shipped to, and never throws on junk", () => {
    expect(verifiedHostsFrom({ hosts: [{ id: "d", status: "pending" }] })).toEqual([]);
    expect(verifiedHostsFrom({})).toEqual([]);
    expect(verifiedHostsFrom(null)).toEqual([]);
    expect(verifiedHostsFrom({ hosts: "not-a-list" })).toEqual([]);
  });
});

describe("a step is recognised before it has run", () => {
  // WHY THIS IS THE WHOLE POINT. `capability` is written into a step's OUTPUT by the handler,
  // so a PENDING step carries null — and pending is exactly when an operator still needs to
  // bind a host or start a preview. Gating those controls on capability alone hid them at the
  // only moment they were useful.
  it("finds the deploy step with capability null", () => {
    expect(isDeployStep(step({ step_id: "deploy", capability: null }))).toBe(true);
  });

  it("finds the preview step with capability null", () => {
    expect(isPreviewStep(step({ step_id: "preview", capability: null }))).toBe(true);
  });

  it("still honours capability when the step id is something else", () => {
    expect(isDeployStep(step({ step_id: "ship_it", capability: "deploy" }))).toBe(true);
    expect(isPreviewStep(step({ step_id: "try_it", capability: "preview" }))).toBe(true);
  });

  it("does not claim unrelated steps", () => {
    expect(isDeployStep(step({ step_id: "build", capability: "build" }))).toBe(false);
    expect(isPreviewStep(step({ step_id: "build", capability: "build" }))).toBe(false);
  });
});


describe("the deploy step binds an app to a host, not just picks one", () => {
  // Asked for many times, and what shipped each time was a bare dropdown that fired on
  // change — which chooses the container name and the port on the operator's behalf and
  // gives them nothing to confirm. The project card has always had the real control.
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("offers the host, the app name and the port", () => {
    expect(SOURCE).toContain('data-testid="dw-deploy-host"');
    expect(SOURCE).toContain('data-testid="dw-deploy-appname"');
    expect(SOURCE).toContain('data-testid="dw-deploy-port"');
  });

  it("waits for an explicit Set target rather than acting on change", () => {
    expect(SOURCE).toContain('data-testid="dw-deploy-settarget"');
    expect(SOURCE).toContain("Set target");
  });

  it("posts to the same endpoint the project card uses, so the two cannot drift", () => {
    expect(SOURCE).toContain('"/api/app-deploy"');
    expect(SOURCE).toContain('op: "set-target"');
  });

  it("binds the BUILD, since that is what has an app to place", () => {
    expect(SOURCE).toContain("buildRunId={buildRunId ?? \"\"}");
  });
});

describe("an action always says it is running", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("keeps the busy flag set through the reload, not just the request", () => {
    // `setBusy(null)` ran BEFORE `load()`, so every action ended with the button snapping back
    // to normal while the console spent seconds re-fetching with nothing on screen. From the
    // operator's side that is a click that did nothing.
    expect(SOURCE).toContain("if (result.ok) await load();");
    expect(SOURCE).toMatch(/finally\s*\{[^}]*setBusy\(null\)/);
  });

  it("shows one indicator that every action gets for free", () => {
    expect(SOURCE).toContain('data-testid="dw-working"');
    expect(SOURCE).toContain('role="status"');
  });

  it("refreshes the view once a binding lands", () => {
    // The write succeeded and the step went on showing the same empty form.
    expect(SOURCE).toContain("if (r.ok) onBound();");
  });
});

describe("re-running does not duplicate a live workflow", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("refuses while the run is still going", () => {
    // Pressed on a mid-flight run it starts a SECOND run on the same directive: two workflows
    // for one project, both at step 6.
    expect(SOURCE).toContain("ACTIVE_RUN_STATES");
    expect(SOURCE).toContain('data-testid="dw-rerun-blocked"');
  });

  it("points at the control that actually retries", () => {
    expect(SOURCE).toMatch(/Try … again|Try \{capability/);
  });

  it("still allows a re-run once the run has stopped", () => {
    expect(SOURCE).toContain('data-testid="dw-rerun"');
  });
});

describe("the agent is reachable from the step", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("opens the console's existing dock rather than a second chat surface", () => {
    expect(SOURCE).toContain("askBossAbout");
    expect(SOURCE).toContain('url.searchParams.set("agent"');
    expect(SOURCE).toContain('url.searchParams.set("dock_ask"');
  });

  it("opens with this step's own words, not an empty box", () => {
    expect(SOURCE).toContain("const askNote");
    expect(SOURCE).toContain('data-testid="dw-ask-boss"');
  });
});
