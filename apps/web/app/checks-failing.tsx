/**
 * CHECKS FAILING ON A MERGE CARD (engine services/checks_failing.py).
 *
 * A build whose checks still fail after its repair rounds opens its pull request anyway, a
 * draft labelled `checks failing`, and the workflow goes on to this card. The card must say so
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
            {g.security ? <b>Security, </b> : null}
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
                {f.security ? <b>Security, </b> : null}
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

/** The failing gates a merge card carries (checks_failing, or its PR lineage's), labelled. */
export function failingGateLabels(r: Record<string, unknown>): string {
  const cf = r.checks_failing as Record<string, unknown> | undefined;
  const gates = cf && Array.isArray(cf.gates) ? cf.gates : [];
  return gates
    .map((g) => (g && typeof g === "object"
      ? String((g as Record<string, unknown>).label || (g as Record<string, unknown>).gate || "")
      : ""))
    .filter(Boolean)
    .join(", ");
}

/**
 * The card's "Automated checks" sentence (engine acceptance_gate.card_words). Never "passed"
 * while a gate of the pull request failed, or while the must-haves were not all met — partial,
 * missing, not judged, or never checked. A card from an older API (no acceptance record) reads
 * as before.
 */
export function automatedChecksLine(r: Record<string, unknown>): string {
  const vstatus = str(r.validation_status);
  const vsummary = str(r.validation_summary);
  const failing = failingGateLabels(r);
  if (failing) return `Automated checks: failing (${failing}).`;
  const acc = r.acceptance && typeof r.acceptance === "object"
    ? (r.acceptance as Record<string, unknown>) : null;
  const verdict = acc ? str(acc.verdict) : "";
  if (acc && verdict !== "met" && verdict !== "no_items") {
    const items = mustHavesOf(r)?.items ?? [];
    const open = items.filter((i) => i.status !== "met").length;
    const why = verdict === "not_checked" || !items.length
      ? "must-haves not checked"
      : `${open} of ${items.length} must-haves not met`;
    return `Automated checks: ${vstatus ? `CI ${vstatus.replaceAll("_", " ")}; ` : ""}${why}.`;
  }
  // Met, with judgement (taste) items answered partial: passed, and says how many are advisory.
  const advisory = acc ? (mustHavesOf(r)?.items ?? []).filter(isAdvisory).length : 0;
  return vstatus
    ? `Automated checks: ${vstatus.replaceAll("_", " ")}${advisory ? `, ${advisory} advisory` : ""}`
      + `${vsummary ? `, ${vsummary}` : ""}.`
    : "";
}

/** A judgement (taste) must-have answered partial: shown, never failing (BOSSAPP-1233). */
export function isAdvisory(i: MustHaveItem): boolean {
  return i.judgement && i.status === "partial";
}

/**
 * THE MUST-HAVE TABLE (engine services/acceptance_gate.py, BOSSAPP-1211): what the acceptance
 * check found for each must-have of the brief and the PRD's tested-and-gating list, on the whole
 * branch, with the files it cites. Missing first, so the card leads with what is not delivered.
 */
export type MustHaveItem = {
  n: number; text: string; status: string; evidence: string;
  /** A matter of taste, tagged when the acceptance contract was made: a partial is advisory. */
  judgement: boolean;
};
export type MustHaveRecord = { verdict: string; items: MustHaveItem[]; why: string; dropped: number };

export function mustHavesOf(recommendation: Record<string, unknown> | null | undefined
): MustHaveRecord | null {
  const raw = recommendation?.acceptance;
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const items = (Array.isArray(r.items) ? r.items : [])
    .filter((i): i is Record<string, unknown> => !!i && typeof i === "object")
    .map((i) => ({ n: typeof i.n === "number" ? i.n : 0, text: str(i.text),
                   status: str(i.status) || "unjudged", evidence: str(i.evidence),
                   judgement: i.judgement === true }))
    .filter((i) => i.text);
  const verdict = str(r.verdict);
  if (!items.length && verdict !== "not_checked" && verdict !== "not_run") return null;
  const rank: Record<string, number> = { missing: 0, partial: 1, unjudged: 2, met: 3 };
  const order = (i: MustHaveItem) => (isAdvisory(i) ? 2.5 : rank[i.status] ?? 2);
  items.sort((a, b) => order(a) - order(b) || a.n - b.n);
  return { verdict, items, why: str(r.why),
           dropped: typeof r.items_dropped === "number" ? r.items_dropped : 0 };
}

const WORD: Record<string, string> = { missing: "MISSING", partial: "PARTIAL", unjudged: "NOT JUDGED", met: "met" };

export function MustHaves({ data }: { data: MustHaveRecord }) {
  const advisory = data.items.filter(isAdvisory).length;
  const count = (s: string) => data.items.filter((i) => i.status === s && !isAdvisory(i)).length;
  const open = data.items.length - count("met") - advisory;
  if (!data.items.length) {
    return (
      <p className="dq-guidance" data-testid="decision-must-haves">
        <b>Must-haves not checked</b>{data.why ? `: ${data.why}` : ""}. Read the change against
        the brief before merging.
      </p>
    );
  }
  return (
    <details className="dq-guidance" open={open > 0} data-testid="decision-must-haves">
      <summary>
        Must-haves: {count("met")} met, {count("partial")} partial, {count("missing")} missing
        {count("unjudged") ? `, ${count("unjudged")} not judged` : ""}
        {advisory ? `, ${advisory} advisory (taste)` : ""}
        {data.dropped ? `, ${data.dropped} more not checked` : ""}
      </summary>
      <table className="v2-table">
        <thead><tr><th>#</th><th>Must-have</th><th>Status</th><th>Evidence</th></tr></thead>
        <tbody>
          {data.items.map((i) => (
            <tr key={i.n} data-status={i.status} data-judgement={i.judgement ? "true" : undefined}>
              <td>{i.n}</td>
              <td>{i.text}{i.judgement ? <span className="dq-muted" title="A matter of taste: never fails the check"> (taste)</span> : null}</td>
              <td><b>{WORD[i.status] ?? i.status}</b>{isAdvisory(i) ? " — advisory (taste)" : ""}</td>
              <td className="v2-mono">{i.evidence}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

/**
 * READY, WITH FOLLOW-UPS (engine quality_bar.merge_followups). A build whose only open items are
 * follow-ups (review findings below the quality bar's blocking severity, gates the bar let
 * through) is a normal ready-to-merge card: never "Checks failing", never a reason to type.
 * BOSS's follow-up build fixes them; the card lists them, P1 first, folded.
 */
export type FollowUpItem = { tier: string; severity: string; text: string };
export type FollowUpsRecord = { count: number; p1: number; queued: boolean; bar: string; items: FollowUpItem[];
  /** Who takes them, in words (engine quality_bar.followup_words): never "BOSS is fixing it" unless a build will. */
  words: string };

export function followUpsOf(recommendation: Record<string, unknown> | null | undefined
): FollowUpsRecord | null {
  if (checksFailingOf(recommendation)) return null;   // a failing card says that instead
  const raw = recommendation?.followups;
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const items = (Array.isArray(r.items) ? r.items : [])
    .filter((i): i is Record<string, unknown> => !!i && typeof i === "object")
    .map((i) => ({ tier: str(i.tier) || "P1", severity: str(i.severity), text: str(i.text) }))
    .filter((i) => i.text);
  const count = typeof r.count === "number" ? r.count : items.length;
  if (!count) return null;
  return { count, p1: typeof r.p1 === "number" ? r.p1 : items.filter((i) => i.tier === "P1").length,
           queued: r.queued === true, bar: str(r.bar) || "standard", items,
           words: str(r.words) };
}

export function FollowUps({ record }: { record: FollowUpsRecord }) {
  const n = record.count;
  return (
    <details className="dq-guidance" data-testid="decision-follow-ups">
      <summary>
        <b>Ready to merge.</b>{" "}
        {record.words || `${n} follow-up${n === 1 ? "" : "s"}${record.queued ? ` queued, BOSS is fixing ${n === 1 ? "it" : "them"}` : " listed for later"}`}
        {record.p1 ? ` (${record.p1} P1)` : ""}. {n === 1 ? "It does" : "They do"} not block at the {record.bar} quality bar.
      </summary>
      <ul>
        {record.items.map((f, i) => (
          <li key={i}>
            <span className="v2-mono">{f.tier}{f.severity ? ` [${f.severity.toUpperCase()}]` : ""}</span>{" "}
            <span style={{ whiteSpace: "pre-wrap" }}>{f.text}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
