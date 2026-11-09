"use client";

/**
 * Where every conversation opens. Mounted once by the console shell; every door, a Passport's
 * Talk, a roster card, a run step, the palette, the presence pill, calls `openTalk`, `openPlan`
 * or `openRoom` (lib/agent-converse.ts), which this hears.
 *
 * A talk reopens the operator's open talk with that agent unless the door asks for a fresh one
 * (New talk): one about a run step reopens the talk about that step, any other door the latest
 * talk with the agent. The server picks it (agent_converse._resumable_talk), so every door
 * agrees and closing the sheet never loses what was said. A plan is always fresh; a room
 * without seats asks who to seat first.
 */

import { GatedButton } from "../../components/gated";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

import {
  CONVERSE_EVENT, converseApi, type ConverseOpening, type ConversationView, type PresetView,
  type SeatInfo,
} from "../../lib/agent-converse";
import type { ConversationContext } from "../../lib/agent-world-types";
import { TOAST_EVENT } from "../../lib/api-write";
import { ConverseFace, seatName } from "./converse-parts";
import { RoomView } from "./room-view";
import { TalkSheet } from "./talk-sheet";

type View =
  | { kind: "talk" | "plan"; conversationId: string; greet: boolean; draft?: string }
  | { kind: "room"; conversationId: string; question?: string }
  | { kind: "pick"; mode: "talk" | "plan" | "room" };

function toast(message: string): void {
  try { window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { type: "error", message } })); }
  catch { /* no window */ }
}

export function ConverseHost({ projectId, actor }: { projectId: string; actor: string }) {
  const api = useMemo(() => converseApi(actor), [actor]);
  const [view, setViewState] = useState<View | null>(null);
  // Every opening remounts its surface, so reopening the conversation already on screen reads
  // it afresh rather than keeping a stale copy.
  const [opened, setOpened] = useState(0);
  const setView = useCallback((next: View | null) => {
    setOpened((n) => n + 1);
    setViewState(next);
  }, []);
  const [agents, setAgents] = useState<SeatInfo[]>([]);
  const [presets, setPresets] = useState<PresetView[]>([]);
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const loadBook = useCallback(async () => {
    try {
      const body = await api.presets(projectId);
      setAgents(body.agents);
      setPresets(body.items);
    } catch { /* names fall back to the role key; the picker says it is empty */ }
  }, [api, projectId]);

  const book = useMemo(() => new Map(agents.map((a) => [a.roleKey, a])), [agents]);

  const create = useCallback(async (kind: "talk" | "plan" | "room", seats: string[],
                                    o: { preset?: string; context?: ConversationContext; resume?: boolean } = {}) => {
    const made = await api.createConversation(projectId, {
      kind, seats, preset: o.preset, resume: o.resume,
      context: o.context ?? { from: kind === "room" ? "room" : "chip" } });
    return (made as unknown as ConversationView).conversationId;
  }, [api, projectId]);

  const open = useCallback(async (o: ConverseOpening) => {
    void loadBook();
    try {
      if (o.kind === "pick") { setView({ kind: "pick", mode: o.mode }); return; }
      if (o.kind === "resume") {
        const c = await api.getView(projectId, o.conversationId);
        setView(c.kind === "room" ? { kind: "room", conversationId: c.conversationId }
          : { kind: c.kind, conversationId: c.conversationId, greet: false });
        return;
      }
      if (o.kind === "room") {
        if (!o.seats?.length && !o.preset) { setView({ kind: "pick", mode: "room" }); return; }
        const id = await create("room", o.seats ?? [], { preset: o.preset, context: o.context });
        setView({ kind: "room", conversationId: id, question: o.question });
        return;
      }
      const roleKey = o.roleKey ?? "pm";
      const id = await create(o.kind, [roleKey], { context: o.context,
                                                   resume: o.kind === "talk" && !o.fresh });
      // A talk opened with a question to ask (an Act card's "Ask about this") waits for it; one
      // opened empty lets the agent say hello first. A reopened talk with turns never greets
      // again (TalkSheet greets only an empty one).
      setView({ kind: o.kind, conversationId: id, greet: !o.draft, draft: o.draft });
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e));
    }
  }, [api, projectId, create, loadBook, setView]);

  useEffect(() => {
    const on = (e: Event) => { void open((e as CustomEvent<ConverseOpening>).detail); };
    window.addEventListener(CONVERSE_EVENT, on);
    return () => window.removeEventListener(CONVERSE_EVENT, on);
  }, [open]);

  const close = useCallback(() => setView(null), [setView]);
  if (!mounted || !view) return null;

  const body = view.kind === "pick" ? (
    <Picker mode={view.mode} agents={agents} presets={presets} onClose={close}
            onPick={(o) => void open(o)} />
  ) : view.kind === "room" ? (
    <RoomView key={`${view.conversationId}:${opened}`} projectId={projectId} actor={actor}
              conversationId={view.conversationId} question={view.question} book={book} onClose={close} />
  ) : (
    <TalkSheet key={`${view.conversationId}:${opened}`} projectId={projectId} actor={actor}
               conversationId={view.conversationId} greet={view.greet} draft={view.draft} book={book} onClose={close}
               onPlan={(roleKey, c) => void open({ kind: "plan", roleKey, context: c.context, fresh: true })}
               onNew={(roleKey) => void open({ kind: "talk", roleKey, fresh: true })} />
  );
  return createPortal(<div className="ag-converse" data-testid="ag-converse">{body}</div>, document.body);
}

/** Who to talk to, plan with, or seat. Rooms: a preset or 2-5 agents, and the first question. */
function Picker({ mode, agents, presets, onPick, onClose }: {
  mode: "talk" | "plan" | "room"; agents: SeatInfo[]; presets: PresetView[];
  onPick: (o: ConverseOpening) => void; onClose: () => void;
}) {
  const [find, setFind] = useState("");
  const [seats, setSeats] = useState<string[]>([]);
  const [question, setQuestion] = useState("");
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const shown = agents.filter((a) => `${a.name} ${a.callsign ?? ""} ${a.roleKey} ${a.tagline ?? ""}`
    .toLowerCase().includes(find.trim().toLowerCase()));
  const room = mode === "room";
  const title = mode === "talk" ? "Talk to" : mode === "plan" ? "Plan it with" : "Open a room";
  const toggle = (k: string) => setSeats((all) => all.includes(k) ? all.filter((x) => x !== k)
    : all.length >= 5 ? all : [...all, k]);
  const ready = seats.length >= 2 && seats.length <= 5;

  return (
    <div className="ag-picker-back" onClick={onClose}>
      <div className="ag-picker" role="dialog" aria-modal="true" aria-label={title}
           data-testid="ag-picker" data-mode={mode} onClick={(e) => e.stopPropagation()}>
        <header className="ag-picker-head">
          <strong>{title}</strong>
          <input type="search" placeholder="Find an agent" aria-label="Find an agent" value={find}
                 autoFocus onChange={(e) => setFind(e.target.value)} />
          <button type="button" className="ag-icon-btn" onClick={onClose} aria-label="Close">×</button>
        </header>
        {room ? (
          <div className="ag-presets" role="group" aria-label="Presets">
            {presets.map((p) => (
              <button key={p.key} type="button" className="ag-preset" disabled={p.missing.length > 0}
                      data-testid={`ag-preset-${p.key}`}
                      data-tip={p.missing.length ? `Needs ${p.missing.join(", ")}` : p.seats.join(", ")}
                      title={p.missing.length ? `Needs ${p.missing.join(", ")}` : undefined}
                      onClick={() => setSeats(p.seats)}>
                <span className="ag-stack">{p.seats.map((k) => <ConverseFace key={k} roleKey={k} size={24} />)}</span>
                <span>{p.label}</span>
              </button>
            ))}
          </div>
        ) : null}
        <div className="ag-picker-grid" role="listbox" aria-multiselectable={room}>
          {shown.map((a) => {
            const on = seats.includes(a.roleKey);
            const who = seatName(a);
            return (
              <button key={a.roleKey} type="button" role="option" aria-selected={on}
                      className="ag-pick" data-testid={`ag-pick-${a.roleKey}`}
                      onClick={() => room ? toggle(a.roleKey)
                        : onPick({ kind: mode, roleKey: a.roleKey, fresh: mode === "plan" })}>
                <ConverseFace roleKey={a.roleKey} size={32} state={on ? "done" : "idle"} />
                <span className="ag-pick-name"><strong>{who.name}</strong>{who.role ? <em>{who.role}</em> : null}</span>
                {a.tagline ? <span className="ag-pick-line" title={a.tagline}>{a.tagline}</span> : null}
              </button>
            );
          })}
          {!shown.length ? <span className="ag-muted">No agents</span> : null}
        </div>
        {room ? (
          <footer className="ag-picker-foot">
            <span className="ag-stack">{seats.map((k) => <ConverseFace key={k} roleKey={k} size={24} />)}</span>
            <span className="ag-muted">{seats.length} of 2-5</span>
            <textarea rows={2} placeholder="First question (optional)" aria-label="First question"
                      value={question} onChange={(e) => setQuestion(e.target.value)} />
            <GatedButton permission="meeting:convene" type="button" className="ag-btn ag-btn-primary" disabled={!ready}
                    data-testid="ag-open-room"
                    onClick={() => onPick({ kind: "room", seats, question: question.trim() || undefined,
                      preset: presets.find((p) => p.seats.join() === seats.join())?.key })}>
              Open room
            </GatedButton>
          </footer>
        ) : null}
      </div>
    </div>
  );
}
