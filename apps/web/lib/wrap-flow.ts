"use client";

/**
 * NO ARROW DANGLES AT A LINE WRAP (chip-flow diagrams on a phone: the Needs-you mini flow, the
 * ship path, Helm's plan, an answer's path, a card's plan strip).
 *
 * A flow is a row of steps; each step after the first is ONE unbreakable unit — its wire (the
 * arrow into it) plus the node — so a wrap can never leave an arrow at the end of a line. The
 * step that starts a new line is marked `data-wrap` and its wire is hidden, so the second line
 * starts with a node, not an arrow into nothing. Measured after layout and again on resize.
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
    const RO = (window as unknown as { ResizeObserver?: typeof ResizeObserver }).ResizeObserver;
    if (!RO) return undefined;
    const ro = new RO(() => mark());
    ro.observe(box);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}
