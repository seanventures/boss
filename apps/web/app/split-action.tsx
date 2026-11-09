"use client";

/**
 * One button that does the usual thing, with the rest behind a caret.
 *
 * The composer had three peers, Ask, Research, Build →, sitting inside the same flex row as
 * the textarea, the workflow picker and the agent picker. Six things competing for one line, so
 * the textarea collapsed to half its width and the controls marched right off toward the edge.
 * Three buttons of equal weight also say the three actions are equally likely, and they are not:
 * asking is free and constant, building spends money and happens once.
 *
 * WHY "ASK" IS THE FACE AND NOT "BUILD". Enter sends, which is the one convention a composer
 * cannot break, and Enter fires whatever the primary button is. A face reading "Build" would
 * mean a stray Enter starts a governed pipeline that spends money, which is the opposite of
 * every instruction this console has been given. Build is one click away in the menu, labelled
 * with what it costs. Flipping the face is a one-line change if that trade is wanted.
 */

// Imported by name so this component can be RENDERED in a test rather than only grepped , 
// tsconfig has jsx:preserve, so a file without React in scope cannot be mounted by vitest.
// A button that ran the wrong action for a day is precisely what source-reading cannot catch.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type Action = {
  id: string;
  label: string;
  /** What pressing it does, in a line. The menu is where somebody learns the difference. */
  hint: string;
  onRun: () => void;
  /** Shown on the item, "spends money", "free". Never buried in a tooltip. */
  cost?: string;
  /** The viewer lacks the permission this needs (components/gated.tsx useGate().reason): the
   *  control stays visible, reads as disabled, says why, and does nothing when pressed. */
  refused?: string;
};

export function SplitAction({ primary: given, others, disabled, busy }: {
  primary: Action;
  others: Action[];
  disabled?: boolean;
  busy?: boolean;
}) {
  // A refused primary stays on its face, says why, and its press does nothing.
  const primary: Action = given.refused ? { ...given, onRun: () => undefined } : given;
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  //: WHERE THE MENU IS ON THE SCREEN, not where it is in the tree.
  //:
  //: The composer box carries `backdrop-filter: blur(6px)`, which creates a stacking context.
  //: So `z-index: 30` on the menu only ever ranked it against its own siblings INSIDE that box,
  //: and the reply block that comes after the box painted straight over it: two sets of text
  //: overlapping, the options unreadable. z-index cannot fix that from the inside, the menu
  //: has to leave the box, which is the same trap the template page hit.
  const [at, setAt] = useState<{ top: number; right: number; up: boolean } | null>(null);

  // Measure before paint, so the menu never appears in the wrong place for a frame.
  useLayoutEffect(() => {
    if (!open) { setAt(null); return; }
    const place = () => {
      const box = wrap.current?.getBoundingClientRect();
      if (!box) return;
      // BELOW IF IT FITS, ABOVE IF IT DOES NOT. Neither is right unconditionally: the composer
      // sits low on the Command Center and high inside the dock.
      const room = window.innerHeight - box.bottom;
      const up = room < 260 && box.top > room;
      setAt({
        top: up ? box.top - 8 : box.bottom + 8,
        right: Math.max(8, window.innerWidth - box.right),
        up,
      });
    };
    place();
    // A fixed menu does not travel with the page, so it is repositioned rather than left
    // hanging over unrelated content.
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  // Click-away and Escape. A menu that only closes by choosing something traps the pointer.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      // The menu is no longer inside `wrap`, it is portalled, so it has to be asked too, or
      // mousedown closes it and the click never reaches the item.
      if (wrap.current?.contains(target) || menu.current?.contains(target)) return;
      setOpen(false);
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
        // NOT type="submit". THE VISIBLE BUTTON RAN A DIFFERENT ACTION THAN ITS LABEL.
        //
        // As a submit button this never called `primary.onRun`, it relied on the enclosing
        // form's onSubmit, which was still wired to Ask from when Ask was the primary. So
        // pressing "Build →" ran a chat turn: forty seconds of a model answering, a reply full
        // of buttons, and no workflow run anywhere. The component declared an onRun and never
        // called it, which is a trap for every future caller too.
        type="button"
        disabled={disabled}
        aria-disabled={primary.refused ? true : undefined}
        data-testid={`split-${primary.id}`}
        title={primary.refused || primary.hint}
        onClick={primary.onRun}
      >
        {busy ? <span className="askboss-spinner" aria-hidden /> : primary.label}
        {primary.refused ? <span className="sr-only">{primary.refused}</span> : null}
      </button>
      {others.length ? <button
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
      </button> : null}
      {open && at && typeof document !== "undefined" ? createPortal(
        <div className="split-action-menu" role="menu" data-testid="split-menu" ref={menu}
             style={{ top: at.top, right: at.right,
                      transform: at.up ? "translateY(-100%)" : undefined }}>
          {others.map((a) => (
            <button
              key={a.id}
              type="button"
              role="menuitem"
              className="split-action-item"
              data-testid={`split-${a.id}`}
              disabled={disabled}
              aria-disabled={a.refused ? true : undefined}
              title={a.refused || undefined}
              onClick={() => { if (a.refused) return; setOpen(false); a.onRun(); }}
            >
              <span className="split-action-item-head">
                {a.label}
                {/* WHAT IT COSTS, on the item. A menu that hides the expensive option among the
                    free ones is how somebody spends money by accident. */}
                {a.cost ? <em className="split-action-cost">{a.cost}</em> : null}
              </span>
              <span className="split-action-item-hint">{a.refused || a.hint}</span>
            </button>
          ))}
        </div>, document.body) : null}
    </div>
  );
}
