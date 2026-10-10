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
import { Out, Tip } from "./pill";

export function CiWorkflowsToggle({ bindingId }: { bindingId: string }) {
  const { projectId } = useBoard();
  const [state, setState] = useState<CiWorkflows | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    fetchCiWorkflows(projectId, bindingId)
      .then((v) => { if (live && typeof v?.enabled === "boolean") setState(v); })
      .catch(() => { /* a viewer without github:read sees nothing here */ });
    return () => { live = false; };
  }, [projectId, bindingId]);
  if (!state) return null;
  const flip = async () => {
    setBusy(true);
    try {
      setState(await setupWrite<CiWorkflows>(projectId, "ci_workflows",
        { binding_id: bindingId, enabled: !state.enabled },
        { label: state.enabled ? "Turn off CI changes" : "Allow CI changes" }));
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
      {state.enabled && state.permissions_url ? (
        <Out href={state.permissions_url} tip={state.note} testid="su-ci-workflows-accept">
          Accept on GitHub
        </Out>
      ) : null}
      {state.enabled && state.installation_url ? (
        <Out href={state.installation_url} tip="Then accept the new permission on the installation."
             testid="su-ci-workflows-install">Installation</Out>
      ) : null}
    </div>
  );
}
