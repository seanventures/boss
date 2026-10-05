"use client";

/**
 * THE HERO (cc v2): the one question — what should your team build next? — beside the team
 * that will answer it. Left: the systems word and when work last happened, the headline, the
 * composer (AskBossHero, laid out as `hero`), and the proof row. Right: the nine teams as tiles
 * of real faces; a face that waits on you wears an amber ring, a working one glows.
 *
 * Every number links to where it is counted: apps and PRs to the Factory, agents to Agent
 * Studio, spend to Budget. The faces are AgentAvatar, the same face on every surface.
 */
import React from "react";

import type { BoardAgent, TeamSummary } from "../../lib/agents-live-board";
import type { CcApp } from "../../lib/command-center-apps";
import type { SystemDot } from "../../lib/command-center-view";
import { AgentAvatar } from "../agent-avatar";
import { nameOf } from "../agents/run-face";
import { linkTo, navigate, plainClick } from "../agents/live-util";

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

/** The eyebrow's systems word: what the Systems dots say, in two or three words. */
export function systemsWord(systems: SystemDot[], reachable: boolean): { text: string; tone: "green" | "amber" | "red" | "grey" } {
  if (!reachable) return { text: "Can't reach BOSS", tone: "grey" };
  const red = systems.filter((s) => s.tone === "red");
  const amber = systems.filter((s) => s.tone === "amber");
  if (red.length) return { text: red.length === 1 ? `${red[0].word} needs a look` : `${red.length} systems need a look`, tone: "red" };
  if (amber.length) return { text: amber.length === 1 ? `${amber[0].word}: ${amber[0].state}` : `${amber.length} systems slow`, tone: "amber" };
  return { text: "All systems OK", tone: "green" };
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
        <b>{n ? n.agents : dash}</b><span>agents · {n ? n.teams : dash} teams</span>
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
export function TeamGrid({ teams, agents, waiting, orgId, still }: {
  teams: TeamSummary[]; agents: BoardAgent[];
  /** Agents an open item waits on you for (an approval they raised, a plan they drafted). */
  waiting: Set<string>;
  orgId?: string | null;
  /** Motion off: faces hold still. */
  still: boolean;
}) {
  const byKey = new Map(agents.map((a) => [a.roleKey, a]));
  const working = agents.filter((a) => a.state === "working" || a.state === "in_panel").length;
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
                 style={{ ["--hue" as string]: TEAM_HUE[t.key] ?? "#5B8CFF" }}>
              <span className="cc-tile-faces">
                {members.slice(0, 8).map((a) => {
                  const who = nameOf(a.roleKey);
                  const isWorking = a.state === "working" || a.state === "in_panel";
                  const isWaiting = waiting.has(a.roleKey);
                  const word = isWaiting ? "waiting on you" : isWorking ? "working" : a.state === "stuck" ? "stuck" : "ready";
                  // A face is a door to that agent (its passport, where Talk is).
                  return (
                    <Go key={a.roleKey} className="cc-face" patch={{ area: "workforce", role: a.roleKey, view: null }}
                        orgId={orgId} title={`${who.name} · ${a.name} · ${word}`} ariaLabel={`${who.name}, ${a.name}, ${word}`}
                        testId="cc-face">
                      <span className="cc-face-in" data-working={isWorking ? "true" : undefined}
                            data-waiting={isWaiting ? "true" : undefined}>
                        <AgentAvatar agent={a.roleKey} size={32} aiBadge={false}
                                     state={still ? "idle" : isWorking ? "working" : "idle"} />
                      </span>
                    </Go>
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
