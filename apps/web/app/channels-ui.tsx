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

import React, { useCallback, useEffect, useRef, useState } from "react";

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

  const connect = useCallback(async (connectionKey?: string) => {
    if (busy) return;
    setBusy(true); setNote(null);
    try {
      const r = await fetch("/api/channels", {
        method: "POST", headers: { "Content-Type": "application/json" },
        // RECONNECT AND CONNECT-ANOTHER ARE THE SAME CALL, differing only in the key. Passing
        // the EXISTING key re-authorises that channel in place and keeps its identity;
        // passing a fresh one adds a second channel beside it.
        body: JSON.stringify({
          action: "connect", project_id: projectId, actor,
          ...(connectionKey ? { connection_key: connectionKey } : {}),
        }),
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

  const disconnect = useCallback(async (connectionKey: string, title: string) => {
    if (busy) return;
    // A CONFIRM, because this revokes the token at Google as well as locally. It is
    // recoverable — reconnect and consent again — but it stops every scheduled publish until
    // somebody does, and that should not happen on a misclick.
    if (!window.confirm(
      `Disconnect ${title}? BOSS will revoke the token at Google and cannot publish to this `
      + "channel until it is reconnected.")) return;
    setBusy(true); setNote(null);
    try {
      const r = await fetch("/api/channels", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "disconnect", project_id: projectId, actor, connection_key: connectionKey,
        }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) { setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) }); return; }
      setNote({ ok: true, text: `${title} disconnected.` });
      void reload();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [busy, projectId, actor, reload]);

  const connected = (channels ?? []).filter((c) => c.status === "active");

  /** A key no existing connection is using, so "connect another" adds rather than replaces. */
  const nextKey = useCallback(() => {
    const taken = new Set((channels ?? []).map((c) => c.connection_key));
    for (let n = 2; n < 50; n += 1) {
      const candidate = `youtube_${n}`;
      if (!taken.has(candidate)) return candidate;
    }
    return `youtube_${Date.now()}`;
  }, [channels]);

  // WHICH THING THE RIGHT-HAND PANE IS SHOWING. The Google client is an ITEM in the list, not
  // a form underneath a channel — see the comment on the library below for why that matters.
  const [selected, setSelected] = useState<string>("client");
  // AT MOST ONCE, AND NEVER AGAIN.
  //
  // Landing on a channel when there is one is a sensible OPENING, and it was written as an
  // effect keyed on `selected` — so it did not open anything, it ENFORCED something: clicking
  // "Google Cloud client" set the selection and the effect put it straight back on the channel,
  // every time, before a frame was painted. The operator reported it as "when I click on Google
  // cloud client nothing happens", which is exactly what it looked like.
  //
  // A ref rather than a state flag: this decides what to do during a render and must not cause
  // another one.
  const landed = useRef(false);
  useEffect(() => {
    if (landed.current) return;
    if (!channels) return;                       // nothing loaded yet; not a decision
    landed.current = true;
    // The client is what everything else needs, so an install without a usable one opens there.
    if (status?.configured && connected.length) setSelected(connected[0].connection_key);
  }, [status?.configured, channels, connected]);

  const current = connected.find((c) => c.connection_key === selected) ?? null;

  return (
    <div className="as-shell" data-testid="channels-home">
      <header className="as-header">
        <div>
          <span className="as-kicker">Channels</span>
          <h2>Where BOSS can publish</h2>
          <p>
            One Google Cloud client for the whole project, and a channel for each account it
            publishes to. Publishing still needs your approval every time.
          </p>
        </div>
        <button type="button" className="as-button secondary"
                data-testid="channel-connect"
                disabled={busy || !status?.configured}
                title={status?.configured ? undefined
                       : "Add the Google Cloud client first — it is what authorises a channel"}
                onClick={() => void connect(connected.length > 0 ? nextKey() : undefined)}>
          + Connect a channel
        </button>
      </header>

      <div className="as-metrics" aria-label="Channels summary">
        <div>
          <span>Channels</span><b>{connected.length}</b>
          <small>{connected.filter((c) => c.health === "healthy").length} usable</small>
        </div>
        <div>
          <span>Google client</span>
          <b>{status?.configured ? "Set" : "None"}</b>
          <small>{status?.configured ? status.client_id_hint : "nothing can connect yet"}</small>
        </div>
      </div>

      {note ? (
        <p className={note.ok ? "setup-ok as-note" : "setup-bad as-note"}
           data-testid="channel-note-top">{note.text}</p>
      ) : null}

      <div className="as-workbench as-two">
        {/* THE CLIENT IS AN ITEM IN THIS LIST, DELIBERATELY.
            It used to be a form BELOW a channel card, and the operator read it as belonging to
            that channel — "the client ID I'm providing is for same client or different
            client?". Prose saying "shared by every Google channel in this project" did not fix
            that, because layout outranks prose: a form under a heading looks like it belongs to
            the heading. Sitting BESIDE the channels, at the same level, it is visibly one thing
            they all share. */}
        <aside className="as-library" aria-label="Channels and the client that authorises them">
          <div className="as-pane-head">
            <span>Connections</span>
            <small>{connected.length + 1} objects</small>
          </div>
          <div className="as-object-list">
            <button type="button"
                    className={selected === "client" ? "as-object active" : "as-object"}
                    data-testid="channel-pick-client"
                    onClick={() => setSelected("client")}>
              <span className="as-mini-orb workflow">GC</span>
              <span>
                <b>Google Cloud client</b>
                <small>shared by every channel</small>
              </span>
              <i className={status?.configured ? "tone-green" : "tone-amber"}>
                {status?.configured ? "configured" : "not set up"}
              </i>
            </button>

            {channels === null ? (
              <p className="detail as-empty">Loading…</p>
            ) : connected.length === 0 ? (
              <p className="detail as-empty" data-testid="channels-empty">
                No channel connected yet.
              </p>
            ) : connected.map((c) => (
              <button type="button" key={c.connection_key}
                      className={selected === c.connection_key ? "as-object active" : "as-object"}
                      data-testid={`channel-pick-${c.connection_key}`}
                      onClick={() => setSelected(c.connection_key)}>
                <span className="as-mini-orb">{initials(c.channel?.title || c.connection_key)}</span>
                <span>
                  <b>{c.channel?.title || c.connection_key}</b>
                  <small>{c.provider}</small>
                </span>
                <i className={c.health === "healthy" ? "tone-green" : "tone-amber"}>
                  {c.health === "healthy" ? "connected" : c.health}
                </i>
              </button>
            ))}
          </div>
        </aside>

        <section className="as-detail" aria-label="Details">
          {selected === "client" ? (
            <ClientPane status={status} form={form} setForm={setForm} busy={busy}
                        onSave={() => void saveClient()} />
          ) : current ? (
            <ChannelPane channel={current} clientReady={Boolean(status?.configured)} busy={busy}
                         onReconnect={() => void connect(current.connection_key)}
                         onDisconnect={() => void disconnect(
                           current.connection_key,
                           current.channel?.title || current.connection_key)} />
          ) : (
            <p className="detail as-empty">Choose something on the left.</p>
          )}
        </section>
      </div>
    </div>
  );
}

/** Initials for the orb, from a channel title. */
function initials(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  return (words[0][0] + (words[1]?.[0] ?? "")).toUpperCase();
}

/** One channel: what it is, what it may do, and the two things you can do to it. */
function ChannelPane({ channel, clientReady, busy, onReconnect, onDisconnect }: {
  channel: Channel; clientReady: boolean; busy: boolean;
  onReconnect: () => void; onDisconnect: () => void;
}) {
  return (
    <>
      <div className="as-pane-head">
        <span>Channel</span>
        <small>{channel.connection_key}</small>
      </div>
      <div className="as-detail-body">
        <h3 className="as-detail-title">{channel.channel?.title || channel.connection_key}</h3>

        {!clientReady ? (
          // THE REASON IT CANNOT BE USED, ON THE THING THAT CANNOT BE USED. The token is fine;
          // what is missing is the client that refreshes it, and that lives one item up the
          // list — so this says so rather than leaving the operator to connect the two facts.
          <p className="setup-bad" data-testid={`channel-needs-client-${channel.connection_key}`}>
            This project has no Google Cloud client, so this channel cannot publish and cannot be
            reconnected. Select <b>Google Cloud client</b> on the left and add yours.
          </p>
        ) : null}

        <dl className="as-facts">
          <div><dt>Provider</dt><dd>{channel.provider}</dd></div>
          <div>
            <dt>State</dt>
            <dd className={channel.health === "healthy" ? "tone-green" : "tone-amber"}>
              {channel.health === "healthy" ? "connected" : channel.health}
            </dd>
          </div>
          {channel.channel?.verified === false ? (
            <div>
              <dt>Read back</dt>
              <dd>failed — {channel.channel?.reason}</dd>
            </div>
          ) : null}
          <div>
            <dt>May do</dt>
            <dd>{channel.capabilities?.length
              ? channel.capabilities.join(", ")
              : "nothing recorded"}</dd>
          </div>
        </dl>

        {/* RECONNECT IS NOT AN EDGE CASE. A testing-mode refresh token expires after seven
            days, so this is the weekly path, and the failure it fixes shows up as a publish
            failing with an auth error rather than as anything resembling an expired token. */}
        <div className="as-detail-actions">
          <button type="button" className="as-button" disabled={busy || !clientReady}
                  data-testid={`channel-reconnect-${channel.connection_key}`}
                  title={clientReady
                    ? "Re-authorise this channel. Needed about weekly while the app is in "
                      + "Google testing mode, where refresh tokens expire after seven days."
                    : "Add the Google Cloud client first"}
                  onClick={onReconnect}>
            Reconnect
          </button>
          <button type="button" className="as-button secondary" disabled={busy}
                  data-testid={`channel-disconnect-${channel.connection_key}`}
                  onClick={onDisconnect}>
            Disconnect
          </button>
        </div>
      </div>
    </>
  );
}

/** The one Google Cloud client the whole project authorises through. */
function ClientPane({ status, form, setForm, busy, onSave }: {
  status: ClientStatus | null;
  form: { client_id: string; client_secret: string; redirect_uri: string };
  setForm: React.Dispatch<React.SetStateAction<
    { client_id: string; client_secret: string; redirect_uri: string }>>;
  busy: boolean;
  onSave: () => void;
}) {
  return (
    <>
      <div className="as-pane-head">
        <span>Google Cloud client</span>
        <small>{status?.configured ? `configured · ${status.client_id_hint}` : "not set up"}</small>
      </div>
      <div className="as-detail-body">
        <h3 className="as-detail-title">One client, every channel</h3>
        <p className="detail">
          This is the OAuth application from <b>your own</b> Google Cloud project — your quota is
          yours, and a leak anywhere else cannot reach your channels. Every Google channel in
          this project authorises against this one client: connecting a second channel needs
          nothing new here.
        </p>
        <p className="detail">
          Create a <b>Web application</b> client with the YouTube Data API and Drive API
          enabled, then paste it below.
        </p>

        <label className="setup-field">
          <span>Client ID</span>
          <input type="text" value={form.client_id} data-testid="channel-client-id"
                 spellCheck={false} autoComplete="off"
                 placeholder="…apps.googleusercontent.com"
                 onChange={(e) => setForm((f) => ({ ...f, client_id: e.target.value }))} />
        </label>
        <label className="setup-field">
          <span>Client secret</span>
          {/* Write-only: never returned by any read path, so the field starts empty even when a
              client IS configured. */}
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
        <div className="as-detail-actions">
          <button type="button" className="as-button" data-testid="channel-client-save"
                  disabled={busy || !form.client_id.trim() || !form.client_secret.trim()}
                  onClick={onSave}>
            {busy ? "Saving…" : status?.configured ? "Replace client" : "Save client"}
          </button>
        </div>
      </div>
    </>
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
