"use client";
/**
 * START FROM AN IDEA, as a dialog (approved mockup Ideas.dc.html, 2026-10-13): filter tabs (All,
 * Starter apps, Team tools) and one card per idea with a drawing of the app's main screen.
 * Picking one fills the brief and the name; the operator edits both before Start.
 *
 * The list is `useIdeas()`'s: the catalogue's starters first, then the written briefs.
 */
import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { IdeaCover } from "../../../app/ask-boss-hero";
import type { IdeaItem } from "./catalogue";
import { IdeaPreview, hasIdeaPreview } from "./idea-previews";

export type IdeaFilter = "all" | "starter" | "team";
const FILTERS: Array<{ key: IdeaFilter; label: string }> = [
  { key: "all", label: "All" }, { key: "starter", label: "Starter apps" }, { key: "team", label: "Team tools" },
];

/** A starter has a reference implementation in the catalogue; a written brief is a team tool. */
export function ideaKind(idea: IdeaItem): Exclude<IdeaFilter, "all"> {
  return idea.card ? "starter" : "team";
}

/** The card's one line: the starter's summary, or the brief's first "must do". */
export function ideaLine(idea: IdeaItem): string {
  if (idea.card?.summary) return idea.card.summary.split(";")[0].trim();
  const must = idea.brief.split("\n").find((l) => l.startsWith("- "));
  return (must ?? idea.brief.split("\n")[1] ?? "").replace(/^- /, "").trim();
}

export const ideaTestId = (title: string) => `np-idea-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;

export function IdeasDialog({ ideas, onPick, onClose }: {
  ideas: IdeaItem[];
  onPick: (idea: IdeaItem) => void;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState<IdeaFilter>("all");
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    first.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); onClose(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  const shown = ideas.filter((i) => filter === "all" || ideaKind(i) === filter);

  const dialog = (
    <div className="np-fs np-ideas-fs" data-testid="np-ideas-dialog"
         onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="np-ideas-card" role="dialog" aria-modal="true" aria-labelledby="np-ideas-h">
        <div className="np-ideas-head">
          <div>
            <h2 id="np-ideas-h">Start from an idea</h2>
            <span className="np-dim" title="Picking one fills in the brief and the name. You can edit both before you start.">
              Fills the brief, ready to edit
            </span>
          </div>
          <button type="button" className="np-x" aria-label="Close" data-testid="np-ideas-close" onClick={onClose}>✕</button>
        </div>
        <div className="np-ideas-tabs" role="tablist" aria-label="Filter ideas">
          {FILTERS.map((f, i) => (
            <button key={f.key} ref={i === 0 ? first : undefined} type="button" role="tab"
                    aria-selected={filter === f.key} data-testid={`np-ideas-tab-${f.key}`}
                    onClick={() => setFilter(f.key)}>{f.label}</button>
          ))}
        </div>
        <div className="np-ideas-grid" data-testid="np-ideas">
          {shown.map((idea) => (
            <button key={idea.title} type="button" className="np-idea" data-testid={ideaTestId(idea.title)}
                    title={idea.brief.split("\n").slice(0, 2).join(" ")} onClick={() => onPick(idea)}>
              <span className="np-idea-art">
                {hasIdeaPreview(idea.title) ? <IdeaPreview title={idea.title} />
                  : <IdeaCover idea={idea} scope="np-idea" />}
              </span>
              <span className="np-idea-t">
                <small>{ideaKind(idea) === "starter" ? "Starter app" : "Team tool"}</small>
                <b>{idea.title}</b>
                <span>{ideaLine(idea)}</span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
  return typeof document === "undefined" ? dialog : createPortal(dialog, document.body);
}
