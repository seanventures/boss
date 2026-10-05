"use client";

/**
 * Infra › Cloud › Capacity (spec §4.8; parity P-S-18, P-S-19, P-S-24, P-S-26..P-S-28,
 * P-S-32..P-S-34): the worker pools, desired against actual, with the queue line, each
 * provider's API health, the counts the old fabric tiles showed, `+ Pool` (cost typed in
 * dollars, sent as micros), `Review scaling`, and the pool drawer's history and scale plans.
 */
import React, { useMemo, useState } from "react";

import { showToast } from "../client-forms";
import type { CloudViewProps } from "./cloud";
import { addPool, reviewScaling } from "./cloud-actions";
import { accountOptions, dollarsToMicros, parseCapacityHeader, parsePool, type CloudPool } from "./cloud-rows";
import { afterWrite } from "./infra-actions";
import { ago, ErrorLine, localTime, useNow } from "./infra-util";
import { Dot, Drawer, DrawerSection, Empty, Sheet, StateWord, ZoneHead } from "./kit";

export function CloudCapacity(p: CloudViewProps) {
  const { body, url, go, canWrite } = p;
  const now = useNow(30_000);
  const rows = useMemo(() => body.rows.map(parsePool), [body.rows]);
  const head = useMemo(() => parseCapacityHeader(body.header), [body.header]);
  const [sheet, setSheet] = useState<"pool" | "review" | null>(null);
  const open = url.pool ? rows.find((r) => r.id === url.pool) ?? null : null;
  const q = p.query.trim().toLowerCase();
  const shown = q ? rows.filter((r) => `${r.poolKey} ${r.kind}`.toLowerCase().includes(q)) : rows;

  return (
    <section className="cloud-sec" aria-labelledby="inf-cloud-capacity">
      <div className="cloud-lines">
        <span className="cloud-line inf-num" data-testid="inf-capacity-queue" title="Work waiting, held and running now">
          {head.queue ? <><span>Queue {head.queue.depth}</span><span aria-hidden> · </span><span>Leased {head.queue.leased}</span>
            <span aria-hidden> · </span><span>Running {head.queue.running}</span></> : "Queue —"}
          {head.queue?.realProvision ? <span className="cloud-chip" title="Scaling creates real machines">Real provision</span> : null}
        </span>
        <span className="cloud-line inf-num inf-muted" data-testid="inf-capacity-counts"
              title={head.fabric ? Object.entries(head.fabric.byKind).map(([k, n]) => `${k} ${n}`).join(" · ") || undefined : undefined}>
          {head.fabric ? <><span>Pools {head.fabric.pools}</span><span aria-hidden> · </span><span>Instances {head.fabric.instances}</span>
            <span aria-hidden> · </span><span title="Changes made on real hosts">Host effects {head.fabric.realHostEffects}</span></>
            : <>Pools {rows.length}</>}
        </span>
      </div>
      <div className="cloud-health" role="list" aria-label="API health" data-testid="inf-cloud-api-health">
        <span className="ag-zone-label">API health</span>
        {head.apiHealth.length === 0 ? <span className="inf-muted">No checks yet</span> : head.apiHealth.map((h) => (
          <span key={h.provider} role="listitem" className="cloud-health-item"
                title={`API ${h.api} · rate limit ${h.rateLimit} · cap ${h.usageCap}${h.checkedAt ? ` · checked ${localTime(h.checkedAt)}` : ""}`}>
            <Dot tone={h.state.tone} label={`${h.provider} ${h.state.word}`} />
            <span data-copy="data">{h.provider}</span><span className="inf-faint">{h.state.word}</span>
          </span>
        ))}
      </div>
      <ZoneHead id="inf-cloud-pools" label="POOLS" count={rows.length}>
        {canWrite ? (
          <>
            <button type="button" className="ag-btn" data-testid="inf-capacity-add-pool" onClick={() => setSheet("pool")}>+ Pool</button>
            <button type="button" className="ag-btn" data-testid="inf-capacity-review" title="Plans scaling from the queue and budget"
                    onClick={() => setSheet("review")}>Review scaling</button>
          </>
        ) : null}
      </ZoneHead>
      {rows.length === 0 ? <Empty testId="inf-cloud-empty">No worker pools</Empty> : (
        <div className="inf-table-wrap">
          <table className="inf-table" data-testid="inf-cloud-table">
            <thead><tr>
              <th scope="col">Pool</th><th scope="col">Kind</th><th scope="col">Desired</th><th scope="col">Actual</th>
              <th scope="col">Min–Max</th><th scope="col">Queue/worker</th><th scope="col">$/hour</th><th scope="col">Plan</th>
            </tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} className="cloud-row" data-open={open?.id === r.id ? "1" : undefined} data-testid={`inf-pool-row-${r.id}`}
                    data-short={r.actual < r.desired ? "1" : undefined} onClick={() => go({ pool: r.id })}>
                  <td><button type="button" className="cloud-open" onClick={(e) => { e.stopPropagation(); go({ pool: r.id }); }}>
                    <span className="inf-trunc" data-copy="data" title={r.poolKey}>{r.poolKey}</span></button></td>
                  <td>{r.kind}</td>
                  <td className="inf-num">{r.desired}</td>
                  <td className="inf-num" title={r.actual < r.desired ? "Fewer running than wanted" : undefined}>{r.actual}</td>
                  <td className="inf-num">{r.min}–{r.max}</td>
                  <td className="inf-num">{r.queuePerWorker ?? "—"}</td>
                  <td className="inf-num">{r.costHour}</td>
                  <td data-testid="inf-capacity-plan" title={r.plan ? `${r.plan.summary}${r.plan.at ? ` · ${localTime(r.plan.at)}` : ""}` : "No plan yet"}>
                    {r.plan ? <StateWord w={{ word: r.plan.word, tone: r.plan.tone }} /> : <span className="inf-faint">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open ? <PoolDrawer pool={open} now={now} onClose={() => go({ pool: null })} /> : null}
      {sheet === "pool" ? <PoolSheet {...p} onClose={() => setSheet(null)} /> : null}
      {sheet === "review" ? <ReviewSheet {...p} pools={rows} onClose={() => setSheet(null)} /> : null}
    </section>
  );
}

function PoolDrawer({ pool, now, onClose }: { pool: CloudPool; now: number | null; onClose: () => void }) {
  return (
    <Drawer label={`${pool.poolKey} pool`} testId="inf-pool-drawer" onClose={onClose}
            head={<><strong className="inf-trunc" data-copy="data" title={pool.poolKey}>{pool.poolKey}</strong>
              <span className="inf-muted inf-num">{pool.actual} of {pool.desired}</span></>}>
      <DrawerSection label="FACTS" testId="inf-pool-facts">
        <dl className="inf-facts">
          <dt>Kind</dt><dd>{pool.kind}</dd>
          <dt>Min–Max</dt><dd className="inf-num">{pool.min}–{pool.max}</dd>
          <dt>Queue/worker</dt><dd className="inf-num">{pool.queuePerWorker ?? "—"}</dd>
          <dt>$/hour</dt><dd className="inf-num">{pool.costHour}</dd>
        </dl>
      </DrawerSection>
      <DrawerSection label="HISTORY" testId="inf-pool-history">
        {pool.history.length === 0 ? <Empty>No scaling yet</Empty> : (
          <ul className="cloud-list">
            {pool.history.map((h, i) => (
              <li key={`${h.at}-${i}`}><span className="inf-num">{h.from} → {h.to}</span>
                <span className="inf-trunc inf-muted" data-copy="data" title={h.reason}>{h.reason}</span>
                <span className="inf-faint" title={h.at ? localTime(h.at) : undefined}>{ago(h.at, now)}</span></li>
            ))}
          </ul>
        )}
      </DrawerSection>
      <DrawerSection label="PLANS" testId="inf-pool-plans">
        {pool.plans.length === 0 ? <Empty>No plans</Empty> : (
          <ul className="cloud-list">
            {pool.plans.map((pl) => (
              <li key={pl.id}><span data-copy="data">{pl.action}{pl.desired !== null ? ` → ${pl.desired}` : ""}</span>
                <span className="inf-trunc inf-muted" data-copy="data" title={pl.reason}>{pl.reason}</span>
                <span className="inf-faint">{ago(pl.at, now)}</span></li>
            ))}
          </ul>
        )}
      </DrawerSection>
    </Drawer>
  );
}

function PoolSheet({ body, projectId, actor, poll, onClose }: CloudViewProps & { onClose: () => void }) {
  const accounts = accountOptions(body.filters, body.header);
  const [f, setF] = useState({ pool_key: "", kind: "local", account: "", current: "0", desired: "1", min: "0", max: "2",
    queue: "5", cost: "0", capabilities: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value });
  const submit = async () => {
    if (!/^[a-z0-9][a-z0-9_-]*$/i.test(f.pool_key.trim())) { setError("Name it: letters, digits, dashes"); return; }
    const ints = [f.current, f.desired, f.min, f.max, f.queue].map(Number);
    if (ints.some((n) => !Number.isInteger(n) || n < 0)) { setError("Counts are whole numbers"); return; }
    if (ints[2] > ints[3]) { setError("Min is above max"); return; }
    const micros = dollarsToMicros(f.cost);
    if (micros === null) { setError("Cost is a dollar amount"); return; }
    setBusy(true); setError(null);
    const r = await addPool(projectId, actor, {
      pool_key: f.pool_key.trim(), kind: f.kind, provider_account_id: f.account || null,
      current_workers: ints[0], desired_workers: ints[1], min_workers: ints[2], max_workers: ints[3],
      max_queue_depth_per_worker: ints[4], cost_per_worker_hour_usd_micros: micros,
      capabilities: f.capabilities.split("\n").map((s) => s.trim()).filter(Boolean),
    });
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Pool saved");
    afterWrite(poll.refresh);
    onClose();
  };
  return (
    <Sheet title="Worker pool" testId="inf-pool-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <div className="inf-form-row">
          <label className="inf-field"><span>Pool key</span><input value={f.pool_key} onChange={set("pool_key")} data-testid="inf-pool-key" /></label>
          <label className="inf-field"><span>Kind</span>
            <select value={f.kind} onChange={set("kind")}><option value="local">Local</option><option value="cloud">Cloud</option></select></label>
          <label className="inf-field"><span>Account</span>
            <select value={f.account} onChange={set("account")}>
              <option value="">None</option>{accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select></label>
        </div>
        <div className="inf-form-row">
          <label className="inf-field"><span>Current</span><input type="number" min={0} value={f.current} onChange={set("current")} /></label>
          <label className="inf-field"><span>Desired</span><input type="number" min={0} value={f.desired} onChange={set("desired")} /></label>
          <label className="inf-field"><span>Min</span><input type="number" min={0} value={f.min} onChange={set("min")} /></label>
          <label className="inf-field"><span>Max</span><input type="number" min={0} value={f.max} onChange={set("max")} /></label>
        </div>
        <div className="inf-form-row">
          <label className="inf-field" title="Queue depth one worker takes before scaling up"><span>Queue/worker</span>
            <input type="number" min={0} value={f.queue} onChange={set("queue")} /></label>
          <label className="inf-field" title="Dollars per worker per hour"><span>$/hour</span>
            <input inputMode="decimal" value={f.cost} onChange={set("cost")} data-testid="inf-pool-cost" /></label>
        </div>
        <label className="inf-field" title="One capability per line"><span>Capabilities</span>
          <textarea rows={2} value={f.capabilities} onChange={set("capabilities")} /></label>
        <ErrorLine error={error} testId="inf-pool-error" />
        <div className="inf-actions">
          <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-pool-submit">Save pool</button>
        </div>
      </form>
    </Sheet>
  );
}

function ReviewSheet({ projectId, actor, poll, pools, onClose }: CloudViewProps & { pools: CloudPool[]; onClose: () => void }) {
  const [pool, setPool] = useState("");
  const [spill, setSpill] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true); setError(null);
    const r = await reviewScaling(projectId, actor, pool || null, spill);
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Scaling reviewed");
    afterWrite(poll.refresh);
    onClose();
  };
  return (
    <Sheet title="Review scaling" testId="inf-review-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <label className="inf-field"><span>Pool</span>
          <select value={pool} onChange={(e) => setPool(e.target.value)} data-testid="inf-review-pool">
            <option value="">Default local pool</option>
            {pools.map((p) => <option key={p.id} value={p.id}>{p.poolKey}</option>)}
          </select></label>
        <label className="inf-check" title="May propose cloud workers when local ones run out">
          <input type="checkbox" checked={spill} onChange={(e) => setSpill(e.target.checked)} data-testid="inf-review-spill" />Allow cloud spillover
        </label>
        <ErrorLine error={error} />
        <div className="inf-actions">
          <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-review-submit">Review</button>
        </div>
      </form>
    </Sheet>
  );
}
