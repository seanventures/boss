/**
 * ONE WAY TO WRITE MONEY on every run surface: the step's Done-by line, the agent card, the
 * chain's credits and the run report. They printed "$0.00", "$0.002" and "$0.01" for amounts of
 * the same size on one screen (walk of ws/step-detail, 2026-10-04).
 *
 * Cents, and "<$0.01" for a spend too small to round to a cent but not nothing; whole dollars
 * from $10.
 */
export function usd(amount: number): string {
  const n = Number(amount) || 0;
  if (n > 0 && n < 0.005) return "<$0.01";
  return n >= 10 ? `$${n.toFixed(0)}` : `$${n.toFixed(2)}`;
}
