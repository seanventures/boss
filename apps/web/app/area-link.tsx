"use client";

import React, { type ReactNode } from "react";

import type { AreaId } from "../lib/area-ids";

// A link that switches console areas CLIENT-SIDE (instant, all area sections
// are already in the DOM, shown/hidden by CSS) instead of doing a Next
// navigation that re-runs the whole server page (~15 API calls). It keeps a real
// href so right-click / cmd-click / open-in-new-tab still work, and dispatches a
// `boss:navigate` event that ConsoleShell listens for to flip the active area
// and sync the URL.
export function AreaLink({
  area,
  orgId,
  className,
  children,
  title,
}: {
  // AreaId, not string: a dead target is a compile error (see lib/area-ids.ts for the two that
  // did nothing when clicked); tests/every-area-link-goes-somewhere.test.ts pins the hrefs.
  area: AreaId;
  orgId?: string;
  className?: string;
  children: ReactNode;
  title?: string;
}) {
  const href = `/?${orgId ? `org=${encodeURIComponent(orgId)}&` : ""}area=${area}`;
  return (
    <a
      className={className}
      href={href}
      title={title}
      onClick={(e) => {
        // let modified clicks (new tab / window) behave natively
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("boss:navigate", { detail: area }));
      }}
    >
      {children}
    </a>
  );
}
