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
/** The open overlays, newest last: Esc and the focus trap belong to the top one only. */
const STACK: object[] = [];
let closedAt = 0;

/** True for a moment after an overlay closed: a page that closes itself on Esc (the agent
 *  Passport) must not take a second, quick Esc, or the closing one's echo, as its own. */
export function overlayJustClosed(within = 400): boolean {
  return STACK.length > 0 || Date.now() - closedAt < within;
}
/** Say an overlay (a popover menu, say) just closed: overlayJustClosed() is true for a moment. */
export function markOverlayClosed(): void { closedAt = Date.now(); }

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), '
  + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Tab and Shift+Tab stay inside `root` (a modal is modal for the keyboard too). */
export function trapTab(e: KeyboardEvent, root: HTMLElement | null): void {
  if (e.key !== "Tab" || !root) return;
  const items = [...root.querySelectorAll<HTMLElement>(FOCUSABLE)]
    .filter((el) => !el.closest("[hidden], [aria-hidden='true']"));
  if (!items.length) { e.preventDefault(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (!active || !root.contains(active)) { e.preventDefault(); first.focus(); return; }
  if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
}

function useOverlayFocus(ref: React.RefObject<HTMLElement | null>, onClose: () => void, where: unknown = null,
                         trap = true) {
  const opener = useRef<Element | null>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const me = useRef<object>({});
  useIsoLayoutEffect(() => {
    opener.current = typeof document !== "undefined" ? document.activeElement : null;
    STACK.push(me.current);
    return () => {
      const at = STACK.indexOf(me.current);
      if (at >= 0) STACK.splice(at, 1);
      closedAt = Date.now();
      const back = opener.current as HTMLElement | null;
      if (back && typeof back.focus === "function" && back.isConnected) back.focus({ preventScroll: true });
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps, once per open
  useIsoLayoutEffect(() => {
    const el = ref.current;
    const target = el?.querySelector<HTMLElement>("[data-autofocus]") ?? el?.querySelector<HTMLElement>("h3") ?? el;
    if (!target) return;
    if (!target.hasAttribute("tabindex") && !/^(BUTTON|INPUT|A|SELECT|TEXTAREA)$/.test(target.tagName)) {
      target.setAttribute("tabindex", "-1");
    }
    target.focus?.({ preventScroll: true });
  }, [where]); // eslint-disable-line react-hooks/exhaustive-deps, on open and on a move
  useEffect(() => {
    // Esc closes THIS overlay (the top one) and nothing under it: the agent Passport closes
    // itself on an Esc nobody handled, so an unmarked one took the whole page down with it.
    const key = (e: KeyboardEvent) => {
      if (STACK[STACK.length - 1] !== me.current) return;
      if (e.key === "Escape") {
        e.preventDefault();
        close.current();
        return;
      }
      if (trap) trapTab(e, ref.current);
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [trap]); // eslint-disable-line react-hooks/exhaustive-deps, ref is stable
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
  // A floating sheet is modal (trap Tab); a drawer beside the cards is not.
  useOverlayFocus(ref, onClose, floating, floating);
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
