"use client";

/**
 * Spend › This month, reconciled (BOSSAPP-864): estimated, reconciled, seat fees and total.
 * The same card posts an invoice and a seat price. It does not close the month.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { tip14 } from "../../lib/govern";
import {
  INVOICE_PROVIDERS, importNotice, postInvoice, postSeatPrice, type InvoiceProvider,
} from "../../lib/invoice-form";

export type Reconciliation = {
  estimated_usd_micros: number; reconciled_usd_micros: number; fees_usd_micros: number;
  total_usd_micros: number; by_provider?: Record<string, { invoice_lines: number }>;
};

const usd = (m: number) => `$${(m / 1_000_000).toFixed(2)}`;

const PROVIDER_LABEL: Record<InvoiceProvider, string> = {
  anthropic: "Anthropic", openai: "OpenAI", openrouter: "OpenRouter",
};

export function invoiceLines(r: Reconciliation): number {
  return Object.values(r.by_provider ?? {}).reduce((n, p) => n + (p.invoice_lines ?? 0), 0);
}

function InvoiceError({ text }: { text: string }) {
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (words.length > 6) {
    return <p className="gv-field-error" role="alert" data-testid="gv-invoice-error">{text}</p>;
  }
  return <span className="gv-field-error" role="alert" data-testid="gv-invoice-error">{text}</span>;
}

export function ReconciliationCard({ projectId }: { projectId: string }) {
  const [r, setR] = useState<Reconciliation | null>(null);
  const [revision, setRevision] = useState(0);
  const [provider, setProvider] = useState<string>("anthropic");
  const [content, setContent] = useState("");
  const [plan, setPlan] = useState("");
  const [seats, setSeats] = useState("1");
  const [monthly, setMonthly] = useState("");
  const [busy, setBusy] = useState<"" | "invoice" | "price">("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void fetch(`/v1/spend/reconciliation?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null)).catch(() => null)
      .then((body) => { if (alive) setR(body as Reconciliation | null); });
    return () => { alive = false; };
  }, [projectId, revision]);

  async function submit(kind: "invoice" | "price", event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(kind);
    setError(null);
    setNotice(null);
    try {
      const result = kind === "invoice"
        ? await postInvoice(fetch, { projectId, provider, content })
        : await postSeatPrice(fetch, { projectId, provider, plan, seats, monthlyDollars: monthly });
      if (!result.ok) {
        setError(result.error);
        return;
      }
      setNotice(importNotice(result) ?? (kind === "price" ? "Saved" : null));
      setRevision((n) => n + 1);
    } catch {
      setError("Can't reach BOSS");
    } finally {
      setBusy("");
    }
  }

  if (!r) return null;
  const lines = invoiceLines(r);
  const lineTip = lines ? `${lines} invoice line${lines === 1 ? "" : "s"} imported` : "No invoice imported yet";
  return (
    <section className="gv-card" aria-labelledby="gv-recon-h" data-testid="gv-reconciliation">
      <h3 id="gv-recon-h" className="gv-sec-head">THIS MONTH, RECONCILED</h3>
      <dl className="gv-pair">
        <div title={tip14("List price x tokens, money only")}><dt>Estimated</dt><dd data-testid="gv-recon-estimated">{usd(r.estimated_usd_micros)}</dd></div>
        <div title={tip14(lineTip)}>
          <dt>Reconciled</dt><dd data-testid="gv-recon-reconciled">{usd(r.reconciled_usd_micros)}</dd></div>
        <div title={tip14("Subscription seats, per day")}><dt>Seat fees</dt><dd data-testid="gv-recon-fees">{usd(r.fees_usd_micros)}</dd></div>
        <div><dt>Total</dt><dd data-testid="gv-recon-total">{usd(r.total_usd_micros)}</dd></div>
      </dl>
      <div className="gv-init">
        <label>
          Provider
          <select className="gv-input" data-testid="gv-invoice-provider" value={provider}
                  title={tip14("Anthropic, OpenAI or OpenRouter")}
                  onChange={(e) => setProvider(e.target.value)}>
            {INVOICE_PROVIDERS.map((id) => <option key={id} value={id}>{PROVIDER_LABEL[id]}</option>)}
          </select>
        </label>
      </div>
      <form className="gv-init" noValidate onSubmit={(e) => void submit("invoice", e)}>
        <label style={{ flexBasis: "100%" }}>
          Invoice
          <textarea className="gv-input" data-testid="gv-invoice-content" rows={5} value={content}
                    title={tip14("CSV or JSON from one provider")}
                    onChange={(e) => setContent(e.target.value)}
                    style={{ height: "auto", minHeight: "6.5rem", width: "100%", padding: "8px 10px" }} />
        </label>
        <GatedButton permission="budget:manage" type="submit" className="ag-btn ag-btn-primary"
                     disabled={busy !== ""} data-testid="gv-invoice-submit" title={tip14("Send the invoice")}>
          Import
        </GatedButton>
      </form>
      <form className="gv-init" noValidate onSubmit={(e) => void submit("price", e)}>
        <label>
          Plan
          <input className="gv-input" data-testid="gv-price-plan" value={plan}
                 title={tip14("Name on the provider bill")} onChange={(e) => setPlan(e.target.value)} />
        </label>
        <label>
          Seats
          <input className="gv-input" data-testid="gv-price-seats" inputMode="numeric" value={seats}
                 title={tip14("Whole seats, at least one")} onChange={(e) => setSeats(e.target.value)} />
        </label>
        <label>
          Monthly
          <input className="gv-input" data-testid="gv-price-monthly" inputMode="decimal" value={monthly}
                 title={tip14("Dollars for one month")} onChange={(e) => setMonthly(e.target.value)} />
        </label>
        <GatedButton permission="budget:manage" type="submit" className="ag-btn ag-btn-primary"
                     disabled={busy !== ""} data-testid="gv-price-submit" title={tip14("Save the seat price")}>
          Save
        </GatedButton>
      </form>
      {notice ? <span className="gv-muted" data-testid="gv-invoice-result">{notice}</span> : null}
      {error ? <InvoiceError text={error} /> : null}
    </section>
  );
}
