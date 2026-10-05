/**
 * ONE TAB STOP PER LIST (independent test D15). Needs you's cards and the RUNNING lanes each
 * held a dozen tab stops (every face, button and link), so reaching Systems or This week from
 * the keyboard took ~80 presses. A list is now one stop: Tab lands on its current item, ↑/↓
 * (and J/K) move between items, Enter (or F2) steps inside the item to its controls, Esc
 * steps back out. The roving pattern (WAI-ARIA APG), written against the DOM so the shared
 * AttentionCard and MissionRow need no change.
 */
import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE = "a[href], button, input, select, textarea, summary, [tabindex]";

function inner(item: HTMLElement): HTMLElement[] {
  return [...item.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el !== item);
}

/** Apply the roving tab order to the items (`selector`) inside `ref`. Pure DOM; re-run on render. */
export function applyRoving(root: HTMLElement, selector: string, current: number, entered: number | null): HTMLElement[] {
  const items = [...root.querySelectorAll<HTMLElement>(selector)];
  items.forEach((item, i) => {
    item.tabIndex = i === Math.min(current, items.length - 1) ? 0 : -1;
    for (const el of inner(item)) {
      if (el.dataset.rovingTab === undefined) el.dataset.rovingTab = el.getAttribute("tabindex") ?? "";
      if (entered === i) {
        const was = el.dataset.rovingTab;
        if (was) el.setAttribute("tabindex", was); else el.removeAttribute("tabindex");
      } else {
        el.tabIndex = -1;
      }
    }
  });
  return items;
}

export function useRoving(ref: RefObject<HTMLElement | null>, selector: string): void {
  const current = useRef(0);
  const entered = useRef<number | null>(null);
  // Every render: new cards or lanes arrive with their own tab stops.
  useEffect(() => {
    const root = ref.current;
    if (root) applyRoving(root, selector, current.current, entered.current);
  });
  useEffect(() => {
    const root = ref.current;
    if (!root) return undefined;
    const itemOf = (el: EventTarget | null) => (el instanceof HTMLElement ? el.closest<HTMLElement>(selector) : null);
    const onKey = (e: KeyboardEvent) => {
      const items = [...root.querySelectorAll<HTMLElement>(selector)];
      const item = itemOf(e.target);
      if (!item) return;
      const i = items.indexOf(item);
      const onItem = e.target === item;
      const k = e.key;
      if (onItem && (k === "ArrowDown" || k === "ArrowUp" || k === "j" || k === "k")) {
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        e.preventDefault();
        const next = Math.max(0, Math.min(items.length - 1, i + (k === "ArrowDown" || k === "j" ? 1 : -1)));
        current.current = next; entered.current = null;
        applyRoving(root, selector, next, null);
        items[next]?.focus();
      } else if (onItem && (k === "Enter" || k === "F2")) {
        e.preventDefault();
        entered.current = i;
        applyRoving(root, selector, i, i);
        inner(item)[0]?.focus();
      } else if (!onItem && k === "Escape") {
        entered.current = null;
        applyRoving(root, selector, i, null);
        item.focus();
      }
    };
    const onFocusIn = (e: FocusEvent) => {
      const item = itemOf(e.target);
      if (!item) return;
      const i = [...root.querySelectorAll<HTMLElement>(selector)].indexOf(item);
      if (i >= 0 && i !== current.current) { current.current = i; applyRoving(root, selector, i, entered.current); }
    };
    const onFocusOut = (e: FocusEvent) => {
      if (entered.current !== null && !root.contains(e.relatedTarget as Node | null)) {
        entered.current = null;
        applyRoving(root, selector, current.current, null);
      }
    };
    root.addEventListener("keydown", onKey);
    root.addEventListener("focusin", onFocusIn);
    root.addEventListener("focusout", onFocusOut);
    return () => {
      root.removeEventListener("keydown", onKey);
      root.removeEventListener("focusin", onFocusIn);
      root.removeEventListener("focusout", onFocusOut);
    };
  }, [ref, selector]);
}
