"use client";

/**
 * Govern › Limits › Budgets by scope (BOSSAPP-863): a budget for the whole org, one project, one
 * agent or one workflow, per day or month, with a warning point and an optional hard stop. A hard
 * stop refuses new work past the cap and stops a running build at its next turn, not at the next
 * stage. Spend is money only (subscriptions are $0).
 *
 * Reads GET /v1/budget-policies/scoped; writes POST / DELETE the same path (budget:manage at org).
 * The agent and workflow pickers read this project's /v1/roles and /v1/workflows; the team picker
 * reads /v1/budget-policies/scoped/teams (BOSSAPP-1295: a team is an identity group, its spend the
 * projects its people work in). "$2,000 pilot" adds the `pilot_2000` preset for the picked team.
 * A cap never asks anyone mid-build: it warns, then refuses new work at a hard stop.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Plus, RocketLaunch, Trash } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { obsWrite } from "../observability/obs-write";

type Scope = "org" | "project" | "role" | "workflow" | "team";
export type ScopedBudget = {
  id: string; scope_type: Scope; scope_id: string; project_id: string | null; period: "day" | "month";
  cap_usd_micros: number; warn_pct: number; hard_stop: boolean; spent_usd_micros?: number; scope_name?: string | null;
};
export type Team = { id: string; key: string; name: string; members: number; projects: string[] };
type Option = { value: string; label: string };

const SCOPE_WORDS: Record<Scope, string> = { org: "Whole org", project: "This project", team: "A team", role: "An agent", workflow: "A workflow" };
/** The `pilot_2000` preset (scoped_budgets.PRESETS): a team's $2,000 a month, hard stop. */
export const PILOT_PRESET = { key: "pilot_2000", label: "$2,000 pilot", usd: 2000 } as const;
const usd = (micros: number | undefined) => `$${((micros ?? 0) / 1_000_000).toFixed(2)}`;
const enc = encodeURIComponent;

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    return r.ok ? ((await r.json()) as T) : null;
  } catch { return null; }
}

export function describeScope(p: ScopedBudget, names: Record<string, string> = {}): string {
  if (p.scope_type === "org") return "Whole org";
  if (p.scope_type === "project") return "Project";
  const who = names[p.scope_id] ?? p.scope_name ?? p.scope_id;
  if (p.scope_type === "team") return `Team ${who}`;
  return p.scope_type === "role" ? `Agent ${who}` : `Workflow ${who}`;
}

export function ScopedBudgetsCard({ projectId, actor }: { projectId: string; actor: string }) {
  const [rows, setRows] = useState<ScopedBudget[] | null>(null);
  const [agents, setAgents] = useState<Option[]>([]);
  const [workflows, setWorkflows] = useState<Option[]>([]);
  const [teams, setTeams] = useState<Team[]>([]);
  const [scope, setScope] = useState<Scope>("role");
  const [target, setTarget] = useState("");
  const [cap, setCap] = useState("");
  const [period, setPeriod] = useState<"day" | "month">("month");
  const [hard, setHard] = useState(true);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);

  const reload = useCallback(() => {
    void getJson<{ policies?: ScopedBudget[] }>(`/v1/budget-policies/scoped?project_id=${enc(projectId)}`)
      .then((b) => setRows(b?.policies ?? []));
  }, [projectId]);
  useEffect(() => {
    reload();
    void getJson<{ items?: { role_key: string; display_name: string }[] }>(`/v1/roles?project_id=${enc(projectId)}`)
      .then((b) => setAgents((b?.items ?? []).map((r) => ({ value: r.role_key, label: r.display_name || r.role_key }))));
    void getJson<{ items?: { id: string; name?: string; workflow_key?: string }[] }>(`/v1/workflows?project_id=${enc(projectId)}`)
      .then((b) => setWorkflows((b?.items ?? []).map((w) => ({ value: w.id, label: w.name || w.workflow_key || w.id }))));
    void getJson<{ teams?: Team[] }>(`/v1/budget-policies/scoped/teams?project_id=${enc(projectId)}`)
      .then((b) => setTeams(b?.teams ?? []));
  }, [projectId, reload]);

  const teamOptions: Option[] = teams.map((t) => ({ value: t.id, label: t.name }));
  const choices = scope === "role" ? agents : scope === "workflow" ? workflows : scope === "team" ? teamOptions : [];
  const names = Object.fromEntries([...agents, ...workflows, ...teamOptions].map((o) => [o.value, o.label]));
  const capNumber = Number(cap);
  const ready = capNumber > 0 && (choices.length === 0 ? scope === "org" || scope === "project" : !!target);

  const send = async (body: Record<string, unknown>, label: string) => {
    setBusy(true); setSaid(null);
    const r = await obsWrite("/v1/budget-policies/scoped", { actor, project_id: projectId, ...body },
                             { scope: "scoped-budgets", label });
    setBusy(false);
    return r;
  };
  const addPilot = async () => {
    const r = await send({ preset: PILOT_PRESET.key, scope_type: "team", scope_id: target }, "Add pilot budget");
    if (!r.ok) { setSaid(r.error ?? "Not saved."); return; }
    setTarget("");
    reload();
  };
  const add = async () => {
    const r = await send({
      scope_type: scope,
      scope_id: scope === "project" ? projectId : target,
      role_project_id: scope === "role" ? projectId : undefined,
      cap_usd_micros: Math.round(capNumber * 1_000_000), period, hard_stop: hard,
    }, "Add budget");
    if (!r.ok) { setSaid(r.error ?? "Not saved."); return; }
    setCap(""); setTarget("");
    reload();
  };
  const remove = async (id: string) => {
    setBusy(true); setSaid(null);
    const r = await obsWrite(`/v1/budget-policies/scoped/${enc(id)}?project_id=${enc(projectId)}`, {},
                             { scope: "scoped-budgets", label: "Remove" }, "DELETE");
    setBusy(false);
    if (!r.ok) { setSaid(r.error ?? "Not removed."); return; }
    reload();
  };

  return (
    <section className="gv-card" aria-labelledby="gv-scoped-h" data-testid="gv-scoped-budgets">
      <h3 id="gv-scoped-h" className="gv-card-title">Budgets by team, agent or workflow</h3>
      {rows === null ? <span className="gv-skel gv-skel-short" aria-busy="true" /> : rows.length ? (
        <ul className="gv-proj-rows">
          {rows.map((p) => (
            <li key={p.id} className="gv-limit-row" data-testid={`gv-scoped-${p.id}`}>
              <span className="gv-limit-label" data-user-text>{describeScope(p, names)}</span>
              <span>{usd(p.spent_usd_micros)} of {usd(p.cap_usd_micros)} / {p.period}</span>
              <span className="gv-limit-hint" title={p.hard_stop ? "Refuses new work and stops a running build at its next turn. Never asks anyone." : "Warns only"}>
                {p.hard_stop ? "Hard stop" : "Warn only"}
              </span>
              <GatedButton permission="budget:manage" type="button" className="ag-btn" disabled={busy}
                           data-testid={`gv-scoped-remove-${p.id}`} aria-label={`Remove ${describeScope(p, names)} budget`}
                           onClick={() => void remove(p.id)}><Trash size={14} aria-hidden />Remove</GatedButton>
            </li>
          ))}
        </ul>
      ) : <span className="gv-muted" data-testid="gv-scoped-none">No scoped budgets</span>}
      <div className="gv-limit-row" data-testid="gv-scoped-add">
        <select className="gv-input" aria-label="Scope" value={scope} data-testid="gv-scoped-scope"
                onChange={(e) => { setScope(e.target.value as Scope); setTarget(""); }}>
          {(Object.keys(SCOPE_WORDS) as Scope[]).map((s) => <option key={s} value={s}>{SCOPE_WORDS[s]}</option>)}
        </select>
        {scope === "role" || scope === "workflow" || scope === "team" ? (
          <select className="gv-input" aria-label={scope === "role" ? "Agent" : scope === "team" ? "Team" : "Workflow"} value={target}
                  data-testid="gv-scoped-target" onChange={(e) => setTarget(e.target.value)}>
            <option value="">{scope === "role" ? "Pick an agent" : scope === "team" ? (teams.length ? "Pick a team" : "No teams yet") : "Pick a workflow"}</option>
            {choices.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        ) : null}
        <span className="gv-money-input">$<input className="gv-input" type="number" inputMode="decimal" step="any"
               aria-label="Cap" value={cap} data-testid="gv-scoped-cap" onChange={(e) => setCap(e.target.value)} /></span>
        <select className="gv-input" aria-label="Period" value={period} data-testid="gv-scoped-period"
                onChange={(e) => setPeriod(e.target.value as "day" | "month")}>
          <option value="month">per month</option><option value="day">per day</option>
        </select>
        <label className="gv-radio"><input type="checkbox" checked={hard} data-testid="gv-scoped-hard"
               onChange={(e) => setHard(e.target.checked)} /> Hard stop</label>
        <GatedButton permission="budget:manage" type="button" className="ag-btn ag-btn-primary" disabled={busy || !ready}
                     data-testid="gv-scoped-save" onClick={() => void add()}><Plus size={14} weight="bold" aria-hidden />Add</GatedButton>
      </div>
      {scope === "team" ? (
        <div className="gv-preset-row" data-testid="gv-scoped-preset-row">
          <GatedButton permission="budget:manage" type="button" className="ag-btn" disabled={busy || !target}
                       data-testid="gv-scoped-preset"
                       title={target ? "$2,000 a month for this team, warn at 80%, hard stop" : "Pick a team first"}
                       onClick={() => void addPilot()}><RocketLaunch size={14} aria-hidden />{PILOT_PRESET.label}</GatedButton>
          <span className="gv-muted">A team spends what its people&apos;s projects spend.</span>
        </div>
      ) : null}
      {said ? <span className="gv-field-error" role="alert" data-testid="gv-scoped-error">{said}</span> : null}
    </section>
  );
}
