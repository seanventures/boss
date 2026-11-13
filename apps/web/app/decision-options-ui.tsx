"use client";

/**
 * A DECISION'S OPTIONS, EACH WITH ITS EFFECTS (BOSSAPP-879; engine decision_options). An agent
 * offered real alternatives — "Cancel the epic", "Give it to QA" — and each one carries typed,
 * signed effects on the work graph. One button per option (the engine's allowed actions, each
 * carrying its option id), the effects listed under it in words, "None of these" to decline.
 * Choosing is the card's approve with `choose`; the engine re-verifies the option's signature
 * and skips any target that changed since the question was asked.
 */
import React, { useState } from "react";

import { GatedButton } from "../components/gated";
import type { DecisionActionOffer, DecisionFeedItem } from "../lib/decisions";
import { obsWrite } from "./observability/obs-write";

type Effect = { type?: string; target?: string; title?: string; status?: string; assignee?: string; body?: string };
type Option = { id: string; label: string; detail: string; effects: Effect[] };

const obj = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const str = (v: unknown) => (typeof v === "string" ? v : "");

/** The card's options, read defensively from its recommendation. */
export function optionsOf(recommendation: unknown): Option[] {
  const rec = obj(recommendation);
  if (rec.action !== "choose_option" || !Array.isArray(rec.effect_options)) return [];
  return rec.effect_options.map(obj).map((o) => ({
    id: str(o.id), label: str(o.label) || str(o.id), detail: str(o.detail),
    effects: (Array.isArray(o.effects) ? o.effects : []).map(obj) as Effect[],
  })).filter((o) => o.id);
}

/** Few words per effect (engine decision_options.describe_effect). */
export function effectWords(e: Effect): string {
  switch (e.type) {
    case "comment": return `Comment on ${e.target}`;
    case "create": return `Create “${e.title}”`;
    case "update_status": return `Set ${e.target} to ${e.status}`;
    case "assign": return `Assign ${e.target} to ${e.assignee}`;
    case "cancel_tree": return `Cancel ${e.target} and its open children`;
    default: return String(e.type ?? "");
  }
}

export function DecisionOptions({ decision, projectId, actorId, offers }: {
  decision: DecisionFeedItem; projectId: string; actorId: string; offers: DecisionActionOffer[];
}) {
  const options = optionsOf(decision.recommendation);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const chosen = str(obj(decision.recommendation).chosen_option);
  if (chosen) {
    const picked = options.find((o) => o.id === chosen);
    return <p className="tq-said" data-testid="options-chosen">Chosen: {picked?.label ?? chosen}</p>;
  }
  const send = async (path: "approve" | "reject", choose?: string) => {
    setBusy(true); setSaid(null);
    const r = await obsWrite(`/v1/decisions/${encodeURIComponent(decision.id)}/${path}`,
                             path === "approve" ? { actor: actorId, choose } : { actor: actorId },
                             { scope: `decision:${decision.id}`, label: path === "approve" ? "Choose" : "None of these" });
    setBusy(false);
    setSaid(r.ok ? (path === "approve" ? "Done — its effects ran." : "Declined — nothing ran.") : r.error ?? "Not sent.");
  };
  const open = decision.status === "pending" || decision.status === "delegated";
  return (
    <div className="tq-form" data-testid="decision-options">
      <ul className="tq-items">
        {options.map((o) => {
          const offer = offers.find((a) => a.action === "approve" && a.choose === o.id);
          return (
            <li key={o.id} data-testid={`decision-option-${o.id}`}>
              <span className="tq-field">
                <span className="tq-label">{o.label}</span>
                {o.detail ? <span className="tq-help">{o.detail}</span> : null}
                <span className="tq-help">{o.effects.length ? o.effects.map(effectWords).join(" · ") : "Carries nothing out"}</span>
              </span>
              <GatedButton permission="decision:approve" projectId={projectId} className="dq-btn approve"
                           disabled={busy || !open || !offer} title={offer?.effect || undefined}
                           onClick={() => void send("approve", o.id)} data-testid={`decision-choose-${o.id}`}>
                Choose
              </GatedButton>
            </li>
          );
        })}
      </ul>
      <div className="dq-actions tq-actions">
        <GatedButton permission="decision:approve" projectId={projectId} className="dq-btn reject"
                     disabled={busy || !open} onClick={() => void send("reject")} data-testid="decision-options-none">
          None of these
        </GatedButton>
      </div>
      {said ? <p className="tq-said" role="status" data-testid="decision-options-said">{said}</p> : null}
    </div>
  );
}
