"use client";

/**
 * Govern › Envelopes (spec §4.4.5): allocations as a tree, the initiative request with its rule
 * as the SERVER states it, and commitments with today's FinOps honesty kept word for word in the
 * tooltips — "not loaded" and "no cost events" are never shown as $0.
 */
import React, { useState } from "react";

import { governWrite, money, readGovern, readNumber, rightsFor, type Allocation, type EnvelopesView } from "../../lib/govern";
import { usePolledRead } from "../../lib/log-stream";
import { showToast } from "../client-forms";
import { consoleHref } from "../url-intent";
import { plural } from "../../lib/factory-floor";

const HONESTY: Record<string, { word: string; tip: string }> = {
  not_loaded: { word: "Not loaded · not $0", tip: "Cloud spend is not loaded. This is not $0.00." },
  no_events: { word: "No cost events · not $0", tip: "No cost events in this window. Absence is not $0.00 spend." },
  recorded: { word: "Last 30d · recorded events", tip: "Totals come from recorded cost events only — not implied exact invoices." },
};

const VERDICT: Record<string, string> = {
  on_track: "On track", under_utilized: "Under used", over_utilized: "Over used",
};

function verdictWord(v: string): string {
  if (VERDICT[v]) return VERDICT[v];
  if (/under/.test(v)) return "Under used";
  if (/over/.test(v)) return "Over used";
  return "On track";
}

function dayWords(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function Tree({ items, parent, depth }: { items: Allocation[]; parent: string | null; depth: number }) {
  const children = items.filter((a) => (a.parent_allocation_id ?? null) === parent);
  if (!children.length) return null;
  return (
    <ul className="gv-tree" data-depth={depth}>
      {children.map((a) => {
        const used = a.actual_usd_micros + a.reserved_usd_micros;
        const ratio = a.amount_usd_micros > 0 ? Math.min(1, used / a.amount_usd_micros) : 0;
        return (
          <li key={a.id} data-testid="gv-allocation">
            <span className="gv-alloc-line">
              <span data-user-text title={a.id}>{a.scope_type.replace(/_/g, " ")} · {a.scope_id}</span>
              <span className="gv-bar" data-thin role="img" aria-label={`${money(used)} of ${money(a.amount_usd_micros)}`}>
                <span className="gv-bar-fill" style={{ width: `${(ratio * 100).toFixed(1)}%` }} />
              </span>
              <span>{money(used)} of {money(a.amount_usd_micros)}</span>
              <span className="gv-muted" title="Still available">{money(a.available_usd_micros)} left</span>
              <span className="gv-muted">{dayWords(a.period_start)} – {dayWords(a.period_end)}</span>
            </span>
            <Tree items={items} parent={a.id} depth={depth + 1} />
          </li>
        );
      })}
    </ul>
  );
}

export function EnvelopesTab({ projectId, actor, paused }: { projectId: string; actor: string; paused: boolean }) {
  const read = usePolledRead<EnvelopesView>({
    key: projectId, pollMs: 60_000, paused,
    read: async (etag, signal) => {
      const got = await readGovern<EnvelopesView>(projectId, "envelopes", {}, { etag, signal });
      return got.ok ? (got.notModified ? { ok: true as const, notModified: true, etag: got.etag }
        : { ok: true as const, data: got.data, etag: got.etag }) : { ok: false as const, error: got.error };
    },
  });
  const [title, setTitle] = useState("");
  const [tier, setTier] = useState("2");
  const [amount, setAmount] = useState("100");
  const [result, setResult] = useState<{ text: string; decision?: string | null; tip?: string } | null>(null);
  const [busy, setBusy] = useState("");
  const v = read.data;
  const search = typeof window === "undefined" ? "" : window.location.search;

  const reconcile = async () => {
    setBusy("reconcile");
    const r = await governWrite("reconcile", { project_id: projectId, actor });
    setBusy("");
    if (r.ok) {
      const n = Number((r.data as { reconciled?: number }).reconciled ?? 0);
      showToast("success", `Reconciled · ${plural(n, "commitment")}`);
      read.reload();
    } else showToast("error", r.status === 403 ? "You can't change this" : r.error);
  };
  const request = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) { setResult({ text: "Name it first" }); return; }
    const t = readNumber(tier, { min: 0, max: 3, whole: true });
    if (t.value === null) { setResult({ text: `Tier: ${t.error}` }); return; }
    const a = readNumber(amount, { min: 0.01, cents: true, money: true });
    if (a.value === null) { setResult({ text: `Amount: ${a.error}` }); return; }
    setBusy("initiative");
    const r = await governWrite("initiative", {
      project_id: projectId, actor, title: title.trim(), tier: t.value,
      amount_usd_micros: Math.round(a.value * 1_000_000),
    });
    setBusy("");
    if (!r.ok) {
      setResult(r.status === 403 ? { text: "You can't change this", tip: rightsFor("initiative") } : { text: r.error });
      return;
    }
    showToast("success", "Initiative requested");
    const pending = r.data.status === "pending_approval";
    setResult({ text: pending ? "Waiting for approval" : "Approved", decision: (r.data.decision_id as string | null) ?? null });
    setTitle("");
  };

  if (read.error && !v) {
    return <div className="ag-empty" role="alert"><span title={read.error}>Can&apos;t reach BOSS</span>
      <button type="button" className="ag-btn" onClick={read.reload}>Retry</button></div>;
  }
  if (!v) return <div className="gv-skel" aria-busy="true" />;
  const honesty = HONESTY[v.finops.state] ?? HONESTY.not_loaded;
  const providers = v.finops.summary?.spend_by_provider ?? [];
  const recorded = providers.reduce((sum, p) => sum + p.cost_usd_micros, 0);
  const rule = v.initiative_rule;

  return (
    <div className="gv-envelopes" data-testid="gv-envelopes">
      <section className="gv-card" aria-labelledby="gv-alloc-h">
        <h3 id="gv-alloc-h" className="gv-sec-head">ALLOCATIONS</h3>
        {v.allocations.length ? <Tree items={v.allocations} parent={null} depth={0} />
          : <span className="gv-muted" data-testid="gv-no-envelopes" title="Created through the API today">No envelopes</span>}
      </section>
      <section className="gv-card" aria-labelledby="gv-init-h">
        <h3 id="gv-init-h" className="gv-sec-head">INITIATIVES</h3>
        <form className="gv-init" noValidate data-testid="gv-initiative-form" onSubmit={(e) => void request(e)}>
          <label>Initiative<input className="gv-input" value={title} placeholder="New GPU cluster"
                                 onChange={(e) => setTitle(e.target.value)} data-testid="gv-init-title" /></label>
          <label>Tier<input className="gv-input" type="number" min={0} max={3} step="any" value={tier}
                           onChange={(e) => setTier(e.target.value)} data-testid="gv-init-tier" /></label>
          <label>Amount ($)<input className="gv-input" type="number" min={0} step="any" value={amount}
                                 onChange={(e) => setAmount(e.target.value)} data-testid="gv-init-amount" /></label>
          <button type="submit" className="ag-btn ag-btn-primary" disabled={busy === "initiative"} data-testid="gv-init-request">Request</button>
        </form>
        <span className="gv-rule" data-testid="gv-init-rule" title="Above this, a person approves first">
          Tier {rule.tier_min}+ over {money(rule.amount_usd_micros)} needs approval
        </span>
        {result ? (
          <span className="gv-result" data-testid="gv-init-result" title={result.tip}>
            {result.text}
            {result.decision ? (
              <> · <a className="ag-link" href={consoleHref(search, { area: "decisions", decision: result.decision })}>Open ↗</a></>
            ) : null}
          </span>
        ) : null}
      </section>
      <section className="gv-card" aria-labelledby="gv-commit-h">
        <h3 id="gv-commit-h" className="gv-sec-head">
          COMMITMENTS
          <button type="button" className="ag-btn" disabled={busy === "reconcile"} data-testid="gv-reconcile"
                  onClick={() => void reconcile()}>Reconcile</button>
        </h3>
        <span className="gv-honesty" data-testid="gv-finops-honesty" data-state={v.finops.state} title={honesty.tip}>
          {honesty.word}
          {v.finops.state === "recorded" ? <b data-testid="gv-finops-amount"> {money(recorded)}</b> : null}
        </span>
        {v.finops.summary ? (
          <span className="gv-muted" data-testid="gv-anomalies">Open anomalies {v.finops.summary.anomalies_open_count ?? 0}</span>
        ) : null}
        {v.finops.state === "recorded" ? (
          <ul className="gv-list">
            {providers.slice(0, 4).map((p) => (
              <li key={p.provider}><span data-user-text>{p.provider}</span><span>{money(p.cost_usd_micros)}</span>
                <span className="gv-muted">{p.event_count} events</span></li>
            ))}
          </ul>
        ) : null}
        {v.reconciliations.length ? (
          <ul className="gv-list" data-testid="gv-reconciliations">
            {v.reconciliations.slice(0, 6).map((r) => (
              <li key={r.id}>
                <span data-user-text title={r.commitment_id}>{r.provider}</span>
                <span>{money(r.actual_usd_micros)} of {money(r.committed_usd_micros)}</span>
                <span className="gv-verdict" data-verdict={r.verdict} title={r.verdict}>{verdictWord(r.verdict)}</span>
                {r.decision_id ? (
                  <a className="ag-link" href={consoleHref(search, { area: "decisions", decision: r.decision_id })}>Decision ↗</a>
                ) : null}
              </li>
            ))}
          </ul>
        ) : <span className="gv-muted" title="Press Reconcile to score each commitment">No reconciliations yet</span>}
      </section>
    </div>
  );
}
