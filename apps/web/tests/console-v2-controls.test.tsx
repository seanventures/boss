import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { DeployControls } from "../components/v2/DeployControls";
import { __resetRunDeploymentCache } from "../lib/run-deployments";

/**
 * The wiring of the per-app controls.
 *
 * The DECISIONS these controls make — what to offer, what to report — are tested for real in
 * console-v2-controls-logic.test.ts against the pure module they live in. What is left here is
 * the wiring, which has no behaviour to exercise without a DOM: which endpoints the component
 * talks to, and what it renders before it knows anything.
 *
 * Those endpoints matter beyond correctness. The test agent matches controls by the requests
 * they ISSUE, so calling the same route handlers as the v1 card is what will let it report
 * these as MOVED rather than MISSING when v1 is retired.
 */

const source = () =>
  readFileSync(resolve(__dirname, "../components/v2/DeployControls.tsx"), "utf8");

beforeEach(() => __resetRunDeploymentCache());
afterEach(() => __resetRunDeploymentCache());

const firstPaint = () =>
  renderToStaticMarkup(
    <DeployControls
      runId="crun_1"
      appName="Shorty"
      projectId="boss-dogfood"
      actor="executive_console"
      deployable
      deployableReason=""
    />,
  );

describe("before it knows anything", () => {
  it("says it is checking rather than asserting a state", () => {
    // "No verified host yet" was stated before the lookup returned, and there WAS a verified
    // host the whole time. On a page this slow, the false claim is what the operator reads.
    expect(firstPaint()).toContain('data-testid="v2-controls-loading"');
  });

  it("offers no action on a state it has not read", () => {
    const out = firstPaint();
    for (const id of ["v2-deploy", "v2-unbind", "v2-retry", "v2-controls-unbound"]) {
      expect(out, id).not.toContain(`data-testid="${id}"`);
    }
  });
});

describe("what it talks to", () => {
  it("deploy and retry go through the app-deploy route handler", () => {
    expect(source()).toContain('apiWrite("/api/app-deploy"');
  });

  it("unbind goes through the deployment-targets route handler", () => {
    const src = source();
    expect(src).toMatch(/\/api\/deployment-targets\?target_id=/);
    expect(src).toMatch(/method:\s*"DELETE"/);
  });

  it("nothing here is a Server Action", () => {
    // A Server Action re-renders the whole route to reply. That is what made "Set target"
    // appear to hang for 115 seconds while its own write took milliseconds, and what cost 82
    // route renders on one Factory page load.
    const src = source();
    expect(src).not.toMatch(/from "\.\.\/\.\.\/app\/actions"/);
    expect(src).not.toMatch(/useActionState/);
  });

  it("the judgements are delegated, not re-derived here", () => {
    // Two copies of "can this deploy?" is two answers waiting to disagree. The component
    // renders; the pure module decides.
    const src = source();
    expect(src).toContain("controlsState(binding");
    expect(src).toContain("outcomeMessage(res, data");
    expect(src).toContain("deployRequest(kind");
  });

  it("polls only while a deployment is actually moving", () => {
    // A card that polls forever keeps a browser and an API busy describing a deployment that
    // finished yesterday — and this console's load problems were request VOLUME.
    expect(source()).toMatch(/if\s*\(!live\)\s*return;/);
  });

  it("every button says what it will do before it is pressed", () => {
    const src = source();
    for (const testid of ["v2-deploy", "v2-unbind", "v2-retry"]) {
      const idx = src.indexOf(`data-testid="${testid}"`);
      expect(idx, `${testid} missing`).toBeGreaterThan(-1);
      expect(src.slice(Math.max(0, idx - 500), idx + 500), `${testid} has no title`)
        .toMatch(/title=/);
    }
  });
});
