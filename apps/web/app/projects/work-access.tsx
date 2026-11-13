"use client";

/**
 * WHO CAN SEE IT (BOSSAPP-876), inside the Tree view's item panel. "Private" hides the item — and
 * everything under it — from everyone but its people (who made it, works it, answers for it) and
 * whoever is granted here; a grant reaches every item underneath. When someone read it past its
 * privacy (break-glass), the notice says who and why. A private BLOCKER shows only as "Private
 * item"; "Read (audited)" is the one way past it — with a reason, recorded, and shown to its people.
 *
 * Turning Private OFF (BOSSAPP-1042 e) used to leave the grant chips and say "Private: only its
 * people…": the handler read `e.target.checked` after the PATCH answered, by when React had put
 * the controlled box back to the old value, so the OFF press said the ON words. The new value is
 * read at the press, the box follows it at once, and grants show only while they mean something
 * (private here or above). Grants are kept, not revoked — they apply again if it is made private.
 */
import React, { useEffect, useState } from "react";
import { Eye, LockSimple, ShieldWarning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { breakGlass, grantAccess, parseNode, partyName, updateItem, type ItemRef, type WorkNode } from "../../lib/work-items";

export function ItemAccess({ projectId, item, demo, onChanged }: {
  projectId: string; item: WorkNode; demo: boolean; onChanged: () => void;
}) {
  const [party, setParty] = useState("");
  const [said, setSaid] = useState<string | null>(null);
  // The press's value until the item reloads with it (or the PATCH is refused).
  const [pressed, setPressed] = useState<boolean | null>(null);
  useEffect(() => { setPressed(null); }, [item.visibility]);
  const after = (r: { ok: boolean; error: string | null }, ok: string) => { setSaid(r.ok ? ok : r.error); if (r.ok) onChanged(); };
  const priv = pressed ?? item.visibility === "private";
  // `restricted` counts the item's own privacy too, so it is only trusted once the item reloaded
  // after a press; until then, grants show only if the press made it private.
  const restricted = pressed === null && item.restricted;
  const setPrivate = (next: boolean) => {
    setPressed(next);
    void updateItem(projectId, item.id, { visibility: next ? "private" : "project" }, "Private").then((r) => {
      if (!r.ok) setPressed(null);
      after(r, next ? "Private: only its people and those granted can see it." : "Visible to the project.");
    });
  };
  const valid = /^(person|agent):[A-Za-z0-9_.@:+-]+$/.test(party.trim());
  return (
    <div className="wt-line" data-testid="wt-access">
      <LockSimple size={12} aria-hidden />
      <label className="wt-field">
        <input type="checkbox" checked={priv} disabled={demo} data-testid="wt-private"
               onChange={(e) => setPrivate(e.target.checked)} />
        Private
      </label>
      {restricted && !priv ? <span className="wt-faint">Private through the item above it.</span> : null}
      {(priv || restricted ? item.access ?? [] : []).map((g) => (
        <span key={g.party} className="wt-label" data-testid={`wt-grant-${g.party}`}>
          {partyName(g.party)}
          <GatedButton permission="work_item:manage" projectId={projectId} className="wt-fold" disabled={demo} aria-label={`Remove ${g.party}`}
                       onClick={() => void grantAccess(projectId, item.id, g.party, true).then((r) => after(r, "Access removed."))}>×</GatedButton>
        </span>
      ))}
      {priv || restricted ? (
        <>
          <input className="input" value={party} onChange={(e) => setParty(e.target.value)} placeholder="person:id or agent:role"
                 aria-label="Grant access to" data-testid="wt-grant-party" disabled={demo} />
          <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo || !valid}
                       onClick={() => void grantAccess(projectId, item.id, party.trim()).then((r) => { setParty(""); after(r, "Granted."); })}
                       data-testid="wt-grant-add">Grant</GatedButton>
        </>
      ) : null}
      {item.break_glass_reads?.length ? (
        <p className="wt-feedback" role="note" data-testid="wt-break-glass-notice">
          <ShieldWarning size={12} aria-hidden /> Read past its privacy by {item.break_glass_reads.map((b) => `${partyName(b.actor) || b.actor} (“${b.reason}”)`).join(", ")}.
        </p>
      ) : null}
      {said ? <span className="wt-said" role="status" data-testid="wt-access-said">{said}</span> : null}
    </div>
  );
}

/** A private blocker: its name is hidden; an audited read shows it once, with a reason. */
export function PrivateBlocker({ projectId, blocker, demo }: { projectId: string; blocker: ItemRef; demo: boolean }) {
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const [seen, setSeen] = useState<WorkNode | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  if (seen) {
    return (
      <span className="wt-feedback" role="note" data-testid="wt-break-glass-seen">
        <Eye size={12} aria-hidden /> {seen.human_key ? `${seen.human_key} · ` : ""}{seen.title} — read under break-glass; recorded and shown to its people.
      </span>
    );
  }
  return (
    <span className="wt-line">
      <span className="wt-blocked" title="A private item blocks this one">Private item</span>
      {asking ? (
        <>
          <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why you need to read it"
                 aria-label="Reason for the audited read" data-testid="wt-break-glass-reason" />
          <GatedButton permission="role_binding:assign" projectId={projectId} className="px-btn" disabled={demo || reason.trim().length < 10}
                       onClick={() => void breakGlass(projectId, blocker.id, reason.trim()).then((r) => {
                         if (r.ok) setSeen(parseNode((r.body as { item?: unknown } | null)?.item)); else setSaid(r.error);
                       })}
                       data-testid="wt-break-glass-go">Read (audited)</GatedButton>
        </>
      ) : (
        <GatedButton permission="role_binding:assign" projectId={projectId} className="px-btn" disabled={demo}
                     onClick={() => setAsking(true)} data-testid="wt-break-glass">Break glass</GatedButton>
      )}
      {said ? <span className="wt-said" role="alert">{said}</span> : null}
    </span>
  );
}
