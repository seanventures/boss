/**
 * THE SECTION LABEL: Setup's green `MODEL` label, shared. Setup's rows, the agent Passport and
 * the Kit use this one component and one CSS rule (`.section-label` in globals.css, beside
 * `.section-head h2.eyebrow`), so the pages cannot drift apart.
 *
 * Level 2 (the default) is the green label: teal, 12px, bold, uppercase, wide tracking.
 * Level 1 is a section title in ink.
 */
import React, { type ReactNode } from "react";

export function SectionLabel({ children, tip, level = 2, as = "h3", id, testid }: {
  children: ReactNode; tip?: string; level?: 1 | 2; as?: "h2" | "h3" | "h4" | "span";
  id?: string; testid?: string;
}) {
  const Tag = as;
  return (
    <Tag className="section-label" data-level={level} id={id} data-testid={testid}
         title={tip} data-tip={tip}>
      {children}
    </Tag>
  );
}
