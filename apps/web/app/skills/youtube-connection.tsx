"use client";

/**
 * YouTube's connection (spec §3.1 Media, J9): the existing Google client + channel flow, in the
 * Kit's words. Two steps because OAuth cannot be one: the Google Cloud client is an ordinary form
 * (its secret write-only — sent once, never read back), and a channel is authorised on Google's
 * consent screen in a new tab, which this section notices when the tab comes back.
 *
 * Same API as before (/api/channels: save_client, connect, disconnect); only the screen changed.
 */
import { GatedButton } from "../../components/gated";
import React, { useCallback, useEffect, useState } from "react";
import { Copy } from "@phosphor-icons/react";

import { apiWrite } from "../../lib/api-write";
import { showToast } from "../client-forms";
import { shortError } from "./kit-copy";

type ClientStatus = { configured: boolean; redirect_uri?: string; client_id_hint?: string;
                      installation_default_redirect_uri?: string | null };
type Channel = { connection_key: string; status: string; health: string; unusable_reason?: string;
                 channel: { title?: string } };

async function post(body: Record<string, unknown>) {
  const r = await apiWrite("/api/channels", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, data, error: String(data?.error ?? `HTTP ${r.status}`) };
}

export function YoutubeConnection({ projectId, actor }: { projectId: string; actor: string }) {
  const [status, setStatus] = useState<ClientStatus | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [form, setForm] = useState({ client_id: "", client_secret: "", redirect_uri: "" });
  const [busy, setBusy] = useState(false);
  const [awaiting, setAwaiting] = useState(0);
  // DISCONNECT ASKS FIRST: it revokes the token at Google and stops every scheduled publish
  // until somebody reconnects.
  const [confirming, setConfirming] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([
        fetch(`/api/channels?view=client&project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" }),
        fetch(`/api/channels?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" }),
      ]);
      if (s.ok) {
        const body = (await s.json()) as ClientStatus;
        setStatus(body);
        setForm((f) => ({ ...f, redirect_uri: f.redirect_uri || body.redirect_uri
                                                || body.installation_default_redirect_uri || "" }));
      }
      if (c.ok) {
        const found = ((await c.json()).channels ?? []) as Channel[];
        setChannels(found);
        if (found.some((x) => x.status === "active")) setAwaiting(0);
      }
    } catch { /* keep what we had */ }
  }, [projectId]);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    const back = () => { if (document.visibilityState === "visible") void reload(); };
    window.addEventListener("focus", back);
    document.addEventListener("visibilitychange", back);
    return () => { window.removeEventListener("focus", back); document.removeEventListener("visibilitychange", back); };
  }, [reload]);
  useEffect(() => {
    if (awaiting <= 0) return;
    const t = setTimeout(() => { void reload(); setAwaiting((n) => n - 1); }, 3000);
    return () => clearTimeout(t);
  }, [awaiting, reload]);

  const saveClient = async () => {
    setBusy(true);
    const r = await post({ action: "save_client", project_id: projectId, actor, ...form });
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    setForm((f) => ({ ...f, client_secret: "" }));
    showToast("success", "Saved");
    void reload();
  };

  const connect = async (key?: string) => {
    setBusy(true);
    const r = await post({ action: "connect", project_id: projectId, actor, ...(key ? { connection_key: key } : {}) });
    setBusy(false);
    if (!r.ok || !r.data?.authorization_url) { showToast("error", shortError(r.error)); return; }
    window.open(String(r.data.authorization_url), "_blank", "noopener");
    setAwaiting(10);
  };

  const disconnect = async (key: string) => {
    setBusy(true);
    const r = await post({ action: "disconnect", project_id: projectId, actor, connection_key: key });
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", "Saved");
    void reload();
  };

  const connected = channels.filter((c) => c.status === "active");
  const ready = Boolean(status?.configured);
  return (
    <div className="kit-conn" data-testid="kit-youtube">
      <div className="kit-row">
        <span className="kit-field-label" data-tip="Your own Google Cloud OAuth client">Google client</span>
        <span className="kit-fact">{ready ? `Set · ${status?.client_id_hint ?? ""}` : "Not set"}</span>
      </div>
      <label className="kit-field">
        <span className="kit-field-label">Client ID</span>
        <input type="text" value={form.client_id} spellCheck={false} data-testid="channel-client-id"
               onChange={(e) => setForm((f) => ({ ...f, client_id: e.target.value }))} />
      </label>
      <label className="kit-field">
        <span className="kit-field-label">Client secret</span>
        <input type="password" value={form.client_secret} autoComplete="new-password" data-testid="channel-client-secret"
               onChange={(e) => setForm((f) => ({ ...f, client_secret: e.target.value }))} />
      </label>
      <label className="kit-field">
        <span className="kit-field-label" data-tip="Paste this into the client's authorised redirect URIs">Redirect URI</span>
        <span className="kit-inline">
          <input type="text" value={form.redirect_uri} spellCheck={false} data-testid="channel-redirect"
                 onChange={(e) => setForm((f) => ({ ...f, redirect_uri: e.target.value }))} />
          <button type="button" className="kit-icon-btn" aria-label="Copy" data-tip="Copy"
                  onClick={() => { try { void navigator.clipboard.writeText(form.redirect_uri); } catch { /* none */ } }}>
            <Copy size={14} />
          </button>
        </span>
      </label>
      <div className="kit-actions">
        <GatedButton permission="integration:manage" type="button" className="kit-btn kit-btn-primary" data-testid="channel-save"
                disabled={busy || !form.client_id.trim() || !form.client_secret.trim() || !form.redirect_uri.trim()}
                onClick={() => void saveClient()}>Save</GatedButton>
        <GatedButton permission="integration:manage" type="button" className="kit-btn" data-testid="channel-connect" disabled={busy || !ready}
                data-tip={ready ? "Sign in to the account you publish from" : "Save the Google client first"}
                onClick={() => void connect(connected.length ? `youtube_${connected.length + 1}` : undefined)}>
          Connect
        </GatedButton>
      </div>
      {connected.map((c) => (
        <div key={c.connection_key} className="kit-row" data-testid={`channel-${c.connection_key}`}>
          <span className="kit-fact" data-tip={c.unusable_reason ?? c.health}>{c.channel.title || c.connection_key}</span>
          <GatedButton permission="integration:manage" type="button" className="kit-link" disabled={busy} onClick={() => void connect(c.connection_key)}>Reconnect</GatedButton>
          <span className="kit-confirm-wrap">
            <GatedButton permission="integration:manage" type="button" className="kit-link" disabled={busy} data-testid={`channel-disconnect-${c.connection_key}`}
                    onClick={() => setConfirming(c.connection_key)}>Disconnect</GatedButton>
            {confirming === c.connection_key ? (
              <span className="kit-pop" role="dialog" aria-label="Confirm disconnect">
                <strong data-tip="BOSS revokes the token at Google">Disconnect YouTube?</strong>
                <span>Agents lose YouTube</span>
                <span className="kit-pop-actions">
                  <GatedButton permission="integration:manage" type="button" className="kit-btn kit-btn-danger kit-btn-sm" data-testid="channel-disconnect-yes"
                          onClick={() => { setConfirming(null); void disconnect(c.connection_key); }}>Disconnect</GatedButton>
                  <button type="button" className="kit-btn kit-btn-sm" onClick={() => setConfirming(null)}>Cancel</button>
                </span>
              </span>
            ) : null}
          </span>
        </div>
      ))}
    </div>
  );
}
