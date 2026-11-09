"use client";

/**
 * CHOOSE A CAPTAIN (Settings › Captain, PX-13): captain-capable roles first — face, name, role
 * and one line on what each does here, the default (Helm) marked Recommended — then "Show all
 * agents (N)" with a search. The current captain is shown but cannot be picked again, so a click
 * on it never re-sends the same choice.
 */
import React, { useMemo, useState } from "react";
import { MagnifyingGlass } from "@phosphor-icons/react";

import { a, o, s } from "../../lib/project-experience";
import { Face, Tag } from "./kit";

export type CaptainChoice = { agent_id: string; role_key: string; name: string; role: string; line: string; captain_capable: boolean; recommended: boolean };

/** The engine's §14 `candidates` with the picker's fields (role, line, captain_capable, recommended). */
export function captainChoices(raw: unknown, fallback: { name: string; role_key: string; agent_id?: string }[]): CaptainChoice[] {
  const listed = a(raw).map((v) => {
    const x = o(v);
    return { agent_id: s(x.agent_id) || s(x.role_key), role_key: s(x.role_key), name: s(x.name) || s(x.role_key), role: s(x.role),
             line: s(x.line), captain_capable: x.captain_capable === true || /captain/.test(s(x.role_key)) && s(x.role_key) !== "chief_of_staff",
             recommended: x.recommended === true || s(x.role_key) === "engineering_captain" };
  }).filter((c) => c.role_key);
  if (listed.length) return listed;
  return fallback.map((m) => ({ agent_id: m.agent_id || m.role_key, role_key: m.role_key, name: m.name, role: "", line: "",
                                captain_capable: /captain/.test(m.role_key), recommended: m.role_key === "engineering_captain" }));
}

export function CaptainPicker({ choices, current, onChoose }: {
  choices: CaptainChoice[]; current: string; onChoose: (c: CaptainChoice) => void;
}) {
  const [all, setAll] = useState(false);
  const [q, setQ] = useState("");
  const capable = choices.filter((c) => c.captain_capable);
  const shown = useMemo(() => {
    const base = all || !capable.length ? choices : capable;
    const needle = q.trim().toLowerCase();
    const found = needle ? base.filter((c) => `${c.name} ${c.role} ${c.line}`.toLowerCase().includes(needle)) : base;
    return [...found].sort((x, y) => Number(y.recommended) - Number(x.recommended) || Number(y.captain_capable) - Number(x.captain_capable));
  }, [all, q, choices, capable]);
  return (
    <div role="listbox" aria-label="Choose a captain" className="px-capk" data-testid="px-set-captain-pick">
      {all ? (
        <label className="px-capk-search">
          <MagnifyingGlass size={13} aria-hidden />
          <input className="px-input" placeholder="Search agents" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search agents" data-testid="px-set-captain-search" autoFocus />
        </label>
      ) : null}
      {shown.map((m) => {
        const isCurrent = m.role_key === current || m.agent_id === current;
        return (
          <button key={m.role_key} type="button" role="option" aria-selected={isCurrent} aria-disabled={isCurrent} disabled={isCurrent}
                  className="px-li px-li-btn px-capk-row" data-testid={`px-set-captain-opt-${m.role_key}`}
                  title={isCurrent ? "The captain now" : `Make ${m.name} the captain`} onClick={() => { if (!isCurrent) onChoose(m); }}>
            <Face agent={{ name: m.name, role_key: m.role_key }} size={28} />
            <span style={{ minWidth: 0 }}>
              <b>{m.name}{m.role ? <span className="px-faint" style={{ fontWeight: 500 }}> · {m.role}</span> : null}</b>
              {m.line ? <small className="px-ell" style={{ display: "block" }}>{m.line}</small> : null}
            </span>
            {isCurrent ? <Tag tone="blue">Current</Tag> : m.recommended ? <Tag tone="green">Recommended</Tag> : <span />}
          </button>
        );
      })}
      {!shown.length ? <p className="px-faint" style={{ margin: "6px 0" }}>No agent matches “{q}”.</p> : null}
      {capable.length && choices.length > capable.length ? (
        <button type="button" className="px-more" style={{ background: "none", border: 0, cursor: "pointer", padding: "8px 0 0" }}
                data-testid="px-set-captain-all" onClick={() => setAll((v) => !v)}>
          {all ? "Captains only" : `Show all agents (${choices.length})`}</button>
      ) : null}
    </div>
  );
}
