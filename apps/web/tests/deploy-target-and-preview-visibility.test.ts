/** The controls an operator needs must survive the fields the API leaves out.
 *
 *  Three separate reports of "I added the host and the workflow still says there is none" and
 *  "the preview is open in another tab and the step says nothing is serving it" came down to
 *  two absent fields — a response key read under the wrong name, and a `capability` that is
 *  null on any step that has not run yet. Nothing here needed a browser to catch. It needed
 *  somebody to assert the shape.
 */
import { describe, it, expect } from "vitest";
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
