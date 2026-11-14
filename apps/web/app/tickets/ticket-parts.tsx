"use client";

/**
 * THE TICKET PARTS every ticket screen shares (BOSSAPP-1047 Phase C): the provider monogram, the
 * ticket brief card (T3 New work, T4 Ask BOSS), the row chip with its sync state (T5, T9), the
 * workflow pick (epic → Major feature, story → Minor feature, bug → Bug fix) and the four states
 * of a read (loading, empty, error in words, permission denied). Classes are tk-*, tokens only.
 */
import React from "react";
import { ArrowSquareOut, CheckCircle, LockSimple, ShieldWarning, Ticket as TicketIcon, WarningCircle } from "@phosphor-icons/react";

import {
  START_WORKFLOWS, TYPE_WORDS, monogram, workflowWords, type Read, type Ticket,
} from "../../lib/tickets";
import "./tickets.css";

/** "J" on a tinted square: which ticket system a ticket or connection is. */
export function ProviderMono({ provider, size = 22, title }: { provider: string; size?: number; title?: string }) {
  return (
    <span className="tk-mono" data-provider={provider.toLowerCase()} style={{ width: size, height: size, fontSize: size * 0.45 }}
          aria-hidden={title ? undefined : true} title={title} role={title ? "img" : undefined} aria-label={title}>
      {monogram(provider)}
    </span>
  );
}

const TONE: Record<string, string> = { todo: "idle", doing: "teal", review: "blue", done: "green" };
const prioTone = (p: string) => (/high|highest|urgent|critical|blocker/i.test(p) ? "amber" : "idle");

/** A small tag (tokens only): type, priority, status. */
export function TkTag({ tone = "idle", children, title, testid }: { tone?: string; children: React.ReactNode; title?: string; testid?: string }) {
  return <span className="tk-tag" data-tone={tone} title={title} data-testid={testid}>{children}</span>;
}

/** "Also read: 2 comments · 1 attachment (dti-policy.pdf) · parent epic LEND-120". */
export function alsoRead(t: Ticket): string {
  const bits = [
    t.comments.length ? `${t.comments.length} comment${t.comments.length === 1 ? "" : "s"}` : "",
    t.attachments.length ? `${t.attachments.length} attachment${t.attachments.length === 1 ? "" : "s"} (${t.attachments.slice(0, 2).map((x) => x.name).join(", ")})` : "",
    t.parent_key ? `parent epic ${t.parent_key}` : "",
  ].filter(Boolean);
  return bits.length ? `Also read: ${bits.join(" · ")}` : "";
}

/**
 * THE TICKET BRIEF CARD (T3, T4): what BOSS read from the ticket. Key (opens the ticket), type,
 * priority and status; the title; the first lines of the body; the acceptance criteria; what else
 * was read. A ticket the injection screen flagged says so. `children` is the card's foot (chips,
 * the one Start).
 */
export function TicketCard({ ticket: t, heading, children, testid = "tk-card", readAt = "read just now" }: {
  ticket: Ticket; heading?: React.ReactNode; children?: React.ReactNode; testid?: string; readAt?: string;
}) {
  const body = t.body.replace(/\s+/g, " ").trim();
  const also = alsoRead(t);
  return (
    <section className="tk-card" data-testid={testid} aria-label={`Ticket ${t.key}`}>
      <div className="tk-card-head">
        <ProviderMono provider={t.provider} />
        {heading ?? null}
        {t.url ? <a className="tk-key" href={t.url} target="_blank" rel="noreferrer noopener" data-testid={`${testid}-key`} title={`Open ${t.key} in its ticket system`}>
          {t.key}<ArrowSquareOut size={11} aria-hidden /></a> : <b className="tk-key">{t.key}</b>}
        <TkTag tone={t.type === "bug" ? "red" : t.type === "epic" ? "violet" : "blue"}>{TYPE_WORDS[t.type]}</TkTag>
        {t.priority ? <TkTag tone={prioTone(t.priority)}>{t.priority}</TkTag> : null}
        {t.status ? <TkTag tone={TONE[t.status_category] ?? "idle"}>{t.status}</TkTag> : null}
        <span className="tk-spacer" />
        <span className="tk-faint tk-hide-phone">{readAt}</span>
      </div>
      <h3 className="tk-title" data-user-text>{t.title}</h3>
      {body ? <p className="tk-body" data-user-text>{body.length > 280 ? `${body.slice(0, 278)}…` : body}</p> : null}
      {t.acceptance_criteria.length ? (
        <div className="tk-ac" data-testid={`${testid}-ac`}>
          <span className="tk-lbl">Acceptance criteria · {t.acceptance_criteria.length}</span>
          <ul>{t.acceptance_criteria.slice(0, 8).map((c, i) => <li key={i}><CheckCircle size={13} weight="fill" aria-hidden /> <span data-user-text>{c}</span></li>)}</ul>
          {t.acceptance_criteria.length > 8 ? <span className="tk-faint">+ {t.acceptance_criteria.length - 8} more</span> : null}
        </div>
      ) : null}
      {also ? <p className="tk-faint tk-also" data-testid={`${testid}-also`}>{also}</p> : null}
      {t.untrusted ? (
        <p className="tk-warn" role="note" data-testid={`${testid}-untrusted`} title={t.screen.reasons.join("; ") || undefined}>
          <ShieldWarning size={13} aria-hidden /> BOSS flagged text in this ticket. Agents treat it as outside input only.</p>
      ) : null}
      {children ? <div className="tk-card-foot">{children}</div> : null}
    </section>
  );
}

/** The workflow a ticket starts on, picked from its type; the person may change it. */
export function WorkflowPick({ value, onChange, testid = "tk-workflow", label = "Workflow" }: {
  value: string; onChange: (v: string) => void; testid?: string; label?: string;
}) {
  const options = (START_WORKFLOWS as readonly string[]).includes(value) ? START_WORKFLOWS : [value, ...START_WORKFLOWS];
  return (
    <label className="tk-pick" title="Picked from the ticket's type: epic → Major feature, story → Minor feature, bug → Bug fix">
      <span className="tk-sr">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} data-testid={testid} aria-label={label}>
        {options.map((k) => <option key={k} value={k}>{workflowWords(k)}</option>)}
      </select>
    </label>
  );
}

/** A row's ticket chip: "J LEND-142 · Jira: In Progress ✓", linking to the ticket. */
export function TicketChip({ provider, ticketKey, url, sync, tone, testid }: {
  provider: string; ticketKey: string; url?: string; sync?: string; tone?: "green" | "amber" | "red"; testid?: string;
}) {
  const inner = (
    <>
      <ProviderMono provider={provider} size={14} />
      <span className="tk-chip-key">{ticketKey}</span>
      {sync ? <span className="tk-chip-sync" data-tone={tone}>{sync}</span> : null}
    </>
  );
  return url ? (
    <a className="tk-chip" href={url} target="_blank" rel="noreferrer noopener" data-testid={testid} title={`Open ${ticketKey}`}
       onClick={(e) => e.stopPropagation()}>{inner}</a>
  ) : <span className="tk-chip" data-testid={testid}>{inner}</span>;
}

/** An illustrated empty state for the ticket screens. */
export function TkEmpty({ title, line, children, testid }: { title: string; line?: string; children?: React.ReactNode; testid?: string }) {
  return (
    <div className="tk-empty" data-testid={testid}>
      <svg className="tk-illus" viewBox="0 0 120 72" aria-hidden>
        <rect x="18" y="14" width="64" height="40" rx="8" fill="none" stroke="var(--border-strong)" strokeDasharray="3 4" />
        <rect x="38" y="22" width="64" height="40" rx="8" fill="var(--surface-2)" stroke="var(--accent-line)" />
        <circle cx="52" cy="36" r="4" fill="var(--teal)" opacity=".8" />
        <rect x="62" y="33" width="30" height="5" rx="2.5" fill="var(--border-strong)" />
        <rect x="48" y="46" width="44" height="5" rx="2.5" fill="var(--border)" />
      </svg>
      <span className="tk-empty-ico" aria-hidden><TicketIcon size={18} weight="duotone" /></span>
      <b>{title}</b>
      {line ? <span className="tk-empty-line">{line}</span> : null}
      {children}
    </div>
  );
}

/** The states of a read that is not data: loading, permission denied, error (in words) + Retry. */
export function TkReadState<T>({ read, what, onRetry, rows = 2, testid = "tk-state" }: {
  read: Read<T> | null; what: string; onRetry?: () => void; rows?: number; testid?: string;
}) {
  if (!read) {
    return (
      <div className="tk-stack" role="status" aria-live="polite" aria-label={`Loading ${what}`} data-testid={`${testid}-loading`}>
        {Array.from({ length: rows }, (_, i) => <div key={i} className="tk-skel" />)}
      </div>
    );
  }
  if (read.ok) return null;
  if (read.denied) {
    return <div className="tk-denied" role="note" data-testid={`${testid}-denied`}><LockSimple size={13} aria-hidden /> {read.error}</div>;
  }
  return (
    <div className="tk-error" role="alert" data-testid={`${testid}-error`}>
      <WarningCircle size={13} aria-hidden /> {what}: {read.error}
      {onRetry ? <> <button type="button" className="tk-linkbtn" onClick={onRetry} data-testid={`${testid}-retry`}>Try again</button></> : null}
    </div>
  );
}
