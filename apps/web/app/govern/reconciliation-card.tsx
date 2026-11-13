"use client";

/**
 * Spend › This month, reconciled (BOSSAPP-864): ESTIMATED (list price x tokens, money only),
 * RECONCILED (the estimate replaced by the provider invoice wherever one was imported), SEAT FEES
 * (subscription prices amortised per day) and the TOTAL. Read-only; prices and invoices are set
 * through /v1/subscription-prices and /v1/invoices/import.
 */
import React, { useEffect, useState } from "react";

export type Reconciliation = {
  estimated_usd_micros: number; reconciled_usd_micros: number; fees_usd_micros: number;
  total_usd_micros: number; by_provider?: Record<string, { invoice_lines: number }>;
};

const usd = (m: number) => `$${(m / 1_000_000).toFixed(2)}`;

export function invoiceLines(r: Reconciliation): number {
  return Object.values(r.by_provider ?? {}).reduce((n, p) => n + (p.invoice_lines ?? 0), 0);
}

export function ReconciliationCard({ projectId }: { projectId: string }) {
  const [r, setR] = useState<Reconciliation | null>(null);
  useEffect(() => {
    let alive = true;
    void fetch(`/v1/spend/reconciliation?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null)).catch(() => null)
      .then((body) => { if (alive) setR(body as Reconciliation | null); });
    return () => { alive = false; };
  }, [projectId]);
  if (!r) return null;
  const lines = invoiceLines(r);
  return (
    <section className="gv-card" aria-labelledby="gv-recon-h" data-testid="gv-reconciliation">
      <h3 id="gv-recon-h" className="gv-sec-head">THIS MONTH, RECONCILED</h3>
      <dl className="gv-pair">
        <div title="List price x tokens, money only"><dt>Estimated</dt><dd data-testid="gv-recon-estimated">{usd(r.estimated_usd_micros)}</dd></div>
        <div title={lines ? `${lines} invoice line${lines === 1 ? "" : "s"} imported` : "No invoice imported yet"}>
          <dt>Reconciled</dt><dd data-testid="gv-recon-reconciled">{usd(r.reconciled_usd_micros)}</dd></div>
        <div title="Subscription seats, per day"><dt>Seat fees</dt><dd data-testid="gv-recon-fees">{usd(r.fees_usd_micros)}</dd></div>
        <div><dt>Total</dt><dd data-testid="gv-recon-total">{usd(r.total_usd_micros)}</dd></div>
      </dl>
    </section>
  );
}
