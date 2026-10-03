"use client";

/**
 * One tooltip for every `[data-tip]` in the Workflow Studio.
 *
 * The studio keeps its explanations out of the layout: a node shows an icon and a number, and
 * the sentence behind it lives in `data-tip`. The lane scrolls inside itself, so a CSS tooltip
 * would be clipped at the lane's edge. This draws a single fixed bubble on `document.body`
 * instead, shared by every lane, palette and chip that asks for it (reference counted, so two
 * lanes on one page do not draw two bubbles).
 *
 * Hover and keyboard focus both show it, and the target is `aria-describedby` the bubble while
 * it shows, so a screen reader hears the same sentence a mouse user reads.
 */
import { useEffect } from "react";

const BUBBLE_ID = "wf-tip";
const SHOW_AFTER_MS = 250;

let users = 0;
let bubble: HTMLDivElement | null = null;
let target: HTMLElement | null = null;
let timer: number | null = null;
let priorDescribedBy: string | null = null;

function tipTarget(node: EventTarget | null): HTMLElement | null {
  if (!(node instanceof Element)) return null;
  const el = node.closest<HTMLElement>("[data-tip]");
  return el && el.dataset.tip ? el : null;
}

function hide() {
  if (timer !== null) { window.clearTimeout(timer); timer = null; }
  if (target) {
    if (priorDescribedBy) target.setAttribute("aria-describedby", priorDescribedBy);
    else target.removeAttribute("aria-describedby");
  }
  target = null;
  priorDescribedBy = null;
  if (bubble) bubble.hidden = true;
}

function place(el: HTMLElement) {
  if (!bubble) return;
  const a = el.getBoundingClientRect();
  const b = bubble.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const left = Math.max(8, Math.min(a.left + a.width / 2 - b.width / 2, vw - b.width - 8));
  const below = a.bottom + 8;
  const top = below + b.height > vh - 8 && a.top - b.height - 8 > 8 ? a.top - b.height - 8 : below;
  bubble.style.left = `${Math.round(left)}px`;
  bubble.style.top = `${Math.round(top)}px`;
}

function show(el: HTMLElement, immediate: boolean) {
  if (target === el) return;
  hide();
  target = el;
  const open = () => {
    timer = null;
    if (target !== el || !el.isConnected) return;
    if (!bubble) {
      bubble = document.createElement("div");
      bubble.id = BUBBLE_ID;
      bubble.className = "wf-tip";
      bubble.setAttribute("role", "tooltip");
      document.body.appendChild(bubble);
    }
    bubble.textContent = el.dataset.tip ?? "";
    bubble.hidden = false;
    priorDescribedBy = el.getAttribute("aria-describedby");
    el.setAttribute("aria-describedby", priorDescribedBy ? `${priorDescribedBy} ${BUBBLE_ID}` : BUBBLE_ID);
    place(el);
  };
  if (immediate) open();
  else timer = window.setTimeout(open, SHOW_AFTER_MS);
}

const onOver = (e: Event) => {
  const el = tipTarget(e.target);
  if (el) show(el, false);
  else if (target) hide();
};
const onFocus = (e: Event) => {
  const el = tipTarget(e.target);
  if (el) show(el, true);
};
const onOut = (e: Event) => {
  const next = tipTarget((e as FocusEvent | PointerEvent).relatedTarget ?? null);
  if (next !== target) hide();
};
const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };

function attach() {
  document.addEventListener("pointerover", onOver);
  document.addEventListener("pointerout", onOut);
  document.addEventListener("focusin", onFocus);
  document.addEventListener("focusout", onOut);
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("scroll", hide, true);
  document.addEventListener("keydown", onKey);
}

function detach() {
  document.removeEventListener("pointerover", onOver);
  document.removeEventListener("pointerout", onOut);
  document.removeEventListener("focusin", onFocus);
  document.removeEventListener("focusout", onOut);
  document.removeEventListener("pointerdown", hide, true);
  document.removeEventListener("scroll", hide, true);
  document.removeEventListener("keydown", onKey);
  hide();
  bubble?.remove();
  bubble = null;
}

/** Turns `data-tip` on for as long as the calling component is mounted. */
export function useTips(): void {
  useEffect(() => {
    if (users === 0) attach();
    users += 1;
    return () => {
      users -= 1;
      if (users === 0) detach();
    };
  }, []);
}
