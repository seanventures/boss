"use client";

/**
 * THE ORG CHART CARRIES AUTHORITY (BOSSAPP-885), drawn on Organization › Lines.
 *
 *   ChainChips     one role's chain of command as chips: QA › Engineering Captain › CTO › you.
 *                  A retired manager is struck through; it blocks the role from starting work.
 *   TakeoverQueue  stalled builds BOSS offered to a manager before asking a person: whose work,
 *                  who holds the offer, until when, and a Take over button (agent:update).
 *
 * Presentational only: the write (POST /api/org, op takeover) is the caller's (workforce-ui).
 */
import React from "react";

import { GatedButton } from "../../components/gated";
import type { OrgChainView, TakeoverOffer } from "../../lib/v2/workforce";
import { localFull } from "../../lib/local-time";

function nameOf(party: string, names: Map<string, string>): string {
  const id = party.replace(/^(agent|person):/, "");
  if (party.startsWith("person:")) return names.get(party) ?? id;
  return names.get(id) ?? id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function ChainChips({ view, names }: { view: OrgChainView | undefined; names: Map<string, string> }) {
  if (!view || view.chain.length < 2) return null;
  return (
    <span className="org-chain" data-testid={`org-chain-${view.chain[0].id}`}
          title={view.blocked ?? "Who takes this role's stalled work, in order, before a person"}>
      {view.chain.slice(1).map((rung, i) => (
        <React.Fragment key={rung.party}>
          {i > 0 ? <span className="org-chain-sep" aria-hidden="true">›</span> : null}
          <span className="org-chain-rung" data-kind={rung.kind}
                data-retired={rung.retired || undefined}>
            {rung.kind === "person" ? nameOf(rung.party, names) : rung.name}
            {rung.retired ? <span className="sr-only"> (retired)</span> : null}
          </span>
        </React.Fragment>
      ))}
      {view.retired_gap ? (
        <span className="org-chain-blocked" data-testid={`org-chain-blocked-${view.chain[0].id}`}>
          Blocked
        </span>
      ) : null}
    </span>
  );
}

export function TakeoverQueue({ offers, names, busy, onTakeOver }: {
  offers: TakeoverOffer[];
  names: Map<string, string>;
  busy: boolean;
  onTakeOver: (offer: TakeoverOffer) => void;
}) {
  if (!offers.length) return null;
  return (
    <section className="org-takeover" data-testid="org-takeover"
             aria-label="Stalled work waiting on a manager">
      <h3 className="org-takeover-head">Waiting on a manager <span>{offers.length}</span></h3>
      {offers.map((offer) => (
        <div className="org-takeover-row" key={offer.id} data-testid={`org-takeover-${offer.run_id}`}>
          <div className="org-takeover-what">
            <span className="org-takeover-title" title={offer.question || offer.reason}>
              {offer.title || offer.run_id}
            </span>
            <span className="detail">
              {nameOf(offer.subject, names)} stalled · {nameOf(offer.offered_to, names)} has it
              {offer.deadline ? ` until ${localFull(offer.deadline)}` : ""}, then the next manager,
              then a person
            </span>
          </div>
          <GatedButton permission="agent:update" type="button" className="org-place-btn"
                       disabled={busy} data-testid={`org-takeover-btn-${offer.run_id}`}
                       title={`Take it over as ${nameOf(offer.offered_to, names)}: the work moves `
                         + "to them and resumes; nobody else is asked"}
                       onClick={() => onTakeOver(offer)}>
            Take over
          </GatedButton>
        </div>
      ))}
    </section>
  );
}
