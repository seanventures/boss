import React from "react";

/**
 * One icon per nav item.
 *
 * Inline SVG rather than an icon font or a package: the console has a strict budget and a whole
 * icon library for nine glyphs is a lot of bytes for nine glyphs. `currentColor` throughout, so
 * an icon dims and brightens with its label instead of needing a second set of active rules.
 *
 * Each one is chosen to say what the screen IS, not to decorate it: Hive is a network of nodes,
 * Workforce is people, Decisions is a gavel-ish check, Receipts is a document.
 */

const PATHS: Record<string, React.ReactNode> = {
  // Projects — stacked layers of work.
  missions: <><path d="M2 4.5 8 1.5l6 3-6 3-6-3Z" /><path d="M2 8l6 3 6-3" /><path d="M2 11.5l6 3 6-3" /></>,
  // Hive — a hub with satellites, which is exactly what the screen draws.
  hive: <><circle cx="8" cy="8" r="2.2" /><circle cx="8" cy="2" r="1.3" /><circle cx="13" cy="11" r="1.3" /><circle cx="3" cy="11" r="1.3" /><path d="M8 5.8V3.3M9.8 9.2l2 1.2M6.2 9.2l-2 1.2" /></>,
  // Workforce — a group of agents.
  factory: <><circle cx="5.5" cy="5" r="2" /><circle cx="11" cy="6" r="1.6" /><path d="M1.5 13c0-2.2 1.8-3.6 4-3.6s4 1.4 4 3.6" /><path d="M10.5 9.6c2 0 3.5 1.3 3.5 3.4" /></>,
  // Org Chart — a tree, one box over two.
  org: <><rect x="5.5" y="1.5" width="5" height="3" rx="1" /><rect x="1" y="10.5" width="4.5" height="3" rx="1" /><rect x="10.5" y="10.5" width="4.5" height="3" rx="1" /><path d="M8 4.5v3M3.2 10.5v-1.5h9.6v1.5" /></>,
  // Decisions — a judgement rendered.
  panels: <><path d="M2 3.5h12" /><path d="M3.5 3.5v9a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1v-9" /><path d="M6 7l1.6 1.7L10.5 6" /></>,
  // Workflows — a branching process.
  boss: <><circle cx="3.5" cy="8" r="1.6" /><circle cx="12.5" cy="4" r="1.6" /><circle cx="12.5" cy="12" r="1.6" /><path d="M5.1 8h2.4a2 2 0 0 0 2-2V5.4M5.1 8h2.4a2 2 0 0 1 2 2v.6" /><path d="M9.5 4.6h1.5M9.5 11.4h1.5" /></>,
  // Receipts — the record.
  timeline: <><path d="M3.5 1.5h9v13l-2-1.2-2 1.2-2-1.2-2 1.2v-13Z" /><path d="M6 5h4M6 8h4M6 11h2.5" /></>,
  // Infra & Models — machines.
  infra: <><rect x="1.5" y="2.5" width="13" height="4.5" rx="1" /><rect x="1.5" y="9" width="13" height="4.5" rx="1" /><path d="M4 4.75h.01M4 11.25h.01" /></>,
  // Settings.
  settings: <><circle cx="8" cy="8" r="2.2" /><path d="M8 1.5v1.8M8 12.7v1.8M14.5 8h-1.8M3.3 8H1.5M12.6 3.4l-1.3 1.3M4.7 11.3l-1.3 1.3M12.6 12.6l-1.3-1.3M4.7 4.7 3.4 3.4" /></>,
};

export function NavIcon({ id }: { id: string }) {
  const d = PATHS[id];
  // No placeholder box for an unknown id: an empty square beside a label reads as a broken
  // image, and the label alone reads as a nav item without an icon, which is what it is.
  if (!d) return null;
  return (
    <svg
      className="v2-nav-icon"
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      data-testid={`v2-nav-icon-${id}`}
    >
      {d}
    </svg>
  );
}
