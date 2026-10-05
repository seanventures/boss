"use client";

/**
 * Infra › Cloud › Resources (spec §4.8; parity P-S-16, P-S-17, P-S-29..P-S-31, P-S-45): every
 * cloud resource BOSS tracks, including the infra droplets (rows of kind `Droplet` with their
 * TTL and expiry), the five scope chips (kept per viewer), `Sync all`, `Clean up…` (the resource
 * janitor) and the resource drawer with its durable links.
 */
import React, { useMemo, useState } from "react";

import { buildHref, deployHref } from "../../lib/infra-view";
import { showToast } from "../client-forms";
import type { CloudViewProps } from "./cloud";
import { cleanUp, syncAccount } from "./cloud-actions";
import { accountOptions, parseResource, type CloudResource } from "./cloud-rows";
import { afterWrite } from "./infra-actions";
import { ago, ErrorLine, localTime, useNow } from "./infra-util";
import { Drawer, DrawerSection, Empty, Sheet, StateWord, ZoneHead } from "./kit";

export type Scope = { provider: string; account: string; env: string; region: string; service: string };
export const EMPTY_SCOPE: Scope = { provider: "", account: "", env: "", region: "", service: "" };
const SCOPE_LABELS: [keyof Scope, string][] = [
  ["provider", "Provider"], ["account", "Account"], ["env", "Env"], ["region", "Region"], ["service", "Service"],
];

export function CloudResources(p: CloudViewProps & { scope: Scope; setScope: (s: Scope) => void }) {
  const { body, url, go, canWrite, scope, setScope, projectId, actor, poll } = p;
  const now = useNow(30_000);
  const rows = useMemo(() => body.rows.map(parseResource), [body.rows]);
  const [cleaning, setCleaning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = url.resource ? rows.find((r) => r.id === url.resource) ?? null : null;
  const q = p.query.trim().toLowerCase();
  const shown = q ? rows.filter((r) => `${r.name} ${r.provider} ${r.owner ?? ""}`.toLowerCase().includes(q)) : rows;
  const scoped = Object.values(scope).some(Boolean);

  const syncAll = async () => {
    setBusy(true); setError(null);
    const r = await syncAccount(projectId, actor, null);
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Sync started");
    afterWrite(poll.refresh);
  };

  return (
    <section className="cloud-sec" aria-labelledby="inf-cloud-resources">
      <ZoneHead id="inf-cloud-resources" label="RESOURCES" count={rows.length}>
        {canWrite ? (
          <>
            <button type="button" className="ag-btn" disabled={busy} data-testid="inf-resources-sync-all"
                    title="Reads inventory and costs from every account" onClick={() => void syncAll()}>Sync all</button>
            <button type="button" className="ag-btn" data-testid="inf-resources-cleanup"
                    title="Runs the resource janitor" onClick={() => setCleaning(true)}>Clean up…</button>
          </>
        ) : null}
      </ZoneHead>
      <div className="cloud-filters" role="group" aria-label="Scope" data-testid="inf-cloud-filters">
        {SCOPE_LABELS.map(([k, label]) => (
          <label key={k} className="cloud-filter">
            <span className="ag-sr">{label}</span>
            <select value={scope[k]} data-testid={`inf-cloud-filter-${k}`} onChange={(e) => setScope({ ...scope, [k]: e.target.value })}>
              <option value="">{label}</option>
              {(body.filters[k] ?? []).map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </label>
        ))}
        {scoped ? <button type="button" className="inf-link" data-testid="inf-cloud-filter-clear" onClick={() => setScope(EMPTY_SCOPE)}>Clear</button> : null}
      </div>
      <ErrorLine error={error} />
      {rows.length === 0 ? <Empty testId="inf-cloud-empty">{scoped ? "Nothing in this scope" : "No cloud resources"}</Empty> : (
        <div className="inf-table-wrap">
          <table className="inf-table" data-testid="inf-cloud-table">
            <thead><tr>
              <th scope="col">Resource</th><th scope="col">Provider / account</th><th scope="col">Location</th><th scope="col">Owner</th>
              <th scope="col">Health</th><th scope="col">Cost/mo</th><th scope="col">Last seen</th><th scope="col">TTL</th>
            </tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} className="cloud-row" data-open={open?.id === r.id ? "1" : undefined}
                    data-testid={r.kind === "Droplet" ? "inf-resource-droplet" : `inf-resource-row-${r.id}`} onClick={() => go({ resource: r.id })}>
                  <td><button type="button" className="cloud-open" onClick={(e) => { e.stopPropagation(); go({ resource: r.id }); }}>
                    <span className="inf-trunc" data-copy="data" title={r.name}>{r.name}</span></button>
                    {r.kind === "Droplet" ? <span className="cloud-chip">Droplet</span> : null}</td>
                  <td><span data-copy="data">{r.provider}{r.account ? ` · ${r.account.name}` : ""}</span></td>
                  <td><span data-copy="data">{[r.region, r.service].filter(Boolean).join(" · ") || "—"}</span></td>
                  <td><span data-copy="data">{r.owner ?? "—"}</span></td>
                  <td><StateWord w={r.health} /></td>
                  <td className="inf-num">{r.costMonth}</td>
                  <td title={r.lastSeenAt ? localTime(r.lastSeenAt) : undefined}>{r.lastSeenAt ? ago(r.lastSeenAt, now) : "—"}</td>
                  <td title={r.expiresAt ? `Expires at ${localTime(r.expiresAt)}` : undefined}>{r.ttl ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open ? <ResourceDrawer r={open} onClose={() => go({ resource: null })} /> : null}
      {cleaning ? <CleanUpSheet {...p} onClose={() => setCleaning(false)} /> : null}
    </section>
  );
}

function ResourceDrawer({ r, onClose }: { r: CloudResource; onClose: () => void }) {
  return (
    <Drawer label={`${r.name} resource`} testId="inf-resource-drawer" onClose={onClose}
            head={<><strong className="inf-trunc" data-copy="data" title={r.name}>{r.name}</strong><StateWord w={r.health} /></>}>
      <DrawerSection label="FACTS" testId="inf-resource-facts">
        <dl className="inf-facts">
          <dt>External ID</dt><dd className="inf-mono" data-copy="data">{r.externalId ?? "—"}</dd>
          <dt>Env</dt><dd data-copy="data">{r.env ?? "—"}</dd>
          <dt>Region</dt><dd data-copy="data">{[r.region, r.service].filter(Boolean).join(" · ") || "—"}</dd>
          <dt>Owner</dt><dd data-copy="data">{r.owner ?? "—"}</dd>
          <dt>Cost/mo</dt><dd>{r.costMonth}</dd>
          <dt>Last seen</dt><dd>{r.lastSeenAt ? localTime(r.lastSeenAt) : "—"}</dd>
          {r.droplet ? (
            <>
              <dt>Status</dt><dd data-copy="data">{r.droplet.status}</dd>
              <dt>Size</dt><dd data-copy="data">{r.droplet.size ?? "—"}</dd>
              <dt>Boundary</dt><dd data-copy="data">{r.droplet.boundary ?? "—"}</dd>
              <dt>TTL</dt><dd>{r.ttl ?? "—"}</dd>
              <dt>Expires</dt><dd>{r.expiresAt ? localTime(r.expiresAt) : "—"}</dd>
              <dt>Ref</dt><dd className="inf-mono" data-copy="data">{r.droplet.providerRef ?? "—"}</dd>
              <dt>Termination</dt><dd data-copy="data">{r.droplet.termination ?? "—"}</dd>
            </>
          ) : null}
        </dl>
      </DrawerSection>
      <DrawerSection label="LINKS" testId="inf-resource-links">
        <div className="inf-actions">
          {r.links.build ? <a className="inf-link" href={buildHref(r.links.build)}>Build ↗</a> : null}
          {r.links.deploy ? <a className="inf-link" href={deployHref(r.links.deploy)}>Deploy ↗</a> : null}
          {r.links.budget ? <a className="inf-link" href="?area=budget" title="Limits live in Govern">Budget ↗</a> : null}
          {r.links.receipt ? <span className="inf-mono inf-muted" data-copy="data" title="Receipt">{r.links.receipt}</span> : null}
          {!r.links.build && !r.links.deploy && !r.links.budget && !r.links.receipt ? <span className="inf-muted">No links</span> : null}
        </div>
      </DrawerSection>
    </Drawer>
  );
}

function CleanUpSheet({ body, projectId, actor, poll, onClose }: CloudViewProps & { onClose: () => void }) {
  const accounts = accountOptions(body.filters, body.header);
  const [account, setAccount] = useState("");
  const [max, setMax] = useState("10");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    const n = Number(max);
    if (!Number.isInteger(n) || n < 1 || n > 100) { setError("Between 1 and 100"); return; }
    setBusy(true); setError(null);
    const r = await cleanUp(projectId, actor, account || null, n);
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Clean up started");
    afterWrite(poll.refresh);
    onClose();
  };
  return (
    <Sheet title="Clean up" testId="inf-cleanup-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <div className="inf-form-row">
          <label className="inf-field"><span>Scope</span>
            <select value={account} onChange={(e) => setAccount(e.target.value)} data-testid="inf-cleanup-account">
              <option value="">All accounts</option>
              {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select></label>
          <label className="inf-field" title="The most resources it may propose to remove"><span>Max candidates</span>
            <input type="number" min={1} max={100} value={max} onChange={(e) => setMax(e.target.value)} data-testid="inf-cleanup-max" /></label>
        </div>
        <ErrorLine error={error} />
        <div className="inf-actions">
          <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-cleanup-submit"
                  title="Looks for resources nothing owns, up to the max">Clean up</button>
        </div>
      </form>
    </Sheet>
  );
}
