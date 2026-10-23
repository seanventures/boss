"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React, { useState } from "react";

import { LaunchModal } from "./LaunchModal";

/**
 * The top bar's primary call to action.
 *
 * A button rather than a link, because this opens a dialogue rather than navigating. It used
 * to be a Link to /v2/new — a route that did not exist, so the console's most prominent
 * control 404ed.
 */
export function LaunchButton({ projectId, orgId }: { projectId: string; orgId?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="v2-primary"
        onClick={() => setOpen(true)}
        data-testid="v2-new-project"
      >
        New build
      </button>
      {open ? (
        <LaunchModal
          projectId={projectId}
          actor="executive_console"
          orgId={orgId}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}
