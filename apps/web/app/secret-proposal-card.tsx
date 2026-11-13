"use client";

/**
 * AN AGENT PROPOSED A SECRET (BOSSAPP-871).
 *
 *   deployer asks you to store stripe_api_key for deployment target shop-prod
 *   "Checkout needs it to take payments."
 *   [ value (never shown again) ]   Store   Decline
 *
 * The agent never sees or sends the value: the person types it here, and Store sends it once to
 * POST /v1/secret-proposals/{id}/store, which approves the card and writes it to the secret
 * store. The field is cleared the moment it is sent. A plain Approve of this card is refused.
 */
import React, { useState } from "react";

import { GatedButton } from "../components/gated";
import { obsWrite } from "./observability/obs-write";

export const SECRET_PROPOSAL_ACTION = "store_proposed_secret";

export type SecretProposal = {
  name: string; role_key: string; reason?: string; target_kind: string; target_id?: string | null;
  binding_name?: string | null; secret_kind?: string;
};

export function secretProposalOf(rec: Record<string, unknown>): SecretProposal | null {
  if (rec.action !== SECRET_PROPOSAL_ACTION || typeof rec.name !== "string") return null;
  const s = (k: string) => (typeof rec[k] === "string" ? (rec[k] as string) : null);
  return { name: rec.name, role_key: s("role_key") ?? "an agent", reason: s("reason") ?? undefined,
           target_kind: s("target_kind") ?? "project", target_id: s("target_id"),
           binding_name: s("binding_name"), secret_kind: s("secret_kind") ?? undefined };
}

function where(p: SecretProposal): string {
  if (p.target_kind === "agent") return `${p.role_key} only`;
  if (p.target_kind === "deployment_target") return `deployment target ${p.target_id ?? ""}`;
  return "this project";
}

export function SecretProposalCard({ decisionId, proposal, actorId, onDone }: {
  decisionId: string; proposal: SecretProposal; actorId: string;
  onDone?: (status: "stored" | "declined") => void;
}) {
  const [value, setValue] = useState("");
  const [declining, setDeclining] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);

  const store = async () => {
    if (!value) { setSaid("Type the value first."); return; }
    setBusy(true); setSaid(null);
    const typed = value;
    setValue("");                                   // never kept in the browser
    const r = await obsWrite(`/v1/secret-proposals/${encodeURIComponent(decisionId)}/store`,
                             { actor: actorId, value: typed },
                             { scope: `decision:${decisionId}`, label: "Store" });
    setBusy(false);
    if (!r.ok) { setSaid(r.error ?? "Not stored."); return; }
    onDone?.("stored");
  };
  const decline = async () => {
    setBusy(true); setSaid(null);
    const r = await obsWrite(`/v1/decisions/${encodeURIComponent(decisionId)}/reject`,
                             { actor: actorId, note: reason.trim() || "Declined" },
                             { scope: `decision:${decisionId}`, label: "Decline" });
    setBusy(false);
    if (!r.ok) { setSaid(r.error ?? "Not sent."); return; }
    onDone?.("declined");
  };

  return (
    <div className="decision-grant-card" data-testid="secret-proposal-card">
      <p className="decision-grant-head">
        <strong>{proposal.role_key}</strong> asks you to store <code data-user-text>{proposal.name}</code> for {where(proposal)}
      </p>
      {proposal.reason ? <blockquote className="decision-grant-reason" data-user-text>{proposal.reason}</blockquote> : null}
      <label className="tq-field" title="Write only. The agent never sees it.">
        <span className="tq-label">Value</span>
        <input className="input" type="password" autoComplete="off" spellCheck={false} value={value}
               onChange={(e) => setValue(e.target.value)} data-testid="secret-proposal-value"
               placeholder="Never shown again" aria-label={`Value of ${proposal.name}`} />
      </label>
      {declining ? (
        <input className="input" value={reason} onChange={(e) => setReason(e.target.value)}
               placeholder="Why not (optional)" aria-label="Why not" data-testid="secret-proposal-why" />
      ) : null}
      <div className="dq-actions">
        <GatedButton permission="secret:manage" type="button" className="ag-btn ag-btn-primary" disabled={busy || !value}
                     data-testid="secret-proposal-store" onClick={() => void store()}>
          {busy ? "Storing…" : "Store"}
        </GatedButton>
        <GatedButton permission="decision:approve" type="button" className="ag-btn" disabled={busy}
                     data-testid="secret-proposal-decline"
                     onClick={() => (declining ? void decline() : setDeclining(true))}>
          {declining ? "Send decline" : "Decline"}
        </GatedButton>
      </div>
      {said ? <p className="tq-error" role="alert" data-testid="secret-proposal-error">{said}</p> : null}
      <p className="decision-grant-effect">
        Stored in the secret store{proposal.target_kind === "deployment_target" ? " and bound to the target" : ""}.
        BOSS keeps a reference, never the value.
      </p>
    </div>
  );
}
