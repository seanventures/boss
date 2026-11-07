"use client";

/**
 * WHAT AN ANSWER CARRIES BESIDES ITS WORDS (screen 09; engine turn_cards): its leading path as
 * flow chips, the records it named as chips that open them (PR · run · file · doc · issue ·
 * environment · a console place), and — when the turn offered to start work — the "New work"
 * card: what, ≈ cost and ≈ time, the plan it would run, and Start / Edit / Not now.
 *
 * One component for every surface the agent answers in (the Ask BOSS dock and the project's
 * captain drawer are the same AgentChat). Start is THE start door (POST /v1/projects/{id}/work)
 * with the card's own body; the card then records what the person chose
 * (POST /v1/engine/agent/proposal) so a reopened thread shows "Started" and the next turn knows.
 */
import React, { useState } from "react";
import {
  ArrowRight, BookOpen, Bug, FileCode, FlowArrow, GitPullRequest, Globe, Hammer, Key, Play, Sparkle, Link as LinkIcon,
} from "@phosphor-icons/react";

import { AgentAvatar } from "./agent-avatar";
import { useWrapMarks } from "../lib/wrap-flow";
import { pxHref, pxWrite, startWork, toWorkKind } from "../lib/project-experience";

export type Citation = { kind: string; ref: string; label: string; href: string; external?: boolean; title?: string };
export type ProposalStep = { key?: string; label: string; type?: string; role_key?: string | null; icon?: string;
  agent?: { name?: string; role_key?: string } | null };
export type Proposal = {
  kind: "new_work"; title: string; text: string; work_kind?: string;
  estimate?: { usd?: number | null; minutes?: number | null; basis?: string; runs?: number } | null;
  steps?: ProposalStep[]; workflow?: string | null;
  start: { href: string; method: string; body: Record<string, unknown> };
  edit_href: string; offer_id?: string | null; state?: "open" | "started" | "dismissed";
  run_id?: string; href?: string;
};

const obj = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const str = (v: unknown) => (typeof v === "string" ? v : "");

/** The server's citations, defensively (an older engine sends none). */
export function citationsOf(raw: unknown): Citation[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(obj).filter((c) => str(c.href) && str(c.label)).map((c) => ({
    kind: str(c.kind) || "doc", ref: str(c.ref), label: str(c.label), href: str(c.href),
    external: Boolean(c.external), title: str(c.title) || undefined,
  }));
}
export function proposalOf(raw: unknown): Proposal | null {
  const p = obj(raw);
  if (p.kind !== "new_work" || !str(p.title) || !obj(p.start).href) return null;
  return p as unknown as Proposal;
}

const CITE_ICON: Record<string, React.ReactNode> = {
  pr: <GitPullRequest size={12} aria-hidden />, run: <FlowArrow size={12} aria-hidden />, file: <FileCode size={12} aria-hidden />,
  doc: <BookOpen size={12} aria-hidden />, issue: <Bug size={12} aria-hidden />, env: <Globe size={12} aria-hidden />,
  place: <Key size={12} aria-hidden />,
};

/** The records an answer named, each a chip that opens it (↗ for GitHub). */
export function CitationChips({ items, testId = "agent-cites" }: { items: Citation[]; testId?: string }) {
  if (!items.length) return null;
  return (
    <div className="ac-cites" data-testid={testId} aria-label="Sources">
      {items.map((c) => (
        <a key={`${c.kind}:${c.ref}`} className="ac-cite" data-kind={c.kind} title={c.title || c.label}
           href={c.external ? c.href : pxHref(c.href)} data-testid={`agent-cite-${c.kind}`}
           {...(c.external ? { target: "_blank", rel: "noreferrer noopener" } : {})}>
          {CITE_ICON[c.kind] ?? <LinkIcon size={12} aria-hidden />}<span>{c.label}</span>{c.external ? <span aria-hidden>↗</span> : null}
        </a>
      ))}
    </div>
  );
}

/** A path as chips with wires: unbreakable steps, no arrow left at a line's end. */
export function AnswerFlow({ nodes, testId, label = "Path" }: {
  nodes: { label: string; state?: string; icon?: React.ReactNode }[]; testId?: string; label?: string;
}) {
  const ref = useWrapMarks<HTMLDivElement>([nodes.map((n) => `${n.label}:${n.state}`).join("|")]);
  if (!nodes.length) return null;
  return (
    <div className="agent-flow" role="list" aria-label={label} data-testid={testId} ref={ref}>
      {nodes.map((n, k) => (
        <span key={`${n.label}-${k}`} className="agent-flow-step" data-flow-step>
          {k ? <span className="agent-flow-w" aria-hidden data-s={nodes[k - 1].state === "done" ? "done" : undefined} /> : null}
          <span className="agent-flow-n" role="listitem" data-s={n.state}>{n.icon}{n.state === "done" && !n.icon ? "✓ " : ""}{n.label}</span>
        </span>
      ))}
    </div>
  );
}

const minutes = (m: number) => (m >= 90 ? `${Math.round(m / 60)}h` : `${m}m`);
const dollars = (v: number) => (v >= 1 ? `$${Math.round(v)}` : `$${v.toFixed(2)}`);

/** "New work · VIP customer flag": ≈ cost, ≈ time, the plan strip and Start / Edit / Not now. */
export function ProposalCard({ projectId, threadId, messageId, proposal, onOutcome }: {
  projectId: string; threadId: string | null; messageId: string | null; proposal: Proposal;
  onOutcome?: (p: Proposal) => void;
}) {
  const [card, setCard] = useState<Proposal>(proposal);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState("");
  const est = card.estimate ?? null;
  const record = async (outcome: "started" | "dismissed", extra: Record<string, unknown> = {}) => {
    if (!threadId || !messageId) return;
    await pxWrite("/v1/engine/agent/proposal", {
      project_id: projectId, thread_id: threadId, message_id: messageId, outcome, ...extra,
    }, outcome === "started" ? "Start" : "Not now");
  };
  const start = async () => {
    if (busy) return;
    setBusy(true); setSaid("");
    const b = card.start.body;
    const res = await startWork(projectId, toWorkKind(b.kind), str(b.text) || card.text, {
      ...(str(b.title) ? { title: str(b.title) } : {}), ...(str(b.source_ref) ? { source_ref: str(b.source_ref) } : {}),
    });
    setBusy(false);
    if (!res.ok) {
      // A start the project isn't ready for: its sentence, and Edit opens the composer with
      // the readiness checklist and Start anyway.
      setSaid(res.error ?? "It didn't start.");
      return;
    }
    const body = obj(res.body);
    const runId = str(body.run_id);
    const href = str(body.href);
    const next: Proposal = { ...card, state: "started", ...(runId ? { run_id: runId } : {}), ...(href ? { href } : {}) };
    setCard(next); onOutcome?.(next);
    await record("started", { ...(runId ? { run_id: runId } : {}), ...(href ? { href } : {}) });
  };
  const dismiss = async () => {
    const next: Proposal = { ...card, state: "dismissed" };
    setCard(next); onOutcome?.(next);
    await record("dismissed");
  };
  if (card.state === "dismissed") return null;
  const steps = card.steps ?? [];
  return (
    <div className="ac-act" data-testid="agent-proposal-card" data-state={card.state ?? "open"}>
      <div className="ac-act-h">
        <span className="ac-ico" aria-hidden><Sparkle size={14} weight="fill" /></span>
        <b className="ac-act-t" title={card.text}>New work · {card.title}</b>
        <span className="ac-spacer" />
        {est?.usd ? <span className="ac-cost" title={`Cost ${est.basis ? `· ${est.basis}` : ""}`} data-testid="agent-proposal-usd">≈ {dollars(est.usd)}</span> : null}
        {est?.minutes ? <span className="ac-cost" title={`Time to the pull request ${est.basis ? `· ${est.basis}` : ""}`} data-testid="agent-proposal-time">≈ {minutes(est.minutes)}</span> : null}
      </div>
      {steps.length ? (
        <AnswerFlow testId="agent-proposal-plan" label="Plan" nodes={steps.map((s) => {
          const role = s.role_key || s.agent?.role_key || "";
          const you = s.type === "you" || s.key === "approve";
          return {
            label: s.label, state: you ? "you" : s.type === "agent" ? "agent" : "todo",
            icon: role && s.type === "agent" ? <AgentAvatar agent={role} size={16} aiBadge={false} tip={s.agent?.name || s.label} />
              : s.key === "build" || s.icon === "hammer" ? <Hammer size={11} aria-hidden />
              : s.key === "pr" || s.icon === "pr" ? <GitPullRequest size={11} aria-hidden /> : null,
          };
        })} />
      ) : null}
      {card.state === "started" ? (
        <div className="ac-act-row" data-testid="agent-proposal-started">
          <span className="ac-done">✓ Started</span>
          {card.href ? <a className="ac-link" href={pxHref(card.href)} data-testid="agent-proposal-open">Open work <ArrowRight size={11} aria-hidden /></a> : null}
        </div>
      ) : (
        <div className="ac-act-row">
          <button type="button" className="ac-btn" data-kind="primary" disabled={busy} onClick={() => void start()} data-testid="agent-proposal-start">
            <Play size={12} weight="fill" aria-hidden /> {busy ? "Starting…" : "Start"}</button>
          <a className="ac-btn" href={pxHref(card.edit_href)} data-testid="agent-proposal-edit" title="Open it in New work to change the words, the kind or the workflow">Edit</a>
          <button type="button" className="ac-btn" data-kind="ghost" onClick={() => void dismiss()} data-testid="agent-proposal-dismiss">Not now</button>
        </div>
      )}
      {said ? <p className="ac-said" role="status" data-testid="agent-proposal-said">{said}</p> : null}
    </div>
  );
}
