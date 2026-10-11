"use client";

/**
 * Infra › Plugins: the infrastructure plugin registry (boss.plugin/v1). One row per plugin: its
 * name and id, the support level BOSS promises, where it came from, what it provides, and
 * whether it is on. Turning a COMMUNITY plugin on first shows what it may touch (its declared
 * permissions) and asks once more; turning any plugin off is one press (the emergency stop).
 * The registry is install-wide, so the list is the same in every project.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { showToast } from "../client-forms";
import { permissionWords, usePlugins, type PluginRow, type PluginSource, type SupportLevel } from "../../lib/infra-view";
import type { InfraTabProps } from "./infra-home";
import { afterWrite, disablePlugin, enablePlugin } from "./infra-actions";
import { Empty, Skeleton, ZoneHead } from "./kit";

export const LEVEL_WORDS: Record<SupportLevel, { label: string; tip: string }> = {
  certified: { label: "Certified", tip: "Every lifecycle stage is tested in CI against pinned versions" },
  experimental: { label: "Experimental", tip: "A documented subset works; some stages are not verified yet" },
  community: { label: "Community", tip: "Third-party; BOSS maintainers make no certification promise" },
};
export const SOURCE_WORDS: Record<PluginSource, { label: string; tip: string }> = {
  builtin: { label: "Built-in", tip: "Ships with BOSS and is reviewed with it" },
  verified: { label: "Verified", tip: "Third-party, pinned to a version and checksum a maintainer checked" },
  community: { label: "Community", tip: "Third-party and unverified; off until an operator turns it on" },
};

function PluginRowView({ p, onChanged }: { p: PluginRow; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const perms = permissionWords(p);
  const lvl = LEVEL_WORDS[p.supportLevel];
  const src = SOURCE_WORDS[p.source];
  const claimed = p.claimedSupportLevel && p.claimedSupportLevel !== p.supportLevel
    ? ` (it claims ${LEVEL_WORDS[p.claimedSupportLevel].label}; a community plugin cannot)` : "";

  const write = async (on: boolean) => {
    setBusy(true);
    const r = on ? await enablePlugin(p.id) : await disablePlugin(p.id);
    setBusy(false);
    setConfirming(false);
    if (!r.ok) showToast("error", r.error);
    else afterWrite(onChanged);
  };
  const pressOn = () => (p.source === "community" ? setConfirming(true) : void write(true));

  return (
    <>
      <tr data-testid={`inf-plugin-row-${p.id}`} data-on={p.enabled ? "1" : undefined}>
        <td>
          <div title={p.description || undefined}><strong data-copy="data">{p.name}</strong></div>
          <div className="inf-faint" data-copy="data">{p.id} · {p.version}</div>
        </td>
        <td><span className="inf-chip" data-testid="inf-plugin-level" title={lvl.tip + claimed}>{lvl.label}</span></td>
        <td><span className="inf-muted" title={src.tip}>{src.label}</span></td>
        <td>
          {p.capabilities.map((c) => (
            <span key={c} className="inf-chip" data-copy="data" title={p.features.length ? `Features: ${p.features.join(", ")}` : c}>{c}</span>
          ))}
        </td>
        <td>
          {p.problems.length ? (
            <span className="inf-notice" data-testid="inf-plugin-problem" title={p.problems.join("\n")}>Blocked</span>
          ) : null}
          {p.enabled ? (
            <GatedButton permission="infra:provision" type="button" className="ag-btn" disabled={busy}
                         data-testid="inf-plugin-off" title="Turn off: new plans stop using it"
                         onClick={() => void write(false)}>{busy ? "…" : "On"}</GatedButton>
          ) : (
            <GatedButton permission="setup:manage" type="button" className="ag-btn" disabled={busy || p.problems.length > 0}
                         data-testid="inf-plugin-on"
                         title={p.problems.length ? p.problems.join("\n") : "Turn on for this install"}
                         onClick={pressOn}>{busy ? "…" : "Off"}</GatedButton>
          )}
        </td>
      </tr>
      {confirming ? (
        <tr data-testid={`inf-plugin-confirm-${p.id}`}>
          <td colSpan={5}>
            <span>Turn on {p.name}? It may: </span>
            <span data-testid="inf-plugin-perms">{perms.length ? perms.join(" · ") : "nothing beyond its own hooks"}</span>
            {" "}
            <GatedButton permission="setup:manage" type="button" className="ag-btn" disabled={busy}
                         data-testid="inf-plugin-confirm-on" title="Its code will run for this install"
                         onClick={() => void write(true)}>Turn on</GatedButton>
            <button type="button" className="ag-btn" data-testid="inf-plugin-confirm-cancel" onClick={() => setConfirming(false)}>Cancel</button>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function PluginsTab({ projectId, query }: InfraTabProps) {
  const plugins = usePlugins(projectId);
  if (!plugins.data) return plugins.error ? <Empty testId="inf-plugins-error">{plugins.error}</Empty> : <Skeleton testId="inf-plugins-loading" />;
  const q = query.trim().toLowerCase();
  const rows = q ? plugins.data.rows.filter((p) => [p.id, p.name, ...p.capabilities, ...p.engines].some((s) => s.toLowerCase().includes(q)))
    : plugins.data.rows;
  return (
    <section aria-labelledby="inf-plugins" data-testid="inf-plugins">
      <ZoneHead id="inf-plugins" label="PLUGINS" count={rows.length}
                tip="What can provision a database, cache or storage for an app, and whether it is on" />
      {rows.length === 0 ? <Empty testId="inf-plugins-empty">No plugins</Empty> : (
        <div className="inf-table-wrap">
          <table className="inf-table" data-testid="inf-plugins-table">
            <thead><tr>
              <th scope="col">Plugin</th>
              <th scope="col" title="What BOSS promises about it">Support</th>
              <th scope="col" title="Where it came from">Source</th>
              <th scope="col" title="What an app can ask it for">Provides</th>
              <th scope="col">State</th>
            </tr></thead>
            <tbody>
              {rows.map((p) => <PluginRowView key={`${p.id}@${p.version}`} p={p} onChanged={plugins.refresh} />)}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
