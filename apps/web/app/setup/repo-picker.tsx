"use client";

/**
 * Pick, don't type. The token's own repository list, searchable; one repository is pre-selected.
 * Typing `owner/name` from memory turned a typo into a 404 that read as "no access".
 */
import { GatedButton, useGate } from "../../components/gated";
import React, { useMemo, useState } from "react";

import type { TokenFound } from "../../lib/setup-types";

export function RepoPicker({ repos, busy, onConnect, exclude = [], preselect = null }: {
  repos: TokenFound["repos"];
  busy: boolean;
  onConnect: (fullName: string) => void;
  exclude?: string[];
  /** The repository this token takes over (an install's), picked already when it is listed. */
  preselect?: string | null;
}) {
  const connectGate = useGate("repository:register");
  const choices = useMemo(() => repos.filter((r) => !exclude.includes(r.full_name)),
                          [repos, exclude]);
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(
    preselect && choices.some((r) => r.full_name === preselect) ? preselect
      : choices.length === 1 ? choices[0].full_name : null);
  const shown = choices.filter((r) => r.full_name.toLowerCase().includes(query.toLowerCase()));
  return (
    <div className="su-picker" data-testid="su-repo-picker">
      <label className="su-field">
        <span className="su-field-label">Repository</span>
        <input type="search" placeholder="Search your repositories…" value={query}
               aria-label="Search your repositories" data-testid="su-repo-search"
               onChange={(e) => setQuery(e.target.value)} />
      </label>
      {/* KEYBOARD FIRST: each option is a focusable button; Up/Down move between them, Enter
          or Space picks, and Enter on the picked one connects. */}
      <ul className="su-picker-list" role="listbox" aria-label="Repositories"
          onKeyDown={(e) => {
            if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
            const options = [...e.currentTarget.querySelectorAll<HTMLButtonElement>("button[role=option]")];
            const at = options.indexOf(document.activeElement as HTMLButtonElement);
            const next = options[Math.max(0, Math.min(options.length - 1,
                                                      at + (e.key === "ArrowDown" ? 1 : -1)))];
            if (next) { e.preventDefault(); next.focus(); }
          }}>
        {shown.map((r) => (
          <li key={r.full_name} role="presentation">
            <button type="button" role="option" aria-selected={picked === r.full_name}
                    className="su-option" data-testid={`su-repo-option-${r.full_name}`}
                    onClick={() => setPicked(r.full_name)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && picked === r.full_name && !busy && connectGate.allowed) {
                        e.preventDefault();
                        onConnect(r.full_name);
                      }
                    }}>
              <span className="su-mono" data-user-text>{r.full_name}</span>
              <span className="su-faint">{r.private ? "private" : "public"}</span>
              <span className="su-faint" data-user-text>{r.default_branch ?? ""}</span>
            </button>
          </li>
        ))}
        {shown.length === 0 ? <li className="su-faint" aria-disabled="true">No match</li> : null}
      </ul>
      <GatedButton permission="repository:register" type="button" className="su-btn su-btn-primary" disabled={!picked || busy}
              data-testid="su-repo-connect" onClick={() => picked && onConnect(picked)}>
        Connect
      </GatedButton>
    </div>
  );
}
