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

export function labHref(labProjectId: string): string {
  return consoleHref({ project_id: labProjectId, area: "code_tasks" });
}
