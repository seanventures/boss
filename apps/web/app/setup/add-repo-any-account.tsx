"use client";

/**
 * ADD REPO FROM EVERY ACCOUNT (operator, 2026-11-06). Add repo listed one connection's
 * repositories — the personal token's — while a repository newly granted to the BOSS GitHub App
 * (paramg/helpdesk-ai-demo) never appeared, and "From another account" opened the whole
 * Add-account wizard. This lists what EVERY connected account reaches (GET /v1/setup/repository/
 * available: this project's connections and the org's GitHub App), grouped by account and
 * searchable; picking one binds it through the existing select (repo_select), which connects
 * the App to the project first when it has to (`org_app`).
 */
import React, { useEffect, useMemo, useState } from "react";
import { CircleNotch } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { fetchAvailableRepos, setupWrite } from "../../lib/setup-client";
import type { AvailableRepoGroup, SetupRefusal, SetupView } from "../../lib/setup-types";
import { useBoard } from "./parts";
import { Refusal } from "./pill";
import { refusalOf } from "./repository-row";

const KIND_WORDS: Record<AvailableRepoGroup["kind"], string> = {
  org_app: "BOSS GitHub App",
  personal_signin: "Signed in with GitHub",
  token: "Access token",
};

/** The groups with only the repositories that match `query` and are not in the project yet. */
export function visibleGroups(groups: AvailableRepoGroup[], query: string): AvailableRepoGroup[] {
  const q = query.trim().toLowerCase();
  return groups.map((g) => ({
    ...g,
    repos: g.repos.filter((r) => !r.in_project && (!q || r.full_name.toLowerCase().includes(q)
                                                   || g.account.toLowerCase().includes(q))),
  })).filter((g) => g.repos.length || g.error || !q);
}

export function AddRepoFromAnyAccount({ onDone }: { onDone: () => void }) {
  const { projectId, apply } = useBoard();
  const connectGate = useGate("repository:register");
  const [groups, setGroups] = useState<AvailableRepoGroup[] | null>(null);
  const [failed, setFailed] = useState<SetupRefusal | null>(null);
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<{ connectionId: string; fullName: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    fetchAvailableRepos(projectId).then((out) => { if (live) setGroups(out.groups ?? []); })
      .catch((error) => { if (live) setFailed(refusalOf(error)); });
    return () => { live = false; };
  }, [projectId]);

  const shown = useMemo(() => visibleGroups(groups ?? [], query), [groups, query]);
  const total = useMemo(() => (groups ?? []).reduce((n, g) => n + g.repos.filter((r) => !r.in_project).length, 0),
                        [groups]);

  const connect = async (choice: { connectionId: string; fullName: string }) => {
    setBusy(true);
    try {
      apply(await setupWrite<SetupView>(projectId, "repo_select", {
        connection_id: choice.connectionId, full_name: choice.fullName, make_default: false,
      }, { label: "Connect" }));
      onDone();
    } catch (error) {
      setFailed(refusalOf(error));
    } finally {
      setBusy(false);
    }
  };

  if (!groups && !failed) {
    return <span className="su-faint su-line" data-testid="su-add-repo-loading">
      <CircleNotch className="su-spin" size={12} aria-hidden />Asking every account…</span>;
  }
  return (
    <div className="su-picker" data-testid="su-repo-picker-all">
      {failed ? <Refusal headline={failed.headline} fix={failed.fix} /> : null}
      {groups ? (
        <>
          <label className="su-field">
            <span className="su-field-label">Repository</span>
            <input type="search" placeholder="Search every account…" value={query}
                   aria-label="Search repositories in every account" data-testid="su-repo-search"
                   onChange={(e) => setQuery(e.target.value)} />
          </label>
          <ul className="su-picker-list" role="listbox" aria-label="Repositories"
              onKeyDown={(e) => {
                if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
                const options = [...e.currentTarget.querySelectorAll<HTMLButtonElement>("button[role=option]")];
                const at = options.indexOf(document.activeElement as HTMLButtonElement);
                const next = options[Math.max(0, Math.min(options.length - 1, at + (e.key === "ArrowDown" ? 1 : -1)))];
                if (next) { e.preventDefault(); next.focus(); }
              }}>
            {shown.map((g) => (
              <React.Fragment key={g.id}>
                <li role="presentation" className="su-picker-group" data-testid={`su-repo-group-${g.id}`}>
                  <span className="su-strong" data-user-text>{g.account}</span>
                  <span className="su-faint"> · {KIND_WORDS[g.kind] ?? "GitHub"}</span>
                  {g.error ? <span className="su-faint" role="status"> · {g.error.headline}{g.error.fix ? `: ${g.error.fix}` : ""}</span> : null}
                  {!g.error && !g.repos.length ? <span className="su-faint"> · nothing new</span> : null}
                </li>
                {g.repos.map((r) => {
                  const on = picked?.fullName === r.full_name && picked.connectionId === g.connection_id;
                  return (
                    <li key={`${g.id}:${r.full_name}`} role="presentation">
                      <button type="button" role="option" aria-selected={on} className="su-option"
                              data-testid={`su-repo-option-${r.full_name}`}
                              onClick={() => setPicked({ connectionId: g.connection_id, fullName: r.full_name })}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" && on && !busy && connectGate.allowed) {
                                  e.preventDefault();
                                  void connect({ connectionId: g.connection_id, fullName: r.full_name });
                                }
                              }}>
                        <span className="su-mono" data-user-text>{r.full_name}</span>
                        <span className="su-faint">{r.private ? "private" : "public"}</span>
                        <span className="su-faint" data-user-text>{r.default_branch ?? ""}</span>
                      </button>
                    </li>
                  );
                })}
              </React.Fragment>
            ))}
            {total === 0 && !query ? (
              <li className="su-faint" aria-disabled="true" data-testid="su-repo-none">
                Every repository these accounts reach is already here.
              </li>
            ) : query && !shown.some((g) => g.repos.length) ? (
              <li className="su-faint" aria-disabled="true">No match</li>
            ) : null}
          </ul>
          <GatedButton permission="repository:register" type="button" className="su-btn su-btn-primary"
                       disabled={!picked || busy} data-testid="su-repo-connect"
                       onClick={() => picked && void connect(picked)}>
            {busy ? <CircleNotch className="su-spin" size={12} aria-hidden /> : null}Connect
          </GatedButton>
        </>
      ) : null}
    </div>
  );
}
