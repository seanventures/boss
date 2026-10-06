"use client";

/**
 * The consent wording, VERBATIM from docs/product/RUN_SCORECARDS.md ("Consent"), and the one
 * place it is rendered. What a person agrees to is never paraphrased, so it is exempt from the
 * console's copy budget (`[data-consent]`, tests/_helpers/copy-budget.ts) — and a test holds this
 * text to the doc's.
 */
import React from "react";

export const CONSENT_TITLE = "Improve BOSS.";
export const CONSENT_TEXT =
  "Share how BOSS is working for you: how runs turned out, your ratings, which features you use, " +
  "and errors. Never your code, prompts, briefs, file names, repo names or secrets. You can see " +
  "exactly what is sent, and turn it off any time.";

/** The heartbeat's fields, as the person reads them (each one is in the payload preview). */
export const HEARTBEAT_WORDS: { key: string; word: string; tip: string }[] = [
  { key: "install_id", word: "random id", tip: "Random, not derived from anything. Rotate it any time." },
  { key: "version", word: "version", tip: "The BOSS release, e.g. 1.0.0-rc.4" },
  { key: "edition", word: "edition", tip: "Community or Enterprise" },
  { key: "install_method", word: "install method", tip: "Compose, source or a test stack" },
  { key: "os", word: "OS", tip: "linux, darwin or windows" },
  { key: "arch", word: "CPU type", tip: "amd64 or arm64" },
  { key: "age", word: "days installed", tip: "A range, such as 8-30" },
  { key: "worked_24h", word: "worked today", tip: "Yes or no: did any work run in 24 hours" },
];

export function ConsentText() {
  return (
    <span className="im-consent" data-consent="" data-testid="im-consent">
      <b>{CONSENT_TITLE}</b> {CONSENT_TEXT}
    </span>
  );
}

export function HeartbeatFields() {
  return (
    <span className="im-fields" aria-label="What the heartbeat says">
      {HEARTBEAT_WORDS.map((f) => (
        <span key={f.key} className="im-field" title={f.tip}>{f.word}</span>
      ))}
    </span>
  );
}
