"use client";

/**
 * The Passport's Loadout › Skills and MCP rows (agent-loadout spec §3.2): what this agent may
 * run, with ONE place per grant.
 *
 * Before this the Passport showed a grant three times, the Kit's ability chips, a skill list
 * with its own `<select>`, and an MCP list with a comma-separated tools box. Now each held thing
 * appears exactly once (`partitionLoadout`): an ability whose tools are all MCP goes in MCP,
 * every other ability in Skills; a held skill or server no held ability covers is a plain chip
 * in its row. Ability chips keep the Kit's look and grant write (PUT holders): filled = held,
 * outlined ★ = recommended for its role and not held. `All abilities ↗` opens Who can on this
 * agent's row.
 */
import { GatedButton } from "../../components/gated";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { DotsThree, FileText, Plus, Star } from "@phosphor-icons/react";

import { useIdentityVersion } from "../../lib/use-agent-identities";
import type { Ability } from "../../lib/kit-types";
import { kitApi, mcpApi, type McpServer } from "../../lib/kit-api";
import { grantMcp, grantSkill, revokeMcp, revokeSkill } from "../../lib/agents";
import { showToast } from "../client-forms";
import { SectionLabel } from "../section-label";
import { Pop } from "../agents/loadout-pop";
import { AbilityIcon } from "./ability-icons";
import { blocked } from "./agents-section";
import { shortError, TIP, agentName } from "./kit-copy";

export type HeldSkill = {
  skill_id: string; label: string; summary: string; source: string; behaviour_kind: string;
  risk_tier: number; enabled: boolean; missing?: boolean;
};
export type HeldServer = {
  mcp_server_id: string; server: string; tools: string[]; enabled: boolean;
  risk_tier?: number | null; description?: string | null;
};
export type AvailableServer = { mcp_server_id: string; server: string; enabled: boolean; risk_tier: number; description?: string | null };

export const LOADOUT_TIPS = {
  skills: "What it may run. Click a chip to give or take it.",
  mcp: "Servers whose tools its steps are offered",
  noSkills: "Steps naming it are refused until it holds one",
  noMcp: "Its steps are offered no MCP tools",
  tools: "Limit which of this server's tools it may use",
  readOnly: "Needs agent-editor rights",
  asks: "Asks a person",
  guide: "A guide: read when needed, it can't act",
};

/** Which row an ability belongs in: MCP when every tool it names is an MCP tool. */
export function loadoutRow(a: Pick<Ability, "tools" | "mcp" | "mcp_names">): "skills" | "mcp" {
  if (a.tools.length) return a.tools.every((t) => t.kind === "mcp") ? "mcp" : "skills";
  return a.mcp.length || (a.mcp_names?.length ?? 0) ? "mcp" : "skills";
}

/** Each held thing exactly once: abilities (held or recommended) by `loadoutRow`; held skills
 *  and servers no HELD ability covers, as plain chips in their row. */
export function partitionLoadout(abilities: Ability[], roleKey: string, skills: HeldSkill[], servers: HeldServer[]) {
  const shown = abilities.filter((a) => a.holders.includes(roleKey) || a.recommended.includes(roleKey));
  const held = shown.filter((a) => a.holders.includes(roleKey));
  const coveredSkills = new Set(held.flatMap((a) => a.tools.filter((t) => t.kind === "skill").map((t) => t.ref.replace(/^skill:/, ""))));
  const coveredServers = new Set(held.flatMap((a) => a.mcp.map((m) => m.server_id)));
  return {
    skills: { abilities: shown.filter((a) => loadoutRow(a) === "skills"), plain: skills.filter((s) => !coveredSkills.has(s.skill_id)) },
    mcp: { abilities: shown.filter((a) => loadoutRow(a) === "mcp"), plain: servers.filter((s) => !coveredServers.has(s.mcp_server_id)) },
  };
}

/** `+ Add › From Library`: the role's own skills (its spec's `allowed_skills`, and those of
 *  abilities recommended for it) first, then the rest. A skill its spec forbids is never
 *  offered, offering it and refusing it later is a control that lies about itself. */
export function offerSkills(available: HeldSkill[], allowed: string[], forbidden: string[],
                            abilities: Ability[], roleKey: string) {
  const forbiddenSet = new Set(forbidden);
  const allowedSet = new Set(allowed);
  const recommended = new Set(abilities.filter((a) => a.recommended.includes(roleKey))
    .flatMap((a) => a.tools.filter((t) => t.kind === "skill").map((t) => t.ref.replace(/^skill:/, ""))));
  const offer = available.filter((s) => !forbiddenSet.has(s.skill_id));
  const itsRole = offer.filter((s) => allowedSet.has(s.skill_id) || recommended.has(s.skill_id));
  return { itsRole, others: offer.filter((s) => !itsRole.includes(s)) };
}

/** A touch screen has no hover, so a chip's tap must not be its grant toggle: it opens the
 *  chip's menu, which says Give or Remove in words (independent test, D9). */
export function coarsePointer(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    && window.matchMedia("(pointer: coarse)").matches;
}
/** Open the ⋯ menu that sits beside a chip. */
function openMenuOf(chip: HTMLElement): void {
  chip.parentElement?.querySelector<HTMLButtonElement>(".ld-more")?.click();
}

const asks = (a: Ability) => a.tools.some((t) => t.policy === "ask" || t.tier >= 2);
const libSkill = (id: string) => `?area=skills&tab=library&lib=skills&skill=${encodeURIComponent(id)}`;
const libMcp = (id: string) => `?area=skills&tab=library&lib=mcp&mcp=${encodeURIComponent(id)}`;
const kitAbility = (key: string) => `?area=skills&ability=${encodeURIComponent(key)}`;

export function LoadoutGrants({ projectId, roleKey, actor, canEdit, skills, available, servers, availableServers,
                                allowed, forbidden, onChanged, onFromFile, initialAbilities }: {
  projectId: string; roleKey: string; actor: string; canEdit: boolean;
  skills: HeldSkill[]; available: HeldSkill[]; servers: HeldServer[]; availableServers: AvailableServer[];
  /** The role's own `allowed_skills` (offered first) and `forbidden_skills` (never offered). */
  allowed: string[]; forbidden: string[];
  onChanged: () => void;
  /** `+ Add › From file`: the SKILL.md sheet (passport-loadout). */
  onFromFile: () => void;
  /** The Kit as already read (a parent that has it, or a test); re-read on mount regardless. */
  initialAbilities?: Ability[];
}) {
  useIdentityVersion(); // names repaint when identities load
  const [abilities, setAbilities] = useState<Ability[] | null>(initialAbilities ?? null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await kitApi.load(projectId, "none");
    setFailed(!r.ok);
    setAbilities(r.ok ? r.data.abilities : []);
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);
  const name = agentName(roleKey);

  const toggle = async (a: Ability) => {
    if (!canEdit) return;
    const holds = a.holders.includes(roleKey);
    if (!holds && blocked(a)) return;
    setBusy(`a:${a.key}`);
    const r = await kitApi.holders(projectId, actor, a.key, holds ? a.holders.filter((x) => x !== roleKey) : [...a.holders, roleKey]);
    setBusy(null);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", holds ? `Took ${a.label} from ${name}` : `Gave ${a.label} to ${name}`);
    setAbilities((list) => (list ?? []).map((x) => (x.key === a.key ? r.data : x)));
    onChanged();
  };
  const write = async (id: string, label: string, give: boolean, run: () => Promise<{ ok: boolean; error?: string }>) => {
    setBusy(id);
    const r = await run();
    setBusy(null);
    if (!r.ok) { showToast("error", shortError((r as { error?: string }).error)); return; }
    showToast("success", give ? `Gave ${label} to ${name}` : `Took ${label} from ${name}`);
    void load();
    onChanged();
  };

  const parts = useMemo(() => partitionLoadout(abilities ?? [], roleKey, skills, servers), [abilities, roleKey, skills, servers]);
  const loading = abilities === null;

  const abilityChip = (a: Ability, row: "skills" | "mcp") => {
    const holds = a.holders.includes(roleKey);
    const off = !holds && blocked(a);
    const tip = !canEdit ? LOADOUT_TIPS.readOnly : off ? `Set up ${a.label} first` : a.tip;
    const single = row === "mcp" && holds && a.mcp.length === 1 ? servers.find((s) => s.mcp_server_id === a.mcp[0].server_id) : undefined;
    return (
      <span className="ld-chip" key={`a:${a.key}`} data-held={holds || undefined}>
        <GatedButton permission="agent_ability:assign" type="button" className="kit-chip kit-chip-btn" aria-pressed={holds}
                disabled={busy === `a:${a.key}` || off || !canEdit} data-tip={tip}
                data-testid={`agent-ability-${a.key}`}
                onClick={(e) => { if (coarsePointer()) openMenuOf(e.currentTarget); else void toggle(a); }}>
          <AbilityIcon name={a.icon} size={14} />
          {a.label}
          {holds && asks(a) ? <span className="ld-dot" aria-label={LOADOUT_TIPS.asks} data-tip={LOADOUT_TIPS.asks} /> : null}
          {!holds ? <Star size={10} weight="fill" aria-label="Recommended" /> : null}
        </GatedButton>
        {canEdit && !off ? (
          <ChipMenu testId={`agent-ability-${a.key}-more`} label={a.label}>
            {(close) => (<>
              {single ? <ToolsItem projectId={projectId} actor={actor} roleKey={roleKey} server={single} onDone={() => { close(); void load(); onChanged(); }} /> : null}
              <a href={kitAbility(a.key)} role="menuitem">Open in Library ↗</a>
              {holds
                ? <button type="button" role="menuitem" onClick={() => { close(); void toggle(a); }}>Remove</button>
                : <GatedButton permission="agent_ability:assign" type="button" role="menuitem" data-testid={`agent-ability-${a.key}-give`}
                          onClick={() => { close(); void toggle(a); }}>Give</GatedButton>}
            </>)}
          </ChipMenu>
        ) : null}
      </span>
    );
  };

  const skillChip = (s: HeldSkill) => {
    const guide = s.behaviour_kind === "guide";
    const off = !s.enabled || s.missing;
    const tip = off ? `Off, ${s.missing ? "not in this project" : "turned off"}` : (s.summary || s.label);
    return (
      <span className="ld-chip" key={`s:${s.skill_id}`} data-held>
        <span className="kit-chip ld-plain" data-off={off || undefined} data-tip={tip} data-testid={`agent-skill-${s.skill_id}`}
              role={canEdit ? "button" : undefined} tabIndex={canEdit ? 0 : undefined}
              onClick={canEdit ? (e) => openMenuOf(e.currentTarget) : undefined}
              onKeyDown={canEdit ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openMenuOf(e.currentTarget); } } : undefined}>
          {guide ? <FileText size={13} aria-label="Guide" /> : null}
          {s.label || s.skill_id}
          {s.risk_tier >= 2 ? <span className="ld-dot" aria-label={LOADOUT_TIPS.asks} data-tip={LOADOUT_TIPS.asks} /> : null}
        </span>
        {canEdit ? (
          <ChipMenu testId={`agent-skill-${s.skill_id}-more`} label={s.label || s.skill_id}>
            {(close) => (<>
              <a href={libSkill(s.skill_id)} role="menuitem">Open in Library ↗</a>
              <GatedButton permission="agent_ability:assign" type="button" role="menuitem" disabled={busy === `s:${s.skill_id}`}
                      onClick={() => { close(); void write(`s:${s.skill_id}`, s.label || s.skill_id, false,
                        () => revokeSkill(projectId, actor, roleKey, s.skill_id)); }}>Remove</GatedButton>
            </>)}
          </ChipMenu>
        ) : null}
      </span>
    );
  };

  const serverChip = (m: HeldServer) => {
    const label = `${m.server}, ${m.tools.length ? `${m.tools.length} tool${m.tools.length === 1 ? "" : "s"}` : "all tools"}`;
    return (
      <span className="ld-chip" key={`m:${m.mcp_server_id}`} data-held>
        <span className="kit-chip ld-plain" data-off={!m.enabled || undefined}
              role={canEdit ? "button" : undefined} tabIndex={canEdit ? 0 : undefined}
              onClick={canEdit ? (e) => openMenuOf(e.currentTarget) : undefined}
              onKeyDown={canEdit ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openMenuOf(e.currentTarget); } } : undefined}
              data-tip={!m.enabled ? "Off, turned off in Skills & MCP" : (m.tools.length ? m.tools.join(", ") : m.description || m.server)}
              data-testid={`agent-mcp-${m.mcp_server_id}`}>
          {label}
          {(m.risk_tier ?? 0) >= 2 ? <span className="ld-dot" aria-label={LOADOUT_TIPS.asks} data-tip={LOADOUT_TIPS.asks} /> : null}
        </span>
        {canEdit ? (
          <ChipMenu testId={`agent-mcp-${m.mcp_server_id}-more`} label={m.server}>
            {(close) => (<>
              <ToolsItem projectId={projectId} actor={actor} roleKey={roleKey} server={m} onDone={() => { close(); onChanged(); }} />
              <a href={libMcp(m.mcp_server_id)} role="menuitem">Open in Library ↗</a>
              <GatedButton permission="agent_ability:assign" type="button" role="menuitem" disabled={busy === `m:${m.mcp_server_id}`}
                      onClick={() => { close(); void write(`m:${m.mcp_server_id}`, m.server, false,
                        () => revokeMcp(projectId, actor, roleKey, m.mcp_server_id)); }}>Remove</GatedButton>
            </>)}
          </ChipMenu>
        ) : null}
      </span>
    );
  };

  const { itsRole, others } = offerSkills(available, allowed, forbidden, abilities ?? [], roleKey);

  const retry = failed ? (
    <button type="button" className="kit-link" data-testid="loadout-retry" onClick={() => void load()}>Can&apos;t reach BOSS API · Retry</button>
  ) : null;

  return (
    <>
      <div className="ld-row" data-testid="loadout-skills">
        <SectionLabel tip={LOADOUT_TIPS.skills} as="h4" testid="loadout-label-skills">Skills</SectionLabel>
        <div className="ld-chips">
          {loading ? <Skeleton /> : null}
          {retry}
          {parts.skills.abilities.map((a) => abilityChip(a, "skills"))}
          {parts.skills.plain.map(skillChip)}
          {!loading && !parts.skills.abilities.length && !parts.skills.plain.length
            ? <span className="kit-faint" data-tip={LOADOUT_TIPS.noSkills} data-testid="loadout-no-skills">No skills</span> : null}
          {canEdit ? (
            <Pop label="+ Add" testId="loadout-skills-add" trigger={<><Plus size={12} /> Add ▾</>}>
              {(close) => (<>
                <SkillPicker groups={[["Its role", itsRole], ["Others", others]]} busy={busy}
                             onPick={(s) => { close(); void write(`s:${s.skill_id}`, s.label || s.skill_id, true,
                               () => grantSkill(projectId, actor, roleKey, s.skill_id)); }} />
                <button type="button" role="menuitem" data-testid="loadout-skills-from-file"
                        onClick={() => { close(); onFromFile(); }}>From file</button>
              </>)}
            </Pop>
          ) : null}
        </div>
        <a className="kit-link ld-tail" href={`?area=skills&tab=who&role=${encodeURIComponent(roleKey)}`}
           data-tip={TIP.allAbilities} data-testid="agent-all-abilities">All abilities ↗</a>
      </div>
      <div className="ld-row" data-testid="loadout-mcp">
        <SectionLabel tip={LOADOUT_TIPS.mcp} as="h4" testid="loadout-label-mcp">MCP</SectionLabel>
        <div className="ld-chips">
          {loading ? <Skeleton /> : null}
          {parts.mcp.abilities.map((a) => abilityChip(a, "mcp"))}
          {parts.mcp.plain.map(serverChip)}
          {!loading && !parts.mcp.abilities.length && !parts.mcp.plain.length
            ? <span className="kit-faint" data-tip={LOADOUT_TIPS.noMcp} data-testid="loadout-no-mcp">No MCP</span> : null}
          {canEdit ? (
            <Pop label="+ Add" testId="loadout-mcp-add" trigger={<><Plus size={12} /> Add ▾</>}>
              {(close) => (<>
                <span className="ld-pop-head">From Library</span>
                {availableServers.length ? availableServers.map((m) => (
                  <GatedButton permission="agent_ability:assign" type="button" role="menuitem" key={m.mcp_server_id} data-testid={`loadout-mcp-pick-${m.mcp_server_id}`}
                          data-tip={m.description || undefined} disabled={busy === `m:${m.mcp_server_id}`}
                          onClick={() => { close(); void write(`m:${m.mcp_server_id}`, m.server, true,
                            () => grantMcp(projectId, actor, roleKey, m.mcp_server_id)); }}>
                    {m.server}{m.enabled ? "" : ", off"}
                  </GatedButton>
                )) : <span className="kit-faint ld-pop-empty">None left</span>}
                <a href="?area=skills&tab=library&lib=mcp&add=1" role="menuitem" data-testid="loadout-mcp-new">Add new ↗</a>
              </>)}
            </Pop>
          ) : null}
        </div>
      </div>
    </>
  );
}

function Skeleton() {
  return <>{[0, 1, 2].map((i) => <span key={i} className="kit-skeleton ld-skel" aria-hidden="true" />)}</>;
}

function ChipMenu({ testId, label, children }: { testId: string; label: string; children: (close: () => void) => React.ReactNode }) {
  return (
    <Pop label={`More for ${label}`} testId={testId} className="ld-more" align="right" trigger={<DotsThree size={14} weight="bold" />}>
      {children}
    </Pop>
  );
}

/** `From Library`: searchable, the role's own skills first; forbidden ones are never listed. */
function SkillPicker({ groups, busy, onPick }: {
  groups: Array<[string, HeldSkill[]]>; busy: string | null; onPick: (s: HeldSkill) => void;
}) {
  const [q, setQ] = useState("");
  const match = (s: HeldSkill) => !q.trim() || `${s.label} ${s.skill_id} ${s.summary}`.toLowerCase().includes(q.trim().toLowerCase());
  const any = groups.some(([, list]) => list.some(match));
  return (
    <span className="ld-picker" data-testid="loadout-skill-picker">
      <span className="ld-pop-head">From Library</span>
      <input className="ld-search" placeholder="Search skills" aria-label="Search skills" value={q}
             data-autofocus onChange={(e) => setQ(e.target.value)} data-testid="loadout-skill-search" />
      <span className="ld-picker-list">
        {groups.map(([title, list]) => {
          const shown = list.filter(match);
          return shown.length ? (
            <span key={title} className="ld-picker-group">
              <span className="ld-picker-title">{title}</span>
              {shown.map((s) => (
                <GatedButton permission="agent_ability:assign" type="button" role="menuitem" key={s.skill_id} disabled={busy === `s:${s.skill_id}`}
                        data-tip={s.summary || undefined} data-testid={`loadout-skill-pick-${s.skill_id}`}
                        onClick={() => onPick(s)}>
                  {s.behaviour_kind === "guide" ? <FileText size={12} /> : null} {s.label || s.skill_id}
                </GatedButton>
              ))}
            </span>
          ) : null;
        })}
        {!any ? <span className="kit-faint ld-pop-empty">No match</span> : null}
      </span>
    </span>
  );
}

/** `Tools…`: the server's tools as checkboxes; saving writes the per-agent allowlist. */
function ToolsItem({ projectId, actor, roleKey, server, onDone }: {
  projectId: string; actor: string; roleKey: string; server: HeldServer; onDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState<McpServer["tools"] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!open) return;
    setFailed(false);
    setAll(null);
    void mcpApi.list(projectId).then((r) => {
      // A failed read says so and offers Retry; it never sits on "Reading…" (D5).
      if (!r.ok) { setFailed(true); return; }
      const found = r.data.items.find((s) => s.id === server.mcp_server_id);
      const tools = found?.tools ?? [];
      setAll(tools);
      // An empty allowlist means every tool: shown as every box ticked.
      setPicked(new Set(server.tools.length ? server.tools : tools.map((t) => t.name)));
    });
  }, [open, projectId, server.mcp_server_id, server.tools, attempt]);
  if (!open) {
    return <button type="button" role="menuitem" data-tip={LOADOUT_TIPS.tools} data-testid={`agent-mcp-tools-${server.mcp_server_id}`}
                   onClick={() => setOpen(true)}>Tools…</button>;
  }
  const save = async () => {
    setSaving(true);
    const everything = !!all && picked.size === all.length;
    const r = await grantMcp(projectId, actor, roleKey, server.mcp_server_id, everything ? [] : Array.from(picked));
    setSaving(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Saved ${server.server} tools`);
    onDone();
  };
  return (
    <span className="ld-tools" data-testid={`agent-mcp-tools-list-${server.mcp_server_id}`}>
      {failed ? (
        <button type="button" role="menuitem" className="linklike ld-retry" data-testid={`agent-mcp-tools-retry-${server.mcp_server_id}`}
                onClick={() => setAttempt((n) => n + 1)}>Can&apos;t reach BOSS API · Retry</button>
      ) : all === null ? <span className="kit-faint">Reading…</span> : all.length === 0 ? <span className="kit-faint">No tools listed</span> : all.map((t) => (
        <label key={t.name} className="ld-tool" data-tip={t.description || undefined}>
          <input type="checkbox" checked={picked.has(t.name)}
                 onChange={(e) => setPicked((prev) => {
                   const next = new Set(prev);
                   if (e.target.checked) next.add(t.name); else next.delete(t.name);
                   return next;
                 })} />
          {t.name}
        </label>
      ))}
      <span className="kit-pop-actions">
        <GatedButton permission="agent_ability:assign" type="button" className="kit-btn kit-btn-sm kit-btn-primary" disabled={saving || !all?.length || picked.size === 0}
                onClick={() => void save()}>Save</GatedButton>
      </span>
    </span>
  );
}
