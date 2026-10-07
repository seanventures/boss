"use client";

/**
 * The Agent focus panel (org-view spec §4.4): one agent, from Teams or Flow. A right drawer
 * (a bottom sheet under 720px) with what it is doing, what waits with it, who it works with,
 * its window's numbers, the steps that name it, what it is missing — and the actions, each where
 * its fact is: Talk, Passport, Give, Change in workflow, Move to….
 *
 * Rebalancing is never automated: next to the busiest agent's steps it says so, and the action
 * is the workflow editor on that step.
 */
import React, { useState } from "react";
import { CaretDown, ChatCircleDots, Star } from "@phosphor-icons/react";

import { STATE_WORD, askAgent, isLive, openAgent } from "../../lib/agent-world";
import type { LiveMap } from "../../lib/agents-live";
import { TEAM_LABELS, TEAM_ORDER } from "../../lib/agents-live-board";
import {
  ageWords, busyWords, openStatusWord, usdWords, windowWords, type OrgBoard, type OrgDays,
} from "../../lib/agents-org-board";
import { AgFace } from "./agent-face";
import type { OrgActions } from "./org-actions";
import { ORG_GIVE_PERMISSION, ORG_MOVE_PERMISSION } from "./org-permissions";
import { GatedButton } from "../../components/gated";
import {
  Drawer, MenuPop, WaitItem, kitHref, liveState, nameOf, teamLabel, workflowHref,
} from "./org-shared";
import { useIdentityVersion } from "../../lib/use-agent-identities";

const ROWS_MAX = 5;

export function OrgAgentPanel({ board, live, roleKey, days, actions, onClose }: {
  board: OrgBoard; live: LiveMap; roleKey: string; days: OrgDays; actions: OrgActions | null;
  onClose: () => void;
}) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const [moveOpen, setMoveOpen] = useState(false);
  const agent = board.agents.find((a) => a.roleKey === roleKey);
  const team = board.teams.find((t) => t.members.includes(roleKey));
  const name = nameOf(roleKey);
  const state = liveState(live, roleKey);
  const presence = live.get(roleKey);
  const now = isLive(presence) && presence?.line ? presence.line : "";
  const gets = board.agentEdges.filter((e) => e.to === roleKey);
  const hands = board.agentEdges.filter((e) => e.from === roleKey);
  const busiest = team && agent && agent.busySeconds > 0
    && Math.max(...team.membersLoad.map((m) => m.busySeconds)) === agent.busySeconds;
  const teamKeys = [...new Set([...TEAM_ORDER, ...board.teams.map((t) => t.key)])];
  const w = agent?.work;

  return (
    <Drawer label={`${name}, ${team ? teamLabel(team.key, board) : ""}`} testid="ag-org-agent" onClose={onClose}>
      <header className="ag-org-agent-head">
        <AgFace roleKey={roleKey} size={48} state={state} decorative />
        <span className="ag-org-agent-who">
          <span className="ag-org-agent-name" data-user-text>{name}</span>
          {team ? <span className="ag-muted">{teamLabel(team.key, board)}</span> : null}
        </span>
        <button type="button" className="ag-btn" data-testid="ag-org-agent-talk" title={`Talk to ${name}`}
                onClick={() => askAgent(roleKey, name, { from: "org" })}>
          <ChatCircleDots size={12} weight="bold" aria-hidden /> Talk
        </button>
        <button type="button" className="ag-link" data-testid="ag-org-agent-passport" title="Open the Passport"
                onClick={() => openAgent(roleKey)}>Passport ↗</button>
      </header>

      <dl className="ag-org-agent-rows">
        <div className="ag-org-agent-row" data-row="now">
          <dt>Now</dt>
          <dd title={STATE_WORD[state]}>{now ? <span className="ag-trunc" data-user-text>{now.replace(/^Now:\s*/, "")}</span> : "Idle"}</dd>
        </div>

        {agent?.open.length ? (
          <div className="ag-org-agent-row" data-row="open">
            <dt>Open work</dt>
            <dd>
              <ul className="ag-org-list">
                {agent.open.slice(0, ROWS_MAX).map((o) => (
                  <li key={o.actId}><WaitItem item={o} who={false} /></li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}

        {gets.length || hands.length ? (
          <div className="ag-org-agent-row" data-row="with">
            <dt>Works with</dt>
            <dd>
              {gets.length ? <Partners title="Gets from" keys={gets.map((e) => [e.from, e.handoffs])} /> : null}
              {hands.length ? <Partners title="Hands to" keys={hands.map((e) => [e.to, e.handoffs])} /> : null}
            </dd>
          </div>
        ) : null}

        {agent ? (
          <div className="ag-org-agent-row" data-row="window">
            <dt title={`The last ${windowWords(days)}`}>{windowWords(days)}</dt>
            <dd className="ag-org-agent-window">
              {w && (w.done || w.failed) ? (
                <span>{[w.done ? `Done ${w.done}` : "", w.failed ? `Failed ${w.failed}` : ""].filter(Boolean).join(" · ")}</span>
              ) : null}
              {agent.busySeconds ? <span title="Time with work open in this window">Busy {busyWords(agent.busySeconds)}</span> : null}
              {board.totals.spendOk && (agent.spend.meteredUsd || agent.spend.planCalls) ? (
                <span>
                  {[agent.spend.meteredUsd ? `${usdWords(agent.spend.meteredUsd)} metered` : "",
                    agent.spend.planCalls ? `${agent.spend.planCalls} on plan` : ""].filter(Boolean).join(" · ")}
                </span>
              ) : null}
              {board.totals.spendOk && agent.models.length ? (
                <span title={`Models it called.${agent.configuredModel ? ` Configured: ${agent.configuredModel}.` : ""}`}>
                  {agent.models.slice(0, 2).map((m) => `${m.model} ${m.calls}`).join(" · ")}
                </span>
              ) : null}
              {!(w && (w.done || w.failed)) && !agent.busySeconds && !agent.spend.meteredUsd && !agent.spend.planCalls
                ? <span className="ag-muted">No work in {windowWords(days)}</span> : null}
            </dd>
          </div>
        ) : null}

        {agent?.namedOn.length ? (
          <div className="ag-org-agent-row" data-row="named">
            <dt>Named on</dt>
            <dd>
              <span>{agent.namedOn.length} {agent.namedOn.length === 1 ? "step" : "steps"}</span>
              {busiest && team ? (
                <span className="ag-warn" data-testid="ag-org-agent-busiest"
                      title="Most busy time on its team this window. Spread steps to rebalance.">
                  Busiest on {teamLabel(team.key, board)}
                </span>
              ) : null}
              <ul className="ag-org-list">
                {agent.namedOn.slice(0, ROWS_MAX).map((n) => (
                  <li key={`${n.workflowKey}:${n.stepId}`}>
                    <span className="ag-trunc" data-user-text title={`${n.workflowName} › ${n.stepTitle}`}>
                      {n.workflowName} › {n.stepTitle}
                    </span>
                    <a className="ag-link" href={workflowHref(n.workflowKey, n.stepId)}
                       title="Open the step in the workflow editor">Change in workflow ↗</a>
                  </li>
                ))}
              </ul>
              {agent.namedOn.length > ROWS_MAX ? <span className="ag-team-more">+{agent.namedOn.length - ROWS_MAX}</span> : null}
            </dd>
          </div>
        ) : null}

        {agent && (agent.gaps.abilities.length || agent.gaps.stepGaps.length) ? (
          <div className="ag-org-agent-row" data-row="missing">
            <dt>Missing</dt>
            <dd>
              <ul className="ag-org-list">
                {agent.gaps.abilities.map((a) => (
                  <li key={a.ability}>
                    <span className="ag-orgteam-chip" title={`Recommended for ${name}. Not held.`}>
                      <Star size={10} weight="bold" aria-hidden /> {a.label}
                    </span>
                    {a.needsSetup ? (
                      <a className="ag-link" href={kitHref(a.ability)} data-testid={`ag-org-agent-setup-${a.ability}`}
                         title={`${a.label} must be set up in the Kit first`}>Set up first ↗</a>
                    ) : actions ? (
                      <GatedButton permission={ORG_GIVE_PERMISSION} type="button" className="ag-link" data-testid={`ag-org-agent-give-${a.ability}`}
                              onClick={() => void actions.give(a.ability, a.label, roleKey)}>Give</GatedButton>
                    ) : null}
                  </li>
                ))}
                {agent.gaps.stepGaps.filter((g) => !agent.gaps.abilities.some((a) => a.ability === g.ability)).map((g) => (
                  <li key={`${g.workflowKey}:${g.stepId}:${g.ability}`}>
                    <span className="ag-trunc" title={`${g.workflowName} › ${g.stepTitle} needs ${g.label}. ${name} lacks it.`}>
                      <span data-user-text>{g.stepTitle}</span> needs <span data-user-text>{g.label}</span>
                    </span>
                    {g.needsSetup ? (
                      <a className="ag-link" href={kitHref(g.ability)} title={`${g.label} must be set up in the Kit first`}>Set up first ↗</a>
                    ) : actions ? (
                      <GatedButton permission={ORG_GIVE_PERMISSION} type="button" className="ag-link" data-testid={`ag-org-agent-give-${g.ability}`}
                              onClick={() => void actions.give(g.ability, g.label, roleKey)}>Give</GatedButton>
                    ) : null}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}

        {actions ? (
          <div className="ag-org-agent-row" data-row="more">
            <dt>More</dt>
            <dd>
              <span className="org-menu-wrap">
                <button type="button" className="ag-link" aria-haspopup="menu" aria-expanded={moveOpen}
                        data-testid="ag-zones-more" onClick={() => setMoveOpen((v) => !v)}>
                  Move to… <CaretDown size={10} weight="bold" aria-hidden />
                </button>
                {moveOpen ? (
                  <MenuPop label="Move to…" onClose={() => setMoveOpen(false)}>
                    {teamKeys.map((k) => (
                      <GatedButton key={k} permission={ORG_MOVE_PERMISSION} type="button" role="menuitem" data-testid={`ag-zones-move-${k}`}
                              disabled={team?.key === k} title={TEAM_LABELS[k]?.tip}
                              onClick={() => { setMoveOpen(false); void actions.move(roleKey, k, teamLabel(k, board)); }}>
                        {teamLabel(k, board)}
                      </GatedButton>
                    ))}
                  </MenuPop>
                ) : null}
              </span>
            </dd>
          </div>
        ) : null}
      </dl>
    </Drawer>
  );
}

function Partners({ title, keys }: { title: string; keys: [string, number][] }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const shown = keys.slice(0, ROWS_MAX);
  return (
    <div className="ag-org-partners">
      <span className="ag-org-partners-title">{title}</span>
      <ul className="ag-org-list">
        {shown.map(([k, n]) => (
          <li key={k}>
            {k === "person" ? <span className="ag-org-you" aria-hidden>●</span>
              : <AgFace roleKey={k} size={24} decorative />}
            <span data-user-text>{nameOf(k)}</span> <span>×{n}</span>
          </li>
        ))}
      </ul>
      {keys.length > shown.length ? <span className="ag-team-more">+{keys.length - shown.length}</span> : null}
    </div>
  );
}
