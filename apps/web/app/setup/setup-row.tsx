"use client";

/**
 * One row of the board: a label column (with the step number in first-run mode), a body, and a
 * right column for "Checked <age>" and the row's menu. Every row is this component, so the five
 * rows line up and share one focus/scroll behaviour.
 */
import React, { forwardRef, type ReactNode } from "react";
import { Check } from "@phosphor-icons/react";

import type { RowKey } from "../../lib/setup-types";
import { SectionLabel } from "../section-label";

const CIRCLED = ["①", "②", "③"];

export const SetupRow = forwardRef<HTMLElement, {
  row: RowKey;
  label: string;
  tip?: string;
  step?: number | null;
  done?: boolean;
  mode?: string;
  focused?: boolean;
  aside?: ReactNode;
  children: ReactNode;
}>(function SetupRow({ row, label, tip, step, done, mode, focused, aside, children }, ref) {
  return (
    <section ref={ref} className="su-row" data-testid={`su-row-${row}`} data-mode={mode}
             data-focused={focused || undefined} aria-label={label} id={`su-${row}`}>
      <div className="su-row-label">
        {step ? <span className="su-step" aria-hidden="true">{CIRCLED[step - 1]}</span> : null}
        {/* The shared green label (D-3), the same as the agent Passport's and the Kit's. */}
        <SectionLabel as="span" tip={tip}>{label}</SectionLabel>
        {done ? <Check className="su-done" size={14} weight="bold" aria-label="done" /> : null}
      </div>
      <div className="su-row-body">{children}</div>
      {aside ? <div className="su-row-aside">{aside}</div> : null}
    </section>
  );
});
