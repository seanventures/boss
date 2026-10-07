"use client";

/**
 * Pick people by name: the chosen ones as chips (× removes), the rest in an "Add" list. Used for
 * a service account's owners and an agent's sponsor. Never shows an id.
 */
import React from "react";
import { X } from "@phosphor-icons/react";

import { Face } from "../../../components/identity/kit";

export type PickPerson = { id: string; name: string; hue: number; email?: string | null };

export function PeoplePick({ people, chosen, onChange, label, single = false, testId }: {
  people: PickPerson[]; chosen: string[]; onChange: (ids: string[]) => void; label: string; single?: boolean; testId?: string;
}) {
  const byId = new Map(people.map((p) => [p.id, p]));
  const rest = people.filter((p) => !chosen.includes(p.id));
  return (
    <div className="idn-row idn-pick" data-testid={testId}>
      {chosen.map((id) => {
        const p = byId.get(id);
        return (
          <span key={id} className="idn-chip">
            <Face name={p?.name ?? "?"} tone={p?.hue ?? 0} size={22} /> {p?.name ?? "Someone"}
            {!single ? (
              <button type="button" className="idn-x" aria-label={`Remove ${p?.name ?? "this person"}`} title="Remove"
                onClick={() => onChange(chosen.filter((x) => x !== id))}><X size={11} aria-hidden /></button>
            ) : null}
          </span>
        );
      })}
      {rest.length ? (
        <label className="idn-add-scope">
          <span className="idn-sr">{label}</span>
          <select value="" onChange={(e) => { const v = e.target.value; if (v) onChange(single ? [v] : [...chosen, v]); }} data-testid={testId ? `${testId}-add` : undefined}>
            <option value="">{single ? (chosen.length ? "Change…" : "Choose…") : "+ Add"}</option>
            {rest.map((p) => <option key={p.id} value={p.id}>{p.name}{p.email ? ` · ${p.email}` : ""}</option>)}
          </select>
        </label>
      ) : null}
    </div>
  );
}
