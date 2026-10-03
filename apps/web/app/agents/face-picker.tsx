"use client";

/**
 * Face: pick a crew member's parts, or Reroll (spec §3.4 Profile › Face, §3.11 Face & voice).
 *
 * Controlled: give it the spec, get the next spec back. Nothing is saved here; the Profile's
 * Save does that. `Auto` on a part means "from the seed", so Reroll keeps every part the
 * operator picked and re-seeds the rest. Cast hats are offered only to the role that owns one,
 * so a custom agent can never be dressed as Vera.
 *
 * Previews show the face at 24 / 48 / 96 in idle, working and needs-you, where it will be seen.
 */
import { ArrowsClockwise } from "@phosphor-icons/react";
import React from "react";

import { rerollSpec } from "../../lib/agent-avatar";
import type { AvatarSpec, AvatarState } from "../../lib/agent-world-types";
import { EMBLEMS } from "../../lib/avatar-kit/emblems";
import { GENERIC_HATS, HAT_BY_ID } from "../../lib/avatar-kit/hats";
import { PALETTES } from "../../lib/avatar-kit/palettes";
import { VISORS } from "../../lib/avatar-kit/visors";
import { AgentAvatar } from "../agent-avatar";

const PREVIEWS: [number, AvatarState][] = [[24, "idle"], [48, "working"], [96, "needs_you"]];

type Part = "hat" | "emblem" | "visor";

export function FacePicker({ value, onChange, name = "Agent" }: {
  value: AvatarSpec;
  onChange(next: AvatarSpec): void;
  /** For the preview's label. */
  name?: string;
}) {
  const own = value.hat && HAT_BY_ID[value.hat]?.cast ? [HAT_BY_ID[value.hat]] : [];
  const options: Record<Part, { id: string; label: string }[]> = {
    hat: [...own, ...GENERIC_HATS], emblem: [...EMBLEMS], visor: [...VISORS],
  };
  const set = (part: keyof AvatarSpec, id: string) => {
    const next = { ...value };
    if (id) (next as Record<string, string>)[part] = id;
    else delete (next as Record<string, unknown>)[part];
    onChange(next);
  };
  return (
    <div className="ag-face-picker" data-testid="ag-face-picker">
      <div className="ag-face-previews" aria-label={`${name}, preview`} role="group">
        {PREVIEWS.map(([size, state]) => (
          <AgentAvatar key={size} spec={value} agent={name} size={size} state={state} aiBadge={false} />
        ))}
      </div>
      <div className="ag-face-parts">
        {(["hat", "emblem", "visor"] as Part[]).map((part) => (
          <label key={part} className="ag-face-field">
            <span>{part === "hat" ? "Hat" : part === "emblem" ? "Emblem" : "Visor"}</span>
            <select value={value[part] ?? ""} onChange={(e) => set(part, e.target.value)}
                    data-testid={`ag-face-${part}`}>
              <option value="">Auto</option>
              {options[part].map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select>
          </label>
        ))}
        <fieldset className="ag-face-field ag-face-palette">
          <legend>Palette</legend>
          {PALETTES.map((p) => (
            <label key={p.id} className="ag-swatch" data-tip={p.label}>
              <input type="radio" name="ag-face-palette" value={p.id} checked={value.palette === p.id}
                     onChange={() => set("palette", p.id)} aria-label={p.label} />
              <span style={{ background: p.body }} aria-hidden />
            </label>
          ))}
        </fieldset>
        <button type="button" className="ag-reroll" onClick={() => onChange(rerollSpec(value))}
                data-tip="New random face. Picked parts stay." data-testid="ag-face-reroll">
          <ArrowsClockwise size={14} weight="bold" aria-hidden />Reroll
        </button>
      </div>
    </div>
  );
}
