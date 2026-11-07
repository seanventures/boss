"use client";

import React, { useEffect, useState } from "react";

import { templatesApi } from "../../lib/templates";
import { consoleHref } from "../console-href";

type Lab = { projectId: string; name: string; appName: string | null };

/** The lab projects this project's insecure-template apps are built in. A lab is a project of
 *  its own (never beside real apps), so its builds are not in this Factory: each gets a link to
 *  its own Factory. Renders nothing when the project has none, or the list cannot be read. */
export function LabLinks({ projectId }: { projectId: string }) {
  const [labs, setLabs] = useState<Lab[]>([]);
  useEffect(() => {
    let live = true;
    templatesApi.labs(projectId).then((l) => { if (live) setLabs(l); }).catch(() => undefined);
    return () => { live = false; };
  }, [projectId]);
  if (!labs.length) return null;
  return (
    <div className="fx-labs" data-testid="fx-labs">
      <span className="fx-labs-k" title="Insecure-by-design training apps are built in their own lab project, never beside real apps">
        Lab projects
      </span>
      {labs.map((l) => (
        <a key={l.projectId} href={labHref(l.projectId)} data-testid="fx-lab-link">{l.name}</a>
      ))}
    </div>
  );
}

/** CODE TASKS › LAB (01-06: Builds, Apps, Lab): every lab project this project's insecure-by-design
 *  template apps are built in, each opening its own Code Tasks. Says so when there is none, and
 *  when the list cannot be read (never a blank tab). */
export function LabTab({ projectId }: { projectId: string }) {
  const [labs, setLabs] = useState<Lab[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    templatesApi.labs(projectId).then((l) => { if (live) setLabs(l); }).catch(() => { if (live) { setFailed(true); setLabs([]); } });
    return () => { live = false; };
  }, [projectId]);
  if (labs === null) return <div className="ag-empty fx-empty" data-testid="fx-lab-loading"><span>Loading lab projects…</span></div>;
  if (!labs.length) {
    return (
      <div className="ag-empty fx-empty" data-testid="fx-lab-empty">
        <span>{failed ? "BOSS couldn't list the lab projects. Try again in a moment." : "No lab projects yet. Insecure-by-design training apps (from a template) are built in their own lab project, never beside real apps."}</span>
      </div>
    );
  }
  return (
    <div className="fx-lab-list" data-testid="fx-lab-list" role="list" aria-label="Lab projects">
      {labs.map((l) => (
        <a key={l.projectId} role="listitem" className="fx-lab-row" href={labHref(l.projectId)} data-testid="fx-lab-link"
           title={`Open ${l.name}'s Code Tasks`}>
          <b>{l.name}</b>{l.appName ? <span className="fx-muted"> · {l.appName}</span> : null}
          <span className="fx-muted fx-lab-go" aria-hidden>Open ›</span>
        </a>
      ))}
    </div>
  );
}

export function labHref(labProjectId: string): string {
  return consoleHref({ project_id: labProjectId, area: "code_tasks" });
}
