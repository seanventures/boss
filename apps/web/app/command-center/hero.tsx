"use client";

/**
 * THE HERO (cc v2): the one question, what should your team build next?, beside the team
 * that will answer it. Left: the systems word and when work last happened, the headline, the
 * composer (AskBossHero, laid out as `hero`), and the proof row. Right: the nine teams as tiles
 * of real faces; a face that waits on you wears an amber ring, a working one glows.
 *
 * Every number links to where it is counted: apps and PRs to the Factory, agents to Agent
 * Studio, spend to Budget. The faces are AgentAvatar, the same face on every surface.
 */
import React, { useEffect, useRef, useState } from "react";

import { createPortal } from "react-dom";

import { openDockAbout } from "../../lib/dock-subject";

import type { BoardAgent, TeamSummary } from "../../lib/agents-live-board";
import type { CcApp } from "../../lib/command-center-apps";
import type { SystemDot } from "../../lib/command-center-view";
import { AgentAvatar } from "../agent-avatar";
import { nameOf } from "../agents/run-face";
import { linkTo, navigate, plainClick } from "../agents/live-util";
import { useIdentityVersion } from "../../lib/use-agent-identities";

/** Each team's hue (the approved look). Unknown teams take the accent. */
export const TEAM_HUE: Record<string, string> = {
  leadership: "#5B8CFF", architecture: "#A78BFA", engineering: "#2EC5C5", quality: "#F2647A",
  operations: "#F5B544", cost: "#34D399", media: "#EC6FCF", thinkers: "#7DD3FC", yours: "#C7D2FE",
};

/** A console link that navigates in place on a plain click. */
export function Go({ patch, orgId, className, children, title, testId, ariaLabel, style }: {
  patch: Record<string, string | null>; orgId?: string | null; className?: string;
  children: React.ReactNode; title?: string; testId?: string; ariaLabel?: string; style?: React.CSSProperties;
}) {
  return (
    <a className={className} href={linkTo(patch, orgId)} title={title} data-testid={testId}
       aria-label={ariaLabel} style={style}
       onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(patch); } }}>
      {children}
    </a>
  );
}

/** `$139` for whole-ish dollars, `$4.02` under ten. */
export function dollars(usd: number): string {
  const v = Math.max(0, usd);
  return v >= 10 ? `$${Math.round(v).toLocaleString("en-US")}` : `$${v.toFixed(2)}`;
}

/** A system dot as a sentence ("Disk nearly full", "Queue stuck"), never `HOST: DISK`. */
export function systemSentence(d: SystemDot): string {
  const key = `${d.key}:${d.state}`.toLowerCase();
  const said: Record<string, string> = {
    "host:disk": "Disk nearly full", "host:slow": "Host is slow", "host:struggling": "Host struggling",
    "models:slow": "Models are slow", "models:failing": "Models failing",
    "repo:expiring": "Repo token expiring", "repo:failing": "Repo failing",
    "budget:near cap": "Budget near its cap", "budget:over cap": "Budget over its cap",
    "queue:slow": "Queue is slow", "queue:stuck": "Queue stuck",
    "previews:slow": "A preview is slow to start", "previews:failed": "A preview failed",
    "deploys:deploying": "Deploying now", "deploys:failed": "A deploy failed",
  };
  return said[key] ?? `${d.word} ${d.state.toLowerCase()}`;
}

/** The area-name's systems words: what the Systems dots say, as a sentence. */
export function systemsWord(systems: SystemDot[], reachable: boolean): { text: string; tone: "green" | "amber" | "red" | "grey" } {
  if (!reachable) return { text: "Can't reach BOSS", tone: "grey" };
  const red = systems.filter((s) => s.tone === "red");
  const amber = systems.filter((s) => s.tone === "amber");
  if (red.length) return { text: red.length === 1 ? systemSentence(red[0]) : `${red.length} systems need a look`, tone: "red" };
  if (amber.length) return { text: amber.length === 1 ? systemSentence(amber[0]) : `${amber.length} systems need a look`, tone: "amber" };
  return { text: "All systems OK", tone: "green" };
}

/** "Juno, Product Manager", or just the name when the agent has no callsign (never twice). */
export function whoLine(roleKey: string, name?: string): string {
  const who = nameOf(roleKey);
  const role = name || who.role;
  return who.name && role && who.name.toLowerCase() !== role.toLowerCase() ? `${who.name}, ${role}` : (who.name || role);
}

export type ProofNumbers = { apps: number; prs: number; agents: number; teams: number; spent: number } | null;

export function Proof({ n, orgId }: { n: ProofNumbers; orgId?: string | null }) {
  const dash = "—";
  return (
    <div className="cc-proof" data-testid="cc-proof">
      <Go patch={{ area: "code_tasks", view: "apps", build: null, role: null }} orgId={orgId}
          title="Apps your team has built. Opens the Factory." testId="cc-proof-apps">
        <b>{n ? n.apps : dash}</b><span>apps built</span>
      </Go>
      <Go patch={{ area: "code_tasks", view: "apps", build: null, role: null }} orgId={orgId}
          title="Pull requests open and ready for your review" testId="cc-proof-prs">
        <b className="pr">{n ? n.prs : dash}</b><span>PRs ready to review</span>
      </Go>
      <Go patch={{ area: "workforce", view: "directory", role: null }} orgId={orgId}
          title="Your agents, in their teams. Opens Agent Studio." testId="cc-proof-agents">
        <b>{n ? n.agents : dash}</b><span>agents, {n ? n.teams : dash} teams</span>
      </Go>
      <Go patch={{ area: "budget", view: null, role: null }} orgId={orgId}
          title="Metered model spend over every build. Subscription calls are free." testId="cc-proof-spent">
        <b>{n ? dollars(n.spent) : dash}</b><span>spent on builds</span>
      </Go>
    </div>
  );
}

export function proofOf(apps: CcApp[] | null, agents: number, teams: number): ProofNumbers {
  if (!apps) return null;
  return {
    apps: apps.length,
    prs: apps.filter((a) => a.prNumber && (a.prState ?? "open") === "open" && a.state === "pr_open").length,
    agents, teams,
    spent: apps.reduce((t, a) => t + (a.costUsd || 0), 0),
  };
}

/** The team, as nine tiles of faces. */
export function TeamGrid({ teams, agents, waiting, working: live, orgId, still }: {
  teams: TeamSummary[]; agents: BoardAgent[];
  /** Agents an open item waits on you for (an approval they raised, a plan they drafted). */
  waiting: Set<string>;
  /** Agents on a live run's current step (the same source as In flight) or with open work. */
  working: Set<string>;
  orgId?: string | null;
  /** Motion off: faces hold still. */
  still: boolean;
}) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const byKey = new Map(agents.map((a) => [a.roleKey, a]));
  const [open, setOpen] = useState<{ key: string; at: DOMRect } | null>(null);
  // ONE SOURCE (re-check R6): the board's own agent state, which the server sets from open
  // work AND the running missions' working hops, what In flight and the pill read too.
  void live;
  const isWorkingAgent = (a: BoardAgent) => a.state === "working" || a.state === "in_panel";
  const working = agents.filter((a) => isWorkingAgent(a) && !waiting.has(a.roleKey)).length;
  const waitingN = agents.filter((a) => waiting.has(a.roleKey)).length;
  const ready = Math.max(0, agents.length - working - waitingN);
  const shown = teams.filter((t) => t.members.length > 0);
  return (
    <div className="cc-hero-r" data-testid="cc-team">
      <div className="cc-hero-r-h">
        <b>Your team</b>
        <span className="cc-links">
          <Go className="cc-link" patch={{ area: "workforce", view: "organization", role: null }} orgId={orgId}
              title="Who hands work to whom" testId="cc-open-map">Map ↗</Go>
          <Go className="cc-link" patch={{ area: "workforce", view: "directory", role: null }} orgId={orgId}
              testId="cc-agent-studio">Agent Studio ↗</Go>
        </span>
      </div>
      <div className="cc-teams">
        {shown.map((t) => {
          const members = t.members.map((k) => byKey.get(k)).filter((a): a is BoardAgent => !!a);
          return (
            <div key={t.key} className="cc-tile" data-testid={`cc-tile-${t.key}`}
                 style={{ ["--hue" as string]: TEAM_HUE[t.key] ?? "var(--boss-c-5b8cff)" }}>
              <span className="cc-tile-faces">
                {members.slice(0, 8).map((a) => {
                  const isWorking = isWorkingAgent(a);
                  const isWaiting = waiting.has(a.roleKey);
                  const word = isWaiting ? "waiting on you" : isWorking ? "working" : a.state === "stuck" ? "stuck" : a.state === "failed" ? "failed" : "ready";
                  const label = `${whoLine(a.roleKey, a.name)}, ${word}`;
                  // A FACE OPENS ITS AGENT'S LIVE LINE (parity d): what it is doing, Talk, and
                  // its page, the old Floor card, on tap or hover.
                  return (
                    <span key={a.roleKey} className="cc-face-wrap">
                      <button type="button" className="cc-face" title={label} aria-label={label}
                              aria-expanded={open?.key === a.roleKey} data-testid="cc-face"
                              data-live={isWorking || isWaiting ? "true" : undefined}
                              onClick={(e) => {
                                const at = e.currentTarget.getBoundingClientRect();
                                setOpen((o) => (o?.key === a.roleKey ? null : { key: a.roleKey, at }));
                              }}>
                        <span className="cc-face-in" data-working={isWorking && !isWaiting ? "true" : undefined}
                              data-waiting={isWaiting ? "true" : undefined}>
                          <AgentAvatar agent={a.roleKey} size={32} aiBadge={false}
                                       state={still ? "idle" : isWorking ? "working" : "idle"} />
                        </span>
                      </button>
                      {open?.key === a.roleKey ? (
                        <FaceCard agent={a} word={word} orgId={orgId} at={open.at} onClose={() => setOpen(null)} />
                      ) : null}
                    </span>
                  );
                })}
                {members.length > 8 ? <span className="cc-face-more">+{members.length - 8}</span> : null}
              </span>
              <Go className="cc-tile-name" patch={{ area: "workforce", view: "directory", role: null }} orgId={orgId}
                  title={t.tip ? `${t.label}: ${t.tip}` : t.label}>
                {t.label} <b>{members.length}</b>
              </Go>
            </div>
          );
        })}
      </div>
      <div className="cc-hero-r-f" data-testid="cc-team-foot">
        <span title="Agents doing work right now"><i className="g" />{working} working</span>
        <span title="Agents with something waiting on you"><i className="a" />{waitingN} waiting on you</span>
        <span title="Agents free to take work"><i className="n" />{ready} ready</span>
      </div>
    </div>
  );
}

/** The agent behind a face: who, what it is doing now, Talk, and its page. */
function FaceCard({ agent, word, orgId, at, onClose }: {
  agent: BoardAgent; word: string; orgId?: string | null; at: DOMRect; onClose: () => void;
}) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const outside = (e: PointerEvent) => {
      const t = e.target as Element | null;
      if (box.current?.contains(t) || t?.closest?.(".cc-face")) return;
      onClose();
    };
    const escape = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    const scrolled = () => onClose();
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    window.addEventListener("scroll", scrolled, { passive: true, capture: true });
    return () => {
      document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape);
      window.removeEventListener("scroll", scrolled, { capture: true });
    };
  }, [onClose]);
  // IN THE POPOVER LAYER (re-check R2): a tile's hover transform made each tile a stacking
  // context, so the card painted under the next tile. Portalled to the body, placed by its face.
  const vw = typeof window !== "undefined" ? window.innerWidth : 1440;
  const style: React.CSSProperties = { top: at.bottom + 6, left: Math.max(8, Math.min(at.left - 8, vw - 256)) };
  if (typeof document === "undefined") return null;
  return createPortal(
    <div className="cc-facecard" role="dialog" aria-label={whoLine(agent.roleKey, agent.name)} ref={box}
         data-testid="cc-facecard" style={style}>
      <b>{whoLine(agent.roleKey, agent.name)}</b>
      <span className="cc-facecard-line">{agent.line && agent.line !== "No work yet" ? agent.line : word === "ready" ? "Ready for work" : word}</span>
      <span className="cc-facecard-acts">
        {/* TALK OPENS THE DOCK (re-check), which makes room for itself, not an overlay. */}
        <button type="button" className="cc-btn sm primary" data-testid="cc-face-talk"
                onClick={() => { onClose(); openDockAbout({ subject: null, question: "", role: agent.name, roleKey: agent.roleKey, history: "push" }); }}>
          Talk
        </button>
        <Go className="cc-btn sm" patch={{ area: "workforce", role: agent.roleKey, view: null }} orgId={orgId}
            testId="cc-face-open">Open ↗</Go>
      </span>
    </div>, document.body);
}
