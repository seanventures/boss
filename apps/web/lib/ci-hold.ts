/**
 * HELD BY CI (BOSSAPP-946): a merge or deploy whose CI has not cleared is not offered as an
 * Approve that the gate then refuses. The engine (ci_gate.approval_hold) sends {state, label,
 * reason}; every surface draws the approve button as `label` ("Checks running…"), disabled,
 * with `reason` beside it — the project drawer, Needs you and Approvals alike.
 */
export type CiHold = { state: string; label: string; reason: string };

export function parseCiHold(v: unknown): CiHold | null {
  if (!v || typeof v !== "object") return null;
  const x = v as Record<string, unknown>;
  const label = typeof x.label === "string" ? x.label : "";
  if (!label) return null;
  return { state: typeof x.state === "string" ? x.state : "", label,
           reason: typeof x.reason === "string" ? x.reason : "" };
}
