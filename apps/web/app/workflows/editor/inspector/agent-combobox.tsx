"use client";

import React, { useId, useMemo, useRef, useState } from "react";
import { Warning } from "@phosphor-icons/react";

import { AgentAvatar } from "../../../agent-avatar";
import { agentMeta } from "../../../agent-chip";
import { stepNeeds } from "../../../../lib/step-view";
import { stepView } from "../../../../lib/workflow-editor-types";
import type { Palette, SpecStep, StepView } from "../../../../lib/workflow-editor-types";
import { Popover } from "./popover";

/** The value the store writes for "nobody named": the captain key is removed. */
export type CaptainChoice = string | null;

type Option = {
  value: CaptainChoice;
  label: string;
  /** `4 skills`, or nothing for the default choice. */
  count?: string;
  /** Right-aligned: what it has, or what it is missing, for THIS step. */
  status?: { ok: boolean; text: string };
  tip: string;
  kind: "default" | "agent" | "stale";
};

/** The default choice's words, per step type (copy guide 4). */
function defaultOption(type: StepView["type"]): Option {
  if (type === "gate") {
    return { value: null, label: "A person", tip: "Waits for a person to answer.", kind: "default" };
  }
  if (type === "builtin") {
    return { value: null, label: "You", tip: "Built-in step: runs as whoever starts the workflow.",
             kind: "default" };
  }
  return { value: null, label: "Auto",
           tip: type === "custom" ? "BOSS picks the agent holding this step's skill."
             : "BOSS picks an agent holding every allowed skill.",
           kind: "default" };
}

/** The option's face: the agent's kit character, or the Auto / You / A person glyph. */
export function Avatar({ value, label, size = 22 }: { value: CaptainChoice; label: string; size?: number }) {
  if (value === null || value === "human") {
    const variant = label === "Auto" || label.startsWith("Auto ·") ? "auto" : label === "You" ? "you" : "person";
    return <AgentAvatar variant={variant} size={size} className="wf-avatar" />;
  }
  const meta = agentMeta(value, label);
  return <AgentAvatar agent={value} size={size} aiBadge={false} className={`mr-avatar tone-${meta.tone} wf-avatar ag-face`} />;
}

/**
 * Who runs this step: `Auto` (or `You` / `A person`), then every agent on the project with how
 * many skills it holds and whether it has what THIS step needs.
 *
 * The needs check is not computed here. Each option asks `stepView` what the step would lack
 * with that agent named, so the combobox, the node's warning badge and the Grant row cannot
 * disagree about whether an agent can run a step.
 */
export function AgentCombobox({ step, view, palette, onChange }: {
  step: SpecStep;
  view: StepView;
  palette: Palette;
  onChange(next: CaptainChoice): void;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  const isGate = view.type === "gate";
  const captain = typeof step.captain === "string" && step.captain.trim() ? step.captain.trim() : null;
  // A gate's default IS a person, so `human` written explicitly is the same choice.
  const current: CaptainChoice = isGate && captain === "human" ? null : captain;

  const options = useMemo<Option[]>(() => {
    const out: Option[] = [defaultOption(view.type)];
    const agents = palette.agents.filter((a) => a.role_key !== palette.humanRole && a.role_key !== "human");
    // What THIS step needs its agent to hold. Empty for a built-in step (BOSS's own tools, no
    // grant), and then no option claims anything: a `✓ has` there would be about nothing.
    const needs = stepNeeds(step, palette);
    for (const agent of agents) {
      const lacks = stepView({ ...step, captain: agent.role_key }, palette).badges.lacks;
      const more = (list: string[]) => (list.length > 1 ? ` +${list.length - 1}` : "");
      const status = lacks.length
        ? { ok: false, text: `✗ missing ${lacks[0]}${more(lacks)}` }
        : needs.length
          ? { ok: true, text: `✓ has ${needs[0]}${more(needs)}` }
          : view.type === "agent" && agent.skills.length === 0 && agent.mcp.length === 0
            ? { ok: false, text: "✗ no skills" }
            : undefined;
      out.push({
        value: agent.role_key, label: agent.name || agent.role_key,
        count: `${agent.skills.length} skill${agent.skills.length === 1 ? "" : "s"}`,
        status, kind: "agent",
        tip: agent.name && agent.name !== agent.role_key ? `${agent.name} · ${agent.role_key}` : agent.role_key,
      });
    }
    // A NAME THE PROJECT DOES NOT HAVE stays visible rather than silently becoming "Auto": the
    // save would refuse it, and the operator should see why before pressing Save.
    if (current && !agents.some((a) => a.role_key === current)) {
      out.push({
        value: current, label: current, kind: "stale",
        status: { ok: false, text: current === "human" ? "✗ only a gate" : "✗ not on this project" },
        tip: current === "human" ? "Only an approval gate can wait for a person."
          : "No agent with this name on this project.",
      });
    }
    return out;
  }, [palette, step, view.type, current]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? options.filter((o) => `${o.label} ${o.value ?? ""}`.toLowerCase().includes(q)) : options;
  }, [options, query]);

  const choose = (option: Option) => {
    setOpen(false);
    setQuery("");
    if (option.value !== current) onChange(option.value);
    trigger.current?.focus();
  };

  const selected = options.find((o) => o.value === current) ?? options[0];
  const label = current === null ? view.agent.label : selected.label;

  return (
    <div className="wf-combo">
      <button ref={trigger} type="button" className="wf-combo-trigger" role="combobox"
              aria-expanded={open} aria-controls={listId} aria-haspopup="listbox"
              aria-label="Agent" title={current === null ? view.agent.tip : selected.tip}
              data-testid="wf-agent-trigger"
              data-stale={selected.kind === "stale" || undefined}
              onClick={() => { setOpen((o) => !o); setActive(Math.max(0, options.indexOf(selected))); }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
                  e.preventDefault(); setOpen(true);
                }
              }}>
        <Avatar value={current} label={label} />
        <span className="wf-combo-label">{label}</span>
        {selected.kind === "stale" ? <Warning size={14} weight="bold" className="wf-amber" aria-hidden /> : null}
        <span className="wf-caret" aria-hidden>▾</span>
      </button>
      <Popover anchor={trigger} open={open} onClose={() => { setOpen(false); setQuery(""); }}
               label="Choose an agent" width={360} matchAnchor testId="wf-agent-pop">
        <input className="wf-pop-search" autoFocus placeholder="Find an agent" value={query}
               aria-label="Find an agent" aria-controls={listId}
               aria-activedescendant={shown[active] ? `${listId}-${active}` : undefined}
               onChange={(e) => { setQuery(e.target.value); setActive(0); }}
               onKeyDown={(e) => {
                 if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(shown.length - 1, i + 1)); }
                 else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
                 else if (e.key === "Enter" && shown[active]) { e.preventDefault(); choose(shown[active]); }
               }} />
        <ul id={listId} role="listbox" className="wf-combo-list" aria-label="Agents">
          {shown.map((option, i) => (
            <li key={option.value ?? "__default"} id={`${listId}-${i}`} role="option"
                aria-selected={option.value === current}
                data-active={i === active || undefined}
                data-testid={`wf-agent-option-${option.value ?? "default"}`}
                className="wf-combo-option" title={option.tip}
                onPointerEnter={() => setActive(i)}
                onClick={() => choose(option)}>
              <Avatar value={option.value} label={option.label} />
              <span className="wf-combo-text">
                <span className="wf-combo-name" data-testid="wf-agent-option-name">{option.label}</span>
                {option.count || option.status ? (
                  <span className="wf-combo-meta">
                    {option.count ? <span className="wf-faint">{option.count}</span> : null}
                    {option.status ? (
                      <span className={option.status.ok ? "wf-combo-ok" : "wf-combo-miss"}
                            data-testid="wf-agent-option-status">{option.status.text}</span>
                    ) : null}
                  </span>
                ) : null}
              </span>
            </li>
          ))}
          {shown.length === 0 ? <li className="wf-faint wf-combo-none">No match</li> : null}
        </ul>
        <a className="wf-pop-foot" href="/?area=workforce" target="_blank" rel="noreferrer">Manage agents ↗</a>
      </Popover>
    </div>
  );
}
