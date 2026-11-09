"use client";

/**
 * INVITE PEOPLE (IDENTITY.md §9.2): emails, access roles (only ones you hold all of, the API
 * enforces the subset rule, the sheet shows which are out of reach) and groups. Each invitation
 * says what happened to its email: sent, failed with the server's reason, or not configured, and
 * whenever it was not emailed, the single-use link to hand over. Resend issues a new link.
 */
import React, { useState } from "react";
import { ArrowClockwise, Check, EnvelopeSimple, Copy, UsersThree, Warning } from "@phosphor-icons/react";

import { IdentityError, idPost } from "../../lib/identity/api";
import type { AccessRole, Group } from "../../lib/identity/types";
import { grantReach } from "../../lib/identity/grant-limit";
import { useGuardedCall, useIdentity } from "./identity-provider";
import { ErrorLine, RoleChip, Sheet } from "./kit";
import { showToast } from "../../app/client-forms";

/** One invitation's answer (POST /v1/identity/users, …/resend). */
type Delivery = { status: "sent" | "failed" | "not_configured"; reason?: string | null; via?: string; inbox_url?: string | null };
type Sent = { email: string; id: string | null; link: string | null; delivery: Delivery | null };
type InviteAnswer = { invitation_id: string; emailed: boolean; link?: string | null; delivery?: Delivery };

const asSent = (email: string, r: InviteAnswer): Sent =>
  ({ email, id: r.invitation_id, link: r.link ?? null, delivery: r.delivery ?? null });

/** Show the result sheet unless every invitation went to the person's own inbox. */
const needsAttention = (list: Sent[]) =>
  list.some((i) => i.link || i.delivery?.status !== "sent" || i.delivery?.via === "dev_inbox");

function statusOf(d: Delivery | null, hasLink: boolean): { label: string; tip: string; bad: boolean } {
  if (!d) return { label: hasLink ? "Not emailed" : "Sent", tip: "", bad: hasLink };
  if (d.status === "sent") {
    return d.via === "dev_inbox"
      ? { label: "In test inbox", tip: "Local test inbox (Mailpit) on this machine: not delivered", bad: false }
      : { label: "Sent", tip: "", bad: false };
  }
  if (d.status === "failed") return { label: "Failed", tip: d.reason ?? "", bad: true };
  return { label: "Not configured", tip: d.reason ?? "Connect Email (SMTP) in Skills & MCP", bad: true };
}

export function InviteSheet({ roles, groups = [], onClose, onInvited }: {
  roles: AccessRole[];
  /** Groups they join on accepting. Only groups managed in BOSS: an IdP's groups are synced, not chosen. */
  groups?: Group[];
  onClose: () => void; onInvited?: () => void;
}) {
  const { org } = useIdentity();
  const [emails, setEmails] = useState("");
  const [role, setRole] = useState("operator");
  const [picked, setPicked] = useState<string[]>([]);
  const pickable = groups.filter((g) => g.source === "manual");
  const [links, setLinks] = useState<Sent[] | null>(null);
  const [resending, setResending] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const held = org?.permissions;
  const reach = (r: AccessRole) => grantReach(held, r);

  const list = emails.split(/[\s,;]+/).map((e) => e.trim()).filter((e) => e.includes("@"));

  const send = async () => {
    setBusy(true);
    setError(null);
    // One invitation per person (POST /v1/identity/users takes one email). A retry after a
    // step-up skips the ones already sent, so nobody is invited twice.
    const invited: Sent[] = [];
    try {
      await guarded("Inviting people grants them access.", async () => {
        for (const email of list) {
          if (invited.some((i) => i.email === email)) continue;
          const r = await idPost<InviteAnswer>("/v1/identity/users", {
            email, bindings: [{ role_key: role, scope_type: "org" }], group_ids: picked,
          });
          invited.push(asSent(email, r));
        }
      });
      onInvited?.();
      if (needsAttention(invited)) setLinks(invited);
      else { showToast("success", `${invited.length} invited`); onClose(); }
    } catch (e) {
      if (invited.length) onInvited?.();
      const sent = invited.length ? `${invited.length} sent; ` : "";
      setError(`${sent}${e instanceof IdentityError ? e.message : "Not sent"}`);
      if (invited.length) setLinks(invited);
    } finally {
      setBusy(false);
    }
  };

  const resend = async (row: Sent) => {
    if (!row.id) return;
    setResending(row.id);
    try {
      await guarded("Resending issues a new link; the old one stops working.", async () => {
        const r = await idPost<InviteAnswer>(`/v1/identity/invitations/${encodeURIComponent(row.id ?? "")}/resend`, {});
        setLinks((all) => (all ?? []).map((x) => (x.id === row.id ? asSent(x.email, r) : x)));
        if (r.delivery?.status === "sent") showToast("success", "Sent again");
        else showToast("error", `Not sent, ${r.delivery?.reason ?? "no reason given"}`);
      });
    } catch (e) {
      showToast("error", e instanceof IdentityError ? e.message : "Not resent");
    } finally {
      setResending(null);
    }
  };

  if (links) {
    const unsent = links.some((l) => l.link);
    return (
      <Sheet title="Invitations" icon={<EnvelopeSimple size={18} aria-hidden />} onClose={onClose} testId="invite-links">
        <p className="idn-lede2" data-copy="prose">
          {unsent ? "Send each unsent person their link. Each works once, for 7 days." : "Each invite works once, for 7 days."}
        </p>
        {links.map((l) => {
          const st = statusOf(l.delivery, Boolean(l.link));
          return (
            <div key={l.email} className="idn-field" data-testid={`invite-row-${l.email}`}>
              <span className="idn-label">
                {l.email}{" "}
                <span className="idn-chip" data-bad={st.bad || undefined} title={!st.bad && st.tip ? st.tip : undefined} data-testid="invite-status">
                  {st.bad ? <Warning size={12} aria-hidden /> : <Check size={12} aria-hidden />} {st.label}
                </span>
                {l.delivery?.inbox_url ? <> <a className="idn-link" href={l.delivery.inbox_url} target="_blank" rel="noreferrer">Open inbox</a></> : null}
              </span>
              {st.bad && st.tip ? <small className="idn-dim" data-testid="invite-reason">{st.tip}</small> : null}
              <div className="idn-secret">
                {l.link ? <code data-copy="data">{l.link}</code> : <span className="idn-spacer" />}
                {l.link ? (
                  <button type="button" className="idn-btn sm" title="Copy the single-use link" onClick={() => navigator.clipboard?.writeText(l.link ?? "").then(() => showToast("success", "Copied"))}>
                    <Copy size={14} aria-hidden /> Copy link
                  </button>
                ) : null}
                {l.id ? (
                  <button type="button" className="idn-btn sm ghost" title="New link and another email; the old link stops working"
                    disabled={resending === l.id} onClick={() => void resend(l)} data-testid="invite-resend">
                    <ArrowClockwise size={14} aria-hidden /> {resending === l.id ? "Sending…" : "Resend"}
                  </button>
                ) : null}
              </div>
            </div>
          );
        })}
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
            const { ok, reason } = reach(r);
            return (
              <button key={r.id} type="button" role="radio" aria-checked={role === r.key} aria-pressed={role === r.key}
                aria-disabled={!ok || undefined} title={reason || undefined}
                onClick={() => { if (ok) setRole(r.key); }}>
                <RoleChip roleKey={r.key} name={r.name} />
              </button>
            );
          })}
        </div>
      </div>
      {pickable.length ? (
        <div className="idn-field">
          <span className="idn-label" id="invite-groups">Groups <span className="idn-dim">optional</span></span>
          <div className="idn-row idn-wrap" role="group" aria-labelledby="invite-groups" data-testid="invite-groups">
            {pickable.map((g) => {
              const on = picked.includes(g.id);
              return (
                <button key={g.id} type="button" className="idn-chip idn-chip-btn" aria-pressed={on} data-on={on || undefined}
                  title={g.bindings.length ? `Also inherits ${g.bindings.map((b) => b.role_name).join(", ")}` : "Adds no access by itself"}
                  onClick={() => setPicked((x) => (on ? x.filter((i) => i !== g.id) : [...x, g.id]))} data-testid={`invite-group-${g.id}`}>
                  {on ? <Check size={12} aria-hidden /> : <UsersThree size={12} aria-hidden />} {g.name}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
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
