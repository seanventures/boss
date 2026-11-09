"use client";

import Link from "next/link";
// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope, see app/previews-ui.tsx.
import React from "react";

/**
 * A section that has not moved to v2 yet.
 *
 * NOT A 404, AND NOT A DEAD END.
 *
 * Six nav entries pointed at routes that did not exist, which is the same defect the operator
 * opened this effort with, a control that looks real and does nothing. The honest version of
 * a half-finished migration is a page that says exactly what is happening and keeps the
 * feature reachable, so nobody has to go looking for where it went.
 *
 * It names what will live here, so the page is not a shrug, and links to the v1 area that has
 * it TODAY, so the answer is one click away rather than a hunt.
 */

export function NotMovedYet({
  title,
  summary,
  v1Area,
  orgId,
}: {
  title: string;
  /** What this screen will show once it moves. Written from the parity map, so it matches
   *  what is actually promised rather than being invented here. */
  summary: string;
  v1Area: string;
  orgId?: string;
}) {
  const q = orgId ? `&org=${encodeURIComponent(orgId)}` : "";
  return (
    <section className="v2-screen" data-testid={`v2-not-moved-${v1Area}`}>
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">{title}</h1>
        <p className="v2-screen-sub">not moved to the new console yet</p>
      </header>

      <p className="v2-notmoved-what">{summary}</p>

      <p className="v2-notmoved-where">
        It works today in the current console, unchanged.{" "}
        <Link
          className="v2-meta-link"
          href={`/?area=${encodeURIComponent(v1Area)}${q}`}
          data-testid={`v2-not-moved-link-${v1Area}`}
        >
          Open it there
        </Link>
      </p>

      <p className="v2-notmoved-note">
        Nothing has been removed. This section is being rebuilt, and the v1 toggle at the top
        stays available until every screen has moved.
      </p>
    </section>
  );
}
