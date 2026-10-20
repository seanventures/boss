"use client";

/**
 * OUTGOING EMAIL — the form the readiness checklist points at.
 *
 * Three of the five examples end in an email, and the only instruction BOSS gave an operator
 * with no mail server was "POST /v1/email/config" — an API call, from a person at a console
 * (review of the readiness change). This is that call as a form. It is drawn in Skills and MCP (the Email ability),
 * and inline beside the "Outgoing email" item on any start checklist, so the fix is where the
 * problem is reported.
 */

import { GatedButton } from "../components/gated";
import React, { useEffect, useState } from "react";

import { apiWrite } from "../lib/api-write";
import { kitApi } from "../lib/kit-api";

type Described = {
  configured?: boolean; host?: string; port?: number; username?: string;
  from_address?: string; from_name?: string; has_password?: boolean;
  /** Where mail goes right now (services/mail_transport.py). */
  route?: { ready: boolean; source: string; detail: string; inbox_url?: string | null };
};

export function EmailSetup({ projectId, compact = false, onSaved, actor = "executive_console" }: {
  projectId: string;
  /** Who presses Send test (the Kit test runs as them and mails them). */
  actor?: string;
  /** Inline on a checklist: no heading, fewer words. */
  compact?: boolean;
  onSaved?: () => void;
}) {
  const [current, setCurrent] = useState<Described | null>(null);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("587");
  const [fromAddress, setFromAddress] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; bad: boolean } | null>(null);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    (async () => {
      try {
        const r = await fetch(`/api/email-config?project_id=${encodeURIComponent(projectId)}`,
                              { cache: "no-store" });
        if (!r.ok || !alive) return;
        const body = await r.json() as Described;
        setCurrent(body);
        if (body.configured) {
          setHost(body.host ?? "");
          setPort(String(body.port ?? 587));
          setFromAddress(body.from_address ?? "");
          setUsername(body.username ?? "");
        }
      } catch { /* the form still works without what was there */ }
    })();
    return () => { alive = false; };
  }, [projectId]);

  const [testing, setTesting] = useState(false);
  const sendTest = async () => {
    setTesting(true);
    setNote(null);
    const r = await kitApi.test(projectId, actor, "email");
    setTesting(false);
    if (!r.ok) { setNote({ text: `Test not sent — ${r.error}`, bad: true }); return; }
    setNote(r.data.ok ? { text: `Test sent · ${r.data.fact ?? ""}`.trim(), bad: false }
                      : { text: `Test failed — ${r.data.error ?? "no reason given"}`, bad: true });
  };

  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      const r = await apiWrite("/api/email-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId, host: host.trim(), port: Number(port) || 587,
          from_address: fromAddress.trim(), username: username.trim(),
          // Absent means "keep what is stored" — changing a port does not need the password.
          ...(password ? { password } : {}),
        }),
      });
      const body = await r.json().catch(() => ({})) as { error?: string };
      if (!r.ok) {
        setNote({ text: `Not saved — ${body.error ?? `failed (${r.status})`}`, bad: true });
        return;
      }
      setPassword("");
      setCurrent({ ...(current ?? {}), configured: true, host, from_address: fromAddress,
                   has_password: Boolean(password) || Boolean(current?.has_password) });
      setNote({ text: `Saved. Mail will go out through ${host.trim()} as `
                      + `${fromAddress.trim()}.`, bad: false });
      onSaved?.();
    } catch (e) {
      setNote({ text: `Not saved — ${String((e as Error)?.message ?? e)}`, bad: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={compact ? "email-setup compact" : "panel email-setup"}
         data-testid="email-setup">
      {compact ? null : (
        <>
          <span className="eyebrow" data-area="setup">Outgoing email</span>
          <h2>Where BOSS sends mail from</h2>
          <p className="detail">
            Workflows that end in an email — a newsletter, a sales signal, a notify step —
            send through this server. The password is kept in the secret store and never
            shown again.
            {current?.configured
              ? ` Configured: ${current.host} as ${current.from_address}.`
              : current?.route?.source === "dev_inbox"
                ? " No provider yet: mail goes to the dev inbox on this machine."
                : current?.route?.source === "env"
                  ? ` Sending through ${current.route.detail} (environment).`
                  : " Nothing is configured yet, so those steps cannot send."}
            {!current?.configured && current?.route?.inbox_url ? (
              <> <a href={current.route.inbox_url} target="_blank" rel="noreferrer"
                    data-testid="email-setup-inbox" title="Mailpit: every email BOSS sent">Open inbox</a></>
            ) : null}
          </p>
        </>
      )}
      <p className="detail email-setup-hint" data-testid="email-setup-hint">
        Brevo or any SMTP: host (e.g. <code>smtp-relay.brevo.com</code>), port 587 STARTTLS,
        your SMTP login and SMTP key — not the account password — and a From address on your own
        verified domain (docs: Email).
      </p>
      <div className="email-setup-grid">
        <label title="Your provider's SMTP relay, e.g. smtp-relay.brevo.com">SMTP host
          <input value={host} onChange={(e) => setHost(e.target.value)}
                 placeholder="smtp.example.com" data-testid="email-setup-host" />
        </label>
        <label title="587 = STARTTLS (usual); 465 = SSL">Port
          <input value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric"
                 data-testid="email-setup-port" />
        </label>
        <label title="An address on a domain you verified with the provider (SPF + DKIM)">Send as
          <input value={fromAddress} onChange={(e) => setFromAddress(e.target.value)}
                 placeholder="boss@example.com" data-testid="email-setup-from" />
        </label>
        <label title="The SMTP login your provider shows (Brevo: SMTP & API › SMTP)">Login
          <input value={username} onChange={(e) => setUsername(e.target.value)}
                 autoComplete="off" data-testid="email-setup-username" />
        </label>
        <label title="The SMTP key, not your account password. Kept in the secret store.">SMTP key
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                 autoComplete="new-password"
                 placeholder={current?.has_password ? "unchanged" : ""}
                 data-testid="email-setup-password" />
        </label>
      </div>
      <GatedButton permission="comms:manage" type="button" className="cap-btn" data-testid="email-setup-save"
              disabled={busy || !host.trim() || !fromAddress.trim()}
              onClick={() => void save()}>
        {busy ? "Saving…" : "Save mail server"}
      </GatedButton>
      <GatedButton permission="skill:invoke" type="button" className="cap-btn ghost" data-testid="email-setup-test"
              disabled={testing || !(current?.configured || current?.route?.ready)} title="Sends a test email to you"
              onClick={() => void sendTest()}>
        {testing ? "Sending…" : "Send test"}
      </GatedButton>
      {note ? (
        <p className={note.bad ? "dw-new-error" : "detail"} data-testid="email-setup-note">
          {note.text}
        </p>
      ) : null}
    </div>
  );
}
