"use client";

/** When a new binding ends: Add access and approving an access request share it. */
import React from "react";

/** How long a new binding lasts: until removed, or it ends by itself. */
export const EXPIRY_CHOICES = [
  { id: "none", label: "No end", days: null },
  { id: "1d", label: "1 day", days: 1 },
  { id: "7d", label: "7 days", days: 7 },
  { id: "30d", label: "30 days", days: 30 },
  { id: "date", label: "Until…", days: null },
] as const;
export type ExpiryChoice = (typeof EXPIRY_CHOICES)[number]["id"];

/** The expires_at to send: null for "No end"; the end of the chosen day (UTC) for a date. */
export function expiresAt(choice: ExpiryChoice, date: string, now: number): string | null {
  const c = EXPIRY_CHOICES.find((x) => x.id === choice);
  if (c?.days) return new Date(now + c.days * 86_400_000).toISOString();
  if (choice === "date" && /^\d{4}-\d{2}-\d{2}$/.test(date)) return `${date}T23:59:59Z`;
  return null;
}

export function ExpiryField({ id, choice, date, onChoice, onDate }: {
  id: string; choice: ExpiryChoice; date: string; onChoice: (c: ExpiryChoice) => void; onDate: (d: string) => void;
}) {
  return (
    <div className="idn-field">
      <span className="idn-label" id={`${id}-l`}>Ends</span>
      <div className="idn-segs" role="radiogroup" aria-labelledby={`${id}-l`} data-testid={`${id}-choices`}>
        {EXPIRY_CHOICES.map((c) => (
          <button key={c.id} type="button" role="radio" aria-checked={choice === c.id} aria-pressed={choice === c.id}
            title={c.id === "none" ? "Lasts until someone removes it" : c.id === "date" ? "Pick the last day" : `Ends ${c.label} from now`}
            onClick={() => onChoice(c.id)} data-testid={`${id}-${c.id}`}>{c.label}</button>
        ))}
      </div>
      {choice === "date" ? (
        <>
          <label className="idn-sr" htmlFor={`${id}-date`}>Last day</label>
          <input id={`${id}-date`} type="date" className="idn-input" value={date} onChange={(e) => onDate(e.target.value)} data-testid={`${id}-date`} />
        </>
      ) : null}
    </div>
  );
}

