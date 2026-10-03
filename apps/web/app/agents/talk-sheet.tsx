"use client";

/**
 * Talk to one agent (spec §0.2 A.1, §3.10): a sheet over whatever the operator was looking at,
 * headed by the agent's face — thinking while its turn is in flight, speaking while its words
 * arrive — its name, the AI badge and its line. A `plan` is the same sheet with a goal: it
 * scopes the work and ends in a start card (§0.2 A.2).
 */

import React, { useEffect, useMemo, useRef } from "react";

import { useConversation, type ConversationView } from "../../lib/agent-converse";
import {
  AiBadge, Composer, ConverseFace, ProblemNote, SeatBook, TurnItem, seatName, useSeat,
  useStickToBottom,
} from "./converse-parts";
import type { SeatInfo } from "../../lib/agent-converse";

export function TalkSheet({ projectId, actor, conversationId, greet, book, onClose, onPlan, onNew }: {
  projectId: string; actor: string; conversationId: string;
  /** A new talk: let the agent open it (a greeting through its composed prompt, never canned). */
  greet?: boolean;
  book: Map<string, SeatInfo>;
  onClose: () => void;
  /** "Plan it": continue this talk as a plan with the same agent and context. */
  onPlan?: (roleKey: string, c: ConversationView) => void;
  onNew?: (roleKey: string) => void;
}) {
  const live = useConversation(projectId, conversationId, actor);
  const { conversation, busy, speaking, thinking, problem, setProblem } = live;
  const roleKey = conversation?.seats[0] ?? "";
  const merged = useMemo(() => {
    const m = new Map(book);
    for (const s of conversation?.seatInfo ?? []) m.set(s.roleKey, s);
    return m;
  }, [book, conversation?.seatInfo]);
  const greeted = useRef(false);
  useEffect(() => {
    if (!greet || greeted.current || !conversation || conversation.turns.length) return;
    greeted.current = true;
    void live.send("");
  }, [greet, conversation, live]);
  const scroller = useStickToBottom(conversation?.turns.map((t) => t.content.length).join(","));

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const state = speaking ? "speaking" : thinking.length ? "thinking" : "idle";
  const plan = conversation?.kind === "plan";
  const lastAgent = [...(conversation?.turns ?? [])].reverse().find((t) => t.speakerKind === "agent");
  const spoke = (conversation?.turns ?? []).some((t) => t.speakerKind === "person");

  return (
    <SeatBook.Provider value={merged}>
      <aside className="ag-talk" role="dialog" aria-modal="false" data-testid="ag-talk"
             data-kind={conversation?.kind ?? ""} aria-label={conversation?.title ?? "Talk"}>
        <TalkHead roleKey={roleKey} state={state} conversation={conversation} plan={plan}
                  onClose={onClose}
                  onPlan={onPlan && conversation && !plan ? () => onPlan(roleKey, conversation) : undefined}
                  onNew={onNew && roleKey ? () => onNew(roleKey) : undefined} />
        <div className="ag-talk-body" ref={scroller} data-testid="ag-talk-body">
          {(conversation?.turns ?? []).map((turn) => (
            <TurnItem key={turn.turnId} turn={turn} live={busy && turn.status === "streaming"}
                      onStart={live.start} onDismiss={live.dismiss} />
          ))}
          {!conversation ? <span className="ag-muted">Opening…</span> : null}
        </div>
        {problem ? <ProblemNote problem={problem} onClose={() => setProblem(null)} /> : null}
        <Composer
          placeholder={plan ? "What do you want to start?" : `Message ${seatName(merged.get(roleKey) ?? null).name}`}
          disabled={busy || !conversation || conversation.status === "closed"}
          autoFocus
          onSend={(message) => void live.send(message)}
          onStop={busy ? live.stop : undefined}
          extra={plan ? (
            <button type="button" className="ag-btn" data-testid="ag-propose"
                    disabled={busy || !spoke}
                    title={spoke ? "Turn this into a start you can review" : "Say what you want first"}
                    onClick={() => void live.propose()}>
              {lastAgent?.kind === "proposal" ? "Propose again" : "Propose"}
            </button>
          ) : null} />
      </aside>
    </SeatBook.Provider>
  );
}

function TalkHead({ roleKey, state, conversation, plan, onClose, onPlan, onNew }: {
  roleKey: string; state: "idle" | "thinking" | "speaking"; conversation: ConversationView | null;
  plan: boolean; onClose: () => void; onPlan?: () => void; onNew?: () => void;
}) {
  const seat = useSeat(roleKey);
  const who = seatName(seat);
  const about = conversation?.context.workflowRunId;
  return (
    <header className="ag-talk-head">
      {roleKey ? <ConverseFace roleKey={roleKey} size={48} state={state} badge /> : null}
      <div className="ag-talk-who">
        <span className="ag-talk-name">
          <strong>{who.name}</strong>
          {who.role ? <span className="ag-muted">{who.role}</span> : null}
          <AiBadge />
          {plan ? <span className="ag-chip ag-chip-accent">Plan</span> : null}
        </span>
        {seat?.tagline ? <span className="ag-tagline" title={seat.tagline}>{seat.tagline}</span> : null}
        {about ? (
          <span className="ag-chip ag-about" title={about}>
            About: {conversation?.context.stepId ?? "run"}
          </span>
        ) : null}
      </div>
      <span className="ag-head-acts">
        {onPlan ? <button type="button" className="ag-btn" data-testid="ag-plan-it" onClick={onPlan}
                          title="Scope a piece of work, then start it">Plan it</button> : null}
        {onNew ? <button type="button" className="ag-icon-btn" onClick={onNew} title="New talk"
                         aria-label="New talk">＋</button> : null}
        <button type="button" className="ag-icon-btn" onClick={onClose} aria-label="Close" title="Close">×</button>
      </span>
    </header>
  );
}
