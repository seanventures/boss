"use client";

/**
 * Skills & MCP's overlays: the drawer and the modals, in one place (K3, K11, K23).
 *
 * PORTALLED TO <body> WHEN THEY FLOAT. A `position: fixed` drawer inside the console's panel was
 * not fixed to the screen: an ancestor with a backdrop-filter becomes the containing block, so
 * on a phone the "bottom sheet" rendered ~2000px down, under every card, and tapping a card
 * showed nothing. A portal puts the sheet (and every modal) where `fixed` means the viewport.
 * On a wide screen the drawer is a column beside the cards and stays in place.
 *
 * FOCUS GOES IN AND COMES BACK. Opening moves focus to the overlay's heading; Esc closes it;
 * closing returns focus to whatever opened it (the card, the button).
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** The width under which the drawer floats instead of sitting beside the cards (kit.css). */
export const FLOAT_QUERY = "(max-width: 1100px)";

const useIsoLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

/** True under `query`, read after mount (the server renders the wide layout). */
export function useMedia(query: string): boolean {
  const [hit, setHit] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const m = window.matchMedia(query);
    const on = () => setHit(m.matches);
    on();
    m.addEventListener?.("change", on);
    return () => m.removeEventListener?.("change", on);
  }, [query]);
  return hit;
}

/** Focus the overlay's heading on open, Esc to close, focus back to the opener on close.
 *  `where` changes when the overlay moves (in place → portalled): focus follows it there. */
function useOverlayFocus(ref: React.RefObject<HTMLElement | null>, onClose: () => void, where: unknown = null) {
  const opener = useRef<Element | null>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useIsoLayoutEffect(() => {
    opener.current = typeof document !== "undefined" ? document.activeElement : null;
    return () => {
      const back = opener.current as HTMLElement | null;
      if (back && typeof back.focus === "function" && back.isConnected) back.focus({ preventScroll: true });
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- once per open
  useIsoLayoutEffect(() => {
    const el = ref.current;
    const target = el?.querySelector<HTMLElement>("[data-autofocus]") ?? el?.querySelector<HTMLElement>("h3") ?? el;
    if (!target) return;
    if (!target.hasAttribute("tabindex") && !/^(BUTTON|INPUT|A|SELECT|TEXTAREA)$/.test(target.tagName)) {
      target.setAttribute("tabindex", "-1");
    }
    target.focus?.({ preventScroll: true });
  }, [where]); // eslint-disable-line react-hooks/exhaustive-deps -- on open and on a move
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close.current(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, []);
}

/** A drawer: a column beside the content when wide, a bottom/side sheet over it when not.
 *  `float`: always a sheet (the agent Passport has no column to sit in). `over`: above a
 *  full-screen page (`.tpl-page`, z 200), which the Kit's own layers sit under. */
export function DrawerShell({ label, testId, onClose, children, float, over }: {
  label: string; testId: string; onClose: () => void; children: React.ReactNode;
  float?: boolean; over?: boolean;
}) {
  const narrow = useMedia(FLOAT_QUERY);
  const floating = narrow || !!float;
  const ref = useRef<HTMLElement>(null);
  useOverlayFocus(ref, onClose, floating);
  const aside = (
    <aside ref={ref} className="kit-drawer" role="dialog" aria-label={label} aria-modal={floating || undefined}
           data-floating={floating || undefined} data-testid={testId}>
      {children}
    </aside>
  );
  if (!floating || typeof document === "undefined") return aside;
  return createPortal(
    <div className="kit-sheet-layer" data-testid={`${testId}-layer`} data-over={over ? "page" : undefined}>
      <div className="kit-modal-scrim" onClick={onClose} />
      {aside}
    </div>,
    document.body,
  );
}

/** A modal: always over the page (portalled), Esc closes it, focus returns to its opener. */
export function ModalShell({ label, testId, onClose, wide, children, over }: {
  label: string; testId: string; onClose: () => void; wide?: boolean; children: React.ReactNode;
  /** Above a full-screen page (the agent Passport), as DrawerShell's `over`. */
  over?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useOverlayFocus(ref, onClose);
  const modal = (
    <div className="kit-modal" role="dialog" aria-modal="true" aria-label={label} data-testid={testId}
         data-over={over ? "page" : undefined}>
      <div className="kit-modal-scrim" onClick={onClose} />
      <div ref={ref} className={`kit-modal-card${wide ? " kit-modal-wide" : ""}`}>{children}</div>
    </div>
  );
  return typeof document === "undefined" ? modal : createPortal(modal, document.body);
}
