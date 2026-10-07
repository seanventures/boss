"use client";

/**
 * IDENTITY › AGENTS (IDENTITY.md §9.6): each Passport with its sponsor, ceiling preset, status,
 * active grants and last run — an agent without a sponsor is flagged — and every delegation
 * grant (person → agents), each opening the lineage view.
 */
import React, { useMemo, useState } from "react";
import { ArrowRight, LinkSimple, LockSimple, Robot, Warning } from "@phosphor-icons/react";

import { AgentAvatar } from "../../agent-avatar";
import { Ago, Face, Perm, Pill, daysUntil, useNow } from "../../../components/identity/kit";
import { EmptyState, ErrorState, type ReadFailure } from "../../../components/identity/states";
import type { Delegation, Passport } from "../../../lib/identity/types";

type Filter = "all" | "active" | "suspended" | "revoked" | "unsponsored";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" }, { id: "active", label: "Active" }, { id: "suspended", label: "Suspended" },
  { id: "revoked", label: "Revoked" }, { id: "unsponsored", label: "No sponsor" },
];

export function statusTone(s: Passport["status"]): "ok" | "warn" | "bad" {
  return s === "active" ? "ok" : s === "suspended" ? "warn" : "bad";
}

function expiry(d: Delegation, now: number) {
  if (d.status !== "active") return <Pill tone={d.status === "revoked" ? "bad" : "mute"}>{d.status}</Pill>;
  if (d.kind === "task") return <Pill tone="info">this build only</Pill>;
  if (!d.expires_at) return <Pill tone="warn" tip="No expiry: shows as risky in posture">until revoked</Pill>;
  const days = daysUntil(d.expires_at, now) ?? 0;
  return <Pill tone={days <= 3 ? "warn" : "ok"}>{days}d left</Pill>;
}

/** A grant's kind in words. */
export function kindWords(k: Delegation["kind"]): string {
  return k === "task" ? "One build" : k === "schedule" ? "Scheduled" : "Standing";
}

export function AgentsScreen({ agents, delegations, delegationsFailure = null }: {
  agents: Passport[]; delegations: Delegation[] | null; delegationsFailure?: ReadFailure | null;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const now = useNow();
  const shown = useMemo(() => agents.filter((a) => filter === "all" ? true : filter === "unsponsored" ? !a.sponsor : a.status === filter), [agents, filter]);
  const unsponsored = agents.filter((a) => !a.sponsor).length;

  return (
    <>
      <div className="idn-sec-h">
        <h1 className="idn-h1">Agents</h1>
        <span className="idn-sub">{agents.length} Passports · {agents.filter((a) => a.status === "suspended").length} suspended</span>
        <span className="idn-spacer" />
        <div className="idn-segs" role="group" aria-label="Show">
          {FILTERS.map((f) => (
            <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>
              {f.label}{f.id === "unsponsored" && unsponsored ? ` · ${unsponsored}` : ""}
            </button>
          ))}
        </div>
      </div>
      {unsponsored ? (
        <div className="idn-note" role="status" data-copy="prose">
          <Warning size={15} aria-hidden /> {unsponsored} {unsponsored === 1 ? "agent has" : "agents have"} no sponsor. Open one to assign an accountable person.
        </div>
      ) : null}
      <div className="idn-agent-grid" data-testid="passports">
        {shown.map((a) => (
          <a key={a.id} className="idn-agent-card" href={`/identity/agents/${a.id}`} data-status={a.status} data-testid={`passport-${a.role_key}`}>
            <AgentAvatar agent={a.role_key} size={44} state={a.status === "active" ? "idle" : a.status === "suspended" ? "paused" : "retired"} tip={a.name} />
            <span className="idn-agent-main">
              <b>{a.name}</b>
              <small>{a.title}</small>
              <span className="idn-agent-meta">
                {a.sponsor
                  ? <span className="idn-row" title="Accountable person"><Face name={a.sponsor.name} tone={a.sponsor.hue} size={22} /> {a.sponsor.name}</span>
                  : <span className="idn-warn idn-row"><Warning size={13} aria-hidden /> No sponsor</span>}
                {a.ceiling_preset ? <span className="idn-chip" title="Access limit: the most anyone may delegate to it">{a.ceiling_preset}</span> : null}
              </span>
              <small>{a.active_grants} {a.active_grants === 1 ? "grant" : "grants"} · last run <Ago iso={a.last_run_at} /></small>
            </span>
            <Pill tone={statusTone(a.status)}>{a.status}</Pill>
          </a>
        ))}
        {!shown.length ? (agents.length
          ? <span className="idn-dim">None match this filter</span>
          : <EmptyState icon={<Robot size={20} />} title="No agents yet" testId="agents-empty">Each agent gets a Passport when it is created in Agent Studio.</EmptyState>) : null}
      </div>

      <section className="idn-card" id="delegations" aria-labelledby="idn-dlg-h">
        <div className="idn-card-h">
          <h2 id="idn-dlg-h">Delegations</h2>
          {delegations ? <span className="idn-sub">{delegations.filter((d) => d.status === "active").length} active</span> : null}
        </div>
        {delegations && !delegations.length ? (
          <EmptyState compact icon={<LinkSimple size={16} />} title="No agent acts for anyone yet" testId="delegations-empty"
            action={<a className="idn-btn sm" href="/account/agents?new=1">Let an agent act for me</a>}>
            A person grants agents part of their access, for a while.
          </EmptyState>
        ) : delegations ? (
          <div className="idn-scroll">
            <table className="idn-t" data-testid="delegations">
              <thead><tr><th>Person → agents</th><th>Grant</th><th>Kind</th><th>Expires</th><th>Last used</th><th><span className="idn-sr">Open</span></th></tr></thead>
              <tbody>
                {delegations.map((d) => (
                  <tr key={d.id} data-href={`/identity/agents/delegations/${d.id}`} onClick={() => window.location.assign(`/identity/agents/delegations/${d.id}`)}>
                    <td>
                      <span className="idn-row idn-nowrap">
                        <Face name={d.user.name} tone={d.user.hue} size={26} />
                        <ArrowRight size={12} className="idn-dim" aria-hidden />
                        <span className="idn-stack">{d.agents.map((g) => <AgentAvatar key={g.id} agent={g.id.replace(/^agt_/, "")} size={24} tip={g.name} />)}</span>
                      </span>
                    </td>
                    <td><a href={`/identity/agents/delegations/${d.id}`} title={`Grant ${d.id}`}><b>{d.label}</b><small className="idn-block idn-dim">{d.user.name}</small></a></td>
                    <td><span className="idn-chip">{kindWords(d.kind)}</span></td>
                    <td>{expiry(d, now)}</td>
                    <td className="idn-dim"><Ago iso={d.last_used_at} /></td>
                    <td><ArrowRight size={14} className="idn-dim" aria-hidden /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : delegationsFailure ? <ErrorState compact what="delegations" failure={delegationsFailure} />
          : <div className="idn-row idn-dim" data-testid="locked-delegations"><LockSimple size={14} aria-hidden /> Needs <Perm p="delegation:read_all" sm /></div>}
      </section>
    </>
  );
}
