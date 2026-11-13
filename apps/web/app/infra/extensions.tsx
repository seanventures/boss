"use client";

/**
 * Infra › Extensions (boss.extension/v1, BOSSAPP-861): add an integration, a tool, a scheduled job,
 * a webhook or a console panel to BOSS without a fork.
 *
 *   INSTALL     a bundle path on the BOSS host (a folder, .zip or .tar.gz) and the checksum its
 *               publisher gave → Check: what it is, what it asks for (each capability in words,
 *               like an app store), and whether the checksum matches → accept → Install.
 *   ROWS        one per installed extension: its capabilities, the trust gate's verdict (files
 *               unchanged since install, manifest valid), On/Off for THIS project, its webhooks'
 *               URLs and secrets, Remove. Its settings panel, if it has one, opens under the row
 *               in a sandboxed frame.
 *
 * Installing, turning on and off, and removing need instance:manage (BOSSAPP-872): running the
 * install is an instance admin's job, separate from any org's data, like Plugins.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { showToast } from "../client-forms";
import { ExtensionSlots } from "../extension-frame";
import { parsePreview, tierWords, useExtensions, type ExtensionPreview, type ExtensionRow } from "../../lib/extensions-view";
import type { InfraTabProps } from "./infra-home";
import {
  afterWrite, disableExtension, enableExtension, extensionWebhooks, installExtension, previewExtension,
  uninstallExtension, type ExtensionWebhook,
} from "./infra-actions";
import { Empty, Skeleton, ZoneHead } from "./kit";

function InstallBox({ onInstalled }: { onInstalled: () => void }) {
  const [source, setSource] = useState("");
  const [checksum, setChecksum] = useState("");
  const [preview, setPreview] = useState<ExtensionPreview | null>(null);
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);

  const check = async () => {
    setBusy(true);
    const r = await previewExtension(source.trim());
    setBusy(false);
    setAccepted(false);
    if (!r.ok) { setPreview(null); showToast("error", r.error); return; }
    setPreview(parsePreview(r.data));
  };
  const install = async () => {
    if (!preview) return;
    setBusy(true);
    const r = await installExtension(source.trim(), checksum.trim(), preview.capabilities.map((c) => c.capability));
    setBusy(false);
    if (!r.ok) { showToast("error", r.error); return; }
    showToast("success", `${preview.name} installed. Turn it on in a project to use it.`);
    setPreview(null); setSource(""); setChecksum(""); setAccepted(false);
    afterWrite(onInstalled);
  };
  const matches = !!preview && checksum.trim() === preview.computedChecksum;

  return (
    <div className="inf-ext-install" data-testid="inf-ext-install">
      <div className="inf-ext-fields">
        <label className="inf-field inf-ext-field">
          <span title="A folder, .zip or .tar.gz on the BOSS host, e.g. plugins/extensions/deploy-notes">Bundle path</span>
          <input data-testid="inf-ext-source" value={source} onChange={(e) => { setSource(e.target.value); setPreview(null); }}
                 placeholder="plugins/extensions/deploy-notes" />
        </label>
        <label className="inf-field inf-ext-field">
          <span title="The sha256 its publisher gave you; BOSS installs only when it matches">Checksum</span>
          <input data-testid="inf-ext-checksum" value={checksum} onChange={(e) => setChecksum(e.target.value)}
                 placeholder="sha256:…" />
        </label>
        <GatedButton permission="instance:manage" type="button" className="ag-btn" data-testid="inf-ext-check"
                     disabled={busy || !source.trim()} title="Read the bundle; nothing is installed yet"
                     onClick={() => void check()}>{busy && !preview ? "…" : "Check"}</GatedButton>
      </div>
      {preview ? (
        <div className="inf-ext-consent" data-testid="inf-ext-consent">
          <div><strong>{preview.name}</strong> <span className="inf-faint">{preview.id}, {preview.version}
            {preview.publisher ? `, from ${preview.publisher}` : ""}</span></div>
          {preview.description ? <div className="inf-muted">{preview.description}</div> : null}
          <div className="inf-ext-asks-head">It asks to:</div>
          <ul data-testid="inf-ext-asks">
            {preview.capabilities.map((c) => <li key={c.capability} title={c.capability}>{c.words}</li>)}
          </ul>
          <div data-testid="inf-ext-checksum-verdict" className={matches ? "inf-muted" : "inf-notice"}>
            {!preview.trust.trusted ? `Its files do not match its own manifest: ${preview.trust.problems.join("; ")}`
              : matches ? "Checksum matches." : `Checksum does not match. BOSS computed ${preview.computedChecksum}.`}
          </div>
          <label className="inf-ext-accept">
            <input type="checkbox" data-testid="inf-ext-accept" checked={accepted} onChange={(e) => setAccepted(e.target.checked)} />
            Allow these
          </label>
          <GatedButton permission="instance:manage" type="button" className="ag-btn" data-testid="inf-ext-install-go"
                       disabled={busy || !accepted || !matches || !preview.trust.trusted}
                       title="Copy it into BOSS and record its checksum as approved"
                       onClick={() => void install()}>{busy ? "…" : "Install"}</GatedButton>
        </div>
      ) : null}
    </div>
  );
}

function Webhooks({ id }: { id: string }) {
  const [hooks, setHooks] = useState<ExtensionWebhook[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  React.useEffect(() => {
    let live = true;
    void extensionWebhooks(id).then((r) => {
      if (!live) return;
      if (r.ok) setHooks(r.data.webhooks ?? []); else setError(r.error);
    });
    return () => { live = false; };
  }, [id]);
  if (error) return <div className="inf-notice">{error}</div>;
  if (!hooks) return <div className="inf-faint">Loading…</div>;
  return (
    <ul data-testid={`inf-ext-hooks-${id}`}>
      {hooks.map((h) => (
        <li key={h.name}>
          <code data-copy="data">POST {h.url}</code>{" "}
          <span className="inf-faint" title={`Sign the raw body: ${h.signatureHeader}: ${h.scheme}`}>secret</span>{" "}
          <code data-copy="data">{h.secret}</code>
        </li>
      ))}
    </ul>
  );
}

function ExtensionRowView({ e, projectId, onChanged }: { e: ExtensionRow; projectId: string; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<"" | "hooks" | "settings">("");
  const toggle = async (on: boolean) => {
    setBusy(true);
    const r = on ? await enableExtension(e.id, projectId) : await disableExtension(e.id, projectId);
    setBusy(false);
    if (!r.ok) showToast("error", r.error); else afterWrite(onChanged);
  };
  const remove = async () => {
    setBusy(true);
    const r = await uninstallExtension(e.id);
    setBusy(false);
    if (!r.ok) showToast("error", r.error); else afterWrite(onChanged);
  };
  const blocked = !e.trust.trusted;
  const hasSettings = e.slots.some((s) => s.type === "settings_panel");
  return (
    <>
      <tr data-testid={`inf-ext-row-${e.id}`} data-on={e.enabled ? "1" : undefined}>
        <td>
          <div title={e.description || undefined}><strong data-copy="data">{e.name}</strong></div>
          <div className="inf-faint" data-copy="data">{e.id}, {e.version}</div>
        </td>
        <td>
          {e.capabilities.map((c) => <span key={c.capability} className="inf-chip" title={c.words}>{c.capability}</span>)}
          {e.tools.length ? (
            <div className="inf-faint" title={e.tools.map((t) => `${t.name}: ${tierWords(t.riskTier)}`).join("\n")}>
              Tools: {e.tools.map((t) => t.name).join(", ")}
            </div>
          ) : null}
        </td>
        <td>
          {blocked ? (
            <span className="inf-notice" data-testid="inf-ext-blocked" title={e.trust.problems.join("\n")}>Blocked</span>
          ) : (
            <span className="inf-muted" data-testid="inf-ext-trusted" title={`Approved checksum ${e.trust.approvedChecksum}`}>Checksum OK</span>
          )}
        </td>
        <td>
          {e.enabled ? (
            <GatedButton permission="instance:manage" type="button" className="ag-btn" disabled={busy}
                         data-testid="inf-ext-off" title="Turn off in this project: its tools, events and panel stop"
                         onClick={() => void toggle(false)}>{busy ? "…" : "On"}</GatedButton>
          ) : (
            <GatedButton permission="instance:manage" type="button" className="ag-btn" disabled={busy || blocked}
                         data-testid="inf-ext-on" title={blocked ? e.trust.problems.join("\n") : "Turn on in this project"}
                         onClick={() => void toggle(true)}>{busy ? "…" : "Off"}</GatedButton>
          )}
          {e.webhooks.length ? (
            <button type="button" className="ag-btn" data-testid="inf-ext-hooks"
                    onClick={() => setOpen(open === "hooks" ? "" : "hooks")}>Webhooks</button>
          ) : null}
          {hasSettings ? (
            <button type="button" className="ag-btn" data-testid="inf-ext-settings"
                    onClick={() => setOpen(open === "settings" ? "" : "settings")}>Settings</button>
          ) : null}
          <GatedButton permission="instance:manage" type="button" className="ag-btn" disabled={busy}
                       data-testid="inf-ext-remove" title="Uninstall everywhere and delete its data"
                       onClick={() => void remove()}>Remove</GatedButton>
        </td>
      </tr>
      {open ? (
        <tr data-testid={`inf-ext-detail-${e.id}`}>
          <td colSpan={4}>
            {open === "hooks" ? <Webhooks id={e.id} /> : <ExtensionSlots type="settings_panel" projectId={projectId} only={e.id} />}
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function ExtensionsTab({ projectId, query }: InfraTabProps) {
  const list = useExtensions(projectId);
  const q = query.trim().toLowerCase();
  const rows = (list.data?.rows ?? []).filter((e) => !q || [e.id, e.name, ...e.capabilities.map((c) => c.capability)]
    .some((s) => s.toLowerCase().includes(q)));
  return (
    <section aria-labelledby="inf-extensions" data-testid="inf-extensions">
      <ZoneHead id="inf-extensions" label="EXTENSIONS" count={rows.length}
                tip="Integrations, tools, jobs, webhooks and panels added to BOSS (boss.extension/v1)" />
      <InstallBox onInstalled={list.refresh} />
      {!list.data ? (list.error ? <Empty testId="inf-extensions-error">{list.error}</Empty> : <Skeleton testId="inf-extensions-loading" />)
        : rows.length === 0 ? <Empty testId="inf-extensions-empty">No extensions installed</Empty> : (
          <div className="inf-table-wrap">
            <table className="inf-table" data-testid="inf-extensions-table">
              <thead><tr>
                <th scope="col">Extension</th>
                <th scope="col" title="What it was allowed to do at install">Can</th>
                <th scope="col" title="Its files still match what was approved">Trust</th>
                <th scope="col">In this project</th>
              </tr></thead>
              <tbody>
                {rows.map((e) => <ExtensionRowView key={e.id} e={e} projectId={projectId} onChanged={list.refresh} />)}
              </tbody>
            </table>
          </div>
        )}
    </section>
  );
}
