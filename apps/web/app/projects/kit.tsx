"use client";

/**
 * The project experience's visual primitives (BOSSAPP-914): the journey strip, the station strip,
 * flow chips, rings, sparklines, icon tiles, faces, links that open in place, drawers, and the
 * empty / loading / error states every surface uses. One look, repeated, so it becomes familiar.
 */
import React, { useEffect, useId, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { createPortal } from "react-dom";
import {
  ArrowSquareOut, ArrowsClockwise, Binoculars, Brain, Compass, FolderPlus, GithubLogo, GitPullRequest, Check, CloudSlash, Flag, Flask, Folders, Hammer, ListChecks, Lock, Pause, RocketLaunch, Warning, X,
  type Icon,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { isExternal, pxHref, readPxUrl, writePxUrl, type Agent, type Health, type Node, type PxUrl, type ReadState, type StepState } from "../../lib/project-experience";

export type Tone = "teal" | "green" | "amber" | "red" | "violet" | "blue";

/** An icon in its own coloured tile: every section, tile and empty state has one. */
export function Ico({ icon: I, c = "blue", size }: { icon: Icon; c?: Tone; size?: "l" }) {
  return <span className="px-ico" data-c={c} data-size={size} aria-hidden><I size={size === "l" ? 22 : 16} weight="duotone" /></span>;
}

export function Tag({ tone, children, title }: { tone?: "green" | "amber" | "red" | "blue" | "violet" | "cost" | "idle"; children: React.ReactNode; title?: string }) {
  return <span className="px-tag" data-tone={tone} title={title}>{children}</span>;
}

export const HEALTH_TONE: Record<Health, "green" | "amber" | "red" | "idle"> = { healthy: "green", degraded: "amber", down: "red", unknown: "idle" };
export const HEALTH_WORD: Record<Health, string> = { healthy: "Healthy", degraded: "Degraded", down: "Down", unknown: "Unknown" };

/** A ring chart: `pct` 0-100, the text inside, colour by meaning. */
export function Ring({ pct, children, tone = "green", size, label }: { pct: number; children?: React.ReactNode; tone?: "green" | "teal" | "amber" | "red"; size?: "s" | "l"; label: string }) {
  const p = Math.max(0, Math.min(100, pct));
  return (
    <span className="px-ring" data-size={size} role="img" aria-label={label}
          style={{ ["--p" as string]: p, ["--c" as string]: `var(--${tone})` }}>
      <span aria-hidden>{children}</span>
    </span>
  );
}

/** A sparkline (or a filled area): the shape of a series, no axes. */
export function Spark({ series, tone = "violet", fill = false, label }: { series: number[]; tone?: string; fill?: boolean; label: string }) {
  if (series.length < 2) return null;
  const w = 120, h = 28;
  const min = Math.min(...series), max = Math.max(...series);
  const span = max - min || 1;
  const pts = series.map((v, i) => [(i / (series.length - 1)) * w, h - 3 - ((v - min) / span) * (h - 6)]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const color = tone === "violet" ? "var(--px-violet, #8b7cf6)" : `var(--${tone})`;
  return (
    <svg className="px-spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label={label}>
      {fill ? <path d={`${d} L${w},${h} L0,${h} Z`} fill={color} opacity={0.18} /> : null}
      <path d={d} fill="none" stroke={color} strokeWidth={1.6} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

const STATE_GLYPH: Partial<Record<StepState, React.ReactNode>> = {
  done: <Check size={13} weight="bold" />, waiting: <Lock size={12} weight="fill" />, failed: <X size={12} weight="bold" />, skipped: <Pause size={11} />,
};
export const STATE_WORD: Record<StepState, string> = { done: "done", now: "in progress", waiting: "needs you", todo: "not started", failed: "failed", skipped: "skipped" };

/** Each journey stage's own icon: a node not done yet shows what it is, never a bare number. */
export const STAGE_ICON: Record<string, Icon> = {
  onboarding: Binoculars, ready: Flag, backlog: ListChecks, working: Hammer, staging: Flask, production: RocketLaunch,
  // The first-run strip (screen 00a): Connect GitHub · Add a model · Add project · Helm onboards · First PR.
  github: GithubLogo, model: Brain, project: FolderPlus, onboard: Compass, pr: GitPullRequest,
};
function nodeGlyph(n: Node, i: number, small: boolean): React.ReactNode {
  const glyph = STATE_GLYPH[n.state];
  if (glyph) return glyph;
  const I = STAGE_ICON[n.key];
  if (I) return <I size={small ? 10 : 14} weight={n.state === "now" ? "bold" : "regular"} />;
  return n.state === "now" ? <ArrowsClockwise size={13} weight="bold" /> : i + 1;
}

/** THE JOURNEY STRIP: the project's life as one diagram. Each node opens what happened there. */
export function Journey({ nodes, size, onOpen, testid = "px-journey" }: { nodes: Node[]; size?: "s"; onOpen?: (n: Node) => void; testid?: string }) {
  if (!nodes.length) return null;
  return (
    <div className="px-jr" data-size={size} data-testid={testid} role="list" aria-label="Journey">
      {nodes.map((n, i) => (
        <React.Fragment key={`${n.key}-${i}`}>
          {i ? <span className="px-jr-wire" aria-hidden data-s={nodes[i - 1].state === "done" ? (n.state === "now" ? "now" : n.state === "done" ? "done" : undefined) : undefined} /> : null}
          <button type="button" role="listitem" className="px-jr-node" data-s={n.state} disabled={!onOpen || !(n.href || n.key)}
                  data-testid={`${testid}-${n.key || i}`} aria-label={`${n.label}: ${STATE_WORD[n.state]}${n.sub ? `, ${n.sub}` : ""}`} title={n.detail || undefined}
                  onClick={onOpen ? () => onOpen(n) : undefined}>
            <span className="px-jr-shape" aria-hidden>{nodeGlyph(n, i, size === "s")}</span>
            <span className="px-jr-cap">{n.label}</span>
            {n.sub && size !== "s" ? <span className="px-jr-meta">{n.sub}</span> : null}
          </button>
        </React.Fragment>
      ))}
    </div>
  );
}

/** A TIMELINE: up to six points, oldest first, a day label over each ("Mon", "Today"), the dot
 *  coloured by kind and a few words under it. Fits any width: points share the row. */
export function Timeline({ points, testid, onOpen }: {
  points: { at: string | null; label: string; kind: string; href?: string }[]; testid?: string; onOpen?: (i: number) => void;
}) {
  if (!points.length) return null;
  const day = (at: string | null) => {
    if (!at) return "";
    const d = new Date(at);
    if (Number.isNaN(d.getTime())) return "";
    const now = new Date();
    const same = (x: Date, y: Date) => x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
    if (same(d, now)) return "Today";
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (same(d, y)) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "short" });
  };
  return (
    <ol className="px-tl" data-testid={testid} aria-label="Timeline">
      {points.map((p, i) => (
        <li key={`${p.label}-${i}`} className="px-tl-p" data-k={p.kind} data-testid={testid ? `${testid}-${i}` : undefined}>
          <span className="px-tl-day">{day(p.at)}</span>
          <span className="px-tl-dot" aria-hidden />
          {p.href ? (
            <a className="px-tl-cap" href={/^https?:/.test(p.href) ? p.href : pxHref(p.href)} {...(/^https?:/.test(p.href) ? { target: "_blank", rel: "noreferrer noopener" } : {})}
               onClick={(e) => { if (!onOpen || /^https?:/.test(p.href!) || e.metaKey || e.ctrlKey) return; e.preventDefault(); onOpen(i); }}>{p.label}</a>
          ) : <span className="px-tl-cap">{p.label}</span>}
        </li>
      ))}
    </ol>
  );
}

/** THE STATION STRIP: one piece of work, Plan · Design · Build · Review · PR · Ship. */
export function Stations({ stages }: { stages: { key: string; label: string; state: StepState }[] }) {
  if (!stages.length) return <span className="px-st" aria-hidden />;
  const said = stages.map((st) => `${st.key || st.label} ${STATE_WORD[st.state]}`).join(", ");
  return (
    <span className="px-st" role="img" aria-label={said}>
      {stages.map((st, i) => (
        <React.Fragment key={`${st.key}-${i}`}>
          {i ? <span className="px-st-w" data-s={stages[i - 1].state === "done" ? "done" : undefined} /> : null}
          <span className="px-st-n" data-s={st.state} title={`${st.key || st.label}: ${STATE_WORD[st.state]}`}>{st.label || st.key.slice(0, 1).toUpperCase()}</span>
        </React.Fragment>
      ))}
    </span>
  );
}

/** FLOW CHIPS: a path or a reason as nodes and arrows (≤ 4 words each). */
export function Flow({ nodes, testid }: { nodes: { label: string; state?: StepState | string; type?: string; icon?: React.ReactNode }[]; testid?: string }) {
  if (!nodes.length) return null;
  return (
    <div className="px-fl" data-testid={testid} role="list">
      {nodes.map((n, i) => (
        <React.Fragment key={`${n.label}-${i}`}>
          {i ? <span className="px-fl-w" aria-hidden data-s={(nodes[i - 1].state ?? "") === "done" ? "done" : undefined} /> : null}
          <span className="px-fl-n" role="listitem" data-t={n.type ?? n.state ?? "todo"}>
            {n.icon}{n.state === "done" && !n.icon ? <Check size={11} weight="bold" aria-hidden /> : null}{n.label}
          </span>
        </React.Fragment>
      ))}
    </div>
  );
}

/** An agent's face from the avatar kit (its role key or callsign), decorative beside its name. */
export function Face({ agent, size = 22, state = "idle", standalone }: { agent: Agent | { name: string; role_key?: string }; size?: number; state?: "idle" | "working"; standalone?: boolean }) {
  if (agent.name === "You" && !agent.role_key) return <AgentAvatar variant="you" size={size} standalone={standalone} tip="You" />;
  return <AgentAvatar agent={agent.role_key || agent.name} size={size} state={state} standalone={standalone} aiBadge={false}
                      tip={agent.name} />;
}
export function Faces({ agents, max = 4 }: { agents: Agent[]; max?: number }) {
  if (!agents.length) return <span className="px-faces" aria-hidden />;
  return (
    <span className="px-faces" title={agents.map((a) => a.name).join(", ")} aria-label={`Agents: ${agents.map((a) => a.name).join(", ")}`} role="img">
      {agents.slice(0, max).map((a, i) => <Face key={`${a.name}-${i}`} agent={a} size={22} />)}
    </span>
  );
}

/** A link from a contract href: in-console hrefs keep the org; external ones open a new tab with ↗. */
export function PxLink({ href, children, className, testid, title, onOpen }: {
  href: string; children: React.ReactNode; className?: string; testid?: string; title?: string; onOpen?: () => void;
}) {
  if (!href) return null;
  const ext = isExternal(href);
  return (
    <a className={className ?? "px-chip"} href={pxHref(href)} data-testid={testid} title={title}
       {...(ext ? { target: "_blank", rel: "noreferrer noopener" } : {})}
       onClick={onOpen ? (e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); onOpen(); } : undefined}>
      {children}{ext ? <ArrowSquareOut size={11} aria-label="opens in a new tab" /> : null}
    </a>
  );
}

/** Open a drawer in place (deep-linkable `&drawer=kind:id`; Back closes it). */
export const openDrawer = (kind: string, id: string) => writePxUrl({ drawer: `${kind}:${id}` });
export const closeDrawer = () => writePxUrl({ drawer: null });

/** A side drawer: focus moves in, Esc and the scrim close it, focus returns to where it was. */
export function Drawer({ title, sub, icon, onClose, children, footer, testid = "px-drawer", wide }: {
  title: React.ReactNode; sub?: React.ReactNode; icon?: React.ReactNode; onClose: () => void; children: React.ReactNode;
  footer?: React.ReactNode; testid?: string; wide?: boolean;
}) {
  const id = useId();
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); onClose(); } };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); before?.focus?.(); };
  }, [onClose]);
  const body = (
    <div className="px">
      <div className="px-scrim" onClick={onClose} data-testid={`${testid}-scrim`} />
      <div className="px-drawer" role="dialog" aria-modal="true" aria-labelledby={id} tabIndex={-1} ref={panel} data-testid={testid}
           style={wide ? { width: "min(620px, 100vw)" } : undefined}>
        <div className="px-drawer-h">
          {icon}
          <div style={{ minWidth: 0, flex: 1 }}>
            <h2 id={id} className="px-ell">{title}</h2>
            {sub ? <div className="px-row px-wrap px-faint" style={{ fontSize: ".58rem", marginTop: 3 }}>{sub}</div> : null}
          </div>
          <button type="button" className="px-icon-btn" onClick={onClose} aria-label="Close" title="Close (Esc)" data-testid={`${testid}-close`}>
            <X size={16} weight="bold" />
          </button>
        </div>
        <div className="px-drawer-b">{children}</div>
        {footer ? <div className="px-drawer-f">{footer}</div> : null}
      </div>
    </div>
  );
  return typeof document === "undefined" ? body : createPortal(body, document.body);
}

/** An illustrated empty state: what it is, why it is empty, and the one thing to do. */
export function Empty({ icon = Folders, title, line, children, testid, tone = "blue" }: {
  icon?: Icon; title: string; line?: string; children?: React.ReactNode; testid?: string; tone?: Tone;
}) {
  return (
    <div className="px-empty" data-testid={testid}>
      <svg className="px-illus" viewBox="0 0 120 84" aria-hidden>
        <circle cx="60" cy="42" r="34" fill="none" stroke="var(--border-strong)" strokeDasharray="3 5" />
        <circle cx="22" cy="20" r="3" fill="var(--teal)" opacity=".7" /><circle cx="100" cy="64" r="3" fill="var(--px-violet, #8b7cf6)" opacity=".7" />
        <circle cx="96" cy="16" r="2" fill="var(--accent)" />
      </svg>
      <div style={{ marginTop: -64, marginBottom: 18 }}><Ico icon={icon} c={tone} size="l" /></div>
      <h3>{title}</h3>
      {line ? <p>{line}</p> : null}
      {children ? <div className="px-row px-wrap" style={{ justifyContent: "center" }}>{children}</div> : null}
    </div>
  );
}

/** Loading: the shape of what is coming, never a spinner over nothing. */
export function Loading({ rows = 3, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="px-stack" role="status" aria-live="polite" aria-label={label} data-testid="px-loading">
      {Array.from({ length: rows }, (_, i) => <div key={i} className="px-skel" style={{ minHeight: i ? 72 : 110 }} />)}
    </div>
  );
}

/** A read that failed: what failed, and Retry. */
export function Failed({ what, error, onRetry }: { what: string; error: string | null; onRetry: () => void }) {
  return (
    <div className="px-card" role="alert" data-testid="px-error">
      <Empty icon={CloudSlash} tone="red" title={`${what} didn't load`} line={error ?? "BOSS did not answer."}>
        <button type="button" className="px-btn" onClick={onRetry} data-testid="px-retry"><ArrowsClockwise size={14} /> Try again</button>
      </Empty>
    </div>
  );
}

/** The four states every read has, so no surface renders blank. */
export function ReadGate<T>({ read, what, children, loadingRows, missing }: {
  read: { data: T | null; state: ReadState; error: string | null; refresh: () => void };
  what: string; children: (data: T) => React.ReactNode; loadingRows?: number; missing?: React.ReactNode;
}) {
  if (read.data) return <>{children(read.data)}</>;
  if (read.state === "loading") return <Loading rows={loadingRows} label={`Loading ${what}`} />;
  if (read.state === "error") return <Failed what={what} error={read.error} onRetry={read.refresh} />;
  return <>{missing ?? <EngineNotReady what={what} />}</>;
}

/** BOSS answered "not found" for this read (the project is gone or out of reach, or an engine
 *  older than this console): say so, and where to go instead. */
export function EngineNotReady({ what, children }: { what: string; children?: React.ReactNode }) {
  return (
    <div className="px-card" data-testid="px-missing">
      <Empty icon={Warning} tone="amber" title={`${what} didn't load`}
             line="BOSS didn't find it for this project. Everything it covers is still in its own area.">
        {children}
      </Empty>
    </div>
  );
}

/** The sample-data banner (`?px_demo=1`). */
export function DemoBanner() {
  return (
    <div className="px-demo-banner" role="note" data-testid="px-demo-banner">
      <Folders size={14} aria-hidden /> Sample data — this is the contract's example project. Actions here send nothing.
      <span className="px-spacer" />
      <a href={pxHref("?area=projects").replace(/[?&]px_demo=1/, "")} onClick={(e) => {
        e.preventDefault();
        const url = new URL(window.location.href); url.searchParams.delete("px_demo"); window.location.assign(url.toString());
      }}>Leave sample</a>
    </div>
  );
}

/** A small toast line under an action: what happened, in words. */
export function Said({ text, tone }: { text: string; tone?: "red" | "green" }) {
  if (!text) return null;
  return <span role={tone === "red" ? "alert" : "status"} className="px-faint" style={{ fontSize: ".6rem", color: tone === "red" ? "var(--red)" : tone === "green" ? "var(--green)" : undefined }}
               data-testid="px-said">{text}</span>;
}

/** Open the Add a project sheet (app/start-project, mounted by the project menu) at a door. */
export function openAddProject(step: "import" | "idea" | "sample" | "1" = "import"): void {
  writePxUrl({ new_project: step });
}

/** The URL, re-read on Back/Forward and on every in-page move. */
export function usePxUrl(): PxUrl {
  // The first render reads the URL the server rendered (Next's search params), so the tab drawn
  // on the server is the one hydration expects; a test without Next's router reads the window.
  const sp = useSearchParams();
  const [url, setUrl] = useState<PxUrl>(() => readPxUrl(sp ? `?${sp.toString()}` : undefined));
  useEffect(() => {
    const read = () => setUrl(readPxUrl());
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  return url;
}
