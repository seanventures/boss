"use client";

/**
 * INVITE PEOPLE (IDENTITY.md §9.2): emails, access roles (only ones you hold all of — the API
 * enforces the subset rule, the sheet shows which are out of reach) and groups. With SMTP the
 * invitations are emailed; without it the sheet shows each single-use link to hand over.
 */
import React, { useState } from "react";
import { EnvelopeSimple, Copy } from "@phosphor-icons/react";

import { IdentityError, idPost } from "../../lib/identity/api";
import type { AccessRole } from "../../lib/identity/types";
import { useGuardedCall, useIdentity } from "./identity-provider";
import { ErrorLine, RoleChip, Sheet } from "./kit";
import { showToast } from "../../app/client-forms";

export function InviteSheet({ roles, onClose, onInvited }: { roles: AccessRole[]; onClose: () => void; onInvited?: () => void }) {
  const { org } = useIdentity();
  const [emails, setEmails] = useState("");
  const [role, setRole] = useState("operator");
  const [links, setLinks] = useState<{ email: string; link: string | null }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const held = org?.permissions;
  const reachable = (r: AccessRole) => !held || r.permissions.every((p) => held.has(p));

  const list = emails.split(/[\s,;]+/).map((e) => e.trim()).filter((e) => e.includes("@"));

  const send = async () => {
    setBusy(true);
    setError(null);
    // One invitation per person (POST /v1/identity/users takes one email). A retry after a
    // step-up skips the ones already sent, so nobody is invited twice.
    const invited: { email: string; link: string | null }[] = [];
    try {
      await guarded("Inviting people grants them access.", async () => {
        for (const email of list) {
          if (invited.some((i) => i.email === email)) continue;
          const r = await idPost<{ invitation_id: string; emailed: boolean; link?: string | null }>("/v1/identity/users", {
            email, bindings: [{ role_key: role, scope_type: "org" }], group_ids: [],
          });
          invited.push({ email, link: r.link ?? null });
        }
      });
      onInvited?.();
      if (invited.some((i) => i.link)) setLinks(invited);
      else { showToast("success", `${invited.length} invited`); onClose(); }
    } catch (e) {
      if (invited.length) onInvited?.();
      const sent = invited.length ? `${invited.length} sent; ` : "";
      setError(`${sent}${e instanceof IdentityError ? e.message : "Not sent"}`);
      if (invited.some((i) => i.link)) setLinks(invited);
    } finally {
      setBusy(false);
    }
  };

  if (links) {
    return (
      <Sheet title="Hand over these links" icon={<EnvelopeSimple size={18} aria-hidden />} onClose={onClose} testId="invite-links">
        <p className="idn-lede2" data-copy="prose">No email is set up, so send each person their link. Each works once, for 7 days.</p>
        {links.map((l) => (
          <div key={l.email} className="idn-field">
            <span className="idn-label">{l.email}</span>
            <div className="idn-secret"><code data-copy="data">{l.link}</code>
              <button type="button" className="idn-btn sm" onClick={() => navigator.clipboard?.writeText(l.link ?? "").then(() => showToast("success", "Copied"))}>
                <Copy size={14} aria-hidden /> Copy
              </button>
            </div>
          </div>
        ))}
        <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn primary" onClick={onClose}>Done</button></div>
      </Sheet>
    );
  }

  return (
    <Sheet title="Invite people" icon={<EnvelopeSimple size={18} aria-hidden />} onClose={onClose} testId="invite-sheet">
      <div className="idn-field">
        <label htmlFor="invite-emails">Emails</label>
        <textarea id="invite-emails" className="idn-input" placeholder="ana@acme.dev, li@acme.dev" value={emails} onChange={(e) => setEmails(e.target.value)} data-autofocus />
      </div>
      <div className="idn-field">
        <span className="idn-label">Access role</span>
        <div className="idn-segs" role="radiogroup" aria-label="Access role">
          {roles.filter((r) => r.key !== "owner" && !r.system).map((r) => {
            const ok = reachable(r);
            return (
              <button key={r.id} type="button" role="radio" aria-checked={role === r.key} aria-pressed={role === r.key}
                aria-disabled={!ok || undefined} title={ok ? r.description : "You don't hold all of it"}
                onClick={() => { if (ok) setRole(r.key); }}>
                <RoleChip roleKey={r.key} name={r.name} />
              </button>
            );
          })}
        </div>
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-dim">{list.length ? `${list.length} ${list.length === 1 ? "person" : "people"}` : "Add an email"}</span>
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!list.length || busy} onClick={send} data-testid="invite-send">
          {busy ? "Sending…" : "Send invites"}
        </button>
      </div>
    </Sheet>
  );
}
