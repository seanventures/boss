/**
 * A GATE THAT WAS JUST ANSWERED STOPS ASKING FOR A PERSON AT ONCE (BOSSAPP-511).
 *
 * Run wfr_3c8ff51c: the operator approved a gate step, and its amber "Needs you" stayed on the
 * run lane until they reloaded the page. The run list reads the decision's state live, but only
 * when it is fetched again, and an approval pressed on Agents › Live or an Infra sheet told the
 * Workflows screen nothing. So an accepted answer is recorded HERE, from the two doors every
 * answer goes through:
 *
 *   - lib/call-action.ts, for a server action whose result names the decision it answered
 *     (`data.decision = {id, status}` — resolveDecisionAction says so);
 *   - lib/api-write.ts, for a browser write to `/v1/decisions/{id}/{approve|reject}` that the
 *     API accepted (Infra's deploy sheets, the stack deploy, the security checklist).
 *
 * Every screen that draws a gate reads its runs through `withAnsweredGates` (optimistic), and
 * the answer also says "changed" so every panel refetches the truth. The overlay never re-opens
 * anything and lapses after ANSWER_TTL_MS, by when the server's own state has long arrived.
 */
import { notifyChanged } from "../app/changed";

export const DECIDED_EVENT = "boss:decision-answered";
export const ANSWER_TTL_MS = 5 * 60_000;

/** Gate states that can still be answered (the same set the run view uses). */
const OPEN = new Set(["pending", "delegated"]);
const VERDICT_STATUS: Record<string, string> = { approve: "approved", reject: "rejected" };

const answered = new Map<string, { status: string; at: number }>();

/** Record an accepted answer. `status` is a decision status (approved/rejected) or a verb. */
export function noteDecided(decisionId: string, status: string): void {
  const s = VERDICT_STATUS[status] ?? status;
  if (!decisionId || !s || OPEN.has(s)) return;
  answered.set(decisionId, { status: s, at: Date.now() });
  if (typeof window === "undefined") return;
  try { window.dispatchEvent(new CustomEvent(DECIDED_EVENT, { detail: { decisionId, status: s } })); } catch { /* no bus */ }
  notifyChanged("decision", { decisionId, status: s });
}

/** The answer this console just gave a decision, while it is fresh; null otherwise. */
export function answeredStatus(decisionId: string | null | undefined): string | null {
  if (!decisionId) return null;
  const hit = answered.get(decisionId);
  if (!hit) return null;
  if (Date.now() - hit.at > ANSWER_TTL_MS) { answered.delete(decisionId); return null; }
  return hit.status;
}

/** A browser write the API accepted: was it an answer to a decision? */
const DECISION_WRITE = /\/v1\/decisions\/([^/?#]+)\/(approve|reject)(?:[?#]|$)/;
export function noteDecisionWrite(url: string, ok: boolean): void {
  if (!ok) return;
  const m = DECISION_WRITE.exec(url);
  if (m) noteDecided(decodeURIComponent(m[1]), m[2]);
}

/** A server action's accepted result: did it answer a decision? */
export function noteDecisionResult(value: unknown): void {
  const v = value as { ok?: unknown; data?: { decision?: { id?: unknown; status?: unknown } } } | null;
  const d = v?.ok === true ? v.data?.decision : undefined;
  if (d && typeof d.id === "string" && typeof d.status === "string") noteDecided(d.id, d.status);
}

type GateStep = { decision_id?: string | null; decision_status?: string | null };

/** The runs as the person should see them now: a gate they just answered reads as answered,
 *  until the server says the same (or something else that is no longer open). Unchanged runs
 *  keep their identity, so nothing re-renders for nothing. */
export function withAnsweredGates<R extends { steps?: S[] | null }, S extends GateStep>(runs: R[]): R[] {
  if (!answered.size) return runs;
  return runs.map((run) => {
    let touched = false;
    const steps = (run.steps ?? []).map((step) => {
      const mine = answeredStatus(step.decision_id);
      if (!mine || (step.decision_status && !OPEN.has(step.decision_status))) return step;
      touched = true;
      return { ...step, decision_status: mine };
    });
    return touched ? { ...run, steps } : run;
  });
}

/** Tests: forget every answer. */
export function resetAnsweredGatesForTest(): void {
  answered.clear();
}
