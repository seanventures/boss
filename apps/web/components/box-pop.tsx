"use client";
/**
 * THE BOX POPOVER, SHOWN WHOLE (walk 2026-11-08: Add a project's CODE popover opened under the
 * sheet's footer and the sheet did not scroll to it — "when I click on something it does not
 * auto expand").
 *
 * Every popover a box or chip opens (Add a project, Add a server, Watch this app) calls
 * `useBoxPop` on its root. When it opens, and whenever it or the window changes size:
 *
 *   1. it is fitted to the part of the window it can actually be seen in — the viewport cut by
 *      every ancestor that clips (the sheet's scroll body ends at the footer, so the footer is
 *      outside that part): never taller or wider than that part, its own content scrolling;
 *   2. a floating popover (position absolute) opens on the side of its box that has room in
 *      view — down or up — and is slid sideways to stay inside;
 *   3. whatever still lies outside is scrolled into view, innermost scroller first;
 *   4. focus moves into it; Esc closes it (only it, never the sheet under it); on close focus
 *      goes back to the box that opened it.
 *
 * `popOutOfView` is the same measure the e2e guard asserts, so the rule and its test agree.
 */
import React, { useLayoutEffect, useRef } from "react";

/** Breathing room between a popover and the edge of what can be seen. */
export const POP_GAP = 8;

type Region = { top: number; bottom: number; left: number; right: number };

function clips(el: Element): boolean {
  const s = getComputedStyle(el);
  return s.overflowX !== "visible" || s.overflowY !== "visible";
}

function scrollsY(el: Element): boolean {
  const s = getComputedStyle(el);
  return (s.overflowY === "auto" || s.overflowY === "scroll") && el.scrollHeight > el.clientHeight + 1;
}

/** The part of the window `el` can be seen in: the viewport, cut by every clipping ancestor's
 *  client box (so a scroll body that stops at a footer leaves the footer out). */
export function visibleRegion(el: HTMLElement): Region {
  const doc = document.documentElement;
  const out: Region = { top: 0, left: 0, bottom: window.innerHeight, right: doc.clientWidth || window.innerWidth };
  for (let a = el.parentElement; a && a !== document.body && a !== doc; a = a.parentElement) {
    if (!clips(a)) continue;
    const r = a.getBoundingClientRect();
    const top = r.top + a.clientTop;
    const left = r.left + a.clientLeft;
    out.top = Math.max(out.top, top);
    out.left = Math.max(out.left, left);
    out.bottom = Math.min(out.bottom, top + a.clientHeight);
    out.right = Math.min(out.right, left + a.clientWidth);
  }
  return out;
}

/** How far `el` lies outside what can be seen (0 when it is whole in view). */
export function popOutOfView(el: HTMLElement, gap = 0): number {
  const v = visibleRegion(el);
  const r = el.getBoundingClientRect();
  return Math.max(0, v.top + gap - r.top) + Math.max(0, r.bottom - (v.bottom - gap))
       + Math.max(0, v.left + gap - r.left) + Math.max(0, r.right - (v.right - gap));
}

/** Fit, place and scroll one open popover (steps 1–3 above). */
export function revealPop(pop: HTMLElement, fit = true): void {
  const st = pop.style;
  st.removeProperty("translate");
  if (fit) { st.removeProperty("max-height"); st.removeProperty("max-width"); }
  const v = visibleRegion(pop);
  if (fit) {
    st.maxHeight = `${Math.max(140, Math.floor(v.bottom - v.top - 2 * POP_GAP))}px`;
    st.maxWidth = `${Math.max(200, Math.floor(v.right - v.left - 2 * POP_GAP))}px`;
    st.overflowY = "auto";
  }
  const floating = getComputedStyle(pop).position === "absolute";
  if (!floating) { scrollIntoSight(pop); return; }
  // Down or up: the side of its box where, scrolled, it is whole in view; the CSS default first.
  if (!pop.dataset.cssPlace) {
    const box = pop.offsetParent?.getBoundingClientRect();
    pop.dataset.cssPlace = box && pop.getBoundingClientRect().bottom <= box.top + 1 ? "up" : "down";
  }
  const first = (pop.dataset.place || pop.dataset.cssPlace) as "down" | "up";
  const order: ("down" | "up")[] = first === "up" ? ["up", "down"] : ["down", "up"];
  for (const place of order) {
    Object.assign(st, place === "down" ? { top: "calc(100% + 8px)", bottom: "auto" } : { top: "auto", bottom: "calc(100% + 8px)" });
    pop.dataset.place = place;
    slideInside(pop, v);
    scrollIntoSight(pop);
    if (popOutOfView(pop, POP_GAP - 1) < 1) return;
  }
  // Neither side fits whole: down, scrolled as far as it goes (below can always be scrolled to).
  Object.assign(st, { top: "calc(100% + 8px)", bottom: "auto" });
  pop.dataset.place = "down";
  slideInside(pop, v);
  scrollIntoSight(pop);
}

/** Sideways: slide a floating popover back inside what can be seen. */
function slideInside(pop: HTMLElement, v: Region): void {
  pop.style.removeProperty("translate");
  const r = pop.getBoundingClientRect();
  let dx = 0;
  if (r.right > v.right - POP_GAP) dx = v.right - POP_GAP - r.right;
  if (r.left + dx < v.left + POP_GAP) dx = v.left + POP_GAP - r.left;
  if (dx) pop.style.translate = `${Math.round(dx)}px 0`;
}

/** Scroll what is still outside into view, innermost scroller first, then the window. */
function scrollIntoSight(pop: HTMLElement): void {
  for (let a = pop.parentElement; a && a !== document.body; a = a.parentElement) {
    if (!scrollsY(a)) continue;
    const box = a.getBoundingClientRect();
    const top = box.top + a.clientTop + POP_GAP;
    const bottom = box.top + a.clientTop + a.clientHeight - POP_GAP;
    const r = pop.getBoundingClientRect();
    let dy = 0;
    if (r.bottom > bottom) dy = r.bottom - bottom;
    if (r.top - dy < top) dy = r.top - top;
    if (Math.abs(dy) >= 1) a.scrollTop += dy;
  }
  const r = pop.getBoundingClientRect();
  if (r.bottom > window.innerHeight - POP_GAP || r.top < POP_GAP) {
    window.scrollBy({ top: r.bottom > window.innerHeight - POP_GAP ? Math.min(r.bottom - window.innerHeight + POP_GAP, r.top - POP_GAP) : r.top - POP_GAP });
  }
}

const FOCUSABLE = "[data-autofocus], button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex='0']";

/** The box that opened `pop`: the focused control outside it, else the expanded box beside it. */
function openerOf(pop: HTMLElement): HTMLElement | null {
  const at = document.activeElement as HTMLElement | null;
  if (at && at !== document.body && !pop.contains(at)) return at;
  for (let n = pop.parentElement, i = 0; n && i < 4; n = n.parentElement, i++) {
    const hit = [...n.querySelectorAll<HTMLElement>("[aria-expanded='true']")].find((e) => !pop.contains(e));
    if (hit) return hit;
  }
  return null;
}

export function useBoxPop<T extends HTMLElement>(ref: React.RefObject<T | null>, opts: {
  /** Esc closes the popover (and only it). Leave out where the popover handles its own keys. */
  onEscape?: () => void;
  /** Move focus into it on open (default true). */
  focus?: boolean;
  /** Size it to what can be seen (default true); false only scrolls it into view (an inline edit). */
  fit?: boolean;
} = {}): void {
  const esc = useRef(opts.onEscape);
  esc.current = opts.onEscape;
  const focus = opts.focus ?? true;
  const fit = opts.fit ?? true;
  useLayoutEffect(() => {
    const pop = ref.current;
    if (!pop) return undefined;
    const opener = openerOf(pop);
    let raf = 0;
    const again = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { if (pop.isConnected) revealPop(pop, fit); }); };
    revealPop(pop, fit);
    // Again once its open animation ends: a moving box measures where it is not staying.
    for (const a of pop.getAnimations?.() ?? []) a.finished.then(again, () => undefined);
    if (focus && !pop.contains(document.activeElement)) pop.querySelector<HTMLElement>(FOCUSABLE)?.focus({ preventScroll: true });
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(again) : null;
    ro?.observe(pop);
    // What it scrolls in can grow under it too (a read landing above it): keep it whole then.
    // A part that appears above it (a new direct child) is caught too.
    const mo = typeof MutationObserver !== "undefined" ? new MutationObserver((recs) => {
      for (const r of recs) for (const n of r.addedNodes) if (n instanceof Element) ro?.observe(n);
      again();
    }) : null;
    for (let a = pop.parentElement; a && a !== document.body; a = a.parentElement) {
      const oy = getComputedStyle(a).overflowY;
      if (oy !== "auto" && oy !== "scroll") continue;
      for (const c of a.children) ro?.observe(c);
      mo?.observe(a, { childList: true });
    }
    window.addEventListener("resize", again);
    // On the window, capturing: ahead of a sheet's own Esc (on the document), which would close
    // the whole sheet.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || !esc.current) return;
      e.preventDefault(); e.stopPropagation();
      esc.current();
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      cancelAnimationFrame(raf);
      ro?.disconnect();
      mo?.disconnect();
      window.removeEventListener("resize", again);
      window.removeEventListener("keydown", onKey, true);
      const at = document.activeElement;
      if (opener?.isConnected && (!at || at === document.body || pop.contains(at))) opener.focus({ preventScroll: true });
    };
  }, [ref, focus, fit]);
}

/** A popover root with `useBoxPop` on it, for markup that has no component of its own. */
export function BoxPop({ as = "div", onEscape, focus, fit, children, ...rest }: React.HTMLAttributes<HTMLElement> & {
  /** "span" where the popover sits inside inline markup. */
  as?: "div" | "span";
  onEscape?: () => void; focus?: boolean; fit?: boolean; "data-testid"?: string; [data: `data-${string}`]: unknown;
}) {
  const ref = useRef<HTMLElement>(null);
  useBoxPop(ref, { onEscape, focus, fit });
  const Tag = as;
  return <Tag ref={ref as React.RefObject<never>} {...rest}>{children}</Tag>;
}
