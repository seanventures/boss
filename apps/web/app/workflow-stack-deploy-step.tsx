"use client";

/**
 * A workflow's deploy step, when the run's repository ships to a STACK target.
 *
 * Observed on boss-1 (wfr_79c63a78b34546a3b927): the deploy step of a repository with an active
 * stack target was rendered as the per-app container flow, "Where it ships, needs you first",
 * a host picker and Set target, then "Approve the release", a path Community has paused, and
 * the stack deployment only appeared after that first approval, asking for a second.
 *
 * Now there is ONE approval: the stack deployment's own decision is the step's gate (the engine
 * no longer holds the step first). So this renders that deployment, its plan, the Approve /
 * Reject its server-decided `actions` offer, live progress, Open app, Roll back, through the
 * same view and the same decision API Infra uses. Nothing here decides which buttons exist.
 */
import React, { useRef } from "react";

import { isLocalRepo } from "../lib/repo-links";
import type { StackDeployment } from "../lib/stack-deploy";
import { StackDeploymentView } from "./stack-deploy-ui";

/** What the API attaches to a deploy step that ships to a stack target (`step.stack_deploy`). */
export type StepStackDeploy = {
  stack_deployment_id?: string | null;
  target_id?: string | null;
  target_name?: string | null;
  environment?: string | null;
  /** The repository it ships: a local one's source is its change, never a pull request. */
  repository?: string | null;
  host?: string | null;
  host_address?: string | null;
  deployment?: StackDeployment | null;
};

export function stackDeployOf(step: { stack_deploy?: unknown }): StepStackDeploy | null {
  const value = step.stack_deploy;
  return value && typeof value === "object" ? (value as StepStackDeploy) : null;
}

export function StackDeployStep({ stack, actor, onChanged }: {
  stack: StepStackDeploy; actor: string; onChanged?: () => void;
}) {
  const where = [stack.target_name, stack.environment].filter(Boolean).join(", ");
  const host = stack.host ? `${stack.host}${stack.host_address ? ` (${stack.host_address})` : ""}` : "";
  const deploymentId = stack.stack_deployment_id || stack.deployment?.id || "";
  // Re-read the run only when the deployment's STATUS moves, the view polls every few seconds
  // while it is live, and re-reading every run on each poll would be a page of requests a tick.
  const lastStatus = useRef<string>(String(stack.deployment?.status ?? ""));
  return (
    <div className="dw-stack-deploy" data-testid="dw-stack-deploy">
      <p className="detail" data-testid="dw-stack-where">
        Ships to stack target <strong>{where || stack.target_id || "this repository's target"}</strong>
        {host ? <> on <strong>{host}</strong></> : null}, as its <code>boss.deploy.yaml</code> declares.
        {deploymentId
          ? " Approving this deployment is the release approval, there is no other."
          : ` BOSS plans the deployment from the build's ${isLocalRepo(stack.repository) ? "merged change" : "pull request"} as soon as this step runs; `
            + "its approval is the release approval."}
      </p>
      {deploymentId ? (
        <StackDeploymentView
          // Keyed by the deployment: a retried step plans a new one, and the view's own state
          // (busy, last error) belongs to the deployment it was showing.
          key={deploymentId}
          deploymentId={deploymentId}
          actor={actor}
          initial={stack.deployment ?? null}
          // The run's view re-reads when the deployment moves, so the step's status follows.
          onChanged={(d) => {
            const now = String(d.status ?? "");
            if (now !== lastStatus.current) { lastStatus.current = now; onChanged?.(); }
          }}
        />
      ) : null}
    </div>
  );
}
