"use client";

/**
 * PROJECT SETTINGS › TICKET SYSTEM (BOSSAPP-1047 Phase C). Which ticket system feeds this project,
 * and which of its projects is this project's DEFAULT (where a bare key and a new ticket land).
 * Everything else about a connection (sign-in, write-back, the pick-up rule, webhooks) lives in
 * ONE place, Setup › Ticket systems; this card links there. A mirror (a connection mapped here
 * from another project) is read-only: "Managed from <project>".
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowSquareOut, Ticket as TicketIcon } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { readConnections, saveTicketSettings, type Connection, type Mapping, type Read } from "../../lib/tickets";
import { ProviderMono, TkEmpty, TkReadState } from "../tickets/ticket-parts";
import { Ico } from "./kit";
import "../tickets/ticket-run.css";

const SETUP_HREF = "?area=setup&focus=ticket_systems";

/** The mappings after making `key` this project's default (other projects' mappings untouched). */
export function withDefault(mappings: Mapping[], projectId: string, key: string): Mapping[] {
  const others = mappings.filter((m) => m.boss_project_id !== projectId);
  const mine = mappings.filter((m) => m.boss_project_id === projectId);
  const kept = mine.some((m) => m.external_project === key) ? mine : [...mine, { external_project: key, boss_project_id: projectId, default: false }];
  return [...others, ...kept.map((m) => ({ ...m, default: m.external_project === key }))];
}

export function TicketSystemCard({ projectId }: { projectId: string }) {
  const [read, setRead] = useState<Read<Connection[]> | null>(null);
  const load = useCallback(() => { void readConnections(projectId).then(setRead); }, [projectId]);
  useEffect(() => { setRead(null); load(); }, [load]);
  const list = read && read.ok ? read.data.filter((c) => c.status !== "not_connected") : [];
  return (
    <section className="px-card" aria-labelledby="px-set-tickets" data-testid="px-set-tickets" style={{ scrollMarginTop: 80 }}>
      <div className="px-card-h">
        <Ico icon={TicketIcon} c="blue" />
        <h2 id="px-set-tickets">Ticket system</h2>
        <span className="px-spacer" />
        <a className="px-more px-linkbtn" href={SETUP_HREF} data-testid="px-set-tickets-manage"
           title="Sign-in, write-back, the new-ticket rule and webhooks live in Setup">Manage in Setup › Ticket systems <ArrowSquareOut size={11} aria-hidden /></a>
      </div>
      {!read || !read.ok ? <TkReadState read={read} what="Ticket systems" onRetry={load} testid="px-set-tickets" />
        : !list.length ? (
          <TkEmpty title="No ticket system yet" line="Connect Jira, Plane or GitHub Issues once; then paste a key anywhere." testid="px-set-tickets-empty">
            <a className="tk-btn" href={SETUP_HREF} data-testid="px-set-tickets-connect">Connect one</a>
          </TkEmpty>
        ) : (
          <ul className="tk-list">{list.map((c) => <ConnectionLine key={c.id} c={c} projectId={projectId} onSaved={load} />)}</ul>
        )}
    </section>
  );
}

function ConnectionLine({ c, projectId, onSaved }: { c: Connection; projectId: string; onSaved: () => void }) {
  const mine = c.settings.mappings.filter((m) => m.boss_project_id === projectId);
  const current = mine.find((m) => m.default)?.external_project ?? mine[0]?.external_project ?? "";
  const keys = [...new Set([...mine.map((m) => m.external_project), ...c.settings.project_keys])];
  const [pick, setPick] = useState(current);
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const [busy, setBusy] = useState(false);
  useEffect(() => { setPick(current); }, [current]);
  const save = async () => {
    if (!pick) return;
    setBusy(true); setSaid({ text: "" });
    const r = await saveTicketSettings(c.id, { mappings: withDefault(c.settings.mappings, projectId, pick) });
    setBusy(false);
    setSaid(r.ok ? { text: `Saved: ${pick} is this project's default.`, tone: "green" } : { text: r.error ?? "Not saved.", tone: "red" });
    if (r.ok) onSaved();
  };
  return (
    <li data-testid={`px-set-ticket-${c.id}`}>
      <ProviderMono provider={c.provider} size={26} />
      <span className="tk-list-main">
        <b>{c.label}</b>
        <span className="tk-faint">{[c.base_url.replace(/^https?:\/\//, ""), c.status === "disconnected" ? "disconnected" : ""].filter(Boolean).join(" · ")}</span>
      </span>
      {c.managed_from ? (
        <span className="tk-muted" data-testid={`px-set-ticket-managed-${c.id}`} title="Change its settings in the project it was connected in">
          {current ? `${current} → this project · ` : ""}Managed from {c.managed_from.project_id}</span>
      ) : (
        <span className="tk-map">
          <label className="tk-muted" htmlFor={`px-set-ticket-pick-${c.id}`}>Default</label>
          <select id={`px-set-ticket-pick-${c.id}`} value={pick} onChange={(e) => setPick(e.target.value)} data-testid={`px-set-ticket-pick-${c.id}`}
                  title="Where a bare key and a new ticket land for this project">
            {!pick ? <option value="">Pick a project…</option> : null}
            {keys.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          <span className="tk-faint" aria-hidden>→ this project</span>
          <GatedButton permission="integration:manage" projectId={projectId} className="tk-btn tk-btn-sm" disabled={busy || !pick || pick === current}
                       onClick={() => void save()} data-testid={`px-set-ticket-save-${c.id}`}>{busy ? "Saving…" : "Save"}</GatedButton>
        </span>
      )}
      {said.text ? <p role="status" className={said.tone === "red" ? "tk-error" : "tk-ok"} style={{ flexBasis: "100%" }} data-testid={`px-set-ticket-said-${c.id}`}>{said.text}</p> : null}
    </li>
  );
}
