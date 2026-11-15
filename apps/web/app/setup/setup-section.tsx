"use client";

/**
 * ONE TOPIC, ONE FOLD (operator, 2026-11-15: "The page is growing big, so we need to collapse
 * based on the topic - like repos, models, ticket system"). Setup's board is a stack of these:
 * Repos, Models, Ticket systems, Events. Folded, a section is its name and one line of state
 * ("Jira · shared · 2 projects mapped"); open, it is the block it always was.
 *
 * Open or closed is remembered per viewer (this browser), never shared and never required: a
 * storage that throws or comes back empty just means the defaults. A section that needs
 * attention (nothing connected, a failing check) or that a link points at (`focus=`) opens
 * regardless, without overwriting what the viewer chose.
 *
 * The body stays mounted while folded (`hidden`), so a self-loading block (Ticket systems) still
 * reads its state for the summary line.
 */
import React, { useEffect, useId, useState } from "react";
import { CaretDown } from "@phosphor-icons/react";

export const SECTIONS_KEY = "boss.setup.sections.v1";
export type SectionId = "repos" | "models" | "ticket_systems" | "events";

export function readOpen(): Partial<Record<SectionId, boolean>> {
  try {
    const raw = window.localStorage.getItem(SECTIONS_KEY);
    const v = raw ? JSON.parse(raw) : {};
    return v && typeof v === "object" ? v : {};
  } catch { return {}; }
}
function writeOpen(id: SectionId, open: boolean): void {
  try { window.localStorage.setItem(SECTIONS_KEY, JSON.stringify({ ...readOpen(), [id]: open })); } catch { /* per-viewer nicety only */ }
}

export function SetupSection({ id, title, icon, summary, tone, attention = false, focused = false, defaultOpen = false, children }: {
  id: SectionId; title: string; icon: React.ReactNode; summary: string; tone?: "green" | "amber" | "red" | "grey";
  /** Needs the viewer (nothing connected, a failing check): opens whatever was remembered. */
  attention?: boolean;
  /** A link points here (`focus=`): opens and stays open. */
  focused?: boolean;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState<boolean>(() => {
    if (focused || attention) return true;
    const saved = typeof window === "undefined" ? undefined : readOpen()[id];
    return saved ?? defaultOpen;
  });
  useEffect(() => { if (focused) setOpen(true); }, [focused]);
  const bodyId = useId();
  const toggle = () => setOpen((was) => { writeOpen(id, !was); return !was; });
  return (
    <section className="su-sec" data-testid={`su-sec-${id}`} data-open={open || undefined} aria-label={title}>
      <button type="button" className="su-sec-head" aria-expanded={open} aria-controls={bodyId} onClick={toggle}
              data-testid={`su-sec-${id}-toggle`} title={open ? `Fold ${title}` : `Open ${title}`}>
        <span className="su-sec-icon" aria-hidden>{icon}</span>
        <h2 className="su-sec-title">{title}</h2>
        <span className="su-sec-sum" data-tone={tone} data-testid={`su-sec-${id}-summary`}>{summary}</span>
        <CaretDown size={14} aria-hidden className="su-sec-caret" />
      </button>
      <div className="su-sec-body" id={bodyId} hidden={!open}>{children}</div>
    </section>
  );
}
