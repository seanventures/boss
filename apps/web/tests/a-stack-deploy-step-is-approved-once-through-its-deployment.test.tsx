import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { StackDeployStep, stackDeployOf } from "../app/workflow-stack-deploy-step";
import type { StackDeployment } from "../lib/stack-deploy";
import { codeOnly } from "./_helpers/source";

(globalThis as Record<string, unknown>).React = React;

/**
 * A workflow deploy step that ships to a STACK target is approved ONCE, through its deployment.
 *
 * boss-1, wfr_79c63a78b34546a3b927: the deploy step of paramg/boss-sandbox — a repository with
 * an active stack target — was held at a release_manager gate before it ran, and drawn as the
 * per-app container flow ("Where it ships · needs you first", host picker, Set target, then
 * "Approve the release"). The stack deployment, with its own decision, only came after.
 * The engine now plans the deployment at once and its decision is the step's gate; the step
 * renders that deployment, with exactly the actions the server says are valid.
 */

const UI = codeOnly("app/delivery-workflows-ui.tsx");

function deployment(over: Partial<StackDeployment> = {}): StackDeployment {
  return {
    id: "sdep_1", project_id: "p", target_id: "deptgt_stack", repository: "paramg/boss-sandbox",
    ref: "pull/12", commit: "e".repeat(40), status: "awaiting_approval", decision_id: "dec_1",
    manifest: { name: "sandbox", steps: { install: ["./install.sh"] }, env_keys: ["DB_URL"] },
    steps: [], actions: ["approve", "reject"], ...over,
  };
}

const where = { target_name: "stack-demo", environment: "production", host: "boss-1",
                host_address: "203.0.113.9", target_id: "deptgt_stack" };

describe("the deploy step of a stack deployment", () => {
  it("renders the deployment with Approve and Reject from its actions, and no Set target", () => {
    const html = renderToStaticMarkup(
      <StackDeployStep actor="ceo" stack={{ ...where, stack_deployment_id: "sdep_1",
                                            deployment: deployment() }} />);
    expect(html).toContain('data-testid="stack-deployment"');
    expect(html).toContain(">Approve<");
    expect(html).toContain(">Reject<");
    expect(html).toContain("stack-demo");
    expect(html).toContain("boss-1");
    expect(html).toContain("./install.sh");
    expect(html).not.toContain("Set target");
    expect(html).not.toContain("Where it ships");
    expect(html).not.toContain("Approve the release");
  });

  it("offers only what the server allows: a healthy deployment opens, it is not approved", () => {
    const html = renderToStaticMarkup(
      <StackDeployStep actor="ceo" stack={{ ...where, stack_deployment_id: "sdep_1",
        deployment: deployment({ status: "healthy", actions: ["open"],
                                 open_url: "https://sandbox.example.test" }) }} />);
    expect(html).toContain('data-testid="stack-open"');
    expect(html).not.toContain(">Approve<");
    expect(html).not.toContain(">Reject<");
  });

  it("before it is planned, names where it ships and that its approval is the release's", () => {
    const html = renderToStaticMarkup(<StackDeployStep actor="ceo" stack={where} />);
    expect(html).toContain("stack-demo");
    expect(html).not.toContain('data-testid="stack-deployment"');
    expect(html).not.toContain("Set target");
  });

  it("is read from the step the API serves", () => {
    expect(stackDeployOf({ stack_deploy: { target_id: "t" } })).toEqual({ target_id: "t" });
    expect(stackDeployOf({ stack_deploy: null })).toBeNull();
    expect(stackDeployOf({})).toBeNull();
  });

  it("replaces the per-app flow on the workflow's deploy step", () => {
    // The stack branch comes FIRST, so neither the Set-target layouts nor Ship now can render
    // beside it.
    const stack = UI.indexOf("{stackDeploy ? (");
    expect(stack).toBeGreaterThan(-1);
    expect(UI.indexOf('data-testid="dw-ship-single"')).toBeGreaterThan(stack);
    expect(UI.indexOf('data-testid="dw-step-target"')).toBeGreaterThan(stack);
    expect(UI).toContain("<StackDeployStep stack={stackDeploy} actor={actor} onChanged={onBound} />");
    expect(UI).toContain("isDeployStep(step, capability) && !stackDeploy && shippedTo && buildRunId");
  });
});
