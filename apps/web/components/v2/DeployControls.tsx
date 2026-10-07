"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import { GatedButton } from "../gated";
import React, { useCallback, useEffect, useState } from "react";

import { consoleHref } from "../../app/console-href";
import {
  approvalsHref, plannedDeploymentFrom, type PlannedDeployment,
} from "../../app/deploy-ui";
import { forgetRunDeployment, loadRunDeployment } from "../../lib/run-deployments";
import {
  controlsState,
  deployRequest,
  outcomeMessage,
  type Binding,
} from "../../lib/v2/deploy-controls";
import { apiWrite } from "../../lib/api-write";

/**
 * Set target, unbind, deploy, retry — the four controls the parity map flags as highest risk.
 *
 * DELIBERATELY THE SAME REQUESTS AS v1
 * ------------------------------------
 * These call /api/app-deploy and /api/deployment-targets, the same route handlers the v1 card
 * uses. Two reasons, and the second is the important one:
 *
 *   - those handlers are already off the Server-Action path, which is what made "Set target"
 *     appear to hang for 115 seconds while its own write took milliseconds;
 *   - the console test agent matches controls by the requests they ISSUE, so reusing them is
 *     what lets it report these as MOVED rather than MISSING when v1 is retired.
 *
 * WHAT EACH BUTTON PROMISES
 * -------------------------
 * Every control says what it will do before it is pressed, and reports what happened after. A
 * button that silently succeeds and a button that silently fails look identical, and this
 * console has shipped both: "retry started" with nothing happening behind it, and an Unbind
 * that worked while the card went on claiming the target was still bound.
 */

export function DeployControls({
  runId,
  appName,
  projectId,
  actor,
  deployable,
  deployableReason,
}: {
  runId: string;
  appName: string;
  projectId: string;
  actor: string;
  deployable: boolean;
  deployableReason: string;
}) {
  // AN EMPTY BINDING IS NOT AN ANSWER UNTIL SOMEONE HAS ASKED.
  //
  // `binding` starts null and the component renders "checking" rather than "not bound". The v1
  // card asserted absence before it had looked, and there WAS a verified host the whole time.
  const [binding, setBinding] = useState<Binding | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [noteIsError, setNoteIsError] = useState(false);
  const [plan, setPlan] = useState<PlannedDeployment | null>(null);

  const load = useCallback(async () => {
    // A lookup error is carried INTO the state rather than swallowed: controlsState turns it
    // into "unreadable", which offers no actions. Treating it as an empty result is what once
    // showed a deployed app as never deployed with an armed Deploy button beside it.
    setBinding((await loadRunDeployment(runId, appName)) as Binding);
  }, [runId, appName]);

  const reload = useCallback(async () => {
    forgetRunDeployment(runId);
    await load();
  }, [runId, load]);

  useEffect(() => {
    void load();
  }, [load]);

  // Every judgement lives in one pure function, tested on its own. The component only renders
  // what it decides.
  const state = controlsState(binding, { deployable, deployableReason });
  const live = state.kind === "ready" && state.live;

  // Poll only while something is actually moving. A card that polls forever keeps a browser
  // and an API busy describing a deployment that finished yesterday.
  useEffect(() => {
    if (!live) return;
    const t = window.setInterval(() => void reload(), 4000);
    return () => window.clearInterval(t);
  }, [live, reload]);

  const post = async (
    kind: "deploy" | "retry",
    args: { targetId?: string | null; deploymentRunId?: string | null },
  ) => {
    setBusy(kind);
    setNote(null);
    setPlan(null);
    try {
      const res = await apiWrite("/api/app-deploy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          deployRequest(kind, { projectId, actor, runId, ...args }),
        ),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setNoteIsError(true);
        setNote(outcomeMessage(res, data, ""));
      } else {
        const nextPlan = plannedDeploymentFrom(data);
        if (nextPlan) {
          setPlan(nextPlan);
        } else {
          setNoteIsError(true);
          setNote("Deployment request did not return a reviewable ChangeSet. No deployment was confirmed.");
        }
      }
    } catch (e) {
      setNoteIsError(true);
      setNote(String((e as Error)?.message ?? e));
    } finally {
      setBusy(null);
      await reload();
    }
  };

  const unbind = async (targetId: string) => {
    setBusy("unbind");
    setNote(null);
    setPlan(null);
    try {
      const res = await apiWrite(
        `/api/deployment-targets?target_id=${encodeURIComponent(targetId)}` +
          `&actor=${encodeURIComponent(actor)}`,
        { method: "DELETE" },
      );
      const data = await res.json().catch(() => ({}));
      setNoteIsError(!res.ok);
      setNote(outcomeMessage(res, data, "target retired — bind a new one to deploy again"));
    } catch (e) {
      setNoteIsError(true);
      setNote(String((e as Error)?.message ?? e));
    } finally {
      setBusy(null);
      await reload();
    }
  };

  if (state.kind === "checking") {
    return (
      <p className="v2-ws-ship" data-testid="v2-controls-loading">
        Checking deployment…
      </p>
    );
  }

  if (state.kind === "unreadable") {
    return (
      <p className="v2-ws-ship" data-testid="v2-controls-error">
        Could not check this app&apos;s deployment ({state.reason}). No action is offered,
        because acting on an unknown state is how the wrong app gets deployed.
      </p>
    );
  }

  return (
    <div className="v2-controls" data-testid="v2-controls">
      <div className="v2-controls-row">
        {state.bound && state.targetId ? (
          <>
            <span className="v2-mono v2-controls-bound" data-testid="v2-controls-target">
              <span className="v2-row-meta-dim">bound to</span> {state.host}
            </span>
            <GatedButton permission="deployment:create"
              type="button"
              className="v2-btn"
              disabled={busy !== null || !state.canDeploy || !!plan}
              onClick={() =>
                void post("deploy", { targetId: state.targetId })
              }
              data-testid="v2-deploy"
              /* What it will do, before it is pressed — or why it will not. */
              title={
                plan
                  ? "This deployment ChangeSet is awaiting review"
                  : state.canDeploy
                  ? `Creates a ChangeSet to review before deploying ${appName} to ${state.host}`
                  : `Cannot plan deployment: ${state.blockedReason}`
              }
            >
              {busy === "deploy" ? "Planning…" : "Plan deployment"}
            </GatedButton>
            <GatedButton permission="deployment_target:manage"
              type="button"
              className="v2-btn v2-btn-quiet"
              disabled={busy !== null}
              onClick={() => void unbind(state.targetId!)}
              data-testid="v2-unbind"
              title="Retires this target. The app stays; only the destination is removed."
            >
              {busy === "unbind" ? "Unbinding…" : "Unbind target"}
            </GatedButton>
          </>
        ) : (
          // NOT a blank space. The v1 card rendered nothing here, which left an app with a
          // merged PR and no visible way forward on its own card.
          <span className="v2-mono v2-controls-unbound" data-testid="v2-controls-unbound">
            {state.blockedReason} — bind a verified host on CI/CD.
          </span>
        )}

        {state.canRetry ? (
          <GatedButton permission="deployment:create"
            type="button"
            className="v2-btn v2-btn-quiet"
            disabled={busy !== null || !!plan}
            onClick={() =>
              void post("retry", { deploymentRunId: state.retryDeploymentId })
            }
            data-testid="v2-retry"
            title="Create a new ChangeSet to review for the same commit and target"
          >
            {busy === "retry" ? "Planning…" : "Plan retry"}
          </GatedButton>
        ) : null}
      </div>

      {state.live ? (
        <p className="v2-controls-note v2-mono" data-testid="v2-controls-live">
          deployment {state.liveStatus} — this refreshes while it runs
        </p>
      ) : null}

      {state.waitingForReview && !plan ? (
        <p className="v2-controls-note" data-testid="v2-controls-plan">
          No deployment is running. Review its ChangeSet in{" "}
          <a href={consoleHref({ area: "infra" })}>ProviderOps ChangeSet Review</a>.
        </p>
      ) : null}

      {plan && plan.next_action === "approve_decision" ? (
        <p className="v2-controls-note" data-testid="v2-controls-plan" role="status">
          Plan ready: {plan.review?.app_name || appName} to {plan.review?.host || "its host"}
          {plan.review?.environment ? ` · ${plan.review.environment}` : ""}. Nothing has
          deployed. <a href={approvalsHref()}>Approve it in Approvals</a>; it then deploys,
          and rolls back if the health check fails.
        </p>
      ) : plan ? (
        <p className="v2-controls-note" data-testid="v2-controls-plan" role="status">
          Nothing has deployed. ChangeSet <code>{plan.changeset_id}</code> is ready.
          Next, open <a href={consoleHref({ area: "infra" })}>ProviderOps ChangeSet Review</a>,
          approve it, then apply it. Run <code>{plan.deployment_run_id}</code> records the plan.
        </p>
      ) : null}

      {note ? (
        <p className="v2-controls-note" data-testid="v2-controls-note"
           role={noteIsError ? "alert" : "status"}>
          {note}
        </p>
      ) : null}
    </div>
  );
}
