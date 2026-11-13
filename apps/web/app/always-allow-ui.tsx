"use client";

/**
 * "ALWAYS ALLOW THIS" ON AN ASK CARD ABOUT A TOOL CALL (BOSSAPP-817). The engine says on the
 * card whether it may be offered (recommendation.always_allow_offer: a tier 0-1 tool on a
 * reviewed, unquarantined server). Pressing it approves the held call AND writes a revocable
 * allow rule for this agent in this project — optionally only for the argument values ticked
 * from the held call's preview, expiring as chosen. Revoke it in Skills & MCP › Rules.
 */
import React, { useState } from "react";

import { GatedButton } from "../components/gated";
import { obsWrite } from "./observability/obs-write";

export type AlwaysAllowOffer = { eligible: boolean; expiry_choices: string[]; tool: string;
  server: string; role: string; preview: Record<string, unknown> };

const EXPIRY_WORDS: Record<string, string> = { "1h": "1 hour", "1d": "1 day", "7d": "7 days", "30d": "30 days", never: "Until revoked" };

/** The offer on a card, read defensively; null when the card does not carry an eligible one. */
export function alwaysAllowOffer(rec: Record<string, unknown> | null | undefined): AlwaysAllowOffer | null {
  if (!rec || rec.action !== "gateway_tool_call") return null;
  const offer = rec.always_allow_offer as Record<string, unknown> | undefined;
  if (!offer || offer.eligible !== true) return null;
  const preview = rec.args_preview && typeof rec.args_preview === "object" && !Array.isArray(rec.args_preview)
    ? rec.args_preview as Record<string, unknown> : {};
  return {
    eligible: true,
    expiry_choices: Array.isArray(offer.expiry_choices) ? offer.expiry_choices.map(String) : ["never"],
    tool: String(rec.tool ?? ""), server: String(rec.server ?? ""), role: String(rec.role_key ?? ""), preview,
  };
}

/** Argument keys a person may pin: plain values only, never a redacted one. */
export function pinnable(preview: Record<string, unknown>): string[] {
  return Object.entries(preview).filter(([, v]) => v !== "[redacted]" && (v === null || typeof v !== "object")).map(([k]) => k);
}

export function AlwaysAllowOption({ decisionId, offer, projectId, actorId }: {
  decisionId: string; offer: AlwaysAllowOffer; projectId: string; actorId: string;
}) {
  const [open, setOpen] = useState(false);
  const [expiry, setExpiry] = useState(offer.expiry_choices.includes("7d") ? "7d" : offer.expiry_choices[0] ?? "never");
  const [pins, setPins] = useState<string[]>([]);
  const [said, setSaid] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const keys = pinnable(offer.preview);
  const send = async () => {
    const r = await obsWrite(`/v1/decisions/${encodeURIComponent(decisionId)}/approve`,
                             { actor: actorId, always_allow: { expiry, match_args: pins } },
                             { scope: `decision:${decisionId}`, label: "Approve + always allow" });
    if (r.ok) setDone(true); else setSaid(r.error ?? "Not approved.");
  };
  if (done) return <p className="tq-said" data-testid="always-allow-done">Approved — {offer.tool} now runs without asking{offer.role ? ` for ${offer.role}` : ""}. Revoke it in Skills & MCP › Rules.</p>;
  return (
    <div className="aa-option" data-testid="always-allow">
      <button type="button" className="kit-link" aria-expanded={open} data-testid="always-allow-toggle"
              onClick={() => setOpen((o) => !o)}>Always allow this…</button>
      {open ? (
        <div className="aa-body">
          <span className="aa-line">Let {offer.role || "this project's agents"} call <b>{offer.tool}</b> on {offer.server} without asking.</span>
          {keys.length ? (
            <span className="aa-line">Only when these match:{" "}
              {keys.map((k) => (
                <label key={k} className="aa-pin">
                  <input type="checkbox" checked={pins.includes(k)} data-testid={`always-allow-pin-${k}`}
                         onChange={(e) => setPins((p) => e.target.checked ? [...p, k] : p.filter((x) => x !== k))} />
                  {k} = {JSON.stringify(offer.preview[k])}
                </label>
              ))}
            </span>
          ) : null}
          <label className="aa-line">For
            <select aria-label="Always allow for" value={expiry} data-testid="always-allow-expiry"
                    onChange={(e) => setExpiry(e.target.value)}>
              {offer.expiry_choices.map((c) => <option key={c} value={c}>{EXPIRY_WORDS[c] ?? c}</option>)}
            </select>
          </label>
          <GatedButton permission="decision:approve" projectId={projectId} className="dq-btn approve"
                       data-testid="always-allow-approve" onClick={() => void send()}>Approve + always allow</GatedButton>
          {said ? <span className="tq-said">{said}</span> : null}
        </div>
      ) : null}
    </div>
  );
}
