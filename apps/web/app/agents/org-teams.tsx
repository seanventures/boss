"use client";

/**
 * Organization › Teams (org-view spec §4.2): a scorecard per team, in the same order and hues as
 * Live's Agent force, so the two stay one spatial memory.
 *
 * EVERY ROW IS A RECORDED FACT and hides when it has nothing true to say: who is busy now (the 4s
 * presence store), what finished or failed in the window, the oldest open item, busy time, who
 * the team hands work to, metered spend and the models it called, and what it is missing (the
 * Kit; roles a workflow names that nobody fills — an empty seat drawn as a dashed `Missing`).
 *
 * Kept from Zones (§5 parity): faces in their real state, live first; the badges; Click selects
 * (the Agent panel), Shift+click / Shift+Enter opens the Passport; one roving 2-D tab stop
 * across every card (nextInGrid); search dims to 30%; the hidden `Team · Agent · State · Now`
 * table; and under 640px the drill-down list with the same testids.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { CaretDown, DotsThree, Star } from "@phosphor-icons/react";

import { STATE_WORD, identityOf, isLive, openAgent } from "../../lib/agent-world";
import type { LiveMap } from "../../lib/agents-live";
import {
  ageWords, busyWords, openStatusWord, usdWords, windowWords,
  type OrgBoard, type OrgDays, type OrgMissingRole, type OrgTeam,
} from "../../lib/agents-org-board";
import { openRoom } from "../../lib/converse-doors";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import type { OrgActions } from "./org-actions";
import { ORG_GIVE_PERMISSION } from "./org-permissions";
import { GatedButton } from "../../components/gated";
import {
  MenuPop, WaitItem, consoleHref, kitHref, liveFirst, liveState, nameOf, teamLabel, teamTip, workflowHref,
} from "./org-shared";
import { Badges, Face, matches, nextInGrid, teamHue, type ZoneMember } from "./team-zones";

const FACES_MAX = 8;
const LOAD_MAX = 4;
const CHIPS_MAX = 3;

export type OrgTeamsProps = {
  projectId: string;
  board: OrgBoard;
  live: LiveMap;
  search: string;
  days: OrgDays;
  narrow: boolean;
  focusTeam?: string | null;
  selected: string | null;
  onSelect: (roleKey: string | null) => void;
  /** `Hands to` / `Gets from`: Flow, isolated on that pair. */
  onWorksWith: (team: string, from: string, to: string) => void;
  /** `Missing` → `Make agent`: the New agent dialog, named and placed. */
  onMakeAgent: (roleKey: string, team: string) => void;
  /** Yours › `Register <name>`: today's Directory register flow. */
  onRegister: (name: string) => void;
  actions: OrgActions | null;
};

function member(roleKey: string, live: LiveMap): ZoneMember {
  const state = liveState(live, roleKey);
  return { roleKey, name: nameOf(roleKey), state, line: live.get(roleKey)?.line ?? "",
           live: isLive({ state, line: "" }) };
}

function working(team: OrgTeam, live: LiveMap): number {
  return team.members.filter((m) => ["working", "thinking", "speaking"].includes(liveState(live, m))).length;
}

/** The teams drawn: an empty Yours is never drawn — unless an empty seat (a missing role)
 *  belongs there, which is something true to say. */
export function shownTeams(board: OrgBoard): OrgTeam[] {
  return board.teams.filter((t) => !(t.key === "yours" && t.members.length === 0
    && !board.missingRoles.some((m) => m.team === "yours")));
}

function stepGapPhrases(team: OrgTeam): { label: string; n: number; tip: string }[] {
  const by = new Map<string, { label: string; n: number; tip: string }>();
  for (const g of team.gaps.stepGaps) {
    // One sentence (a tooltip stays under 14 words): the first step; the count says the rest.
    const x = by.get(g.ability) ?? { label: g.label, n: 0,
                                     tip: `${g.workflowName} › ${g.stepTitle} needs ${g.label}. ${nameOf(g.roleKey)} lacks it.` };
    x.n += 1;
    by.set(g.ability, x);
  }
  return [...by.values()];
}

type Givable = { ability: string; label: string; roleKey: string; needsSetup: boolean };

/** Everything `Give ▾` lists on this team: recommended-not-held, then a step's need. One the
 *  Kit would refuse until it is set up says so and links there (walk D6) — never a Give that
 *  fails. */
export function givable(team: OrgTeam): Givable[] {
  const out = new Map<string, Givable>();
  for (const a of team.gaps.abilities) {
    for (const r of a.roles) out.set(`${a.ability}:${r}`, { ability: a.ability, label: a.label, roleKey: r, needsSetup: a.needsSetup });
  }
  for (const g of team.gaps.stepGaps) {
    if (team.members.includes(g.roleKey)) {
      out.set(`${g.ability}:${g.roleKey}`, { ability: g.ability, label: g.label, roleKey: g.roleKey, needsSetup: g.needsSetup });
    }
  }
  return [...out.values()];
}

/** What `Give recommended` will actually give: the team's recommended gaps that need no setup. */
export function recommendedGive(team: OrgTeam): { abilities: number; roles: string[] } {
  const ready = team.gaps.abilities.filter((a) => !a.needsSetup);
  return { abilities: ready.length, roles: [...new Set(ready.flatMap((a) => a.roles))] };
}

export function OrgTeams(props: OrgTeamsProps): React.ReactElement {
  useIdentityVersion(); // names and faces are resolved at render
  const teams = shownTeams(props.board);
  if (props.narrow) return <TeamList {...props} teams={teams} />;
  return <TeamGrid {...props} teams={teams} />;
}

// ── The grid ────────────────────────────────────────────────────────────────────────────────

function TeamGrid(props: OrgTeamsProps & { teams: OrgTeam[] }) {
  const { teams, live, selected, onSelect, focusTeam } = props;
  const host = useRef<HTMLDivElement>(null);
  const faces = useRef(new Map<string, HTMLElement>());
  const order = useMemo(() => teams.flatMap((t) => liveFirst(t.members, live).slice(0, FACES_MAX)), [teams, live]);
  const [active, setActive] = useState(0);

  useEffect(() => {
    if (!focusTeam) return;
    host.current?.querySelector(`[data-team="${CSS.escape?.(focusTeam) ?? focusTeam}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [focusTeam]);

  // One roving tab stop across every card: arrows move in 2-D, Enter selects, Shift+Enter opens.
  const onKeyDown = (e: React.KeyboardEvent) => {
    const at = order.indexOf((e.target as HTMLElement).dataset?.role ?? "");
    if (at < 0) return;
    if (["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) {
      const rects = order.map((k) => {
        const r = faces.current.get(k)?.getBoundingClientRect();
        return r ? { left: r.left, top: r.top, width: r.width || 1, height: r.height || 1 } : { left: 0, top: 0, width: 1, height: 1 };
      });
      const next = nextInGrid(rects, at, e.key);
      e.preventDefault();
      setActive(next);
      faces.current.get(order[next])?.focus();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (e.shiftKey) openAgent(order[at]);
      else onSelect(selected === order[at] ? null : order[at]);
    }
  };

  return (
    <div className="ag-zones ag-orgteams" data-testid="ag-zones" data-layout="cards" ref={host}
         onKeyDown={onKeyDown} role="group" aria-label="Teams">
      <span className="ag-sr" data-testid="ag-orgteams">Teams</span>
      {teams.map((t) => (
        <TeamCard key={t.key} {...props} team={t} order={order} active={active} setActive={setActive}
                  faces={faces.current} missing={props.board.missingRoles.filter((m) => m.team === t.key)} />
      ))}
      <SrTable teams={teams} live={live} board={props.board} />
    </div>
  );
}

function SrTable({ teams, live, board }: { teams: OrgTeam[]; live: LiveMap; board: OrgBoard }) {
  return (
    <table className="ag-sr" tabIndex={0} aria-label="Teams" data-testid="ag-zones-table">
      <thead><tr><th>Team</th><th>Agent</th><th>State</th><th>Now</th></tr></thead>
      <tbody>
        {teams.flatMap((t) => liveFirst(t.members, live).map((m) => (
          <tr key={`${t.key}:${m}`}>
            <td>{teamLabel(t.key, board)}</td><td data-user-text>{nameOf(m)}</td>
            <td>{STATE_WORD[liveState(live, m)]}</td><td data-user-text>{live.get(m)?.line ?? ""}</td>
          </tr>
        )))}
      </tbody>
    </table>
  );
}

type CardProps = OrgTeamsProps & {
  team: OrgTeam; order: string[]; active: number; setActive: (i: number) => void;
  faces: Map<string, HTMLElement>; missing: OrgMissingRole[];
};

function TeamCard(p: CardProps) {
  const { team, board, live, search, days, selected, onSelect, order, active, setActive, faces, missing } = p;
  const label = teamLabel(team.key, board);
  const people = liveFirst(team.members, live);
  const shown = people.slice(0, FACES_MAX);
  const more = people.length - shown.length;
  const anyLive = people.some((r) => isLive(live.get(r)));
  const w = team.work;
  const anyWork = w.done + w.failed + w.open > 0 || team.waitingTotal > 0;
  const anyBusy = team.membersLoad.some((m) => m.busySeconds > 0);
  const anySpend = board.totals.spendOk && (team.spend.meteredUsd > 0 || team.spend.planCalls > 0);
  const hasGaps = !team.gaps.kitOk || team.gaps.abilities.length > 0 || team.gaps.stepGaps.length > 0
    || team.gaps.missingRoles > 0;
  const hands = board.teamEdges.filter((e) => e.from === team.key && e.to !== team.key).slice(0, 2);
  const gets = board.teamEdges.filter((e) => e.to === team.key && e.from !== team.key).slice(0, 1);
  const noWork = !anyWork && !anyBusy && !anySpend && !hands.length && !gets.length;
  const collapsed = noWork && !hasGaps && !anyLive;
  const titleId = `ag-orgteam-title-${team.key}`;
  return (
    <section className="ag-orgteam" data-testid={`ag-orgteam-${team.key}`} data-team={team.key}
             data-focus={p.focusTeam === team.key || undefined} data-collapsed={collapsed || undefined}
             aria-labelledby={titleId} style={{ ["--team-hue" as string]: teamHue(team.key) }}>
      <header className="ag-orgteam-head">
        <span className="ag-team-label" id={titleId} title={teamTip(team.key, board)}>{label}</span>
        {team.members.length ? <span className="ag-team-count">{team.members.length}</span> : null}
        <Badges team={{ working: working(team, live), needsYou: team.needsYou }} />
        <span className="ag-spacer" />
        <TeamMenu {...p} />
      </header>
      <div className="ag-orgteam-faces">
        {shown.map((roleKey) => {
          const m = member(roleKey, live);
          const idx = order.indexOf(roleKey);
          const unregistered = !board.agents.find((a) => a.roleKey === roleKey)?.registered;
          return (
            <button key={roleKey} type="button" className="ag-orgteam-agent ag-zones-agent"
                    ref={(el) => { if (el) faces.set(roleKey, el); else faces.delete(roleKey); }}
                    data-role={roleKey} data-testid={`ag-zones-agent-${roleKey}`}
                    data-dim={matches(search, m) ? undefined : "search"}
                    data-selected={selected === roleKey || undefined}
                    tabIndex={idx === active ? 0 : -1} aria-pressed={selected === roleKey}
                    aria-label={`${m.name}, ${STATE_WORD[m.state]}${m.line ? `: ${m.line}` : ""}`}
                    title={`${m.name}, ${STATE_WORD[m.state]}${m.line ? `: ${m.line}` : ""}`}
                    onFocus={() => setActive(idx)}
                    onClick={(e) => {
                      if (e.shiftKey) { openAgent(roleKey); return; }
                      onSelect(selected === roleKey ? null : roleKey);
                    }}>
              <Face m={m} size={32} still={!m.live} />
              <span className="ag-orgteam-name" data-user-text>{m.name}</span>
              {unregistered ? <span className="ag-orgteam-tag">Not registered</span> : null}
            </button>
          );
        })}
        {more > 0 ? <span className="ag-team-more" title={people.slice(FACES_MAX).map(nameOf).join(", ")}>+{more}</span> : null}
        {missing.map((m) => <MissingFace key={m.roleKey} role={m} team={team.key} onMakeAgent={p.onMakeAgent} />)}
      </div>
      {collapsed ? null : (
        <dl className="ag-orgteam-rows">
          {noWork ? (
            <div className="ag-orgteam-row" data-row="none">
              <dt className="ag-sr">Work</dt>
              <dd className="ag-muted" title="Nobody on this team was handed work.">No work in {windowWords(days)}</dd>
            </div>
          ) : null}
          {w.done || w.failed || w.open ? (
            <div className="ag-orgteam-row" data-row="work">
              <dt className="ag-sr">Work</dt>
              <dd>
                {w.done ? <span title={`Finished in the last ${windowWords(days)}`}>Done {w.done}</span> : null}
                {w.failed ? <span className="ag-bad" title="Ended in an error">Failed {w.failed}</span> : null}
                {w.open ? <span title="Started, not finished">Open {w.open}</span> : null}
              </dd>
            </div>
          ) : null}
          <WaitingRow team={team} />
          {anyBusy ? <LoadRow team={team} days={days} /> : null}
          {hands.length ? (
            <div className="ag-orgteam-row" data-row="with">
              <dt>Hands to</dt>
              <dd>
                {hands.map((e) => (
                  <button key={e.to} type="button" className="ag-link" title="Handoffs in this window. Press to see them."
                          data-testid={`ag-orgteam-hands-${team.key}-${e.to}`}
                          onClick={() => p.onWorksWith(team.key, e.from, e.to)}>
                    {teamLabel(e.to, board)} ×{e.handoffs}
                  </button>
                ))}
              </dd>
            </div>
          ) : null}
          {gets.length ? (
            <div className="ag-orgteam-row" data-row="gets">
              <dt>Gets from</dt>
              <dd>
                {gets.map((e) => (
                  <button key={e.from} type="button" className="ag-link" title="Handoffs in this window. Press to see them."
                          data-testid={`ag-orgteam-gets-${team.key}-${e.from}`}
                          onClick={() => p.onWorksWith(team.key, e.from, e.to)}>
                    {teamLabel(e.from, board)} ×{e.handoffs}
                  </button>
                ))}
              </dd>
            </div>
          ) : null}
          {anySpend ? (
            <div className="ag-orgteam-row" data-row="spend">
              <dt>Spend</dt>
              <dd>
                {team.spend.meteredUsd > 0 ? <span title="Model spend billed per call in this window">{usdWords(team.spend.meteredUsd)} metered</span> : null}
                {team.spend.planCalls > 0 ? <span title="Calls covered by a subscription. Not counted as money.">{team.spend.planCalls} on plan</span> : null}
              </dd>
            </div>
          ) : null}
          {board.totals.spendOk && team.models.length ? (
            <div className="ag-orgteam-row" data-row="models">
              <dt>Models</dt>
              <dd title={`Models this team called.${team.configuredModels.length ? ` Configured: ${team.configuredModels.join(", ")}.` : ""}`}>
                {team.models.slice(0, 2).map((m) => <span key={m.model} title={`${m.model} ${m.calls}`} data-user-text>{m.model} {m.calls}</span>)}
                {team.models.length > 2 ? <span className="ag-team-more">+{team.models.length - 2}</span> : null}
              </dd>
            </div>
          ) : null}
          {hasGaps ? <GapsRow team={team} actions={p.actions} /> : null}
        </dl>
      )}
    </section>
  );
}

function WaitingRow({ team }: { team: OrgTeam }) {
  const [open, setOpen] = useState(false);
  if (!team.waiting.length) return null;
  const rows = open ? team.waiting : team.waiting.slice(0, 1);
  const extra = team.waitingTotal - 1;
  return (
    <div className="ag-orgteam-row" data-row="waiting">
      <dt>Waiting</dt>
      <dd className="ag-orgteam-waits">
        {rows.map((w) => (
          <WaitItem key={w.actId} item={w} testid={`ag-orgteam-wait-${w.actId}`} />
        ))}
        {extra > 0 && !open ? (
          <button type="button" className="ag-link" data-testid={`ag-orgteam-more-waiting-${team.key}`}
                  onClick={() => setOpen(true)}>+{extra} more waiting</button>
        ) : null}
      </dd>
    </div>
  );
}

function LoadRow({ team, days }: { team: OrgTeam; days: OrgDays }) {
  const rows = team.membersLoad.slice(0, LOAD_MAX);
  const max = Math.max(1, ...rows.map((r) => r.busySeconds));
  const more = team.membersLoad.length - rows.length;
  return (
    <div className="ag-orgteam-row" data-row="load">
      <dt title="Time with work open in this window">Busy</dt>
      <dd className="ag-orgteam-load">
        {rows.map((r) => (
          <span key={r.roleKey} className="ag-orgteam-bar-row" title={`${nameOf(r.roleKey)} busy ${busyWords(r.busySeconds)} of ${windowWords(days)}`}>
            <span data-user-text>{nameOf(r.roleKey)}</span>
            <span className="ag-orgteam-bar" aria-hidden><span style={{ width: `${Math.max(r.busySeconds ? 4 : 1, (r.busySeconds / max) * 100)}%` }} /></span>
            <span className="ag-orgteam-num">{busyWords(r.busySeconds)}</span>
          </span>
        ))}
        {more > 0 ? <span className="ag-team-more">+{more}</span> : null}
      </dd>
    </div>
  );
}

function GapsRow({ team, actions }: { team: OrgTeam; actions: OrgActions | null }) {
  const [open, setOpen] = useState(false);
  if (!team.gaps.kitOk) {
    return (
      <div className="ag-orgteam-row" data-row="gaps">
        <dt>Gaps</dt>
        <dd className="ag-muted" title={`Couldn't read the Kit: ${team.gaps.kitReason ?? "unknown"}`}>Gaps unknown</dd>
      </div>
    );
  }
  const chips = team.gaps.abilities;
  const phrases = stepGapPhrases(team);
  const gives = givable(team);
  return (
    <div className="ag-orgteam-row" data-row="gaps">
      <dt>Gaps</dt>
      <dd className="ag-orgteam-gaps">
        {chips.slice(0, CHIPS_MAX).map((a) => (
          <span key={a.ability} className="ag-orgteam-chip" data-testid={`ag-orgteam-gap-${team.key}-${a.ability}`}
                title={`Recommended for ${a.roles.map(nameOf).join(", ")}. Not held.`}>
            <Star size={10} weight="bold" aria-hidden /> {a.label}
          </span>
        ))}
        {chips.length > CHIPS_MAX ? <span className="ag-team-more">+{chips.length - CHIPS_MAX}</span> : null}
        {team.gaps.missingRoles ? (
          <span title="Roles a workflow names that nobody fills">
            {team.gaps.missingRoles} missing {team.gaps.missingRoles === 1 ? "role" : "roles"}
          </span>
        ) : null}
        {phrases.map((x) => (
          <span key={x.label} title={x.tip}>{x.n} {x.n === 1 ? "step" : "steps"} can&apos;t use <span data-user-text>{x.label}</span></span>
        ))}
        {gives.length && actions ? (
          <span className="org-menu-wrap">
            <button type="button" className="ag-link" aria-haspopup="menu" aria-expanded={open}
                    data-testid={`ag-orgteam-give-${team.key}`} onClick={() => setOpen((v) => !v)}>
              Give <CaretDown size={10} weight="bold" aria-hidden />
            </button>
            {open ? (
              <MenuPop label="Give" onClose={() => setOpen(false)}>
                {gives.map((g) => (g.needsSetup ? (
                  <a key={`${g.ability}:${g.roleKey}`} role="menuitem" href={kitHref(g.ability)}
                     data-testid={`ag-orgteam-setup-${g.ability}-${g.roleKey}`}
                     title={`${g.label} must be set up in the Kit before anyone can hold it`}>
                    <span data-user-text>{g.label}</span> · Set up first ↗
                  </a>
                ) : (
                  <GatedButton key={`${g.ability}:${g.roleKey}`} permission={ORG_GIVE_PERMISSION} type="button" role="menuitem"
                          data-testid={`ag-orgteam-give-${g.ability}-${g.roleKey}`}
                          title={`Give ${g.label} to ${nameOf(g.roleKey)}`}
                          onClick={() => { setOpen(false); void actions.give(g.ability, g.label, g.roleKey); }}>
                    <span data-user-text>{g.label}</span> → <span data-user-text>{nameOf(g.roleKey)}</span>
                  </GatedButton>
                )))}
              </MenuPop>
            ) : null}
          </span>
        ) : null}
      </dd>
    </div>
  );
}

function MissingFace({ role, team, onMakeAgent }: { role: OrgMissingRole; team: string;
                                                    onMakeAgent: (roleKey: string, team: string) => void }) {
  const [open, setOpen] = useState(false);
  const first = role.steps[0];
  return (
    <span className="org-menu-wrap">
      <button type="button" className="ag-orgteam-missing" data-testid={`ag-orgteam-missing-${role.roleKey}`}
              aria-haspopup="menu" aria-expanded={open}
              title={`${role.roleKey} is named by ${role.steps.length} ${role.steps.length === 1 ? "step" : "steps"}. Nobody fills it.`}
              onClick={() => setOpen((v) => !v)}>
        <span className="ag-orgteam-missing-face" aria-hidden />
        <span className="ag-orgteam-name">Missing</span>
      </button>
      {open ? (
        <MenuPop label="Missing role" onClose={() => setOpen(false)}>
          <button type="button" role="menuitem" data-testid={`ag-orgteam-make-${role.roleKey}`}
                  onClick={() => { setOpen(false); onMakeAgent(role.roleKey, team); }}>Make agent</button>
          {first ? (
            <a role="menuitem" href={workflowHref(first.workflowKey, first.stepId)}
               title={`${first.workflowName} › ${first.stepTitle}`}>Open workflow ↗</a>
          ) : null}
        </MenuPop>
      ) : null}
    </span>
  );
}

function TeamMenu(p: CardProps) {
  const { team, board, live, actions } = p;
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const busy = new Map(team.membersLoad.map((m) => [m.roleKey, m.busySeconds]));
  const seats = [...team.members].sort((a, b) => {
    const la = isLive(live.get(a)) ? 0 : 1;
    const lb = isLive(live.get(b)) ? 0 : 1;
    return la - lb || (busy.get(b) ?? 0) - (busy.get(a) ?? 0);
  }).slice(0, 5);
  const rec = recommendedGive(team);
  const unregistered = team.members.filter((m) => !board.agents.find((a) => a.roleKey === m)?.registered);
  return (
    <span className="org-menu-wrap">
      <button type="button" className="ag-icon-btn" aria-label={`More for ${teamLabel(team.key, board)}`}
              aria-haspopup="menu" aria-expanded={open} data-testid={`ag-orgteam-more-${team.key}`}
              title="More" onClick={() => { setOpen((v) => !v); setConfirm(false); }}>
        <DotsThree size={16} weight="bold" aria-hidden />
      </button>
      {open ? (
        <MenuPop label={`${teamLabel(team.key, board)} menu`} onClose={() => setOpen(false)}>
          {confirm ? (
            <>
              <span className="org-menu-head">
                Give {rec.abilities} {rec.abilities === 1 ? "ability" : "abilities"} to {rec.roles.length} {rec.roles.length === 1 ? "agent" : "agents"}
              </span>
              <GatedButton permission={ORG_GIVE_PERMISSION} type="button" role="menuitem" data-testid={`ag-orgteam-give-recommended-yes-${team.key}`}
                      onClick={() => { setOpen(false); setConfirm(false); void actions?.giveRecommended(rec.roles); }}>Give</GatedButton>
              <button type="button" role="menuitem" onClick={() => setConfirm(false)}>Cancel</button>
            </>
          ) : (
            <>
              {team.members.length >= 2 ? (
                <button type="button" role="menuitem" data-testid={`ag-orgteam-room-${team.key}`}
                        title={seats.map(nameOf).join(", ")}
                        onClick={() => { setOpen(false); openRoom({ seats }); }}>Open room</button>
              ) : null}
              {rec.abilities && actions ? (
                <GatedButton permission={ORG_GIVE_PERMISSION} type="button" role="menuitem" data-testid={`ag-orgteam-give-recommended-${team.key}`}
                        onClick={() => setConfirm(true)}>Give recommended</GatedButton>
              ) : null}
              <a role="menuitem" href={consoleHref({ area: "workforce", view: "activity", layout: null, days: null,
                                                     isolate: null, run: null, focus: null, role: null })}>Activity ↗</a>
              <a role="menuitem" href={consoleHref({ area: "workforce", view: "directory", layout: null, days: null,
                                                     isolate: null, run: null, focus: null, role: null })}
                 title={`${teamLabel(team.key, board)} in the Directory`}>Directory ↗</a>
              {unregistered.map((r) => (
                <button key={r} type="button" role="menuitem" data-testid={`ag-orgteam-register-${r}`}
                        onClick={() => { setOpen(false); p.onRegister(identityOf(r).name || nameOf(r)); }}>
                  Register <span data-user-text>{nameOf(r)}</span>
                </button>
              ))}
            </>
          )}
        </MenuPop>
      ) : null}
    </span>
  );
}

// ── Under 640px: the drill-down list ────────────────────────────────────────────────────────

function TeamList(props: OrgTeamsProps & { teams: OrgTeam[] }) {
  const { teams, live, board, search, onSelect, focusTeam } = props;
  const [open, setOpen] = useState<string | null>(focusTeam ?? null);
  return (
    <div className="ag-zones ag-zones-list ag-orgteams" data-testid="ag-zones" data-layout="list">
      <span className="ag-sr" data-testid="ag-orgteams">Teams</span>
      {teams.map((t) => {
        const label = teamLabel(t.key, board);
        const oldest = t.waiting[0];
        const gaps = t.gaps.abilities.length + t.gaps.stepGaps.length + t.gaps.missingRoles;
        const stats = [
          t.work.done ? `Done ${t.work.done}` : "",
          t.work.failed ? `Failed ${t.work.failed}` : "",
          oldest ? `Waiting ${ageWords(oldest.since)}` : "",
          gaps ? `${gaps} ${gaps === 1 ? "gap" : "gaps"}` : "",
        ].filter(Boolean);
        return (
          <div key={t.key} className="ag-zones-lrow" data-team={t.key} style={{ ["--team-hue" as string]: teamHue(t.key) }}>
            <button type="button" className="ag-zones-lhead" aria-expanded={open === t.key}
                    data-testid={`ag-zones-team-${t.key}`} onClick={() => setOpen(open === t.key ? null : t.key)}>
              <span className="ag-team-label" title={teamTip(t.key, board)}>{label}</span>
              {t.members.length ? <span className="ag-team-count">{t.members.length}</span> : null}
              <Badges team={{ working: working(t, live), needsYou: t.needsYou }} />
              {stats.length ? (
                <span className="ag-zones-lstats">
                  {stats.map((x, i) => <span key={x}>{i ? " · " : ""}{x}</span>)}
                </span>
              ) : null}
            </button>
            {open === t.key ? (
              <div className="ag-zones-lbody">
                <ul className="ag-zones-lmembers">
                  {liveFirst(t.members, live).map((r) => member(r, live)).filter((m) => matches(search, m)).map((m) => (
                    <li key={m.roleKey}>
                      <button type="button" className="ag-zones-lmember" onClick={() => onSelect(m.roleKey)}
                              data-testid={`ag-zones-member-${m.roleKey}`}>
                        <Face m={m} size={24} still />
                        <span data-user-text>{m.name}</span>
                        <span className="ag-zones-lstate"> · {STATE_WORD[m.state]}</span>
                      </button>
                    </li>
                  ))}
                </ul>
                {t.waiting.length ? (
                  <ul className="ag-zones-lwaits">
                    {t.waiting.map((w) => (
                      <li key={w.actId}><WaitItem item={w} /></li>
                    ))}
                  </ul>
                ) : null}
                {t.gaps.abilities.length ? (
                  <div className="ag-zones-lgaps">
                    {t.gaps.abilities.map((a) => (
                      <span key={a.ability} className="ag-orgteam-chip" title={`Recommended for ${a.roles.map(nameOf).join(", ")}. Not held.`}>
                        <Star size={10} weight="bold" aria-hidden /> {a.label}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
