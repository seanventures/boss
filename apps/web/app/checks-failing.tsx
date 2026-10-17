/**
 * CHECKS FAILING ON A MERGE CARD (engine services/checks_failing.py).
 *
 * A build whose checks still fail after its repair rounds opens its pull request anyway — a
 * draft labelled `checks failing` — and the workflow goes on to this card. The card must say so
 * before anything else, security first, and its merge is a person's stated decision: the engine
 * refuses a plain Approve, so the card asks for the reason and sends a forced merge.
 */
import React from "react";

export type ChecksFailingGate = { gate: string; label: string; security: boolean; detail?: string };
export type ChecksFailingFinding = {
  gate: string; stage: string; security: boolean; severity: string; where?: string; text: string;
};
export type ChecksFailingRecord = {
  gates: ChecksFailingGate[];
  findings: ChecksFailingFinding[];
  security: boolean;
  draft?: boolean;
  followup_run_id?: string;
};

const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** The record a merge decision carries, or null (an older API, or nothing failing). */
export function checksFailingOf(recommendation: Record<string, unknown> | null | undefined
): ChecksFailingRecord | null {
  const raw = recommendation?.checks_failing;
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const gates = (Array.isArray(r.gates) ? r.gates : [])
    .filter((g): g is Record<string, unknown> => !!g && typeof g === "object")
    .map((g) => ({ gate: str(g.gate), label: str(g.label) || str(g.gate),
                   security: g.security === true, detail: str(g.detail) || undefined }))
    .filter((g) => g.gate);
  if (!gates.length) return null;
  const findings = (Array.isArray(r.findings) ? r.findings : [])
    .filter((f): f is Record<string, unknown> => !!f && typeof f === "object")
    .map((f) => ({ gate: str(f.gate), stage: str(f.stage), security: f.security === true,
                   severity: str(f.severity), where: str(f.where) || undefined, text: str(f.text) }))
    .filter((f) => f.text);
  // Security first, as the engine orders them; kept stable for anything else.
  const first = <T extends { security: boolean }>(xs: T[]) =>
    [...xs.filter((x) => x.security), ...xs.filter((x) => !x.security)];
  return { gates: first(gates), findings: first(findings), security: r.security === true
             || gates.some((g) => g.security),
           draft: r.draft === true, followup_run_id: str(r.followup_run_id) || undefined };
}

export function ChecksFailing({ record }: { record: ChecksFailingRecord }) {
  return (
    <div className="dq-guidance dq-checks-failing" role="alert" data-testid="decision-checks-failing"
         data-security={record.security || undefined}>
      <p>
        <strong>Checks failing.</strong>{" "}
        {record.security ? (
          <span className="cap-badge tone-red" data-testid="decision-checks-security">
            security finding open
          </span>
        ) : null}{" "}
        BOSS&apos;s checks still fail after the repair rounds, so this pull request opened
        {record.draft ? " as a draft" : ""} with them listed. It merges only if you decide to and
        say why{record.followup_run_id ? "; a follow-up build is fixing them" : ""}.
      </p>
      <ul data-testid="decision-checks-gates">
        {record.gates.map((g) => (
          <li key={g.gate} data-security={g.security || undefined}>
            {g.security ? <b>Security · </b> : null}
            <b>{g.label}</b> <span className="v2-mono">({g.gate})</span>
            {g.detail ? <>: {g.detail}</> : null}
          </li>
        ))}
      </ul>
      {record.findings.length ? (
        <details open={record.security} data-testid="decision-checks-findings">
          <summary>{record.findings.length} open finding{record.findings.length === 1 ? "" : "s"}</summary>
          <ul>
            {record.findings.map((f, i) => (
              <li key={`${f.gate}-${i}`} data-security={f.security || undefined}>
                {f.security ? <b>Security · </b> : null}
                <span className="v2-mono">[{f.severity.toUpperCase()}] {f.stage}{f.where ? ` ${f.where}` : ""}</span>{" "}
                <span style={{ whiteSpace: "pre-wrap" }}>{f.text}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
