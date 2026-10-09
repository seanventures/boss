"use client";

/** Small pieces App › Security, the checklist and the report share. */
import React from "react";

import { IdentityError } from "../../../lib/identity/api";
import type { Severity } from "../../../lib/security-review";

const SEV_WORD: Record<Severity, string> = { critical: "Critical", high: "High", medium: "Medium", low: "Low", info: "Info" };

/** A severity as a word on its colour: never colour alone. */
export function SevPill({ severity }: { severity: Severity }) {
  return <span className="sec-sev" data-sev={severity}>{SEV_WORD[severity]}</span>;
}

/** A refusal in the console's words; the API's own sentence when it gave one. */
export function errorWords(e: unknown): string {
  if (e instanceof IdentityError) {
    const said = typeof e.body.message === "string" ? e.body.message
      : typeof e.body.detail === "string" ? e.body.detail : "";
    return said || e.message;
  }
  return e instanceof Error ? e.message : String(e);
}
