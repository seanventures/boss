"use client";

/**
 * THE ROOM (spec §0.2 A.3): 2-5 specialists at one table. The operator asks; each seat answers
 * in turn, in its own voice, seeing what the seats before it said; "Let them reply" gives each
 * one more turn to answer another seat; an @mention asks one seat directly; Summarize has the
 * lead close it with decisions, dissent and next actions that start a run or open a decision.
 *
 * It must feel like a live room, and only because it IS one: a seat rings `thinking` from the
 * moment its turn starts until its first word, `speaking` while its words arrive, and `done`
 * once it has spoken this round. Seats still to speak say "Up next". The seat whose turn it is
 * holds the floor (`data-turn`): its face lifts and glows on a lit floor, and the face itself
 * plays the kit's speaking or thinking motion. Nothing moves otherwise; under reduced motion
 * the floor and glow stay, still.
 *
 * THE TITLE IS WHAT THE ROOM IS ABOUT, and it does not change under the operator: the first
 * question asked (clipped, the whole of it in the tooltip). Who is in the room is shown as
 * faces beside it, never as a title that a first turn then swaps out.
 *
 * Layout: the table stays put and the transcript scrolls inside its own pane — the page never
 * scrolls (the operator's note on the Run view, applied here from the start).
 */

import { GatedButton } from "../../components/gated";
import React, { useEffect, useMemo, useRef, useState } from "react";
import { RateScope } from "../improve/rate-it";

import {
  mentionsIn, useConversation, type ConversationView, type SeatInfo, type SummaryView, type TurnView,
} from "../../lib/agent-converse";

/**
 * The room's title, on one line: its first question once one is asked; before that, the name
 * it opened with (a preset's label, or who is seated), never a blank "New room".
 */
export function roomTitle(turns: TurnView[], pending?: string, opened?: string): { text: string; full: string } {
  const first = turns.find((t) => t.speakerKind === "person" && t.content.trim())?.content ?? pending ?? opened ?? "";
  const line = first.trim().replace(/\s+/g, " ");
  if (!line) return { text: "New room", full: "" };
  return { text: line.length > 72 ? `${line.slice(0, 70)}…` : line, full: line };
}
import type { AvatarState } from "../../lib/agent-world-types";
import {
  AiBadge, Composer, ConverseFace, ProblemNote, ProposalCard, SeatBook, StopButton, TurnItem, humanise, seatName,
  useSeat, useStickToBottom,
} from "./converse-parts";

export function RoomView({ projectId, actor, conversationId, question, book, onClose }: {
  projectId: string; actor: string; conversationId: string;
  /** The first question, sent once the room is open. */
  question?: string;
  book: Map<string, SeatInfo>;
  onClose: () => void;
}) {
  const live = useConversation(projectId, conversationId, actor);
  const { conversation, busy, answering, speaking, thinking, queue, problem, setProblem } = live;
  const [to, setTo] = useState<string[]>([]);
  const [debate, setDebate] = useState(false);
  const asked = useRef(false);
  useEffect(() => {
    if (!question || asked.current || !conversation || conversation.turns.length) return;
    asked.current = true;
    void live.send(question);
  }, [question, conversation, live]);

  const merged = useMemo(() => {
    const m = new Map(book);
    for (const s of conversation?.seatInfo ?? []) m.set(s.roleKey, s);
    return m;
  }, [book, conversation?.seatInfo]);
  const turns = conversation?.turns ?? [];
  const scroller = useStickToBottom(turns.map((t) => t.content.length).join(","));

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = previous; };
  }, [onClose, busy]);

  // Who has spoken since the operator last did: they wear `done` until the next question.
  const lastPerson = turns.map((t) => t.speakerKind).lastIndexOf("person");
  const spokeThisRound = new Set(turns.slice(lastPerson + 1)
    .filter((t) => t.speakerKind === "agent" && t.status === "complete").map((t) => t.roleKey));
  const stateOf = (k: string): AvatarState => speaking === k ? "speaking"
    : thinking.includes(k) ? "thinking"
    : busy && spokeThisRound.has(k) ? "done"
    : (merged.get(k)?.lifecycle ?? "active") !== "active" ? "retired" : "idle";
  const latest = (k: string) => [...turns].reverse().find((t) => t.roleKey === k && t.speakerKind === "agent");
  const byTurn = new Map(turns.map((t) => [t.turnId, t]));
  const nobodySpoke = !turns.some((t) => t.speakerKind === "agent" && t.status === "complete");

  const seats = conversation?.seats ?? [];
  // Who each seat is, with the callsign seatName shows (the @ menu inserts it; it resolves back).
  const named = seats.map((k) => {
    const info = merged.get(k) ?? { roleKey: k, name: humanise(k), lifecycle: "active" };
    const who = seatName(info);
    return { ...info, callsign: who.role ? who.name : null };
  });
  const title = roomTitle(turns, question, conversation?.title);
  const toggle = (k: string) => setTo((all) => all.includes(k) ? all.filter((x) => x !== k) : [...all, k]);

  const send = (message: string) => {
    const addressed = Array.from(new Set([...to, ...mentionsIn(message, seats, named)]));
    void live.send(message, { to: addressed, rounds: debate ? 2 : 1 });
  };

  return (
    <SeatBook.Provider value={merged}>
      <div className="ag-room" role="dialog" aria-modal="true" data-testid="ag-room"
           aria-label={title.full || "Room"}>
        <header className="ag-room-head">
          <strong className="ag-room-title" data-testid="ag-room-title" data-user-text
                  data-empty={title.full ? undefined : "1"} title={title.full || undefined}>
            {conversation ? title.text : "Opening…"}
          </strong>
          <span className="ag-room-members ag-stack" data-testid="ag-room-members"
                title={named.map((s) => seatName(s).name).join(" · ")}>
            {seats.map((k) => <ConverseFace key={k} roleKey={k} size={24} state={stateOf(k)} />)}
          </span>
          <span className="ag-spacer" />
          {answering ? <StopButton onStop={live.stop} /> : null}
          <GatedButton permission="meeting:convene" type="button" className="ag-btn" data-testid="ag-summarize"
                  disabled={busy || nobodySpoke}
                  title={nobodySpoke ? "Ask the room something first" : "The lead sums up: decisions and next steps"}
                  onClick={() => void live.summarize()}>Summarize</GatedButton>
          <GatedButton permission="meeting:convene" type="button" className="ag-btn" disabled={busy || nobodySpoke}
                  title="Turn the discussion into a start you can review"
                  onClick={() => void live.propose()}>Propose</GatedButton>
          <button type="button" className="ag-icon-btn" onClick={onClose} aria-label="Close" title="Close">×</button>
        </header>

        <div className="ag-table" data-testid="ag-table" data-busy={answering ? "1" : undefined}>
          {seats.map((k, i) => {
            const state = stateOf(k);
            return (
              <Seat key={k} roleKey={k} index={i} count={seats.length} state={state}
                    lead={conversation?.leadRoleKey === k}
                    turn={state === "speaking" || state === "thinking"}
                    next={answering && queue[0] !== k && queue.includes(k) && state === "idle"}
                    line={speaking === k ? latest(k)?.content ?? "" : ""}
                    selected={to.includes(k)}
                    onToggle={() => toggle(k)} />
            );
          })}
        </div>

        <div className="ag-room-feed" ref={scroller} data-testid="ag-room-feed" aria-live="off">
          <RateScope.Provider value={{ projectId, actor }}>
          {turns.length === 0 ? <span className="ag-muted ag-room-empty">Ask the room</span> : null}
          {turns.map((turn) => turn.kind === "summary" && turn.summary ? (
            <SummaryCard key={turn.turnId} turn={turn} summary={turn.summary} onStart={live.start}
                         onDecision={live.openDecision} />
          ) : (
            <TurnItem key={turn.turnId} turn={turn} live={busy && turn.status === "streaming"}
                      onStart={live.start} onDismiss={live.dismiss}
                      replyTo={turn.replyToTurnId ? seatName(merged.get(byTurn.get(turn.replyToTurnId)?.roleKey ?? "") ?? null).name : null} />
          ))}
          </RateScope.Provider>
        </div>

        {problem ? <ProblemNote problem={problem} onClose={() => setProblem(null)} /> : null}
        <div className="ag-room-compose">
          <div className="ag-to-row" role="group" aria-label="Who answers">
            <span className="ag-muted">To</span>
            <button type="button" className="ag-chip ag-chip-btn" aria-pressed={!to.length}
                    onClick={() => setTo([])}>Everyone</button>
            {seats.map((k) => (
              <ToToggle key={k} roleKey={k} on={to.includes(k)} onToggle={() => toggle(k)} />
            ))}
          </div>
          <Composer placeholder="Ask the room · @ to mention" seats={named}
                    disabled={busy || !conversation || conversation.status === "closed"} autoFocus
                    onSend={send}
                    onMention={(k) => setTo((all) => all.includes(k) ? all : [...all, k])}
                    extra={(
                      <label className="ag-toggle" title="Each answers another seat once more">
                        <input type="checkbox" checked={debate} onChange={(e) => setDebate(e.target.checked)}
                               data-testid="ag-debate" />
                        <span>Let them reply</span>
                      </label>
                    )} />
        </div>
      </div>
    </SeatBook.Provider>
  );
}

/** The stage face: big enough to read a face on (≥72px), bigger while it holds the floor. */
export const SEAT_FACE = 80;

function Seat({ roleKey, index, count, state, lead, turn, next, line, selected, onToggle }: {
  roleKey: string; index: number; count: number; state: AvatarState; lead: boolean;
  /** This seat holds the floor: thinking or speaking now. */
  turn: boolean;
  next: boolean; line: string;
  selected: boolean; onToggle: () => void;
}) {
  const seat = useSeat(roleKey);
  const who = seatName(seat);
  return (
    <button type="button" className="ag-seat" data-state={state} data-testid={`ag-seat-${roleKey}`}
            data-turn={turn ? "1" : undefined} aria-pressed={selected}
            // The table is an arc: the middle seat sits furthest back, the ends come forward.
            style={{ ["--lift" as string]: `${Math.round(Math.abs(index - (count - 1) / 2) * 14)}px` }}
            title={selected ? "Asking only the selected seats" : `Ask ${who.name} directly`}
            onClick={onToggle}>
      {line ? <span className="ag-seat-line" data-user-text>{tail(line)}</span> : null}
      {state === "thinking" ? <span className="ag-seat-line ag-seat-dots" aria-hidden><i /><i /><i /></span> : null}
      <span className="ag-seat-floor" aria-hidden />
      <ConverseFace roleKey={roleKey} size={SEAT_FACE} state={state} badge
                    label={who.role ? `${who.name}, ${who.role}` : who.name} />
      <span className="ag-seat-name">{who.name}{lead ? <span className="ag-lead" title="Writes the summary">★</span> : null}</span>
      <span className="ag-seat-role" data-next={next ? "1" : undefined}>{next ? "Up next" : who.role ?? ""}</span>
    </button>
  );
}

/** The last words a seat is saying, as plain text for its bubble. */
function tail(line: string): string {
  const plain = line.replace(/[*_`#>]/g, "").replace(/\s+/g, " ").trim();
  return plain.length > 140 ? `…${plain.slice(-138)}` : plain;
}

function ToToggle({ roleKey, on, onToggle }: { roleKey: string; on: boolean; onToggle: () => void }) {
  const seat = useSeat(roleKey);
  return (
    <button type="button" className="ag-chip ag-chip-btn" aria-pressed={on} onClick={onToggle}
            data-testid={`ag-to-${roleKey}`}>
      <ConverseFace roleKey={roleKey} size={16} />{seatName(seat).name}
    </button>
  );
}

/** The lead's close: what the room concluded, who was for and against, and what happens next. */
function SummaryCard({ turn, summary, onStart, onDecision }: {
  turn: TurnView; summary: SummaryView;
  onStart: (id: string, o?: { startAnyway?: string[]; inputs?: Record<string, string> }) => Promise<string>;
  onDecision: (turnId: string, index: number) => Promise<string>;
}) {
  const by = seatName(useSeat(summary.byRoleKey));
  return (
    <section className="ag-summary" data-testid="ag-summary" aria-label="Room summary">
      <header className="ag-summary-head">
        <ConverseFace roleKey={summary.byRoleKey} size={24} />
        <span className="ag-eyebrow">Summary</span>
        <span className="ag-muted">by {by.name}</span>
        <AiBadge />
      </header>
      <div className="ag-text" data-user-text>{summary.text}</div>
      {summary.decisions.length ? (
        <ul className="ag-decisions">
          {summary.decisions.map((d) => (
            <li key={d.text}>
              <span data-user-text>{d.text}</span>
              <span className="ag-stance">
                {d.for.map((k) => <Stance key={`f${k}`} roleKey={k} word="For" />)}
                {d.against.map((k) => <Stance key={`a${k}`} roleKey={k} word="Against" />)}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {summary.nextActions.length ? (
        <ol className="ag-next">
          {summary.nextActions.map((a, i) => (
            <li key={`${turn.turnId}-${i}`} data-kind={a.kind}>
              <span className="ag-next-head">
                {a.ownerRoleKey ? <ConverseFace roleKey={a.ownerRoleKey} size={20} /> : null}
                <span data-user-text>{a.text}</span>
              </span>
              {a.kind === "start" && a.proposal ? (
                <ProposalCard proposal={a.proposal} onStart={onStart} compact />
              ) : a.kind === "decision" ? (
                <DecisionButton decisionId={a.decisionId} onOpen={() => onDecision(turn.turnId, i)} />
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

function Stance({ roleKey, word }: { roleKey: string; word: "For" | "Against" }) {
  const who = seatName(useSeat(roleKey));
  return (
    <span className="ag-chip" data-stance={word.toLowerCase()} title={`${who.name}: ${word.toLowerCase()}`}>
      <i className="ag-dot" aria-hidden />{word} · {who.name}
    </span>
  );
}

function DecisionButton({ decisionId, onOpen }: { decisionId?: string; onOpen: () => Promise<string> }) {
  const [opened, setId] = useState("");
  const id = opened || decisionId;
  const [pending, setPending] = useState(false);
  if (id) return <a className="ag-btn" href={`?area=decisions&decision=${encodeURIComponent(id)}`}>Decision open ↗</a>;
  return (
    <GatedButton permission="meeting:convene" type="button" className="ag-btn" disabled={pending} data-testid="ag-open-decision"
            onClick={async () => { setPending(true); try { setId(await onOpen()); } finally { setPending(false); } }}>
      Open decision
    </GatedButton>
  );
}

export type { ConversationView };
