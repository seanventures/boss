"use client";

/**
 * Connecting a channel BOSS can publish to.
 *
 * Two steps, and they are two because OAuth cannot be one: entering the Google client is an
 * ordinary form, and authorising it leaves for Google's consent screen and comes back. A wizard
 * that pretended otherwise would stall on the redirect.
 *
 * The client SECRET is write-only here. It is sent once and never read back — the status
 * endpoint returns whether one is configured and the last twelve characters of the id, which is
 * enough to tell which client is loaded and useless to anyone else.
 */

import React, { useCallback, useEffect, useState } from "react";

type ClientStatus = {
  configured: boolean;
  source?: string | null;
  redirect_uri?: string;
  client_id_hint?: string;
  callback_path?: string;
};

type Channel = {
  connection_key: string;
  provider: string;
  status: string;
  health: string;
  capabilities: string[];
  channel: { verified?: boolean; title?: string; reason?: string };
};

export function ChannelsHome({ projectId, actor }: { projectId: string; actor: string }) {
  const [status, setStatus] = useState<ClientStatus | null>(null);
  const [channels, setChannels] = useState<Channel[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [form, setForm] = useState({ client_id: "", client_secret: "", redirect_uri: "" });

  const reload = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([
        fetch(`/api/channels?view=client&project_id=${encodeURIComponent(projectId)}`,
              { cache: "no-store" }),
        fetch(`/api/channels?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" }),
      ]);
      if (s.ok) {
        const body = (await s.json()) as ClientStatus;
        setStatus(body);
        // Prefilled from what the API says it expects, so the value pasted into Google and the
        // value BOSS receives the redirect on cannot disagree — that mismatch is the single
        // commonest failure in this whole flow.
        setForm((f) => ({
          ...f,
          redirect_uri: f.redirect_uri || body.redirect_uri || defaultRedirect(body),
        }));
      }
      if (c.ok) {
        const found = ((await c.json()).channels ?? []) as Channel[];
        setChannels(found);
        if (found.some((x) => x.status === "active")) setAwaiting(0);
      }
    } catch { /* leave what we had */ }
  }, [projectId]);

  useEffect(() => { void reload(); }, [reload]);

  // CONSENT HAPPENS IN ANOTHER TAB, SO THIS ONE HAS TO NOTICE WHEN IT IS DONE.
  //
  // The operator authorises on Google's page and switches back to a console that fetched its
  // channel list before any of that happened — so it still says "No channel connected yet"
  // about a channel that is connected. Telling them to refresh is not a fix; the page knows
  // perfectly well when it has been returned to.
  useEffect(() => {
    const onReturn = () => {
      if (document.visibilityState === "visible") void reload();
    };
    window.addEventListener("focus", onReturn);
    document.addEventListener("visibilitychange", onReturn);
    return () => {
      window.removeEventListener("focus", onReturn);
      document.removeEventListener("visibilitychange", onReturn);
    };
  }, [reload]);

  // AND A SHORT POLL AFTER PRESSING CONNECT, because a fast operator can finish consent
  // without this tab ever losing focus — on a second monitor, or with the popup dismissed by
  // the browser. Bounded: ten checks over thirty seconds, then it stops rather than polling a
  // dead connection attempt forever.
  const [awaiting, setAwaiting] = useState(0);
  useEffect(() => {
    if (awaiting <= 0) return;
    const timer = setTimeout(() => {
      void reload();
      setAwaiting((n) => n - 1);
    }, 3000);
    return () => clearTimeout(timer);
  }, [awaiting, reload]);

  const saveClient = useCallback(async () => {
    if (busy) return;
    setBusy(true); setNote(null);
    try {
      const r = await fetch("/api/channels", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "save_client", project_id: projectId, actor, ...form }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) { setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) }); return; }
      // The secret is cleared from the form the moment it is stored: it cannot be read back,
      // so leaving it on screen only creates somewhere for it to be seen.
      setForm((f) => ({ ...f, client_secret: "" }));
      setNote({ ok: true, text: "Client saved. You can connect the channel now." });
      void reload();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [busy, form, projectId, actor, reload]);

  const connect = useCallback(async () => {
    if (busy) return;
    setBusy(true); setNote(null);
    try {
      const r = await fetch("/api/channels", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "connect", project_id: projectId, actor }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok || !data?.authorization_url) {
        setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) });
        return;
      }
      // A NEW TAB, not a redirect. The console holds unsaved state on other screens, and
      // Google's consent ends on a page that is not this app.
      window.open(String(data.authorization_url), "_blank", "noopener");
      setAwaiting(10);
      setNote({ ok: true, text: "Authorise in the tab that opened — this page will notice." });
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [busy, projectId, actor]);

  const connected = (channels ?? []).filter((c) => c.status === "active");

  return (
    <section className="panel-stack" aria-label="Channels">
      {/* NOT COLLAPSIBLE, BECAUSE IT IS ALREADY SHORT.
          Collapsing this was wrong twice over: a connected channel is ONE LINE, so it was
          never what made the page long — the client form and the twenty-two built-in skills
          were — and a summary reading "Channels — Makkal Sinthanai" names one channel, which
          makes "expand it to connect a different one" incoherent. What varies is how much
          EXPLANATION is needed: a lot on the first visit, none once a channel is connected. */}
      <article className="stack-card" data-testid="channel-connections">
        <div className="section-head">
          <div>
            <span className="eyebrow">Channels</span>
            <h2>Where BOSS can publish.</h2>
            {connected.length === 0 ? (
              <p className="detail">
                A channel is connected once and held as a credential BOSS uses but no agent can
                read. Publishing still needs your approval every time.
              </p>
            ) : null}
          </div>
        </div>
        {channels === null ? <p className="detail">Loading…</p>
         : connected.length === 0 ? (
           <p className="detail" data-testid="channels-empty">
             No channel connected yet.
           </p>
         ) : (
          <ul className="skill-list" data-testid="channel-list">
            {connected.map((c) => (
              <li key={c.connection_key} className="skill-row">
                <span className="skill-row-main">
                  <b>{c.channel?.title || c.connection_key}</b>
                  <span className="cap-badge tone-muted">{c.provider}</span>
                  {c.health === "healthy" ? (
                    <span className="cap-badge tone-green">connected</span>
                  ) : (
                    <span className="cap-badge tone-amber">{c.health}</span>
                  )}
                </span>
                {c.channel?.verified === false ? (
                  <span className="detail">
                    Stored, but the channel could not be read back: {c.channel?.reason}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        <div className="dm-starters">
          {/* PRIMARY only when there is nothing connected. Once there is, connecting another
              is a rare action and should not be the loudest thing on the card. */}
          <button type="button"
                  className={connected.length === 0 ? "cap-btn primary" : "linklike"}
                  data-testid="channel-connect"
                  disabled={busy || !status?.configured}
                  title={status?.configured ? undefined
                         : "Add your Google OAuth client below first"}
                  onClick={() => void connect()}>
            {connected.length > 0 ? "Connect another channel" : "Connect YouTube"}
          </button>
        </div>
      </article>

      <details className="stack-card" data-testid="channel-client" open={!status?.configured}>
        <summary className="skill-form-summary">
          {status?.configured
            ? `Google OAuth client — configured (${status.client_id_hint})`
            : "Google OAuth client — not set up yet"}
        </summary>
        <p className="detail">
          BOSS uses your own Google Cloud project, not a shared one: your quota is yours, and a
          leak anywhere else cannot reach your channel. Create an OAuth client (Web application)
          with the YouTube Data API and Drive API enabled, then paste it here.
        </p>
        <div className="setup-form">
          <label className="setup-field">
            <span>Client ID</span>
            <input type="text" value={form.client_id} data-testid="channel-client-id"
                   spellCheck={false} placeholder="…apps.googleusercontent.com"
                   onChange={(e) => setForm((f) => ({ ...f, client_id: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>Client secret</span>
            {/* Write-only: it is never returned by any read path, so the field starts empty
                even when a client IS configured. */}
            <input type="password" value={form.client_secret} data-testid="channel-client-secret"
                   spellCheck={false} autoComplete="off" placeholder="GOCSPX-…"
                   onChange={(e) => setForm((f) => ({ ...f, client_secret: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>Authorised redirect URI</span>
            <input type="text" value={form.redirect_uri} data-testid="channel-redirect"
                   spellCheck={false}
                   onChange={(e) => setForm((f) => ({ ...f, redirect_uri: e.target.value }))} />
            <span className="detail">
              Paste this exact value into your OAuth client in Google Cloud Console. A mismatch
              here is the commonest failure in this flow, and Google reports it as
              <code> redirect_uri_mismatch</code>.
            </span>
          </label>
          <button type="button" className="cap-btn" data-testid="channel-client-save"
                  disabled={busy || !form.client_id.trim() || !form.client_secret.trim()}
                  onClick={() => void saveClient()}>
            {busy ? "Saving…" : status?.configured ? "Replace client" : "Save client"}
          </button>
        </div>
        {note ? (
          <p className={note.ok ? "setup-ok" : "setup-bad"} data-testid="channel-note">
            {note.text}
          </p>
        ) : null}
      </details>
    </section>
  );
}

/** What to register with Google, when nothing has been stored yet. */
function defaultRedirect(status: ClientStatus): string {
  if (typeof window === "undefined") return "";
  const path = status.callback_path || "/v1/channels/youtube/callback";
  // The API host, not the console's: the callback is an API route, and sending Google to the
  // web origin produces a 404 after consent — the most confusing possible failure, because the
  // authorisation itself succeeded.
  return `https://boss-api.local.sean.network${path}`;
}
