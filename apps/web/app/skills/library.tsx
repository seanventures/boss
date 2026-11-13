"use client";

/**
 * Library (spec §5.5): every skill and every MCP server, one list each, with authoring and any
 * custom server. Skills BOSS uses itself (machinery only, held by nobody, in no ability) are
 * behind `Show internal`, the old page explained them in a sentence per row.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DotsThree, FileText, MagnifyingGlass, Plus, Sparkle } from "@phosphor-icons/react";

import type { Ability, AbilityStatus } from "../../lib/kit-types";
import { ReadFailure } from "../../components/locked-panel";
import { mcpApi, type CrewMember, type KitWithCrew, type McpServer } from "../../lib/kit-api";
import { openDockAbout } from "../../lib/dock-subject";
import { HolderStack, StatusPill } from "./ability-card";
import { AddMcpSheet } from "./add-mcp-sheet";
import { GatewaysSheet } from "./gateways-sheet";
import { ImportSheet } from "./import-sheet";
import { plural, riskOf, TIP, PROVENANCE } from "./kit-copy";
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
      + " workflow step runs, trimming a video, posting to a channel, calling one tool on an"
      + " MCP server. Ask me what you need to know, then draft it and I will create it."
      + ` Start by telling me which skills ${projectId} already has, so I do not ask for one`
      + " that exists.",
  });
}

/** A skill's name on screen (K13): the Kit's label for it, else its id said as words. */
export function skillLabel(id: string, ability: Ability | undefined): string {
  const row = ability?.tools.find((t) => t.ref === `skill:${id}`);
  if (row?.label) return row.label;
  const words = id.split(".").slice(-2).join(" ").replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : id;
}

/** How many workflow steps use THIS item (K13), not the whole ability it belongs to. A step
 *  record names what it uses (`via`); one that does not is counted for nobody rather than for
 *  every skill of the ability. */
export function usedBy(ability: Ability | undefined, ref: string): number {
  if (!ability) return 0;
  const server = ref.startsWith("mcp:") ? ref.slice(4) : null;
  return ability.used_in.filter((u) => u.via === ref || (server !== null && u.via?.startsWith(`mcp:${server}`))).length;
}

/**
 * A shipped skill's status (K13). One in a Kit ability is as ready as its ability, Open an
 * issue is not Ready while File tickets is not set up. One outside every ability is Ready only
 * when the catalogue says it can run (`usable`); a cost sync with no provider key is Set up.
 */
export function builtInStatus(b: BuiltIn, ability: Ability | undefined, holders: number): AbilityStatus {
  if (b.enabled === false && !b.system) return "off";
  if (ability) {
    if (ability.status === "ready" || ability.status === "unheld") return holders ? "ready" : "unheld";
    return ability.status;
  }
  if (b.usable === false) return "setup";
  if (b.usable === undefined) return holders ? "untested" : "unheld";
  return holders ? "ready" : "unheld";
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
  const [gateways, setGateways] = useState(false);
  const [menu, setMenu] = useState(false);
  const [filters, setFilters] = useState({ yours: false, builtIn: false, internal: false });

  const enc = encodeURIComponent(projectId);
  // THE LATEST READ WINS (K12). Two reads in flight (a review's, then a grant's) could land out of
  // order, and the older answer then put back the row as it was before the grant.
  const skillSeq = useRef(0);
  const serverSeq = useRef(0);
  // CAN'T REACH IS SAID, NOT SHOWN AS EMPTY (K14): a 502 or a dropped connection is a red bar
  // with Retry, never "no skills".
  const [skillsError, setSkillsError] = useState<string | null>(null);
  const [serversError, setServersError] = useState<string | null>(null);
  const [skillsStatus, setSkillsStatus] = useState(0);
  const [serversStatus, setServersStatus] = useState(0);
  const loadSkills = useCallback(async () => {
    const mine = ++skillSeq.current;
    try {
      const [a, b, c] = await Promise.all([
        fetch(`/api/skills?project_id=${enc}`, { cache: "no-store" }),
        fetch(`/api/skills?view=builtin&project_id=${enc}`, { cache: "no-store" }),
        fetch(`/api/skills?view=assignments&project_id=${enc}`, { cache: "no-store" }),
      ]);
      if (mine !== skillSeq.current) return;
      const bad = [a, b, c].find((r) => !r.ok);
      if (bad) {
        const said = await bad.json().catch(() => ({})) as { error?: string; reason?: string };
        setSkillsStatus(bad.status);
        setSkillsError(String(said.reason ?? said.error ?? `HTTP ${bad.status}`));
        return;
      }
      const [sa, sb, sc] = await Promise.all([a.json(), b.json(), c.json()]);
      if (mine !== skillSeq.current) return;
      setSkills((sa.skills ?? []) as Skill[]);
      setBuiltIn(((sb.items ?? []) as BuiltIn[]).filter((x) => x.source !== "authored"));
      setAssignments((sc.assignments ?? []) as Assignment[]);
      setSkillsError(null);
    } catch (e) {
      if (mine === skillSeq.current) { setSkillsStatus(0); setSkillsError(String((e as Error)?.message ?? e)); }
    }
  }, [enc]);
  const loadServers = useCallback(async (check: "none" | "stale" = "stale") => {
    const mine = ++serverSeq.current;
    const r = await mcpApi.list(projectId, check);
    if (mine !== serverSeq.current) return;
    if (!r.ok) { setServersStatus(r.status ?? 0); setServersError(r.error); return; }
    setServers(r.data.items);
    setServersError(null);
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
               /** A guide (a SKILL.md playbook): marked with a page icon. */
               guide?: boolean;
               holders: string[]; ability: Ability | undefined; used: number; internal: boolean; tip: string };
  const rows: Row[] = useMemo(() => {
    const out: Row[] = [];
    for (const s of skills ?? []) {
      const tier = s.catalogue?.risk_tier ?? s.risk_tier;
      const holders = holdersOf(s.skill_key);
      out.push({ id: s.skill_key, label: s.label || s.skill_key,
                 origin: s.behaviour_kind === "agent_program" ? "agent" : "yours", tier,
                 guide: s.behaviour_kind === "guide",
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
      out.push({ id: b.id, label: skillLabel(b.id, ability), origin: "built_in", tier: b.risk_tier ?? 0,
                 status: builtInStatus(b, ability, holders.length),
                 holders, ability, used: usedBy(ability, `skill:${b.id}`), internal,
                 tip: b.description ? `${b.id}, ${b.description}` : b.id });
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
              {segment === "mcp" ? (
                <button type="button" role="menuitem" data-testid="kit-lib-gateways"
                        data-tip="Serve chosen servers to Claude Code, Cursor and other MCP clients"
                        onClick={() => { setMenu(false); setGateways(true); }}>Gateways</button>
              ) : null}
            </span>
          ) : null}
        </span>
      </div>

      {(segment === "skills" ? skillsError : serversError) ? (
        <ReadFailure testId="kit-lib-error" retryTestId="kit-lib-retry"
                     problem={segment === "skills" ? { text: skillsError ?? "", status: skillsStatus } : { text: serversError ?? "", status: serversStatus }}
                     onRetry={() => void (segment === "skills" ? loadSkills() : loadServers("none"))} />
      ) : null}

      {segment === "skills" ? (
        <>
          <div className="kit-filters">
            <button type="button" className="kit-chip kit-chip-btn" aria-pressed={filters.yours} onClick={() => setFilters((f) => ({ ...f, yours: !f.yours, builtIn: false }))}>Yours</button>
            <button type="button" className="kit-chip kit-chip-btn" aria-pressed={filters.builtIn} onClick={() => setFilters((f) => ({ ...f, builtIn: !f.builtIn, yours: false }))}>Built in</button>
            <button type="button" className="kit-chip kit-chip-btn" aria-pressed={filters.internal} data-tip={TIP.showInternal}
                    data-testid="kit-lib-internal" onClick={() => setFilters((f) => ({ ...f, internal: !f.internal }))}>Show internal</button>
          </div>
          {skills === null ? (skillsError ? null : <div className="kit-row kit-skeleton" aria-busy="true" />) : (
            <ul className="kit-list" data-testid="kit-skill-list">
              {visibleSkills.length === 0 ? (
                <li className="kit-list-empty" data-testid="kit-lib-empty">
                  <span>No matches</span>
                  {q ? <button type="button" className="kit-link" onClick={() => setQuery("")}>Clear</button> : null}
                </li>
              ) : null}
              {visibleSkills.map((r) => (
                <li key={`${r.origin}:${r.id}`} className="kit-list-row" data-testid={`kit-skill-${r.id}`}
                    onClick={() => showSkill(r.id)}>
                  <span className="kit-list-name" data-tip={r.tip} data-user-text={r.origin !== "built_in" || undefined}>
                    {r.guide ? <><FileText size={13} aria-label="Guide" data-testid={`kit-guide-${r.id}`} />{" "}</> : null}{r.label}
                  </span>
                  <span className="kit-chip" data-tip={r.origin === "built_in" ? TIP.builtIn : r.origin === "agent" ? "Written by an agent on an earlier run" : TIP.custom}>
                    {r.origin === "built_in" ? "Built in" : r.origin === "agent" ? "Agent-written" : "Yours"}
                  </span>
                  {r.ability ? <span className="kit-chip" data-tone="accent" data-tip={r.ability.tip}>{r.ability.label}</span> : <span />}
                  <RiskDot tier={r.tier} />
                  <HolderStack holders={r.holders} max={3} size={20} />
                  <span className="kit-faint" data-tip={TIP.usedIn}>Used in {r.used}</span>
                  <StatusPill status={r.status} tip={r.ability ? r.ability.status_tip : undefined} />
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        servers === null ? (serversError ? null : <div className="kit-row kit-skeleton" aria-busy="true" />) : (
          <ul className="kit-list" data-testid="kit-mcp-list">
            {visibleServers.length === 0 ? (
              <li className="kit-list-empty" data-testid="kit-lib-empty">
                <span>No matches</span>
                {q ? <button type="button" className="kit-link" onClick={() => setQuery("")}>Clear</button> : null}
              </li>
            ) : null}
            {visibleServers.map((s) => {
              const prov = PROVENANCE[serverProvenance(s)];
              const holders = (s.references?.agents ?? []).map((a) => a.role_key);
              return (
                <li key={s.id} className="kit-list-row" data-testid={`kit-mcp-${s.name}`} onClick={() => showServer(s.id)}>
                  <span className="kit-list-name" data-tip={s.description ?? s.url} data-user-text>{s.name}</span>
                  <span className="kit-chip" data-tip={prov.tip}>{prov.label}</span>
                  <span className="kit-faint">{plural(s.tools.length, "tool")}</span>
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
        <McpDrawer projectId={projectId} actor={actor} server={server} crew={crew} abilities={abilities}
                   onClose={() => showServer(null)}
                   onChanged={(next) => {
                     if (next) setServers((all) => (all ?? []).map((x) => (x.id === next.id ? next : x)));
                     void loadServers("none"); onKitChanged();
                   }}
                   onRemoved={() => { showServer(null); void loadServers("none"); onKitChanged(); }} />
      ) : null}
      {adding ? (
        <AddMcpSheet projectId={projectId} actor={actor} onClose={() => setAdding(false)}
                     onAdded={(id) => { setAdding(false); void loadServers("none").then(() => showServer(id)); onKitChanged(); }} />
      ) : null}
      {gateways ? (
        <GatewaysSheet projectId={projectId} actor={actor} servers={servers ?? []} onClose={() => setGateways(false)} />
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
