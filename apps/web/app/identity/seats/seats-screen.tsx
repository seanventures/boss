"use client";

/**
 * IDENTITY › APPROVAL SEATS (IDENTITY.md §6.13): each seat, who holds it and which risk tiers it
 * approves. Changing a holder is identity administration: approval_rule:manage, with step-up
 * (the guarded call asks for a fresh second factor). The API refuses a tier left with nobody.
 */
import React, { useState } from "react";
import { Armchair, Plus, X } from "@phosphor-icons/react";

import { showToast } from "../../client-forms";
import { useConsoleRefresh } from "../../use-console-refresh";
import { CanButton, useCan, useGuardedCall } from "../../../components/identity/identity-provider";
import { ErrorLine, Face, Pill } from "../../../components/identity/kit";
import { ErrorState, type ReadFailure } from "../../../components/identity/states";
import { IdentityError, enc, idPut } from "../../../lib/identity/api";
import { hueOf } from "../../../lib/identity/adapt-people";
import { nextHolders, seatCards, tiersWords, type SeatCard, type SeatsResponse } from "../../../lib/identity/seats";

type PersonOption = { id: string; name: string };

function words(e: unknown): string {
  if (e instanceof IdentityError) return e.code === "step_up_cancelled" ? "Not confirmed" : e.message;
  return "That didn't work";
}

function SeatCardView({ card, twoPerson, people }: { card: SeatCard; twoPerson: number[]; people: PersonOption[] }) {
  const guarded = useGuardedCall();
  const refresh = useConsoleRefresh();
  const may = useCan("approval_rule:manage").allowed;
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ids = card.holders.map((h) => h.id);
  const candidates = people.filter((p) => !ids.includes(p.id));
  const save = async (next: string[], done: string) => {
    setBusy(true);
    setError(null);
    try {
      await guarded(`Changing who holds the ${card.label} seat is recorded.`,
        () => idPut(`/v1/identity/seats/${enc(card.key)}`, { principal_ids: next }));
      showToast("success", done);
      setAdding("");
      refresh("now");
    } catch (e) {
      setError(words(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="idn-gcard" data-testid={`seat-${card.key}`}>
      <span className="idn-gcard-h">
        <span className="idn-ico"><Armchair size={16} aria-hidden /></span>
        <span className="idn-grow"><b>{card.label}</b>
          <small title="Decisions at these risk tiers need this seat's approval">{tiersWords(card.tiers, twoPerson)}</small>
        </span>
        {card.vacant ? <Pill tone="warn" tip="Nobody holds it: an active Owner approves instead">Vacant</Pill> : null}
      </span>
      <span className="idn-row" data-testid={`seat-holders-${card.key}`}>
        {card.holders.map((h) => (
          <span key={h.id} className="idn-chip" title={h.id}>
            <Face name={h.name} tone={hueOf(h.id)} size={22} /> {h.name}
            {may ? (
              <button type="button" className="idn-btn sm ghost" disabled={busy} aria-label={`Remove ${h.name}`}
                title={`Remove ${h.name} from this seat`} data-testid={`seat-remove-${card.key}-${h.id}`}
                onClick={() => save(nextHolders(ids, { remove: h.id }), `${h.name} removed`)}>
                <X size={12} aria-hidden />
              </button>
            ) : null}
          </span>
        ))}
        {!card.holders.length ? <span className="idn-dim">Nobody</span> : null}
      </span>
      {may && candidates.length ? (
        <span className="idn-row">
          <select className="idn-input" value={adding} onChange={(e) => setAdding(e.target.value)} aria-label={`Add to ${card.label}`}
            data-testid={`seat-pick-${card.key}`}>
            <option value="">Add a person…</option>
            {candidates.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <CanButton permission="approval_rule:manage" className="idn-btn sm" disabled={!adding || busy}
            tip="They approve this seat's tiers" data-testid={`seat-add-${card.key}`}
            onClick={() => save(nextHolders(ids, { add: adding }), "Holder added")}>
            <Plus size={13} weight="bold" aria-hidden /> Add
          </CanButton>
        </span>
      ) : null}
      <ErrorLine error={error} />
    </section>
  );
}

export function SeatsScreen({ data, people, failure = null }: { data: SeatsResponse | null; people: PersonOption[]; failure?: ReadFailure | null }) {
  const cards = data ? seatCards(data) : [];
  const vacant = data?.vacant_tiers ?? [];
  return (
    <>
      <div className="idn-sec-h">
        <h1 className="idn-h1">Approval seats</h1>
        <span className="idn-sub" title="Who approves decisions at each risk tier">
          {data ? `${cards.length} seats${vacant.length ? ` · ${vacant.length} vacant tier${vacant.length === 1 ? "" : "s"}` : ""}` : ""}
        </span>
      </div>
      {failure ? <ErrorState what="approval seats" failure={failure} testId="seats-error" /> : null}
      <div className="idn-glist" data-testid="seats-list">
        {cards.map((c) => <SeatCardView key={c.key} card={c} twoPerson={data?.two_person_tiers ?? []} people={people} />)}
      </div>
    </>
  );
}
