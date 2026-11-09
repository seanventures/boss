"use client";

/** The pieces Research and Panels share: the confidence ring, the verdict pill, the stage
 *  stepper, the BOSS mark that stands for the facilitator, and the "how it works" steps. */
import React, { useEffect, useState, type ReactNode } from "react";

// The screens' one stylesheet, loaded with the pieces every one of them uses, the full pages
// and the compact views inside a workflow run's step panel alike.
import "./research.css";

import { ago as agoText, type Stage, type Tone } from "../../lib/research-panels";

export function Ring({ value, size = 40, color = "var(--teal)", big = false, live = false, label }: {
  /** 0..1 */
  value: number | null | undefined;
  size?: number;
  color?: string;
  big?: boolean;
  /** A spinner instead of a value, while the number is still being worked out. */
  live?: boolean;
  label?: string;
}) {
  const p = Math.max(0, Math.min(100, Math.round((Number(value) || 0) * 100)));
  return (
    <span className="rp-ring" data-size={big ? "big" : undefined} data-live={live ? "true" : undefined}
          role="img" aria-label={label ?? (live ? "working" : `${p}% confidence`)}
          style={{ ["--p" as string]: p, ["--c" as string]: color, width: size, height: size }}>
      <b>{live ? "" : `${p}%`}</b>
    </span>
  );
}

export function Verdict({ tone, children, big = false, testId }: {
  tone: Tone; children: ReactNode; big?: boolean; testId?: string;
}) {
  return (
    <span className={`rp-verdict${big ? " big" : ""}`} data-tone={tone} data-testid={testId}>
      {children}
    </span>
  );
}

const GLYPH: Record<Stage["state"], string> = { done: "✓", now: "→", todo: "", stopped: "✕", skipped: "–" };

export function Stepper({ stages, live = false, testId }: { stages: Stage[]; live?: boolean; testId?: string }) {
  return (
    <ol className="rp-stepper" data-testid={testId} aria-label="Stages">
      {stages.map((s, i) => (
        <li key={s.key} className="rp-ph" data-state={s.state} data-live={live && s.state === "now" ? "true" : undefined}
            aria-current={s.state === "now" ? "step" : undefined}>
          <i aria-hidden>{GLYPH[s.state] || i + 1}</i>{s.label}
        </li>
      ))}
    </ol>
  );
}

/** The facilitator is BOSS itself: its mark, the size of a face. */
export function BossMark({ size = 28 }: { size?: number }) {
  return (
    <span className="rp-mark" style={{ width: size, height: size }} aria-label="Facilitator" role="img">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/boss-mark.png" alt="" />
    </span>
  );
}

export function How({ steps }: { steps: string[] }) {
  return (
    <ol className="rp-how" aria-label="How it works">
      {steps.map((s, i) => <li key={s}><i aria-hidden>{i + 1}</i>{s}</li>)}
    </ol>
  );
}

/** The current query string, after mount and on every Back/Forward, what links built with
 *  consoleHref keep (the org) without rendering a different href on the server. */
export function useSearch(): string {
  const [search, setSearch] = useState("");
  useEffect(() => {
    const read = () => setSearch(window.location.search);
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  return search;
}

/** Replace some of the URL's parameters as a new history entry (Back returns). */
export function pushParams(patch: Record<string, string | null>): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  for (const [k, v] of Object.entries(patch)) {
    if (v) url.searchParams.set(k, v);
    else url.searchParams.delete(k);
  }
  window.history.pushState(null, "", url.toString());
}

/** Text clipped on a word, with the whole of it one hover away. */
export function clip(text: string | null | undefined, max: number): string {
  const t = String(text ?? "").replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.lastIndexOf(" ", max - 1);
  return `${t.slice(0, cut > max / 2 ? cut : max - 1).replace(/[,;:\s]+$/, "")}…`;
}

/** A technical id kept out of the copy, one press away for support. */
export function CopyId({ id, label = "Copy id" }: { id: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="rp-link sm rp-copy" title={id} data-testid="rp-copy-id"
            onClick={() => {
              try { void navigator.clipboard?.writeText(id); } catch { /* no clipboard */ }
              setDone(true);
              window.setTimeout(() => setDone(false), 1500);
            }}>
      {done ? "Copied" : label}
    </button>
  );
}

/** "3m ago", worked out after mount, the server's clock is not the browser's, and an age that
 *  differs between the two is a hydration error. */
export function Ago({ iso, prefix = "" }: { iso?: string | null; prefix?: string }) {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(t);
  }, []);
  if (!iso || now === null) return null;
  const text = agoText(iso, now);
  return text ? <>{prefix}{text}</> : null;
}
