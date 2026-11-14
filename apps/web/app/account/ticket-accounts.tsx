"use client";

/**
 * MY ACCOUNT › TICKET ACCOUNTS (BOSSAPP-1047 Phase C). Your own sign-ins to ticket systems
 * ("Connect with Atlassian": OAuth, with Okta SSO behind Atlassian), so BOSS comments and moves
 * tickets AS YOU instead of as the connection's bot.
 *
 *   GET    /v1/me/ticket-identities                       yours, with `email_matches`
 *   POST   /v1/ticket-systems/{id}/oauth/start/load       {authorize_url}: the console opens it
 *   DELETE /v1/ticket-systems/{id}/oauth/me               disconnect (the vault item is shredded)
 *
 *   POST   /v1/me/ticket-identities/{id}/confirm          "It's mine" (kept by the API, audited)
 *
 * An account whose email is not your verified BOSS email asks you to confirm it is yours: "It's
 * mine" is the API's record (`confirmed_by_person`, for that account only); "Not mine"
 * disconnects it.
 */
import React, { useCallback, useEffect, useState } from "react";
import { LinkSimple, Warning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useIdentity } from "../../components/identity/identity-provider";
import {
  confirmTicketIdentity, disconnectTicketOauth, readCatalog, readConnections, readIdentities, startTicketOauth,
  type CatalogItem, type Connection, type Identity, type Read,
} from "../../lib/tickets";
import { ProviderMono, TkEmpty, TkReadState } from "../tickets/ticket-parts";
import { providerLabel } from "../projects/linked-ticket";
import "../tickets/ticket-run.css";

function urlProject(): string | null {
  try { return new URLSearchParams(window.location.search).get("project_id"); } catch { return null; }
}

export function TicketAccounts({ projectId: given = null }: { projectId?: string | null }) {
  const identity = useIdentity();
  const projectId = given ?? identity.projectId ?? urlProject();
  const [read, setRead] = useState<Read<Identity[]> | null>(null);
  const [connectable, setConnectable] = useState<Connection[]>([]);
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const [busy, setBusy] = useState("");
  const load = useCallback(() => { void readIdentities().then(setRead); }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!projectId) return;
    let live = true;
    void Promise.all([readConnections(projectId), readCatalog()]).then(([c, cat]) => {
      if (!live || !c.ok) return;
      const oauth = new Set((cat.ok ? cat.data : [] as CatalogItem[]).filter((x) => x.oauth).map((x) => x.provider));
      setConnectable(c.data.filter((x) => x.status === "connected" && oauth.has(x.provider)));
    });
    return () => { live = false; };
  }, [projectId]);

  const mine = read && read.ok ? read.data : [];
  const connect = async (c: Connection) => {
    setBusy(`c:${c.id}`); setSaid({ text: "" });
    const r = await startTicketOauth(c.id);
    setBusy("");
    const url = r.ok ? String((r.body as { authorize_url?: unknown } | null)?.authorize_url ?? "") : "";
    if (!r.ok || !url) { setSaid({ text: r.error ?? "Atlassian did not give BOSS a sign-in page.", tone: "red" }); return; }
    try { window.open(url, "_blank", "noopener"); } catch { /* blocked: the line says what to do */ }
    setSaid({ text: `Finish signing in to ${c.label} in the new tab, then come back.`, tone: "green" });
  };
  const confirm = async (i: Identity) => {
    if (!i.connection_id) return;
    setBusy(`m:${i.credential_id}`); setSaid({ text: "" });
    const r = await confirmTicketIdentity(i.connection_id);
    setBusy("");
    setSaid(r.ok ? { text: `Confirmed: ${i.account || "this account"} is yours.`, tone: "green" } : { text: r.error ?? "Not confirmed.", tone: "red" });
    if (r.ok) load();
  };
  const disconnect = async (i: Identity) => {
    if (!i.connection_id) return;
    setBusy(`d:${i.credential_id}`); setSaid({ text: "" });
    const r = await disconnectTicketOauth(i.connection_id);
    setBusy("");
    setSaid(r.ok ? { text: `Disconnected. BOSS uses the connection's bot for ${providerLabel(i.provider)} now.`, tone: "green" } : { text: r.error ?? "Not disconnected.", tone: "red" });
    if (r.ok) load();
  };
  const linked = new Set(mine.map((i) => i.connection_id));
  // An install without ticket systems (an older API): no card at all, never an error.
  if (read && !read.ok && read.missing) return null;

  return (
    <section className="idn-card" aria-labelledby="acc-tickets-h" data-testid="ticket-accounts">
      <div className="idn-card-h"><h2 id="acc-tickets-h">Ticket accounts</h2><span className="idn-sub">BOSS acts as you in Jira</span></div>
      {!read || !read.ok ? <TkReadState read={read} what="Your ticket accounts" onRetry={load} testid="ticket-accounts" />
        : !mine.length && !connectable.length ? (
          <TkEmpty title="No ticket account linked" line="Connect your own Jira account so comments and moves show your name, not BOSS Bot's." testid="ticket-accounts-empty" />
        ) : (
          <ul className="tk-list">
            {mine.map((i) => {
              const ask = i.needs_confirmation && !i.confirmed_by_person;
              return (
                <li key={i.credential_id} data-testid={`ticket-account-${i.credential_id}`}>
                  <ProviderMono provider={i.provider} size={26} />
                  <span className="tk-list-main">
                    <b>{i.display_name || providerLabel(i.provider)}</b>
                    <span className="tk-faint">{[i.email || i.account, i.status !== "active" && i.status ? i.status : "", i.linked_at ? `linked ${i.linked_at.slice(0, 10)}` : ""].filter(Boolean).join(" · ")}</span>
                  </span>
                  <GatedButton permission="connection:use_own" projectId={i.project_id} className="tk-btn tk-btn-sm" disabled={Boolean(busy) || !i.connection_id}
                               onClick={() => void disconnect(i)} data-testid={`ticket-account-off-${i.credential_id}`}>Disconnect</GatedButton>
                  {ask ? (
                    <span className="tk-amber" role="note" data-testid={`ticket-account-mismatch-${i.credential_id}`}>
                      <Warning size={13} aria-hidden /> This account&apos;s email is not your BOSS email. Is it yours?
                      <button type="button" className="tk-linkbtn" data-testid={`ticket-account-mine-${i.credential_id}`} disabled={Boolean(busy) || !i.connection_id}
                              title="BOSS records that this account is yours" onClick={() => void confirm(i)}>It&apos;s mine</button>
                      <GatedButton permission="connection:use_own" projectId={i.project_id} className="tk-linkbtn" disabled={Boolean(busy) || !i.connection_id}
                                   onClick={() => void disconnect(i)} data-testid={`ticket-account-notmine-${i.credential_id}`}>Not mine, disconnect</GatedButton>
                    </span>
                  ) : null}
                </li>
              );
            })}
            {connectable.filter((c) => !linked.has(c.id)).map((c) => (
              <li key={`connect-${c.id}`} data-testid={`ticket-account-connect-${c.id}`}>
                <ProviderMono provider={c.provider} size={26} />
                <span className="tk-list-main"><b>{c.label}</b><span className="tk-faint">{c.base_url.replace(/^https?:\/\//, "")} · not linked</span></span>
                <GatedButton permission="connection:use_own" projectId={projectId} className="tk-btn tk-btn-sm" data-kind="primary" disabled={Boolean(busy)}
                             onClick={() => void connect(c)} data-testid={`ticket-account-connect-btn-${c.id}`}
                             title="Sign in once; your organisation's single sign-on (Okta) may ask too">
                  <LinkSimple size={12} aria-hidden /> {busy === `c:${c.id}` ? "Opening…" : c.provider === "jira" ? "Connect with Atlassian" : `Connect ${c.label}`}</GatedButton>
              </li>
            ))}
          </ul>
        )}
      {said.text ? <p role="status" className={said.tone === "red" ? "tk-error" : "tk-ok"} data-testid="ticket-accounts-said">{said.text}</p> : null}
    </section>
  );
}
