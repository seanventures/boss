"use client";

import { useEffect, useState } from "react";

import { projectIdFromSearch, searchWithProject } from "./console-owner";

/** Human label for the object kind a deep link opened, for the switch banner's copy. */
const OBJECT_LABEL: Record<string, string> = {
  build: "build",
  workflow_run: "workflow run",
  meeting: "meeting",
  decision: "decision",
  self_fix: "self-fix",
  shepherd_escalation: "escalation",
  agent_thread: "thread",
};

/**
 * Build L: the console landed on a deep-linked object's OWN project (resolved server-side before
 * the fetch). Stamp `project_id=` onto the URL so every later poll and in-console link keeps it , 
 * a copy/pasted link that omitted the project now self-corrects and stays corrected, and tell
 * the operator their project switched so the different data on screen is not read as data loss.
 */
export function OwnerSwitchBanner({
  projectId,
  projectName,
  objectType,
}: {
  projectId: string;
  projectName: string | null;
  objectType: string;
}) {
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    // Rewrite the URL to carry the resolved project, without a history entry, so polls and the
    // back button keep it. Only when it is missing or wrong, so we never fight a real navigation.
    if (typeof window === "undefined") return;
    if (projectIdFromSearch(window.location.search) === projectId) return;
    const next = searchWithProject(window.location.search, projectId);
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${next}`);
  }, [projectId]);

  if (dismissed) return null;
  const label = OBJECT_LABEL[objectType] ?? "item";
  return (
    <div className="project-away" role="status" data-testid="owner-switch-banner">
      <span>
        Switched to <strong>{projectName ?? "this " + label + "’s project"}</strong>, the project
        this {label} belongs to.
      </span>
      <button type="button" className="bx-link-button" onClick={() => setDismissed(true)}>
        Dismiss
      </button>
    </div>
  );
}
