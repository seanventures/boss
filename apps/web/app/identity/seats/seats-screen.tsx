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
import { IdentityError, enc, idPut, refusalSentence } from "../../../lib/identity/api";
import { hueOf } from "../../../lib/identity/adapt-people";
import { nextHolders, seatCards, tiersWords, type SeatCard, type SeatsResponse } from "../../../lib/identity/seats";

type PersonOption = { id: string; name: string };

function words(e: unknown): string {
  if (e instanceof IdentityError && e.code === "step_up_cancelled") return "Not confirmed";
  return refusalSentence(e);
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

/** ONE PERSON APPROVES EVERY TIER (the single-operator waiver, D29). A tier-3 decision needs two
 *  different people; an org run by one person (boss-1, 2026-11-03: the operator approved each
 *  tier-3 card twice, as two accounts) turns that off here. The API always took it
 *  (PUT /v1/identity/policy single_operator_waiver); the console had no control for it. */
function OnePersonSwitch({ waiver }: { waiver: { effective?: boolean; setting?: boolean | null; active_people?: number } | null | undefined }) {
  const guarded = useGuardedCall();
  const refresh = useConsoleRefresh();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const on = Boolean(waiver?.effective);
  const flip = async () => {
    setBusy(true); setError(null);
    try {
      await guarded(on ? "Tier-3 decisions will need two different people again."
                       : "One person will approve every tier, tier 3 included.",
        () => idPut("/v1/identity/policy", { single_operator_waiver: !on }));
      showToast("success", on ? "Two people for tier 3" : "One person approves every tier");
      refresh("now");
    } catch (e) { setError(words(e)); } finally { setBusy(false); }
  };
  return (
    <div className="idn-line" data-testid="seats-one-person">
      <span title="Tier-3 decisions normally need two different people. Turn this on when one person runs this org.">
        {on ? "One person approves every tier" : "Tier 3 needs two different people"}
      </span>
      <CanButton permission="security_policy:manage" className="idn-btn sm" disabled={busy}
                 data-testid="seats-one-person-toggle" onClick={() => void flip()}>
        {on ? "Require two people" : "One person approves every tier"}
      </CanButton>
      <ErrorLine error={error} />
    </div>
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
      {data ? <OnePersonSwitch waiver={data.single_operator_waiver} /> : null}
      <div className="idn-glist" data-testid="seats-list">
        {cards.map((c) => <SeatCardView key={c.key} card={c} twoPerson={data?.two_person_tiers ?? []} people={people} />)}
      </div>
    </>
  );
}
