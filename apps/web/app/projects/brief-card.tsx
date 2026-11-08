"use client";

/**
 * THE BRIEF CARD: the one card every box a person types work into draws (New work on a project:
 * "Task"; Add a project draws the same card with its own rows: "Project brief" from an idea,
 * "Repo summary" from a repo). The heading names what the card IS, never a hedge. The person's words stay above
 * it; Helm's rewrite is the card, with the phrases Helm added dotted-underlined (`Marked`), and
 * three presses: Looks right · Fix it (every line editable in place) · Rewrite with a hint.
 *
 * States: reading (skeleton while Helm reads the words and the code graph) · ready · editing ·
 * confirmed (folded to a line of chips; Edit opens it) · fallback (no model answered: Start uses
 * the words as written). The parent maps its own brief into `rows`, so the card never knows
 * which fields a kind of work has.
 */
import React, { useId, useState } from "react";
import { ArrowsClockwise, CaretDown, CaretUp, Check, MagicWand, PencilSimple, WarningCircle } from "@phosphor-icons/react";

import type { Agent } from "../../lib/project-experience";
import { Face } from "./kit";
import "./brief-card.css";

export type BriefRow = {
  key: string; label: string;
  /** A small word after the label ("code graph"). */
  sub?: string;
  /** The row as read. */
  value: React.ReactNode;
  /** The row while Fix it is on (an input), else the value stays. */
  edit?: React.ReactNode;
  /** Added by the conversation (the green "+ from chat" row). */
  fresh?: boolean;
  /** An open question (amber label). */
  question?: boolean;
  /** Skeleton widths while reading (%, one bar each). */
  skeleton?: number[];
  /** The column it sits in when the card has two (Add a project's brief). */
  col?: 1 | 2;
};

export type BriefState = "reading" | "ready" | "confirmed" | "fallback";

/** Text with the phrases Helm added marked (dotted underline), everything else the person's. */
export function Marked({ text, added }: { text: string; added: string[] }) {
  const phrases = added.map((p) => p.trim()).filter((p) => p.length > 1 && text.includes(p)).sort((x, y) => y.length - x.length);
  if (!phrases.length) return <>{text}</>;
  const out: React.ReactNode[] = [];
  let rest = text;
  let k = 0;
  while (rest) {
    let at = -1; let hit = "";
    for (const p of phrases) {
      const i = rest.indexOf(p);
      if (i >= 0 && (at < 0 || i < at)) { at = i; hit = p; }
    }
    if (at < 0) { out.push(rest); break; }
    if (at > 0) out.push(rest.slice(0, at));
    out.push(<mark key={k++} className="px-ub-mark" title="Helm added this">{hit}</mark>);
    rest = rest.slice(at + hit.length);
  }
  return <>{out}</>;
}

function Grid({ rows, columns, children }: { rows: BriefRow[]; columns: 1 | 2; children: (r: BriefRow) => React.ReactNode }) {
  if (columns === 1) return <div className="px-ub-grid">{rows.map(children)}</div>;
  return (
    <div className="px-ub-grid" data-cols="2">
      <div className="px-ub-col">{rows.filter((r) => (r.col ?? 1) === 1).map(children)}</div>
      <div className="px-ub-col">{rows.filter((r) => r.col === 2).map(children)}</div>
    </div>
  );
}

export function BriefCard({
  state, agent, heading, title, sub, rows, chips, editing, onConfirm, onFix, onRewrite, onOpen, onRetry, confirmed, reading, foot, testid = "px-ub",
  columns = 1, live, actions, hintPlaceholder,
}: {
  state: BriefState; agent: Agent | null;
  /** What the card is, said plainly: "Task", "Project brief", "Repo summary". */
  heading: string;
  /** The brief's own title, the heading once confirmed and folded. */
  title: React.ReactNode; sub?: React.ReactNode;
  rows: BriefRow[];
  /** The folded card's chips (confirmed). */
  chips?: React.ReactNode;
  editing: boolean;
  onConfirm?: () => void; onFix?: () => void; onRewrite?: (hint: string) => void;
  /** Open the folded card again (Edit). */
  onOpen?: () => void; onRetry?: () => void;
  confirmed?: boolean;
  /** What Helm is reading (shown in the skeleton's line). */
  reading?: React.ReactNode;
  /** A line under the actions ("This becomes the work item's spec…"). */
  foot?: React.ReactNode;
  testid?: string;
  /** Two columns on a wide card (rows say which with `col`); one on a phone. */
  columns?: 1 | 2;
  /** A live tag in the head ("reading" while Helm still fills it). */
  live?: React.ReactNode;
  /** Presses of the surface's own beside the card's (Change, …). */
  actions?: React.ReactNode;
  hintPlaceholder?: string;
}) {
  const [hint, setHint] = useState("");
  const [open, setOpen] = useState(false);
  const hintId = useId();
  const face = agent ?? { name: "Helm", role_key: "engineering_captain" };
  if (state === "fallback") {
    return (
      <section className="px-ub" data-state="fallback" data-testid={testid} aria-live="polite">
        <div className="px-ub-h">
          <span className="px-ico" data-c="amber" aria-hidden><WarningCircle size={16} /></span>
          <div><b>Start uses your words as written</b><small>{sub ?? `${face.name} couldn't read it just now.`}</small></div>
          {onRetry ? <button type="button" className="px-btn" data-size="s" onClick={onRetry} data-testid={`${testid}-retry`}><ArrowsClockwise size={12} aria-hidden /> Try again</button> : null}
        </div>
      </section>
    );
  }
  if (state === "reading") {
    return (
      <section className="px-ub" data-state="reading" data-testid={testid} aria-busy="true" aria-live="polite">
        <div className="px-ub-h">
          <Face agent={face} size={30} state="working" />
          <div><b data-testid={`${testid}-heading`}>{heading}</b><small>{reading ?? "Reading your words and the code graph…"}</small></div>
          <span className="px-tag" data-tone="blue"><span className="px-dot" data-s="now" aria-hidden /> reading</span>
        </div>
        <Grid rows={rows} columns={columns}>{(r) => (
            <div key={r.key} className="px-ub-f">
              <span className="px-lbl">{r.label}</span>
              <span className="px-ub-sks">{(r.skeleton ?? [70]).map((w, i) => <span key={i} className="px-ub-sk" style={{ width: `${w}%` }} />)}</span>
              <i />
            </div>
          )}</Grid>
      </section>
    );
  }
  if (state === "confirmed" && !open) {
    return (
      <section className="px-ub" data-ok="" data-state="confirmed" data-testid={testid}>
        <div className="px-ub-h">
          <Face agent={face} size={30} />
          <div><b data-testid={`${testid}-title`}>{title}</b>
            <small className="px-ub-ok"><Check size={11} weight="bold" aria-hidden /> {sub ?? "Brief confirmed"}</small></div>
          {onOpen ? <button type="button" className="px-btn" data-size="s" onClick={() => { setOpen(true); onOpen(); }} data-testid={`${testid}-edit`}><PencilSimple size={12} aria-hidden /> Edit</button> : null}
        </div>
        <div className="px-ub-chips">
          {chips}
          <button type="button" className="px-icon-btn px-ub-open" aria-label="Open the whole brief" title="Open the whole brief" onClick={() => setOpen(true)} data-testid={`${testid}-open`}>
            <CaretDown size={13} aria-hidden /></button>
        </div>
      </section>
    );
  }
  return (
    <section className="px-ub" data-ok={confirmed ? "" : undefined} data-edit={editing ? "" : undefined} data-state={confirmed ? "confirmed" : "ready"} data-testid={testid}>
      <div className="px-ub-h">
        <Face agent={face} size={30} />
        <div><b data-testid={`${testid}-heading`}>{heading}</b>
          {confirmed ? <small className="px-ub-ok"><Check size={11} weight="bold" aria-hidden /> {sub ?? "Confirmed"}</small> : <small>{sub}</small>}</div>
        {live}
        <span className="px-ub-key px-hide-phone" aria-hidden><i>your words</i><i><mark className="px-ub-mark">Helm added</mark></i></span>
        {confirmed && open ? <button type="button" className="px-icon-btn" aria-label="Fold the brief" title="Fold" onClick={() => setOpen(false)}><CaretUp size={13} aria-hidden /></button> : null}
      </div>
      <Grid rows={rows} columns={columns}>{(r) => (
          <div key={r.key} className="px-ub-f" data-new={r.fresh ? "" : undefined} data-q={r.question ? "" : undefined} data-on={editing && r.edit ? "" : undefined} data-testid={`${testid}-row-${r.key}`}>
            <span className="px-lbl">{r.label}{r.sub ? <small>{r.sub}</small> : null}</span>
            <div className="px-ub-v">{editing && r.edit ? r.edit : r.value}</div>
            {r.edit && onFix && !editing ? (
              <button type="button" className="px-ub-ed" aria-label={`Edit ${r.label}`} title={`Edit ${r.label}`} onClick={onFix}><PencilSimple size={12} aria-hidden /></button>
            ) : <i />}
          </div>
        )}</Grid>
      <div className="px-ub-act">
        {!confirmed && onConfirm ? (
          <button type="button" className="px-btn" data-kind="ok" onClick={onConfirm} title="Confirms it (Start also confirms it)" data-testid={`${testid}-ok`}>
            <Check size={12} weight="bold" aria-hidden /> Looks right</button>
        ) : null}
        {onFix ? (
          <button type="button" className="px-btn" aria-pressed={editing} onClick={onFix} data-testid={`${testid}-fix`}>
            {editing ? <><Check size={12} aria-hidden /> Done</> : <><PencilSimple size={12} aria-hidden /> {confirmed ? "Edit" : "Fix it"}</>}</button>
        ) : null}
        {onRewrite && !confirmed ? (
          <>
            <label className="px-ub-hint" data-filled={hint ? "" : undefined} htmlFor={hintId}>
              <MagicWand size={12} aria-hidden />
              <input id={hintId} value={hint} onChange={(e) => setHint(e.target.value)} placeholder={hintPlaceholder ?? "Hint for a rewrite, e.g. “smaller first step”"} maxLength={300}
                     onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); onRewrite(hint); setHint(""); } }} data-testid={`${testid}-hint`} />
            </label>
            <button type="button" className="px-btn" data-size="s" onClick={() => { onRewrite(hint); setHint(""); }} data-testid={`${testid}-rewrite`}>
              <ArrowsClockwise size={12} aria-hidden /> Rewrite</button>
          </>
        ) : null}
        {actions}
        {foot ? <span className="px-faint px-ub-foot">{foot}</span> : null}
      </div>
    </section>
  );
}
