"use client";

import { consoleHref } from "./console-href";

/** Shown while the console is on ANOTHER project of the org (?project_id=, e.g. a template's
 *  lab project): which one, and the way back to the org's own. */
export function ProjectAway({ name, homeName, lab }: { name: string; homeName: string; lab: boolean }) {
  return (
    <div className="project-away" role="status" data-testid="project-away" data-lab={lab || undefined}>
      <span>
        {lab ? "Lab project: " : "Project: "}<strong>{name}</strong>
        {lab ? ", insecure by design, never deployed to a host" : ""}
      </span>
      <a href={consoleHref({ project_id: undefined })}>← Back to {homeName}</a>
    </div>
  );
}
