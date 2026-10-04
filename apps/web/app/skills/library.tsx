"use client";

/**
 * Library (spec §5.5): every skill and every MCP server, one list each, with authoring and any
 * custom server. Skills BOSS uses itself (machinery only, held by nobody, in no ability) are
 * behind `Show internal` — the old page explained them in a sentence per row.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { DotsThree, MagnifyingGlass, Plus, Sparkle } from "@phosphor-icons/react";

import type { Ability } from "../../lib/kit-types";
import { mcpApi, type CrewMember, type KitWithCrew, type McpServer } from "../../lib/kit-api";
import { openDockAbout } from "../../lib/dock-subject";
import { HolderStack, StatusPill } from "./ability-card";
import { AddMcpSheet } from "./add-mcp-sheet";
import { ImportSheet } from "./import-sheet";
import { riskOf, TIP, PROVENANCE } from "./kit-copy";
import { writeKitParams, type KitParams } from "./kit-url";
import { McpDrawer, serverStatus } from "./mcp-drawer";
import { RiskDot } from "./tools-section";
import { type BuiltIn, type Skill, skillStatus } from "./library-data";
import { SkillDrawer } from "./skill-drawer";

type Assignment = { role_key: string; skill_id: string };

/** Open the dock addressed to the Program Manager to describe a skill (text unchanged). */
export function askBossAboutSkills(projectId: string): void {
  openDockAbout({
    subject: null,
    role: "Program Manager",
    question:
      "I want to create a skill for this project. A skill is the reusable unit of doing that a"
      + " workflow step runs — trimming a video, posting to a channel, calling one tool on an"
      + " MCP server. Ask me what you need to know, then draft it and I will create it."
      + ` Start by telling me which skills ${projectId} already has, so I do not ask for one`
      + " that exists.",
  });
}

export function serverProvenance(s: McpServer): "built_in" | "official" | "custom" {
  const source = String((s.provenance as { source?: string })?.source ?? "");
  if (source === "builtin" || source === "builtin_edited") return "built_in";
  if (source === "catalogue") return "official";
  return "custom";
}

export function Library({ projectId, actor, kit, crew, initial, onKitChanged }: {
  projectId: string; actor: string; kit: KitWithCrew | null; crew: CrewMember[];
  initial: KitParams; onKitChanged: () => void;
}) {
  const [segment, setSegment] = useState<"skills" | "mcp">(initial.lib);
  const [query, setQuery] = useState("");
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [builtIn, setBuiltIn] = useState<BuiltIn[] | null>(null);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [openSkill, setOpenSkill] = useState<string | null>(initial.skill);
  const [creating, setCreating] = useState(false);
  const [openServer, setOpenServer] = useState<string | null>(initial.mcp);
  const [adding, setAdding] = useState(false);
  const [importing, setImporting] = useState(false);
  const [menu, setMenu] = useState(false);
  const [filters, setFilters] = useState({ yours: false, builtIn: false, internal: false });

  const enc = encodeURIComponent(projectId);
  const loadSkills = useCallback(async () => {
    try {
      const [a, b, c] = await Promise.all([
        fetch(`/api/skills?project_id=${enc}`, { cache: "no-store" }),
        fetch(`/api/skills?view=builtin&project_id=${enc}`, { cache: "no-store" }),
        fetch(`/api/skills?view=assignments&project_id=${enc}`, { cache: "no-store" }),
      ]);
      setSkills(a.ok ? (((await a.json()).skills ?? []) as Skill[]) : []);
      setBuiltIn(b.ok ? (((await b.json()).items ?? []) as BuiltIn[]).filter((s) => s.source !== "authored") : []);
      if (c.ok) setAssignments(((await c.json()).assignments ?? []) as Assignment[]);
    } catch { setSkills((s) => s ?? []); setBuiltIn((s) => s ?? []); }
  }, [enc]);
  const loadServers = useCallback(async (check: "none" | "stale" = "stale") => {
    const r = await mcpApi.list(projectId, check);
    setServers(r.ok ? r.data.items : []);
  }, [projectId]);

  useEffect(() => { void loadSkills(); void loadServers(); }, [loadSkills, loadServers]);

  const abilities: Ability[] = kit?.abilities ?? [];
  const abilityOfSkill = useMemo(() => {
    const map = new Map<string, Ability>();
    for (const a of abilities) for (const t of a.tools) if (t.ref.startsWith("skill:")) map.set(t.ref.slice(6), a);
    return map;
  }, [abilities]);
  const crewKeys = useMemo(() => new Set(crew.map((c) => c.role_key)), [crew]);
  const holdersOf = (id: string) => assignments.filter((a) => a.skill_id === id).map((a) => a.role_key);

  type Row = { id: string; label: string; origin: "yours" | "built_in" | "agent"; tier: number; status: ReturnType<typeof skillStatus>;
               holders: string[]; ability: Ability | undefined; used: number; internal: boolean; tip: string };
  const rows: Row[] = useMemo(() => {
    const out: Row[] = [];
    for (const s of skills ?? []) {
      const tier = s.catalogue?.risk_tier ?? s.risk_tier;
      const holders = holdersOf(s.skill_key);
      out.push({ id: s.skill_key, label: s.label || s.skill_key,
                 origin: s.behaviour_kind === "agent_program" ? "agent" : "yours", tier,
                 status: skillStatus(s.enabled, s.catalogue?.usable !== false, tier >= 1 && !s.trusted, holders.length),
                 holders, ability: undefined,
                 used: (s.references?.custom_steps?.length ?? 0) + (s.references?.workflows?.length ?? 0),
                 internal: false, tip: s.summary || s.skill_key });
    }
    for (const b of builtIn ?? []) {
      const holders = holdersOf(b.id);
      const ability = abilityOfSkill.get(b.id);
      const callers = b.permissions?.allowed_callers ?? [];
      const internal = !ability && holders.length === 0 && !callers.includes("*")
        && callers.every((c) => !crewKeys.has(c));
      out.push({ id: b.id, label: b.id, origin: "built_in", tier: b.risk_tier ?? 0,
                 status: skillStatus(b.enabled !== false || Boolean(b.system), true, false, holders.length),
                 holders, ability, used: ability?.used_in.length ?? 0, internal, tip: b.description ?? b.id });
    }
    return out;
  }, [skills, builtIn, assignments, abilityOfSkill, crewKeys]); // eslint-disable-line react-hooks/exhaustive-deps

  const q = query.trim().toLowerCase();
  const visibleSkills = rows.filter((r) => (filters.internal || !r.internal)
    && (!filters.yours || r.origin !== "built_in") && (!filters.builtIn || r.origin === "built_in")
    && (!q || `${r.id} ${r.label} ${r.tip}`.toLowerCase().includes(q)));
  const visibleServers = (servers ?? []).filter((s) => !q || `${s.name} ${s.description ?? ""}`.toLowerCase().includes(q));

  const pick = (seg: "skills" | "mcp") => { setSegment(seg); writeKitParams({ lib: seg === "mcp" ? "mcp" : null, skill: null, mcp: null }); };
  const showSkill = (id: string | null) => { setOpenSkill(id); setCreating(false); writeKitParams({ skill: id }); };
  const showServer = (id: string | null) => { setOpenServer(id); writeKitParams({ mcp: id }); };
  const skill = (skills ?? []).find((s) => s.skill_key === openSkill) ?? null;
  const builtin = (builtIn ?? []).find((b) => b.id === openSkill) ?? null;
  const server = (servers ?? []).find((s) => s.id === openServer) ?? null;

  return (
    <div className="kit-lib" data-testid="kit-library" data-drawer={(skill || builtin || creating || server) ? "open" : undefined}>
      <div className="kit-head">
        <span className="kit-seg" role="radiogroup" aria-label="Library">
          <button type="button" role="radio" aria-checked={segment === "skills"} data-testid="kit-lib-skills" onClick={() => pick("skills")}>Skills</button>
          <button type="button" role="radio" aria-checked={segment === "mcp"} data-testid="kit-lib-mcp" onClick={() => pick("mcp")}>MCP</button>
        </span>
        <label className="kit-search">
          <MagnifyingGlass size={14} aria-hidden="true" />
          <input type="search" value={query} placeholder="Find…" aria-label="Find" onChange={(e) => setQuery(e.target.value)} />
        </label>
        <span className="kit-spacer" />
        <button type="button" className="kit-btn" data-tip={TIP.describe} data-testid="kit-lib-describe"
                onClick={() => askBossAboutSkills(projectId)}><Sparkle size={14} weight="fill" /> Describe</button>
        {segment === "skills" ? (
          <button type="button" className="kit-btn kit-btn-primary" data-testid="kit-lib-new-skill"
                  onClick={() => { setOpenSkill(null); setCreating(true); }}><Plus size={14} /> New skill</button>
        ) : (
          <button type="button" className="kit-btn kit-btn-primary" data-testid="kit-lib-add-mcp"
                  onClick={() => setAdding(true)}><Plus size={14} /> Add MCP</button>
        )}
        <span className="kit-confirm-wrap">
          <button type="button" className="kit-icon-btn" aria-label="More" data-testid="kit-lib-more" onClick={() => setMenu((m) => !m)}>
            <DotsThree size={18} />
          </button>
          {menu ? (
            <span className="kit-pop kit-menu" role="menu" data-testid="kit-lib-menu">
              <button type="button" role="menuitem" data-testid="kit-lib-import" onClick={() => { setMenu(false); setImporting(true); }}>Import</button>
              <button type="button" role="menuitem" data-testid="kit-lib-export" onClick={() => { setMenu(false); exportAll(segment, skills ?? [], servers ?? []); }}>Export all</button>
            </span>
          ) : null}
        </span>
      </div>

      {segment === "skills" ? (
        <>
          <div className="kit-filters">
            <button type="button" className="kit-chip kit-chip-btn" aria-pressed={filters.yours} onClick={() => setFilters((f) => ({ ...f, yours: !f.yours, builtIn: false }))}>Yours</button>
            <button type="button" className="kit-chip kit-chip-btn" aria-pressed={filters.builtIn} onClick={() => setFilters((f) => ({ ...f, builtIn: !f.builtIn, yours: false }))}>Built in</button>
            <button type="button" className="kit-chip kit-chip-btn" aria-pressed={filters.internal} data-tip={TIP.showInternal}
                    data-testid="kit-lib-internal" onClick={() => setFilters((f) => ({ ...f, internal: !f.internal }))}>Show internal</button>
          </div>
          {skills === null ? <div className="kit-row kit-skeleton" aria-busy="true" /> : (
            <ul className="kit-list" data-testid="kit-skill-list">
              {visibleSkills.map((r) => (
                <li key={`${r.origin}:${r.id}`} className="kit-list-row" data-testid={`kit-skill-${r.id}`}
                    onClick={() => showSkill(r.id)}>
                  <span className="kit-list-name" data-tip={r.tip} data-user-text={r.origin !== "built_in" || undefined}>{r.label}</span>
                  <span className="kit-chip" data-tip={r.origin === "built_in" ? TIP.builtIn : r.origin === "agent" ? "Written by an agent on an earlier run" : TIP.custom}>
                    {r.origin === "built_in" ? "Built in" : r.origin === "agent" ? "Agent-written" : "Yours"}
                  </span>
                  {r.ability ? <span className="kit-chip" data-tone="accent" data-tip={r.ability.tip}>{r.ability.label}</span> : <span />}
                  <RiskDot tier={r.tier} />
                  <HolderStack holders={r.holders} max={3} size={20} />
                  <span className="kit-faint" data-tip={TIP.usedIn}>Used in {r.used}</span>
                  <StatusPill status={r.status} />
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        servers === null ? <div className="kit-row kit-skeleton" aria-busy="true" /> : (
          <ul className="kit-list" data-testid="kit-mcp-list">
            {visibleServers.map((s) => {
              const prov = PROVENANCE[serverProvenance(s)];
              const holders = (s.references?.agents ?? []).map((a) => a.role_key);
              return (
                <li key={s.id} className="kit-list-row" data-testid={`kit-mcp-${s.name}`} onClick={() => showServer(s.id)}>
                  <span className="kit-list-name" data-tip={s.description ?? s.url} data-user-text>{s.name}</span>
                  <span className="kit-chip" data-tip={prov.tip}>{prov.label}</span>
                  <span className="kit-faint">{s.tools.length} tools</span>
                  <span className="kit-risk" data-tone={riskOf(s.risk_tier).tone} data-tip={riskOf(s.risk_tier).tip}>{riskOf(s.risk_tier).label}</span>
                  <HolderStack holders={holders} max={3} size={20} />
                  <span />
                  <StatusPill status={serverStatus(s, holders.length)} tip={s.last_error ?? undefined} />
                </li>
              );
            })}
          </ul>
        )
      )}

      {creating || skill || builtin ? (
        <SkillDrawer projectId={projectId} actor={actor} skill={creating ? null : skill} builtIn={creating ? null : builtin}
                     creating={creating} crew={crew} holders={openSkill ? holdersOf(openSkill) : []}
                     servers={servers ?? []} builtIns={builtIn ?? []} ability={openSkill ? abilityOfSkill.get(openSkill) : undefined}
                     onClose={() => { setCreating(false); showSkill(null); }}
                     onSaved={(key) => { void loadSkills(); onKitChanged(); if (key) { setCreating(false); showSkill(key); } }} />
      ) : null}
      {server ? (
        <McpDrawer projectId={projectId} actor={actor} server={server} crew={crew}
                   onClose={() => showServer(null)}
                   onChanged={() => { void loadServers("none"); onKitChanged(); }}
                   onRemoved={() => { showServer(null); void loadServers("none"); onKitChanged(); }} />
      ) : null}
      {adding ? (
        <AddMcpSheet projectId={projectId} actor={actor} onClose={() => setAdding(false)}
                     onAdded={(id) => { setAdding(false); void loadServers("none").then(() => showServer(id)); onKitChanged(); }} />
      ) : null}
      {importing ? (
        <ImportSheet projectId={projectId} actor={actor} onClose={() => setImporting(false)}
                     onImported={() => { setImporting(false); void loadSkills(); void loadServers("none"); }} />
      ) : null}
    </div>
  );
}

function exportAll(segment: "skills" | "mcp", skills: Skill[], servers: McpServer[]): void {
  // CONFIG AS CODE, never a secret: a server's auth is exported as its kind, not its token.
  const data = segment === "skills"
    ? { skills: skills.map((s) => ({ skill_key: s.skill_key, label: s.label, summary: s.summary,
                                      behaviour_kind: s.behaviour_kind, config: s.config, knowledge: s.knowledge,
                                      inputs: s.inputs, produces: s.produces, risk_tier: s.risk_tier })) }
    : { mcpServers: Object.fromEntries(servers.map((s) => [s.name, {
        type: "http", url: s.url, auth: { type: s.auth.type, header_name: s.auth.header_name },
        risk_tier: s.risk_tier, tool_policy: s.tool_policy }])) };
  try {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url; a.download = segment === "skills" ? "skills.json" : "mcp.json";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch { /* nothing to download into */ }
}
