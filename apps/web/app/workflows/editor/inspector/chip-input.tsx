"use client";

import React, { useState } from "react";
import { X } from "@phosphor-icons/react";

/**
 * A list of short names, typed one at a time: Enter or comma adds, × or Backspace on an empty
 * box removes. Replaces "type them comma-separated" boxes, where a stray space or a trailing
 * comma became an input called "".
 */
export function ChipInput({ value, onChange, placeholder, label, testId, pattern = /^[A-Za-z0-9._-]+$/ }: {
  value: string[];
  onChange(next: string[]): void;
  placeholder?: string;
  label: string;
  testId?: string;
  /** What one name may contain. Inputs and outputs are identifiers the runtime looks up. */
  pattern?: RegExp;
}) {
  const [draft, setDraft] = useState("");
  const [bad, setBad] = useState(false);

  const add = (raw: string) => {
    const names = raw.split(",").map((s) => s.trim()).filter(Boolean);
    if (!names.length) return true;
    if (names.some((n) => !pattern.test(n))) { setBad(true); return false; }
    onChange([...value, ...names.filter((n) => !value.includes(n))]);
    setDraft("");
    setBad(false);
    return true;
  };

  return (
    <div className="wf-chips wf-chip-input" data-testid={testId}>
      {value.map((name) => (
        <span key={name} className="wf-chip">
          {name}
          <button type="button" className="wf-chip-x" aria-label={`Remove ${name}`}
                  onClick={() => onChange(value.filter((v) => v !== name))}><X size={10} weight="bold" /></button>
        </span>
      ))}
      <input className="wf-chip-box" value={draft} placeholder={value.length ? "" : placeholder}
             aria-label={label} aria-invalid={bad || undefined}
             title={bad ? "Letters, numbers, dot, dash or underscore" : undefined}
             data-testid={testId ? `${testId}-box` : undefined}
             onChange={(e) => {
               const next = e.target.value;
               if (next.endsWith(",")) { if (!add(next.slice(0, -1))) setDraft(next.slice(0, -1)); return; }
               setDraft(next);
               setBad(false);
             }}
             onKeyDown={(e) => {
               if (e.key === "Enter") { e.preventDefault(); add(draft); }
               else if (e.key === "Backspace" && !draft && value.length) onChange(value.slice(0, -1));
             }}
             onBlur={() => { if (draft.trim()) add(draft); }} />
    </div>
  );
}
