"use client";

/**
 * The pieces every conversation surface shares: a face with a truthful state, the AI badge, a
 * turn, the start card, a failure said plainly, and the composer. The talk sheet and the room
 * are arrangements of these, so a turn reads the same in both.
 *
 * FACES. The kit's <AgentAvatar> draws every agent here, as on every other surface; a seat in
 * the room adds its state word under the face.
 */

import React, { createContext, useContext, useEffect, useRef, useState } from "react";

import { AgentAvatar } from "../agent-avatar";
import { Markdown } from "../markdown";
import type { AvatarSize, AvatarState, ModelFailure } from "../../lib/agent-world-types";
import {
  ConverseRefusal, NEEDS_MODEL, splitCitations, type ConverseProblem, type ProposalView,
  type SeatInfo, type TurnView,
} from "../../lib/agent-converse";
import { missingRequired, workflowInputFields, type WorkflowInputField } from "../../lib/workflow-inputs";

// ── Names ──────────────────────────────────────────────────────────────────────────────────

const ACRONYMS: Record<string, string> = { qa: "QA", sre: "SRE", pm: "PM", cto: "CTO", ux: "UX" };
export function humanise(key: string): string {
  return key.split("_").map((w) => ACRONYMS[w] ?? (w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
}

/** Who each role key is, from the conversation's seats and the project's agents. */
export const SeatBook = createContext<Map<string, SeatInfo>>(new Map());

export function useSeat(roleKey: string | null | undefined): SeatInfo | null {
  const book = useContext(SeatBook);
  if (!roleKey) return null;
  return book.get(roleKey) ?? { roleKey, name: humanise(roleKey), lifecycle: "active" };
}

/** "Vera · QA Lead" when there is a callsign, else the role's name. Callsigns sit beside the
 *  role, never instead of it (§13.9). */
export function seatName(seat: SeatInfo | null): { name: string; role?: string } {
  if (!seat) return { name: "Agent" };
  return seat.callsign ? { name: seat.callsign, role: seat.name } : { name: seat.name };
}

// ── Faces ──────────────────────────────────────────────────────────────────────────────────

export const STATE_WORD: Record<AvatarState, string> = {
  idle: "Idle", thinking: "Thinking", speaking: "Speaking", working: "Working",
  needs_you: "Needs you", done: "Done", stuck: "Stuck", paused: "Paused", retired: "Retired",
};
const STATE_GLYPH: Partial<Record<AvatarState, string>> = {
  thinking: "…", speaking: "≋", working: "⚙", needs_you: "!", done: "✓", stuck: "!",
};

/**
 * One agent's face, with its state carried by a ring AND a word (never colour alone, §7). Next
 * to a name it is decorative; on its own it is an image named "<name>, <state>".
 */
export function ConverseFace({ roleKey, size = 32, state = "idle", label, badge }: {
  /** A role key; "boss" for BOSS's own turns. */
  roleKey: string; size?: AvatarSize; state?: AvatarState;
  /** Set when the face stands alone, without its name beside it. */
  label?: string;
  /** Show the state word under the face (seats in the room). */
  badge?: boolean;
}) {
  const boss = roleKey === "boss";
  return (
    <span className="ag-cv-face" data-state={state} data-testid={`ag-face-${roleKey}`}>
      <AgentAvatar agent={boss ? undefined : roleKey} variant={boss ? "boss" : "agent"} size={size}
                   state={state} standalone={Boolean(label)} aiBadge={false} className="ag-face" />
      {badge && state !== "idle" ? (
        <span className="ag-cv-badge" aria-hidden>
          {STATE_GLYPH[state] ? <b>{STATE_GLYPH[state]}</b> : null}{STATE_WORD[state]}
        </span>
      ) : null}
    </span>
  );
}

export { AiBadge } from "./agent-face";

// ── Failures and refusals ──────────────────────────────────────────────────────────────────

export function FailureNote({ failure, onRetry }: { failure: ModelFailure; onRetry?: () => void }) {
  return (
    <div className="ag-fail" role="alert" data-testid="ag-fail">
      <span className="ag-fail-head" title={failure.action}>{failure.headline}</span>
      <span className="ag-fail-acts">
        {failure.href ? <a href={failure.href} className="ag-link" title={failure.action}>Fix it ↗</a> : null}
        {failure.retry && onRetry ? <button type="button" className="ag-btn" onClick={onRetry}>Retry</button> : null}
      </span>
    </div>
  );
}

export function ProblemNote({ problem, onClose }: { problem: ConverseProblem; onClose?: () => void }) {
  if (problem.failure) return <FailureNote failure={problem.failure} />;
  const needsModel = problem.code === "needs_model";
  return (
    <div className="ag-fail" role="alert" data-testid="ag-problem" data-code={problem.code ?? ""}>
      <span className="ag-fail-head" title={problem.message}>
        {needsModel ? "Needs a model" : problem.code === "budget" ? "Budget refused this"
          : problem.code === "busy" ? "Still answering" : "Not sent"}
      </span>
      <span className="ag-fail-acts">
        {needsModel ? <a className="ag-link" href="/?area=setup&step=model" title={NEEDS_MODEL}>Settings ↗</a> : null}
        {problem.code === "budget" ? <a className="ag-link" href="/?area=budget">Budget ↗</a> : null}
        {onClose ? <button type="button" className="ag-icon-btn" aria-label="Dismiss" onClick={onClose}>×</button> : null}
      </span>
    </div>
  );
}

// ── The start card ─────────────────────────────────────────────────────────────────────────

type StartFn = (proposalId: string, o?: { startAnyway?: string[]; inputs?: Record<string, string> }) => Promise<string>;

export function runHref(runId: string): string {
  return `?area=delivery_workflows&wfr=${encodeURIComponent(runId)}`;
}

/**
 * The plan, as a card the operator reviews. Nothing starts until Start is pressed; Start goes
 * through the composer's own door, so a project that cannot run it yet says what is missing,
 * and "Start anyway" names exactly the keys the operator was shown.
 */
export function ProposalCard({ proposal, onStart, onDismiss, compact }: {
  proposal: ProposalView; onStart: StartFn; onDismiss?: (id: string) => void; compact?: boolean;
}) {
  const [inputs, setInputs] = useState<Record<string, string>>(proposal.inputs);
  const [pending, setPending] = useState(false);
  const [refused, setRefused] = useState<{ message: string; missing: { key: string; label?: string; fix?: string }[] } | null>(null);
  const [runId, setRunId] = useState(proposal.runId ?? "");
  const [error, setError] = useState("");
  useEffect(() => { setInputs(proposal.inputs); }, [proposal.inputs]);
  useEffect(() => { if (proposal.runId) setRunId(proposal.runId); }, [proposal.runId]);
  const live = proposal.status === "proposed" && !runId;
  const edited = Object.keys(inputs).some((k) => inputs[k] !== proposal.inputs[k]);
  // THE WORKFLOW'S OWN START FORM, read as the composer reads it: its declared fields, each
  // labelled and kinded the same way. A card from before the schema rode along keeps its keys.
  const declared = workflowInputFields(proposal.inputSchema);
  const fields: WorkflowInputField[] = declared.length ? declared : Object.keys(inputs).map((name) => ({
    name, label: humanise(name), help: "", required: false, example: "", default: "", kind: "text" as const }));
  // Start waits for a required field only where the card can take it; a compact card's Start
  // is refused by the door with the same checklist, which says what is missing.
  const blank = compact ? [] : missingRequired(fields, inputs);

  const start = async (anyway?: string[]) => {
    setPending(true);
    setError("");
    try {
      const id = await onStart(proposal.proposalId, { startAnyway: anyway, ...(edited ? { inputs } : {}) });
      setRunId(id);
      setRefused(null);
    } catch (e) {
      if (e instanceof ConverseRefusal && e.readiness?.missing?.length) {
        setRefused({ message: e.message, missing: e.readiness.missing });
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <section className="ag-proposal" data-status={runId ? "started" : proposal.status}
             data-testid="ag-proposal" aria-label="Start proposal">
      <header className="ag-proposal-head">
        <span className="ag-eyebrow">Plan</span>
        <strong className="ag-proposal-title" data-user-text>{proposal.title}</strong>
        {proposal.why ? <span className="ag-why" data-tip={proposal.why} title={proposal.why}>Why</span> : null}
      </header>
      <dl className="ag-proposal-rows">
        <dt>Workflow</dt>
        <dd data-user-text>{proposal.workflowName ?? proposal.workflowKey ?? proposal.workClass}</dd>
        <dt>Do</dt>
        <dd data-user-text>{proposal.directive}</dd>
        {!compact && fields.length ? (<>
          <dt>Inputs</dt>
          <dd className="ag-proposal-inputs">
            {fields.map((f) => (
              <label key={f.name} className="ag-input">
                <span>{f.label}{f.required ? " *" : ""}</span>
                {f.kind === "boolean" ? (
                  <input type="checkbox" checked={inputs[f.name] === "true"} disabled={!live} aria-label={f.label}
                         onChange={(e) => setInputs((all) => ({ ...all, [f.name]: e.target.checked ? "true" : "false" }))} />
                ) : (
                  <input value={inputs[f.name] ?? ""} disabled={!live} aria-label={f.label}
                         type={f.kind === "number" ? "number" : f.kind === "email" ? "email" : f.kind === "url" ? "url" : "text"}
                         placeholder={f.example || undefined}
                         onChange={(e) => setInputs((all) => ({ ...all, [f.name]: e.target.value }))} />
                )}
              </label>
            ))}
          </dd>
        </>) : null}
        {!compact && proposal.acceptanceCriteria.length ? (<>
          <dt>Done when</dt>
          <dd><ul className="ag-criteria" data-user-text>
            {proposal.acceptanceCriteria.map((c) => <li key={c}>{c}</li>)}
          </ul></dd>
        </>) : null}
        {proposal.agents.length ? (<>
          <dt>Who runs it</dt>
          <dd className="ag-steps">
            {proposal.agents.map((a) => <StepAgent key={a.stepId} label={a.stepLabel} roleKey={a.roleKey} />)}
          </dd>
        </>) : null}
      </dl>
      {proposal.readiness && !proposal.readiness.ok && live && !refused ? (
        <div className="ag-ready" data-ok="false">
          {proposal.readiness.problems.map((p) => (
            <span key={p} className="ag-chip ag-chip-warn" title={p}>{p.length > 40 ? `${p.slice(0, 38)}…` : p}</span>
          ))}
        </div>
      ) : null}
      {refused ? (
        <div className="ag-ready" data-ok="false" data-testid="ag-refused" title={refused.message}>
          {refused.missing.map((m) => (
            <span key={m.key} className="ag-chip ag-chip-warn" title={m.fix ?? m.label}>{m.label ?? m.key}</span>
          ))}
        </div>
      ) : null}
      {error ? <span className="ag-error" role="alert" title={error}>Not started</span> : null}
      <footer className="ag-proposal-foot">
        {runId ? (
          <a className="ag-btn ag-btn-primary" href={runHref(runId)} data-testid="ag-open-run">Open run ↗</a>
        ) : proposal.status === "proposed" ? (<>
          {refused ? (
            <button type="button" className="ag-btn ag-btn-primary" disabled={pending}
                    data-testid="ag-start-anyway"
                    onClick={() => void start(refused.missing.map((m) => m.key))}>Start anyway</button>
          ) : (
            <button type="button" className="ag-btn ag-btn-primary" disabled={pending || blank.length > 0}
                    title={blank.length ? `Needs ${blank.join(", ")}` : undefined}
                    data-testid="ag-start" onClick={() => void start()}>
              {pending ? "Starting…" : "Start"}
            </button>
          )}
          {onDismiss ? <button type="button" className="ag-btn" disabled={pending}
                               onClick={() => onDismiss(proposal.proposalId)}>Dismiss</button> : null}
        </>) : (
          <span className="ag-muted">{proposal.status === "superseded" ? "Replaced" : "Dismissed"}</span>
        )}
        {runId ? <span className="ag-chip ag-chip-ok">Started</span> : null}
      </footer>
    </section>
  );
}

function StepAgent({ label, roleKey }: { label: string; roleKey: string | null }) {
  const seat = useSeat(roleKey);
  return (
    <span className="ag-step" data-testid={`ag-step-${label}`}>
      <span className="ag-step-label">{label}</span>
      {roleKey ? (
        <span className="ag-step-agent"><ConverseFace roleKey={roleKey} size={20} />{seatName(seat).name}</span>
      ) : <span className="ag-step-agent ag-muted" title="BOSS picks the agent holding the step's skills">Auto</span>}
    </span>
  );
}

// ── A turn ─────────────────────────────────────────────────────────────────────────────────

export function TurnItem({ turn, onStart, onDismiss, replyTo, live }: {
  turn: TurnView; onStart: StartFn; onDismiss?: (id: string) => void;
  /** The seat this turn answers, in a debate round. */
  replyTo?: string | null;
  /** This turn's words are arriving now. */
  live?: boolean;
}) {
  const seat = useSeat(turn.roleKey);
  if (turn.speakerKind === "person") {
    return (
      <div className="ag-turn ag-turn-you" data-testid="ag-turn-person">
        <div className="ag-bubble" data-user-text>{turn.content}</div>
        {turn.addressedTo.length ? (
          <span className="ag-to">{turn.addressedTo.map((k) => <ToChip key={k} roleKey={k} />)}</span>
        ) : null}
      </div>
    );
  }
  const { text, acts } = splitCitations(turn.content, turn.citedActs);
  const who = seatName(seat);
  return (
    <div className="ag-turn ag-turn-agent" data-testid="ag-turn-agent" data-role={turn.roleKey ?? ""}
         data-status={turn.status}>
      <ConverseFace roleKey={turn.roleKey ?? "boss"} size={24}
                    state={live ? (text ? "speaking" : "thinking") : turn.status === "error" ? "stuck" : "idle"} />
      <div className="ag-turn-body">
        <div className="ag-turn-who">
          <strong>{who.name}</strong>
          {who.role ? <span className="ag-muted">{who.role}</span> : null}
          {replyTo ? <span className="ag-reply" title="Answering another seat">↩ {replyTo}</span> : null}
        </div>
        {turn.kind === "error" && turn.error ? <FailureNote failure={turn.error} />
          : live && !text ? <span className="ag-typing" aria-label="Thinking"><i /><i /><i /></span>
          : turn.kind === "summary" ? null
          : <div className="ag-text" data-user-text aria-live={live ? "polite" : undefined}>
              <Markdown text={text} />{live ? <span className="ag-caret" aria-hidden /> : null}
            </div>}
        {acts.length ? (
          <span className="ag-cites">
            {acts.map((id) => <a key={id} className="ag-chip" href={`?area=workforce&role=${turn.roleKey ?? ""}&act=${id}`}
                                 title={id}>I ran this ↗</a>)}
          </span>
        ) : null}
        {turn.proposal ? <ProposalCard proposal={turn.proposal} onStart={onStart} onDismiss={onDismiss} /> : null}
      </div>
    </div>
  );
}

function ToChip({ roleKey }: { roleKey: string }) {
  const seat = useSeat(roleKey);
  return <span className="ag-chip">@{seatName(seat).name}</span>;
}

// ── The composer ───────────────────────────────────────────────────────────────────────────

/** A text box that sends on Enter (Shift+Enter for a new line), with an @-mention menu when
 *  there are seats to mention. */
export function Composer({ placeholder, disabled, onSend, seats, extra, autoFocus, onStop, initial }: {
  placeholder: string; disabled?: boolean; onSend: (message: string) => void;
  /** What the box starts with (a door's question); the operator sends it. */
  initial?: string;
  seats?: SeatInfo[]; extra?: React.ReactNode; autoFocus?: boolean;
  /** While an answer streams: Send becomes Stop. */
  onStop?: () => void;
}) {
  const [value, setValue] = useState(initial ?? "");
  const [menu, setMenu] = useState<SeatInfo[]>([]);
  const [pick, setPick] = useState(0);
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (autoFocus) box.current?.focus(); }, [autoFocus]);

  const mentionAt = (text: string, caret: number) => {
    const before = text.slice(0, caret);
    const m = /@([a-z0-9_]*)$/i.exec(before);
    return m ? { start: caret - m[0].length, query: m[1].toLowerCase() } : null;
  };
  const refreshMenu = (text: string, caret: number) => {
    const at = seats?.length ? mentionAt(text, caret) : null;
    setMenu(at ? (seats ?? []).filter((s) => s.roleKey.includes(at.query)
      || s.name.toLowerCase().includes(at.query) || (s.callsign ?? "").toLowerCase().includes(at.query)) : []);
    setPick(0);
  };
  const choose = (seat: SeatInfo) => {
    const el = box.current;
    const caret = el?.selectionStart ?? value.length;
    const at = mentionAt(value, caret);
    if (!at) return;
    const next = `${value.slice(0, at.start)}@${seat.roleKey} ${value.slice(caret)}`;
    setValue(next);
    setMenu([]);
    requestAnimationFrame(() => el?.focus());
  };
  const send = () => {
    const said = value.trim();
    if (!said || disabled) return;
    onSend(said);
    setValue("");
    setMenu([]);
  };

  return (
    <div className="ag-composer">
      {menu.length ? (
        <ul className="ag-mention-menu" role="listbox" aria-label="Mention a seat">
          {menu.map((s, i) => (
            <li key={s.roleKey} role="option" aria-selected={i === pick}>
              <button type="button" onMouseDown={(e) => { e.preventDefault(); choose(s); }}>
                <ConverseFace roleKey={s.roleKey} size={20} />
                <span>{seatName(s).name}</span><code>@{s.roleKey}</code>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <textarea ref={box} rows={2} value={value} placeholder={placeholder} aria-label={placeholder}
                disabled={disabled}
                onChange={(e) => { setValue(e.target.value); refreshMenu(e.target.value, e.target.selectionStart); }}
                onKeyDown={(e) => {
                  if (menu.length && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
                    e.preventDefault();
                    setPick((p) => (p + (e.key === "ArrowDown" ? 1 : menu.length - 1)) % menu.length);
                  } else if (menu.length && (e.key === "Enter" || e.key === "Tab")) {
                    e.preventDefault();
                    choose(menu[pick]);
                  } else if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  } else if (e.key === "Escape" && menu.length) {
                    e.stopPropagation();
                    setMenu([]);
                  }
                }} />
      <div className="ag-composer-row">
        {extra}
        <span className="ag-spacer" />
        {onStop ? <StopButton onStop={onStop} /> : (
          <button type="button" className="ag-btn ag-btn-primary" disabled={disabled || !value.trim()}
                  data-testid="ag-send" onClick={send}>Send</button>
        )}
      </div>
    </div>
  );
}

/** Cut the answering off now: the seat speaking stops, the seats after it do not speak. */
export function StopButton({ onStop }: { onStop: () => void }) {
  return (
    <button type="button" className="ag-btn" data-testid="ag-stop" title="Stop answering now"
            onClick={onStop}>Stop</button>
  );
}

/** Keep a scroll container pinned to the newest turn while the operator has not scrolled up. */
export function useStickToBottom(dep: unknown) {
  const ref = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => { stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48; };
    el.addEventListener("scroll", onScroll);
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    const el = ref.current;
    if (el && stuck.current) el.scrollTop = el.scrollHeight;
  }, [dep]);
  return ref;
}
