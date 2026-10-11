"use client";

/**
 * APP CONNECTORS on the Command Center's "Browse templates" (next to the brief templates): login,
 * payments and the other approved code templates (templates gallery), which used to be reachable
 * only through New build › From a template. A card opens the same template sheet; Start is the
 * same governed apply, and the run opens in Workflows.
 */
import React, { useState } from "react";

import { TemplateGallery } from "../../components/v2/templates/gallery";
import { TemplateSheet } from "../../components/v2/templates/template-sheet";
import type { TemplateCard } from "../../lib/templates";
import { showToast } from "../client-forms";
import { consoleHref } from "../console-href";

/** Where a started connector's run is watched: this project's Workflows, on the run. */
export function connectorRunHref(projectId: string, workflowRunId: string | null): string {
  return consoleHref({ project_id: projectId, area: "delivery_workflows", ...(workflowRunId ? { wfr: workflowRunId } : {}) });
}

export function ConnectorsShelf({ projectId }: { projectId: string }) {
  const [picked, setPicked] = useState<TemplateCard | null>(null);
  return (
    <section className="cc-shelf" aria-labelledby="cc-conn-h" data-testid="cc-connectors">
      <div className="cc-sec-h">
        <h2 id="cc-conn-h">App connectors</h2>
        <span className="cc-sub" title="Code templates: each adds a tested feature, such as sign-in or payments, to a new app">
          Add sign-in, payments and more
        </span>
      </div>
      <TemplateGallery projectId={projectId} testId="cc-connectors-gallery" onPick={setPicked} />
      {picked ? (
        <TemplateSheet projectId={projectId} card={picked} target={{ kind: "new" }} onClose={() => setPicked(null)}
                       onStarted={(runId) => {
                         setPicked(null);
                         showToast("success", `${picked.name} started`);
                         window.location.assign(connectorRunHref(projectId, runId));
                       }} />
      ) : null}
    </section>
  );
}
