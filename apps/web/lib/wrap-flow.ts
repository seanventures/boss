"use client";

/**
 * NO ARROW DANGLES AT A LINE WRAP (chip-flow diagrams on a phone: the Needs-you mini flow, the
 * ship path, Helm's plan, an answer's path, a card's plan strip).
 *
 * A flow is a row of steps; each step after the first is ONE unbreakable unit — its wire (the
 * arrow into it) plus the node — so a wrap can never leave an arrow at the end of a line. The
 * step that starts a new line is marked `data-wrap` and its wire is hidden, so the second line
 * starts with a node, not an arrow into nothing. The wire keeps its width (visibility, not
 * display): hiding it by removing it would pull the step back up a line and push the next one
 * down with its arrow showing. Measured after layout, after fonts and on resize.
 */
import { useLayoutEffect, useRef } from "react";

const useIsoLayoutEffect = typeof window === "undefined" ? () => undefined : useLayoutEffect;

/** Marks each `[data-flow-step]` child that begins a new line. Returns the container's ref. */
export function useWrapMarks<T extends HTMLElement>(deps: unknown[] = []) {
  const ref = useRef<T | null>(null);
  useIsoLayoutEffect(() => {
    const box = ref.current;
    if (!box) return undefined;
    const mark = () => {
      const steps = Array.from(box.querySelectorAll<HTMLElement>(":scope > [data-flow-step]"));
      steps.forEach((el) => el.removeAttribute("data-wrap"));
      let top: number | null = null;
      for (const el of steps) {
        const t = el.offsetTop;
        if (top !== null && t > top + 2) el.setAttribute("data-wrap", "1");
        top = t;
      }
    };
    mark();
    // Lines also move when fonts land or a drawer finishes sliding in, without the box resizing:
    // mark again then too.
    const later = window.setTimeout(mark, 320);
    const frame = window.requestAnimationFrame?.(mark);
    try { void (document as unknown as { fonts?: { ready: Promise<unknown> } }).fonts?.ready.then(mark); } catch { /* no font API */ }
    const RO = (window as unknown as { ResizeObserver?: typeof ResizeObserver }).ResizeObserver;
    if (!RO) return () => window.clearTimeout(later);
    const ro = new RO(() => mark());
    ro.observe(box);
    if (box.parentElement) ro.observe(box.parentElement);
    return () => { ro.disconnect(); window.clearTimeout(later); if (frame) window.cancelAnimationFrame?.(frame); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}
