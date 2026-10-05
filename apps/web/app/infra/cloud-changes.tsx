"use client";

/**
 * Infra › Cloud › Changes (spec §4.8; parity P-S-35, P-S-36, P-S-43): every proposed change to
 * a cloud account — its operations, rollback plan, receipts and drift reports — with `Approve`
 * and `Apply` where the server says they apply, a `Drift` chip that narrows to changes with
 * drift, and `+ Change`: an Operations editor that refuses invalid JSON with the line it broke
 * on, and a cost typed in dollars (sent as micros). Every "change set" link lands here
 * (`?area=infra&view=cloud&cloud=changes&changeset=<id>`).
 */
import React, { useMemo, useState } from "react";

import { showToast } from "../client-forms";
import type { CloudViewProps } from "./cloud";
import { applyChange, approveChange, createChange } from "./cloud-actions";
import { accountOptions, dollarsToMicros, parseChange, parseOperations, type CloudChange } from "./cloud-rows";
import { afterWrite } from "./infra-actions";
import { ago, ErrorLine, localTime, useNow } from "./infra-util";
import { Drawer, DrawerSection, Empty, Sheet, StateWord, ZoneHead } from "./kit";

const OPS_EXAMPLE = `[
  {
    "skill_id": "digitalocean.droplet",
    "action": "resize",
    "resource_type": "droplet",
    "resource_ref": "drop_1",
    "desired_state": { "size": "s-2vcpu-4gb" }
  }
]`;

export function CloudChanges(p: CloudViewProps) {
  const { body, url, go, canWrite } = p;
  const now = useNow(30_000);
  const rows = useMemo(() => body.rows.map(parseChange), [body.rows]);
  const [drift, setDrift] = useState(false);
  const [adding, setAdding] = useState(false);
  const open = url.changeset ? rows.find((r) => r.id === url.changeset) ?? null : null;
  const q = p.query.trim().toLowerCase();
  const shown = rows.filter((r) => (!drift || r.drift.length > 0) && (!q || `${r.title} ${r.account?.name ?? ""}`.toLowerCase().includes(q)));
  const drifted = rows.filter((r) => r.drift.length > 0).length;

  return (
    <section className="cloud-sec" aria-labelledby="inf-cloud-changes">
      <ZoneHead id="inf-cloud-changes" label="CHANGES" count={rows.length}>
        <span className="inf-seg" role="group" aria-label="Filter">
          <button type="button" className="inf-seg-btn" aria-pressed={!drift} onClick={() => setDrift(false)}>All</button>
          <button type="button" className="inf-seg-btn" aria-pressed={drift} data-testid="inf-change-drift-chip"
                  title="Changes whose account drifted from what BOSS expects" onClick={() => setDrift(true)}>
            Drift{drifted ? <span className="cloud-seg-n">{drifted}</span> : null}
          </button>
        </span>
        {canWrite ? <button type="button" className="ag-btn" data-testid="inf-change-new" onClick={() => setAdding(true)}>+ Change</button> : null}
      </ZoneHead>
      {rows.length === 0 ? <Empty testId="inf-cloud-empty">No changes yet</Empty> : (
        <div className="inf-table-wrap">
          <table className="inf-table" data-testid="inf-cloud-table">
            <thead><tr>
              <th scope="col">Change</th><th scope="col">Account</th><th scope="col">Risk</th><th scope="col">Cost</th>
              <th scope="col">State</th><th scope="col">Age</th>
            </tr></thead>
            <tbody>
              {shown.length === 0 ? <tr><td colSpan={6} className="inf-muted">No drift</td></tr> : null}
              {shown.map((c) => (
                <tr key={c.id} className="cloud-row" data-open={open?.id === c.id ? "1" : undefined} data-testid={`inf-change-row-${c.id}`}
                    onClick={() => go({ changeset: c.id })}>
                  <td><button type="button" className="cloud-open" onClick={(e) => { e.stopPropagation(); go({ changeset: c.id }); }}>
                    <span className="inf-trunc" data-copy="data" title={c.title}>{c.title}</span></button>
                    {c.drift.length ? <span className="cloud-chip" data-kind="drift">Drift</span> : null}</td>
                  <td><span data-copy="data">{c.account?.name ?? "—"}</span></td>
                  <td className="inf-num" title="Higher tiers need a closer look">Tier {c.risk}</td>
                  <td className="inf-num">{c.cost}</td>
                  <td><StateWord w={c.state} /></td>
                  <td title={c.createdAt ? localTime(c.createdAt) : undefined}>{ago(c.createdAt, now)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open ? <ChangeDrawer {...p} change={open} onClose={() => go({ changeset: null })} /> : null}
      {adding ? <ChangeSheet {...p} onClose={() => setAdding(false)} /> : null}
    </section>
  );
}

function ChangeDrawer({ change: c, actor, poll, canWrite, onClose }: CloudViewProps & { change: CloudChange; onClose: () => void }) {
  const [json, setJson] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const act = async (kind: "approve" | "apply") => {
    setBusy(kind); setError(null);
    const r = kind === "approve" ? await approveChange(actor, c.id) : await applyChange(actor, c.id);
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", kind === "approve" ? "Change approved" : "Change applied");
    afterWrite(poll.refresh);
  };
  return (
    <Drawer label={`${c.title} change`} testId="inf-change-drawer" width={520} onClose={onClose}
            head={<><strong className="inf-trunc" data-copy="data" title={c.title}>{c.title}</strong><StateWord w={c.state} /></>}>
      <span className="inf-muted"><span data-copy="data">{c.account?.name ?? "No account"}</span> · Tier {c.risk} · {c.cost}</span>
      {c.summary ? <span className="inf-muted inf-trunc" data-copy="data" title={c.summary}>{c.summary}</span> : null}
      {canWrite && (c.can.approve || c.can.apply) ? (
        <div className="inf-actions">
          {c.can.approve ? <button type="button" className="ag-btn ag-btn-primary" disabled={busy !== null} data-testid="inf-change-approve"
                                   onClick={() => void act("approve")}>Approve</button> : null}
          {c.can.apply ? <button type="button" className="ag-btn ag-btn-primary" disabled={busy !== null} data-testid="inf-change-apply"
                                 title="Runs the operations against the provider" onClick={() => void act("apply")}>Apply</button> : null}
        </div>
      ) : null}
      <ErrorLine error={error} testId="inf-change-error" />
      <DrawerSection label="OPERATIONS" testId="inf-change-ops-list"
                     actions={<button type="button" className="inf-link" aria-pressed={json} onClick={() => setJson((v) => !v)}>{json ? "Hide JSON" : "Show JSON"}</button>}>
        {json ? <pre className="cloud-code" data-copy="data">{c.operationsJson || JSON.stringify(c.operations, null, 2)}</pre> : (
          c.operations.length === 0 ? <Empty>No operations</Empty> : (
            <ul className="cloud-list">
              {c.operations.map((o, i) => (
                <li key={i}><span data-copy="data">{o.action}</span><span className="inf-muted" data-copy="data">{o.resourceType}{o.resourceRef ? ` · ${o.resourceRef}` : ""}</span>
                  {o.destructive ? <span className="cloud-chip" data-kind="destructive" title="Deletes or replaces something">Destructive</span> : null}</li>
              ))}
            </ul>
          )
        )}
      </DrawerSection>
      <DrawerSection label="ROLLBACK" testId="inf-change-rollback">
        {c.rollbackPlan.length === 0 ? <Empty>No rollback plan</Empty> : (
          <ol className="cloud-list">{c.rollbackPlan.map((s, i) => <li key={i}><span data-copy="data">{s}</span></li>)}</ol>
        )}
      </DrawerSection>
      <DrawerSection label="RECEIPTS" testId="inf-change-receipts">
        {c.receipts.length === 0 ? <Empty>No receipts yet</Empty> : (
          <ul className="cloud-list">{c.receipts.map((r) => (
            <li key={r.id}><span data-copy="data">{r.action}</span><span className="inf-faint">{r.status}</span>
              <span className="inf-mono inf-faint" data-copy="data">{r.id}</span></li>
          ))}</ul>
        )}
      </DrawerSection>
      <DrawerSection label="DRIFT" testId="inf-change-drift">
        {c.drift.length === 0 ? <Empty>No drift</Empty> : (
          <ul className="cloud-list">{c.drift.map((d) => (
            <li key={d.id}><span data-copy="data">{d.classification}</span>
              <span className="inf-trunc inf-muted" data-copy="data" title={d.summary}>{d.summary}</span>
              <span className="inf-num inf-faint" title="Unmanaged · orphaned · security-affecting">{d.unmanaged} · {d.orphaned} · {d.security}</span></li>
          ))}</ul>
        )}
      </DrawerSection>
    </Drawer>
  );
}

function ChangeSheet({ body, projectId, actor, poll, go, onClose }: CloudViewProps & { onClose: () => void }) {
  const accounts = accountOptions(body.filters, body.header);
  const [f, setF] = useState({ account: "", title: "", summary: "", ops: OPS_EXAMPLE, cost: "0", risk: "2", rollback: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const checked = useMemo(() => parseOperations(f.ops), [f.ops]);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value });
  const submit = async () => {
    if (!f.title.trim() || !f.summary.trim()) { setError("Title and summary"); return; }
    if (!checked.ok) { setError(checked.error); return; }
    const micros = dollarsToMicros(f.cost);
    if (micros === null) { setError("Cost is a dollar amount"); return; }
    setBusy(true); setError(null);
    const r = await createChange(projectId, actor, {
      provider_account_id: f.account || null, title: f.title.trim(), summary: f.summary.trim(), operations: checked.ops,
      expected_cost_usd_micros: micros, risk_tier: Number(f.risk),
      rollback_plan: f.rollback.split("\n").map((s) => s.trim()).filter(Boolean),
    });
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Change created");
    afterWrite(poll.refresh);
    onClose();
    if (r.data.id) go({ changeset: r.data.id });
  };
  return (
    <Sheet title="Change" testId="inf-change-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <div className="inf-form-row">
          <label className="inf-field"><span>Account</span>
            <select value={f.account} onChange={set("account")} data-testid="inf-change-account">
              <option value="">None</option>{accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select></label>
          <label className="inf-field" title="Higher tiers need a closer look"><span>Risk</span>
            <select value={f.risk} onChange={set("risk")}>{[1, 2, 3, 4].map((n) => <option key={n} value={n}>Tier {n}</option>)}</select></label>
          <label className="inf-field" title="Dollars this change is expected to cost"><span>Cost $</span>
            <input inputMode="decimal" value={f.cost} onChange={set("cost")} data-testid="inf-change-cost" /></label>
        </div>
        <label className="inf-field"><span>Title</span><input value={f.title} onChange={set("title")} data-testid="inf-change-title" /></label>
        <label className="inf-field"><span>Summary</span><input value={f.summary} onChange={set("summary")} data-testid="inf-change-summary" /></label>
        <label className="inf-field" data-invalid={checked.ok ? undefined : "1"} title="A list of operations: skill_id, action, resource_type">
          <span>Operations</span>
          <textarea rows={8} value={f.ops} onChange={set("ops")} spellCheck={false} data-testid="inf-change-ops"
                    aria-invalid={!checked.ok} aria-describedby={checked.ok ? undefined : "inf-change-ops-error"} />
        </label>
        {!checked.ok ? <span id="inf-change-ops-error" className="inf-error" role="alert" data-testid="inf-change-ops-error">{checked.error}</span> : null}
        <label className="inf-field" title="One step per line"><span>Rollback plan</span>
          <textarea rows={2} value={f.rollback} onChange={set("rollback")} /></label>
        <ErrorLine error={error} testId="inf-change-sheet-error" />
        <div className="inf-actions">
          <button type="submit" className="ag-btn ag-btn-primary" disabled={busy || !checked.ok} data-testid="inf-change-submit">Create</button>
        </div>
      </form>
    </Sheet>
  );
}
