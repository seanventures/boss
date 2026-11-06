"use client";

/**
 * ONBOARDING IN PROGRESS (BOSSAPP-914 screen 10, contract §8): the project page while Helm onboards.
 *
 *   Discover → Ready for work → Propose backlog → Work it → Deploy        (the stage rail)
 *   Now: <what the agent is doing>                                         (one line, live)
 *   Discovered (radial diagram) · Ready for work (checklist: who does it) · Backlog proposed
 *
 * The agent does every checklist item it can; the ones only a person can do are amber with their
 * one action, and the same items sit in the project's Needs-you bar.
 */
import React, { useState } from "react";
import {
  ArrowsClockwise, Check, Compass, Eye, ListChecks, ListNumbers, MagnifyingGlass, Play, Rocket, Sparkle,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { projectHref, pxHref, startOnboarding, startWork, type Node, type Onboarding } from "../../lib/project-experience";
import { Face, Ico, Said, STATE_WORD } from "./kit";

const STAGE_ICON: Record<string, React.ReactNode> = {
  discover: <MagnifyingGlass size={16} weight="bold" />, ready: <ListChecks size={16} weight="bold" />, backlog: <ListNumbers size={16} weight="bold" />,
  first_work: <ArrowsClockwise size={16} weight="bold" />, deploy: <Rocket size={16} weight="bold" />,
};

/** Helm in the middle, what it found around it: each a node that turns green when found. */
export function DiscoverDiagram({ nodes }: { nodes: Node[] }) {
  const n = Math.max(nodes.length, 1);
  const W = 420, H = 210, cx = W / 2, cy = H / 2;
  const pts = nodes.map((_, i) => {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / n + (n % 2 ? 0 : Math.PI / n);
    return { x: cx + Math.cos(a) * 160, y: cy + Math.sin(a) * 74 };
  });
  return (
    <div style={{ position: "relative" }} data-testid="px-discover">
      <svg className="px-radial" viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={`Discovered: ${nodes.map((x) => `${x.label} ${STATE_WORD[x.state]}`).join(", ")}`}>
        {pts.map((p, i) => (
          <line key={`e${i}`} x1={cx} y1={cy} x2={p.x} y2={p.y} stroke={nodes[i].state === "done" ? "var(--green)" : "var(--teal)"}
                strokeWidth={1.6} strokeDasharray={nodes[i].state === "done" ? undefined : "5 4"} opacity={0.8} />
        ))}
        {pts.map((p, i) => (
          <g key={`n${i}`}>
            <circle cx={p.x} cy={p.y} r={11} fill="var(--surface)" stroke={nodes[i].state === "done" ? "var(--green)" : nodes[i].state === "failed" ? "var(--red)" : "var(--teal)"} strokeWidth={2} />
            {nodes[i].state === "done" ? <path d={`M${p.x - 4} ${p.y} l3 3 l5 -6`} stroke="var(--green)" strokeWidth={2} fill="none" /> : null}
            <text x={p.x} y={p.y + 25} textAnchor="middle">{nodes[i].label}</text>
            {nodes[i].sub ? <text x={p.x} y={p.y + 38} textAnchor="middle" style={{ fill: "var(--faint)", fontSize: 10 }}>{nodes[i].sub}</text> : null}
          </g>
        ))}
        <circle cx={cx} cy={cy} r={24} fill="color-mix(in srgb, var(--accent) 18%, var(--surface))" stroke="var(--accent)" strokeWidth={2} />
      </svg>
      <span style={{ position: "absolute", left: "50%", top: "50%", transform: "translate(-50%, -50%)" }} aria-hidden>
        <Face agent={{ name: "Helm", role_key: "engineering_captain" }} size={36} state="working" />
      </span>
    </div>
  );
}

export function OnboardingView({ projectId, ob }: { projectId: string; ob: Onboarding }) {
  const [said, setSaid] = useState("");
  const ready = ob.checklist.filter((c) => c.state === "done").length;
  const start = async () => {
    if (!ob.first_work) return;
    const r = await startWork(projectId, ob.first_work.source === "bug" ? "bug" : "ticket", ob.first_work.label);
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Started — it's on Work.") : (r.error ?? "It didn't start."));
  };
  const restart = async () => {
    const r = await startOnboarding(projectId);
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Onboarding restarted.") : (r.error ?? "It didn't restart."));
  };
  return (
    <div className="px-stack" data-testid="px-onboarding">
      <section className="px-card">
        <div className="px-ob" role="list" aria-label="Onboarding stages" data-testid="px-ob-stages">
          {ob.stages.map((st) => (
            <div key={st.key} className="px-ob-s" data-s={st.state} role="listitem" aria-label={`${st.label}: ${STATE_WORD[st.state]}`}>
              <span className="px-jr-shape" aria-hidden>{st.state === "done" ? <Check size={16} weight="bold" /> : STAGE_ICON[st.key] ?? <Sparkle size={16} />}</span>
              <span className="px-ob-cap">{st.label}</span>
              <span className="px-ob-meta">{st.sub}</span>
            </div>
          ))}
        </div>
        {ob.now ? (
          <div className="px-now" style={{ marginTop: 14 }} data-testid="px-ob-now" aria-live="polite">
            <Face agent={{ name: "Helm", role_key: "engineering_captain" }} size={22} state="working" />
            <b>Now</b><span>{ob.now}</span>
            <span className="px-spacer" />
            <a href={pxHref("?area=agent_ops")} data-testid="px-ob-watch"><Eye size={12} /> Watch</a>
          </div>
        ) : null}
        {ob.state === "failed" ? (
          <div className="px-row" style={{ marginTop: 10 }}>
            <span className="px-tag" data-tone="red">Onboarding stopped</span>
            <GatedButton permission="project:update" projectId={projectId} className="px-btn" data-size="s" data-testid="px-ob-restart" onClick={() => void restart()}>
              <ArrowsClockwise size={12} /> Try again</GatedButton>
          </div>
        ) : null}
      </section>
      <div className="px-grid px-g3">
        <section className="px-card" aria-labelledby="px-ob-d">
          <div className="px-card-h"><Ico icon={MagnifyingGlass} c="teal" /><h2 id="px-ob-d">Discovered</h2>
            {ob.stages.find((x) => x.key === "discover")?.state === "done" ? <span className="px-tag" data-tone="green">done</span> : <span className="px-tag" data-tone="blue">live</span>}</div>
          <DiscoverDiagram nodes={ob.discover.nodes} />
          <a href={projectHref(projectId, { tab: "knowledge" })} data-testid="px-ob-dossier">Open dossier →</a>
        </section>
        <section className="px-card" aria-labelledby="px-ob-r" style={{ borderColor: "color-mix(in srgb, var(--teal) 40%, transparent)" }}>
          <div className="px-card-h"><Ico icon={ListChecks} c="teal" /><h2 id="px-ob-r">Ready for work</h2>
            <span className="px-tag" data-tone="blue">{ready}/{ob.checklist.length}</span></div>
          <div data-testid="px-ob-checklist">
            {ob.checklist.map((c) => {
              const you = c.owner === "you" && c.state !== "done";
              return (
                <div key={c.key} className="px-ck-i" data-s={you ? "you" : c.state} data-testid={`px-ob-item-${c.key}`}>
                  <span className="px-ck-b" aria-hidden>{c.state === "done" ? <Check size={11} weight="bold" /> : you ? "!" : ""}</span>
                  <span className="px-ck-t">{c.label}<small>{c.detail}</small><span className="px-sr">{you ? "needs you" : STATE_WORD[c.state]}</span></span>
                  {you && c.action ? <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(c.action.href)} data-testid={`px-ob-act-${c.key}`}>{c.action.label}</a>
                    : <Face agent={{ name: c.owner === "you" ? "You" : "Helm", role_key: c.owner === "you" ? "" : "engineering_captain" }} size={20} />}
                </div>
              );
            })}
          </div>
        </section>
        <section className="px-card" aria-labelledby="px-ob-b">
          <div className="px-card-h"><Ico icon={ListNumbers} c="violet" /><h2 id="px-ob-b">Backlog proposed</h2>
            <Face agent={{ name: "Marshal", role_key: "chief_of_staff" }} size={20} /><small className="px-faint">by Marshal</small></div>
          {ob.backlog.length ? ob.backlog.slice(0, 4).map((b, i) => (
            <div key={b.id || i} className="px-li">
              <span className="px-rank">{i + 1}</span>
              <span><b>{b.label}</b><small>{b.source}</small></span>
              <span className="px-tag">{/bug|ci|issue/i.test(b.source) ? "bug" : "feature"}</span>
            </div>
          )) : <p className="px-faint">Marshal ranks open issues, failing CI and TODOs once Helm has read the repo.</p>}
          {ob.first_work ? (
            <div className="px-ny" style={{ marginTop: 10, background: "color-mix(in srgb, var(--px-violet) 8%, var(--surface))", borderColor: "color-mix(in srgb, var(--px-violet) 40%, transparent)" }}
                 data-testid="px-ob-first">
              <Sparkle size={16} aria-hidden />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="px-ny-t">{ob.first_work.label}</div>
                <small className="px-faint">{[ob.first_work.estimate_min !== null ? `≈ ${ob.first_work.estimate_min} min` : "", ob.first_work.estimate_usd !== null ? `≈ $${ob.first_work.estimate_usd.toFixed(2)}` : ""].filter(Boolean).join(" · ")}</small>
              </div>
              <GatedButton permission="workflow_run:start" projectId={projectId} className="px-btn" data-kind="primary" data-size="s" data-testid="px-ob-start"
                           onClick={() => void start()}><Play size={11} weight="fill" /> Start</GatedButton>
            </div>
          ) : null}
          <div className="px-row" style={{ marginTop: 10 }}>
            <a className="px-btn" data-size="s" href={projectHref(projectId, { tab: "roadmap" })} data-testid="px-ob-roadmap"><Compass size={12} /> See the roadmap</a>
          </div>
          <Said text={said} />
        </section>
      </div>
    </div>
  );
}
