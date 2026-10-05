"use client";

/**
 * The facet rail (spec §4.2.5): LEVEL, STREAM, AGENT, RUN, PROCESS, STATUS, each value with the
 * SERVER's count for the current query (a facet ignores its own selection, so a count says how
 * many lines picking it would show). The agent list used to be built from the 200 rows on
 * screen; these are counted over the whole window. A click adds or removes the value's token in
 * the query bar — the bar is the truth, the rail reflects it.
 */
import React, { useState } from "react";
import {
  ArrowsLeftRight, Brain, ChatText, CloudArrowUp, Lightning, Prohibit, Queue, Receipt, Terminal,
  TreeStructure,
} from "@phosphor-icons/react";

import type { ActivityFacets } from "../../lib/activity-log";
import { toggleToken, type LogToken, type LogTokenKey } from "../../lib/log-url";
import { nameOf, RunFace } from "../agents/run-face";
import { LEVELS, SERVICE_LABEL, STATUSES, STREAMS } from "./log-model";
import { useIdentityVersion } from "../../lib/use-agent-identities";

export const STREAM_ICON: Record<string, React.ComponentType<{ size?: number; weight?: "bold" | "regular" }>> = {
  event: Lightning, workflow_step: TreeStructure, process_step: ArrowsLeftRight, agent_turn: ChatText,
  llm_call: Brain, app_log: Terminal, deployment: CloudArrowUp, job: Queue, deadletter: Prohibit,
  receipt: Receipt,
};

const AGENTS_SHOWN = 12;
const RUNS_SHOWN = 8;

function count(n: number | undefined, exact: boolean): string {
  if (typeof n !== "number") return "";
  return `${exact ? "" : "≥"}${n >= 10_000 ? `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1)}k` : n.toLocaleString()}`;
}

function Section({ id, label, open, onToggle, children }: {
  id: string; label: string; open: boolean; onToggle: () => void; children: React.ReactNode;
}) {
  return (
    <section className="lx-rail-sec" data-testid={`lx-rail-${id}`}>
      <button type="button" className="lx-rail-head" aria-expanded={open} onClick={onToggle}>{label}</button>
      {open ? <ul className="lx-rail-list">{children}</ul> : null}
    </section>
  );
}

function RailRow({ k, value, label, n, icon, testid, pressed, exact, loaded, onToggle, onOnly }: {
  k: LogTokenKey; value: string; label: React.ReactNode; n?: number; icon?: React.ReactNode; testid: string;
  pressed: boolean; exact: boolean; loaded: boolean; onToggle: () => void; onOnly?: () => void;
}) {
  return (
    <li className="lx-rail-row" data-key={k} data-value={value}>
      <button type="button" className="lx-rail-pick" data-testid={testid} aria-pressed={pressed} onClick={onToggle}>
        <span className="lx-check" aria-hidden />
        {icon}
        <span className="lx-rail-label">{label}</span>
        <span className="lx-count" title={exact ? undefined : "At least this many. Counting stopped early."}>
          {loaded ? count(n ?? 0, exact) : ""}
        </span>
      </button>
      {onOnly ? (
        <button type="button" className="lx-rail-only" title="Show only this stream" onClick={onOnly}>Only</button>
      ) : null}
    </li>
  );
}

export function FacetRail({ facets, tokens, showVerbose, onTokens, onVerbose }: {
  facets: ActivityFacets | null;
  tokens: LogToken[];
  showVerbose: boolean;
  onTokens: (tokens: LogToken[]) => void;
  onVerbose: (show: boolean) => void;
}) {
  useIdentityVersion(); // names land when the install's agents load
  const [closed, setClosed] = useState<Record<string, boolean>>({ status: true });
  const [moreAgents, setMoreAgents] = useState(false);
  const [moreRuns, setMoreRuns] = useState(false);
  const [agentFind, setAgentFind] = useState("");
  const exact = facets?.exact !== false;
  const has = (key: LogTokenKey, value: string) => tokens.some((t) => t.key === key && t.value === value && !t.negate);
  const toggle = (key: LogTokenKey, value: string) => onTokens(toggleToken(tokens, { key, value }));
  const only = (key: LogTokenKey, value: string) =>
    onTokens([...tokens.filter((t) => t.key !== key), { key, value }]);
  const levelChosen = tokens.some((t) => t.key === "level" && !t.negate);
  const hidden = !showVerbose && !levelChosen ? facets?.levels.verbose ?? 0 : 0;
  const sec = (id: string) => ({ open: !closed[id], onToggle: () => setClosed((c) => ({ ...c, [id]: !c[id] })) });

  const agents = (facets?.agents ?? []).filter((a) => !agentFind
    || nameOf(a.roleKey).name.toLowerCase().includes(agentFind.toLowerCase()));
  return (
    <nav className="lx-rail" aria-label="Filters" data-testid="lx-rail" aria-busy={facets === null || undefined}>
      {tokens.length ? (
        <button type="button" className="ag-link lx-rail-clear" data-testid="lx-clear-all"
                onClick={() => onTokens([])}>Clear all</button>
      ) : null}
      <Section id="level" label="LEVEL" {...sec("level")}>
        {LEVELS.map((l) => (
          <RailRow key={l.key} k="level" value={l.key} pressed={has("level", l.key)} exact={exact} loaded={facets !== null} onToggle={() => toggle("level", l.key)} n={facets?.levels[l.key]} testid={`lx-level-${l.key}`}
               icon={<span className="lx-dot" data-level={l.key} aria-hidden />} label={l.label} />
        ))}
        <li className="lx-rail-row">
          <button type="button" className="lx-rail-pick" data-testid="lx-hide-verbose" aria-pressed={!showVerbose}
                  onClick={() => onVerbose(!showVerbose)}>
            <span className="lx-check" aria-hidden />
            <span className="lx-rail-label">Hide verbose</span>
            {hidden ? <span className="lx-count" title="Verbose lines hidden. Toggle to show.">{count(hidden, exact)} hidden</span> : null}
          </button>
        </li>
      </Section>
      <Section id="stream" label="STREAM" {...sec("stream")}>
        {STREAMS.map((s) => {
          const Icon = STREAM_ICON[s.key];
          return (
            <RailRow key={s.key} k="src" value={s.key} pressed={has("src", s.key)} exact={exact} loaded={facets !== null} onToggle={() => toggle("src", s.key)} onOnly={() => only("src", s.key)} n={facets?.sources[s.key]} testid={`lx-src-${s.key}`}
                 icon={Icon ? <Icon size={13} aria-hidden /> : null}
                 label={<span title={s.hint}>{s.label}</span>} />
          );
        })}
      </Section>
      <Section id="agent" label="AGENT" {...sec("agent")}>
        {moreAgents ? (
          <li className="lx-rail-row">
            <input className="lx-rail-find" placeholder="Find an agent" aria-label="Find an agent" value={agentFind}
                   onChange={(e) => setAgentFind(e.target.value)} />
          </li>
        ) : null}
        {agents.slice(0, moreAgents ? 40 : AGENTS_SHOWN).map((a) => (
          <RailRow key={a.roleKey} k="agent" value={a.roleKey} pressed={has("agent", a.roleKey)} exact={exact} loaded={facets !== null} onToggle={() => toggle("agent", a.roleKey)} n={a.n} testid={`lx-agent-${a.roleKey}`}
               icon={<RunFace roleKey={a.roleKey} size={20} />}
               label={<span data-user-text>{nameOf(a.roleKey).name}</span>} />
        ))}
        {facets && facets.agents.length === 0 ? <li className="lx-rail-none">None</li> : null}
        {!moreAgents && (facets?.agents.length ?? 0) > AGENTS_SHOWN ? (
          <li><button type="button" className="ag-link" onClick={() => setMoreAgents(true)}>
            +{(facets!.agents.length - AGENTS_SHOWN) + (facets!.agentsMore || 0)} more
          </button></li>
        ) : null}
      </Section>
      <Section id="run" label="RUN" {...sec("run")}>
        {(facets?.runs ?? []).slice(0, moreRuns ? 40 : RUNS_SHOWN).map((r) => {
          const key: LogTokenKey = r.id.startsWith("crun_") ? "run" : "wfr";
          return (
            <RailRow key={r.id} k={key} value={r.id} pressed={has(key, r.id)} exact={exact} loaded={facets !== null} onToggle={() => toggle(key, r.id)} n={r.n} testid={`lx-run-${r.id}`}
                 label={<span data-user-text title={r.id}>{r.label || r.id}</span>} />
          );
        })}
        {facets && facets.runs.length === 0 ? <li className="lx-rail-none">None</li> : null}
        {!moreRuns && (facets?.runs.length ?? 0) > RUNS_SHOWN ? (
          <li><button type="button" className="ag-link" onClick={() => setMoreRuns(true)}>+{facets!.runs.length - RUNS_SHOWN} more</button></li>
        ) : null}
      </Section>
      <Section id="process" label="PROCESS" {...sec("process")}>
        {["api", "worker"].map((svc) => (
          <RailRow key={svc} k="svc" value={svc} pressed={has("svc", svc)} exact={exact} loaded={facets !== null} onToggle={() => toggle("svc", svc)} n={facets?.services[svc]} testid={`lx-svc-${svc}`}
               label={<span title="Which BOSS process wrote it">{SERVICE_LABEL[svc]}</span>} />
        ))}
      </Section>
      <Section id="status" label="STATUS" {...sec("status")}>
        {STATUSES.map((s) => (
          <RailRow key={s.key} k="status" value={s.key} pressed={has("status", s.key)} exact={exact} loaded={facets !== null} onToggle={() => toggle("status", s.key)} n={facets?.statuses[s.key]} testid={`lx-status-${s.key}`}
               label={s.label} />
        ))}
      </Section>
    </nav>
  );
}
