"use client";

/**
 * One button that does the usual thing, with the rest behind a caret.
 *
 * The composer had three peers — Ask, Research, Build → — sitting inside the same flex row as
 * the textarea, the workflow picker and the agent picker. Six things competing for one line, so
 * the textarea collapsed to half its width and the controls marched right off toward the edge.
 * Three buttons of equal weight also say the three actions are equally likely, and they are not:
 * asking is free and constant, building spends money and happens once.
 *
 * WHY "ASK" IS THE FACE AND NOT "BUILD". Enter sends, which is the one convention a composer
 * cannot break — and Enter fires whatever the primary button is. A face reading "Build" would
 * mean a stray Enter starts a governed pipeline that spends money, which is the opposite of
 * every instruction this console has been given. Build is one click away in the menu, labelled
 * with what it costs. Flipping the face is a one-line change if that trade is wanted.
 */

import { useEffect, useRef, useState } from "react";

export type Action = {
  id: string;
  label: string;
  /** What pressing it does, in a line. The menu is where somebody learns the difference. */
  hint: string;
  onRun: () => void;
  /** Shown on the item — "spends money", "free". Never buried in a tooltip. */
  cost?: string;
};

export function SplitAction({ primary, others, disabled, busy }: {
  primary: Action;
  others: Action[];
  disabled?: boolean;
  busy?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  // Click-away and Escape. A menu that only closes by choosing something traps the pointer.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="split-action" ref={wrap} data-testid="split-action">
      <button
        className="split-action-main"
        type="submit"
        disabled={disabled}
        data-testid={`split-${primary.id}`}
        title={primary.hint}
      >
        {busy ? <span className="askboss-spinner" aria-hidden /> : primary.label}
      </button>
      <button
        className="split-action-caret"
        type="button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Other things to do with this"
        data-testid="split-more"
        onClick={() => setOpen((o) => !o)}
      >
        ▾
      </button>
      {open ? (
        <div className="split-action-menu" role="menu" data-testid="split-menu">
          {others.map((a) => (
            <button
              key={a.id}
              type="button"
              role="menuitem"
              className="split-action-item"
              data-testid={`split-${a.id}`}
              disabled={disabled}
              onClick={() => { setOpen(false); a.onRun(); }}
            >
              <span className="split-action-item-head">
                {a.label}
                {/* WHAT IT COSTS, on the item. A menu that hides the expensive option among the
                    free ones is how somebody spends money by accident. */}
                {a.cost ? <em className="split-action-cost">{a.cost}</em> : null}
              </span>
              <span className="split-action-item-hint">{a.hint}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
