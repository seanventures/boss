/**
 * The tab title's `(n)` prefix: ONE OWNER (agents-home walk, D5).
 *
 * The pill used to write `(n) ` onto document.title once, when its count changed. Next sets the
 * title again on every navigation and every refresh (the page's metadata), so the prefix
 * vanished on Command Center, on Observability and after deciding — until a reload. Now the
 * shell holds the count here, and a MutationObserver puts the prefix back whenever anything
 * else rewrites the title. Nothing else in the console writes document.title.
 */
import { useEffect } from "react";

const PREFIX = /^\(\d+\) /;
let count = 0;
let observer: MutationObserver | null = null;
let users = 0;

/** The title as it should read for `n` things that need you. */
export function titled(title: string, n: number): string {
  const base = title.replace(PREFIX, "");
  return n > 0 ? `(${n}) ${base}` : base;
}

function apply(): void {
  if (typeof document === "undefined") return;
  const want = titled(document.title, count);
  if (document.title !== want) document.title = want;
}

function watch(): void {
  if (observer || typeof MutationObserver === "undefined" || typeof document === "undefined") return;
  observer = new MutationObserver(() => apply());
  observer.observe(document.head, { subtree: true, childList: true, characterData: true });
}

/** Set the count the title carries. Re-applied after any other write to the title. */
export function setTitleCount(n: number): void {
  count = Math.max(0, Math.floor(n || 0));
  apply();
}

/** The shell's hook: the title says `(n)` while `n` things need you, on every area. */
export function useTitleCount(n: number): void {
  useEffect(() => {
    users += 1;
    watch();
    return () => {
      users -= 1;
      if (users > 0) return;
      observer?.disconnect();
      observer = null;
      count = 0;
      apply();
    };
  }, []);
  useEffect(() => { setTitleCount(n); }, [n]);
}
