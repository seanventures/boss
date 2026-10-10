"use client";

/**
 * "Builds may change CI workflows" for one repository (services/ci_gate.py). Off by default.
 * Turning it on is an Owner/Admin act with step-up (github:connect), recorded; when on, the GitHub
 * App needs `workflows: write`, and an App created earlier must accept it at GitHub — the link
 * goes straight to the App's permissions page, then the installation's page.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { fetchCiWorkflows, setupWrite, type CiWorkflows } from "../../lib/setup-client";
import { toast, useBoard } from "./parts";
import { Dot, Out, Tip } from "./pill";
import { GITHUB_VERIFIED_EVENT, sayVerified } from "./github-access-row";

/** VERIFIED, NOT ASSUMED: what GitHub says about `workflows` on the installation, and the one
 *  page that closes the gap (the App's permissions page, or the installation's review). */
function WorkflowsAtGitHub({ state, busy, onRecheck }: { state: CiWorkflows; busy: boolean; onRecheck: () => void }) {
  const w = state.workflows ?? { state: "unknown" as const, fix_url: null };
  const again = (
    <button type="button" className="su-link" onClick={onRecheck} disabled={busy} data-testid="su-ci-workflows-recheck">
      Check again
    </button>
  );
  if (w.state === "accepted") {
    return <span className="su-line" data-testid="su-ci-workflows-state" data-state="accepted">
      <Dot tone="green" />GitHub: Workflows accepted ✓</span>;
  }
  const fix = w.fix_url ?? (w.state === "waiting" ? state.installation_url : state.permissions_url) ?? null;
  const words = w.state === "waiting" ? "GitHub: waiting for you to accept Workflows"
    : w.state === "app_lacks" ? "GitHub: the App does not ask for Workflows yet"
    : "GitHub: not confirmed yet";
  return (
    <span className="su-line" data-testid="su-ci-workflows-state" data-state={w.state}>
      <Dot tone="amber" />{words}
      {fix ? (
        <Out href={fix} tip={state.note} testid="su-ci-workflows-accept">
          {w.state === "waiting" ? "Accept on GitHub" : "Open on GitHub"}
        </Out>
      ) : null}
      {again}
    </span>
  );
}

export function CiWorkflowsToggle({ bindingId }: { bindingId: string }) {
  const { projectId } = useBoard();
  const [state, setState] = useState<CiWorkflows | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    const read = () => fetchCiWorkflows(projectId, bindingId)
      .then((v) => { if (live && typeof v?.enabled === "boolean") setState(v); })
      .catch(() => { /* a viewer without github:read sees nothing here */ });
    void read();
    // GitHub access asked GitHub: show the same stored answer, never an older one.
    const other = (e: Event) => { if ((e as CustomEvent<{ from?: string }>).detail?.from !== "ci") void read(); };
    window.addEventListener(GITHUB_VERIFIED_EVENT, other);
    return () => { live = false; window.removeEventListener(GITHUB_VERIFIED_EVENT, other); };
  }, [projectId, bindingId]);
  if (!state) return null;
  const recheck = async () => {
    setBusy(true);
    try { setState(await fetchCiWorkflows(projectId, bindingId, true)); sayVerified("ci"); }
    catch { toast("GitHub did not answer. Try again in a minute.", "error"); }
    finally { setBusy(false); }
  };
  const flip = async () => {
    setBusy(true);
    try {
      setState(await setupWrite<CiWorkflows>(projectId, "ci_workflows",
        { binding_id: bindingId, enabled: !state.enabled },
        { label: state.enabled ? "Turn off CI changes" : "Allow CI changes" }));
      sayVerified("ci");
    } catch (error) {
      toast(error instanceof Error ? error.message : "Could not change it", "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="su-line" data-testid="su-ci-workflows">
      <Tip tip={state.help} className="su-faint">CI changes</Tip>
      <GatedButton permission="github:connect" type="button" className="su-btn su-btn-sm"
                   aria-pressed={state.enabled} disabled={busy} onClick={flip}
                   title={`${state.help} Needs ${state.requires}.`}
                   data-testid="su-ci-workflows-toggle">
        {state.enabled ? "Allowed" : "Off"}
      </GatedButton>
      {state.enabled ? <WorkflowsAtGitHub state={state} busy={busy} onRecheck={recheck} /> : null}
    </div>
  );
}
