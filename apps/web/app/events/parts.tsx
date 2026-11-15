"use client";

/**
 * The small parts every Events screen draws (P9a-7): a source's mark, a status chip, a face for a
 * person or an agent, a sparkline, an empty state, a read's state in words, and the screen's place
 * in the URL (`?area=setup&events=<tab>&conn=<id>&ev=<id>`) so Back, a reload and a shared link
 * land on the same screen.
 */
import React, { useCallback, useEffect, useState, type ReactNode } from "react";
import { ArrowClockwise } from "@phosphor-icons/react";

import { agentMeta } from "../agent-chip";
import { chipOf, labelOfKind, type Read } from "../../lib/intake";

// ── where the screen is ───────────────────────────────────────────────────────────────────────
export type EvTab = "connections" | "connect" | "rules" | "stream" | "dead" | "people";
export const EV_TABS: EvTab[] = ["connections", "connect", "rules", "stream", "dead", "people"];
export type EvPlace = { tab: EvTab | null; conn: string; ev: string; kind: string };

function readPlace(): EvPlace {
  try {
    const sp = new URLSearchParams(window.location.search);
    const t = sp.get("events") || "";
    return { tab: (EV_TABS as string[]).includes(t) ? (t as EvTab) : null, conn: sp.get("conn") || "", ev: sp.get("ev") || "", kind: sp.get("kind") || "" };
  } catch {
    return { tab: null, conn: "", ev: "", kind: "" };
  }
}

/** Move within Events (or leave it with `events: null`), as a history entry. */
export function goEvents(patch: Partial<Record<"events" | "conn" | "ev" | "kind", string | null>>, mode: "push" | "replace" = "push"): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("area", "setup");
    for (const [k, v] of Object.entries(patch)) { if (v === null || v === "") url.searchParams.delete(k); else url.searchParams.set(k, v as string); }
    if (patch.events === null) for (const k of ["conn", "ev", "kind"]) url.searchParams.delete(k);
    if (url.toString() === window.location.href) return;
    if (mode === "push") window.history.pushState(null, "", url.toString());
    else window.history.replaceState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* not a browser */ }
}

/** The Events place from the URL; `initialTab` is what the server rendered (no flash). */
export function useEvPlace(initialTab: string): EvPlace {
  const [place, setPlace] = useState<EvPlace>(() => ({
    tab: (EV_TABS as string[]).includes(initialTab) ? (initialTab as EvTab) : null, conn: "", ev: "", kind: "",
  }));
  useEffect(() => {
    const sync = () => setPlace(readPlace());
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  return place;
}

// ── marks, chips, faces ───────────────────────────────────────────────────────────────────────
const MONO: Record<string, string> = { slack: "S", github: "GH", webhook: "W", teams: "T", servicenow: "SN", salesforce: "SF", mail: "M" };
export function SourceMark({ kind, size }: { kind: string; size?: "sm" }) {
  return <span className="ev-mark" data-kind={kind} data-size={size} aria-hidden>{MONO[kind] || (kind.slice(0, 2).toUpperCase() || "?")}</span>;
}

export function StatusChip({ status, count, testId }: { status: string; count?: number; testId?: string }) {
  const c = chipOf(status);
  return <span className="ev-chip" data-tone={c.tone} title={c.tip} data-testid={testId}>{c.word}{count !== undefined ? ` · ${count.toLocaleString("en-US")}` : ""}</span>;
}

export function Chip({ tone, tip, children, testId }: { tone: string; tip?: string; children: ReactNode; testId?: string }) {
  return <span className="ev-chip" data-tone={tone} title={tip} data-testid={testId}>{children}</span>;
}

/** Two letters and a stable tone for a person; an agent uses the console's agent initials. */
export function Face({ name, agent, size, tip }: { name: string; agent?: boolean; size?: "lg"; tip?: string }) {
  const meta = agent ? agentMeta(name, name) : null;
  const words = (name || "?").replace(/^@/, "").replace(/[._-]+/g, " ").trim().split(/\s+/);
  const initials = meta ? meta.initials : (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2)).toUpperCase();
  let sum = 0;
  for (const ch of name || "") sum = (sum + ch.charCodeAt(0)) % 997;
  return <span className="ev-face" data-tone={String(sum % 5)} data-size={size} title={tip || name} aria-hidden>{initials}</span>;
}

export function AgentName({ agentKey, name }: { agentKey: string; name?: string }) {
  return <span className="ev-who" title={`${name || agentKey}, AI agent`}><Face name={agentKey} agent /><span>{name || agentKey}</span></span>;
}

/** A small area sparkline from counts; empty counts draw a flat line. */
export function Spark({ values, tone, width = 120, height = 30 }: { values: number[]; tone?: "violet"; width?: number; height?: number }) {
  const vs = values.length > 1 ? values : [0, 0];
  const max = Math.max(1, ...vs);
  const step = width / (vs.length - 1);
  const pts = vs.map((v, i) => `${(i * step).toFixed(1)},${(height - 4 - (v / max) * (height - 8)).toFixed(1)}`).join(" ");
  return (
    <svg className="ev-spark" data-tone={tone} width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <polygon points={`0,${height} ${pts} ${width},${height}`} />
      <polyline points={pts} />
    </svg>
  );
}

export function Empty({ icon, title, line, children, testId }: { icon: ReactNode; title: string; line: string; children?: ReactNode; testId?: string }) {
  return (
    <div className="ev-empty" data-testid={testId}>
      <span className="ev-empty-art" aria-hidden>{icon}</span>
      <b>{title}</b>
      <span>{line}</span>
      {children}
    </div>
  );
}

/** Loading, refused, failed: in words, with Retry. Nothing when the read is fine. */
export function ReadState<T>({ read, what, onRetry, testId }: { read: Read<T> | null; what: string; onRetry: () => void; testId?: string }) {
  if (read === null) return <div className="ev-skel" aria-busy="true" aria-label={`Loading ${what}`} data-testid={testId ? `${testId}-loading` : undefined} />;
  if (read.ok) return null;
  return (
    <div className="ev-said" data-tone={read.denied ? "amber" : "red"} role="alert" data-testid={testId ? `${testId}-error` : undefined}>
      <span>{read.denied ? read.error : `${what}: ${read.error}`}</span>{" "}
      {read.denied ? null : <button type="button" className="ev-link" onClick={onRetry}><ArrowClockwise size={12} aria-hidden /> Retry</button>}
    </div>
  );
}

/** A one-line result of a press, green or red. */
export function Said({ said, testId }: { said: { text: string; tone: "green" | "red" | "amber" } | null; testId?: string }) {
  if (!said) return null;
  return <div className="ev-said" data-tone={said.tone === "green" ? undefined : said.tone} role="status" data-testid={testId}>{said.text}</div>;
}

/** A read that loads on mount and again on `reload()`. */
export function useRead<T>(load: () => Promise<Read<T>>, deps: unknown[]): { read: Read<T> | null; reload: () => void; set: (r: Read<T>) => void } {
  const [read, setRead] = useState<Read<T> | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  const reload = useCallback(() => { void run().then(setRead); }, [run]);
  useEffect(() => {
    let live = true;
    setRead(null);
    void run().then((r) => { if (live) setRead(r); });
    return () => { live = false; };
  }, [run]);
  return { read, reload, set: setRead };
}

export const kindName = labelOfKind;
