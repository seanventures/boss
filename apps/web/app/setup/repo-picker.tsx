"use client";

/**
 * Pick, don't type. The token's own repository list, searchable; one repository is pre-selected.
 * Typing `owner/name` from memory turned a typo into a 404 that read as "no access".
 */
import React, { useMemo, useState } from "react";

import type { TokenFound } from "../../lib/setup-types";

export function RepoPicker({ repos, busy, onConnect, exclude = [] }: {
  repos: TokenFound["repos"];
  busy: boolean;
  onConnect: (fullName: string) => void;
  exclude?: string[];
}) {
  const choices = useMemo(() => repos.filter((r) => !exclude.includes(r.full_name)),
                          [repos, exclude]);
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(choices.length === 1
                                                      ? choices[0].full_name : null);
  const shown = choices.filter((r) => r.full_name.toLowerCase().includes(query.toLowerCase()));
  return (
    <div className="su-picker" data-testid="su-repo-picker">
      <label className="su-field">
        <span className="su-field-label">Repository</span>
        <input type="search" placeholder="Search your repositories…" value={query}
               aria-label="Search your repositories" data-testid="su-repo-search"
               onChange={(e) => setQuery(e.target.value)} />
      </label>
      <ul className="su-picker-list" role="listbox" aria-label="Repositories">
        {shown.map((r) => (
          <li key={r.full_name} role="option" aria-selected={picked === r.full_name}
              data-testid={`su-repo-option-${r.full_name}`}
              onClick={() => setPicked(r.full_name)}>
            <span className="su-mono" data-user-text>{r.full_name}</span>
            <span className="su-faint">{r.private ? "private" : "public"}</span>
            <span className="su-faint" data-user-text>{r.default_branch ?? ""}</span>
          </li>
        ))}
        {shown.length === 0 ? <li className="su-faint" aria-disabled="true">No match</li> : null}
      </ul>
      <button type="button" className="su-btn su-btn-primary" disabled={!picked || busy}
              data-testid="su-repo-connect" onClick={() => picked && onConnect(picked)}>
        Connect
      </button>
    </div>
  );
}
