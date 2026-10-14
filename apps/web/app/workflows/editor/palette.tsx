"use client";

/**
 * Everything that can become a step, in four groups: Agent, Built-in, Your steps, Flow.
 *
 * Rows are a glyph and a name. What a step does, needs and makes is a three-line tooltip, never
 * a paragraph in the column. A row is clicked (insert after the selection, or at the end) or
 * dragged onto a gap in the lane. Branch blocks — Branch, Do at the same time, Repeat until — are
 * offered when the server lists them, and land whole (lib/workflow-blocks.ts `placeBlock`).
 */
import { CaretLeft, CaretRight, LockSimple, MagnifyingGlass, PencilSimple, Plus, Warning } from "@phosphor-icons/react";
import React, { useMemo, useState } from "react";

import type { BlockDef, CustomStepDef, Palette, PaletteItem, StepView } from "../../../lib/workflow-editor-types";
import { NodeIcon } from "../flow/type-icons";
import { useTips } from "../flow/tip-layer";
import { beginDrag } from "../flow/use-lane-drag";

export type PaletteGroup = "agent" | "builtin" | "custom" | "flow";
export type PaletteEntry = {
  key: string; item: PaletteItem; label: string; group: PaletteGroup; type: StepView["type"];
  icon?: string; tip: string; forced?: boolean; disabled?: boolean; edit?: CustomStepDef;
  /** Searched as well as the label. */
  summary: string;
};

export const GROUP_LABEL: Record<PaletteGroup, string> = {
  agent: "Agent", builtin: "Built-in", custom: "Your steps", flow: "Flow",
};
const BLOCK_LABEL: Record<BlockDef["kind"], string> = {
  decision: "Approval gate", timer: "Wait", subworkflow: "Run another workflow",
};
const BLOCK_TYPE: Record<BlockDef["kind"], StepView["type"]> = {
  decision: "gate", timer: "wait", subworkflow: "subworkflow",
};
export const AGENT_TASK_TIP = "An agent does it from your instructions.";

function minutes(n: number | undefined): string {
  if (!n || n <= 0) return "";
  return n >= 90 ? `~${Math.round(n / 60)}h` : `~${Math.round(n)} min`;
}

/** At most three lines: what it does · what it needs and makes · how long and who decides. */
export function entryTip(summary: string | undefined, needs: string[], makes: string[],
                         typical?: number, forced?: boolean): string {
  const io = [needs.length ? `Needs: ${needs.join(", ")}` : "", makes.length ? `Makes: ${makes.join(", ")}` : ""]
    .filter(Boolean).join(" · ");
  const extra = [minutes(typical), forced ? "Always asks a person" : ""].filter(Boolean).join(" · ");
  return [summary?.trim() ?? "", io, extra].filter(Boolean).join("\n");
}

/** Every row the palette and QuickAdd offer, in display order. */
export function paletteEntries(palette: Palette | null): PaletteEntry[] {
  const out: PaletteEntry[] = [{
    key: "agent-task", item: { agentTask: true }, label: "Agent task", group: "agent", type: "agent",
    tip: AGENT_TASK_TIP, summary: AGENT_TASK_TIP,
  }];
  if (!palette) return out;
  for (const c of palette.capabilities) {
    const forced = !!c.approval_forced_manual;
    out.push({
      key: `cap:${c.key}`, item: { capability: c.key }, label: c.label || c.key, group: "builtin",
      type: c.is_gate ? "gate" : "builtin", icon: c.key, forced, summary: c.summary ?? "",
      tip: entryTip(c.summary, c.required_inputs ?? [], c.produces ?? [], c.typical_minutes, forced),
    });
  }
  for (const s of palette.customSteps) {
    const forced = !!s.approval_forced_manual;
    out.push({
      key: `custom:${s.step_key}`, item: { customStep: s.step_key }, label: s.label || s.step_key,
      group: "custom", type: "custom", forced, disabled: !s.usable, edit: s, summary: s.summary ?? "",
      tip: !s.usable && s.unusable_reason
        ? s.unusable_reason
        : entryTip(s.summary, s.inputs.filter((i) => i.required).map((i) => i.name), s.produces, undefined, forced),
    });
  }
  for (const b of palette.blocks) {
    if (!BLOCK_LABEL[b.kind]) continue;
    out.push({
      key: `block:${b.kind}`, item: { block: b.kind }, label: BLOCK_LABEL[b.kind], group: "flow",
      type: BLOCK_TYPE[b.kind], tip: b.summary, summary: b.summary,
    });
  }
  for (const c of palette.controls ?? []) {
    out.push({
      key: `control:${c.kind}`, item: { control: c.kind }, label: c.label, group: "flow",
      type: CONTROL_TYPE[c.kind], tip: c.summary, summary: c.summary,
    });
  }
  return out;
}

const CONTROL_TYPE: Record<NonNullable<PaletteItem["control"]>, StepView["type"]> = {
  condition: "condition", parallel: "parallel", bounded_loop: "loop",
};

export function matches(entry: PaletteEntry, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return entry.label.toLowerCase().includes(q) || entry.summary.toLowerCase().includes(q)
    || (entry.icon ?? "").toLowerCase().includes(q);
}

function Row({ entry, onPick, onEdit, disabled, rail }: {
  entry: PaletteEntry; onPick(item: PaletteItem): void; onEdit?(def: CustomStepDef): void;
  disabled?: boolean; rail?: boolean;
}) {
  const off = disabled || entry.disabled;
  return (
    <li className="wf-pal-row" data-disabled={off ? "true" : undefined} data-group={entry.group}>
      <button type="button" className="wf-pal-item" data-tip={rail ? `${entry.label}\n${entry.tip}` : entry.tip}
              aria-label={rail ? entry.label : undefined} aria-disabled={off || undefined}
              data-testid={`wf-pal-${entry.key}`}
              onClick={() => { if (!off) onPick(entry.item); }}
              onPointerDown={(e) => {
                if (off || e.button !== 0) return;
                beginDrag({ kind: "item", item: entry.item }, e.nativeEvent, e.currentTarget);
              }}>
        <span className="wf-pal-icon" data-type={entry.type}><NodeIcon icon={entry.icon} type={entry.type} size={14} /></span>
        {rail ? null : <span className="wf-pal-label">{entry.label}</span>}
        {!rail && entry.forced ? <LockSimple size={12} weight="fill" className="wf-pal-lock" aria-label="Always asks a person" /> : null}
        {!rail && entry.disabled ? <Warning size={12} weight="fill" className="wf-pal-warn" aria-label="Not usable" /> : null}
      </button>
      {!rail && entry.edit && onEdit ? (
        <button type="button" className="wf-pal-edit" aria-label={`Edit ${entry.label}`} data-tip="Edit step"
                onClick={() => onEdit(entry.edit!)}>
          <PencilSimple size={12} weight="bold" aria-hidden />
        </button>
      ) : null}
    </li>
  );
}

export function StepPalette({ palette, onPick, onNewStep, onEditStep, collapsed, onToggle, disabled, sheet }: {
  palette: Palette | null;
  onPick(item: PaletteItem): void;
  onNewStep(): void;
  onEditStep(def: CustomStepDef): void;
  collapsed?: boolean;
  onToggle?(): void;
  disabled?: boolean;
  /** Drawn inside the small-screen sheet rather than as the left column. */
  sheet?: boolean;
}) {
  useTips();
  const [query, setQuery] = useState("");
  const entries = useMemo(() => paletteEntries(palette), [palette]);
  const shown = entries.filter((e) => matches(e, query));
  const groups = (["agent", "builtin", "custom", "flow"] as PaletteGroup[])
    .map((g) => ({ g, rows: shown.filter((e) => e.group === g) }))
    .filter(({ g, rows }) => rows.length || (g === "custom" && !query));
  const rail = !!collapsed && !sheet;

  return (
    <aside className="wf-palette" data-collapsed={rail ? "true" : undefined} data-sheet={sheet ? "true" : undefined}
           aria-label="Steps" data-testid="wf-palette">
      <div className="wf-pal-top">
        {rail ? null : (
          <label className="wf-pal-search">
            <MagnifyingGlass size={14} weight="bold" aria-hidden />
            <input type="search" placeholder="Add a step…" aria-label="Add a step" value={query}
                   onChange={(e) => setQuery(e.target.value)} data-testid="wf-pal-search" />
          </label>
        )}
        {onToggle && !sheet ? (
          <button type="button" className="wf-icon-btn wf-pal-toggle" onClick={onToggle}
                  aria-label={rail ? "Show steps" : "Hide steps"} aria-expanded={!rail}
                  data-tip={rail ? "Show steps" : "Hide steps"}>
            {rail ? <CaretRight size={14} weight="bold" aria-hidden /> : <CaretLeft size={14} weight="bold" aria-hidden />}
          </button>
        ) : null}
      </div>
      <div className="wf-pal-scroll">
        {groups.map(({ g, rows }) => (
          <section key={g} className="wf-pal-group" aria-label={GROUP_LABEL[g]}>
            {rail ? null : <h3 className="wf-pal-heading">{GROUP_LABEL[g]}</h3>}
            <ul className="wf-pal-list">
              {rows.map((entry) => (
                <Row key={entry.key} entry={entry} onPick={onPick} onEdit={onEditStep} disabled={disabled} rail={rail} />
              ))}
              {g === "custom" && !rail ? (
                <li className="wf-pal-row">
                  <button type="button" className="wf-pal-item wf-pal-new" onClick={onNewStep} disabled={disabled}
                          data-testid="wf-pal-new-step">
                    <span className="wf-pal-icon"><Plus size={14} weight="bold" aria-hidden /></span>
                    <span className="wf-pal-label">New step</span>
                  </button>
                </li>
              ) : null}
            </ul>
          </section>
        ))}
      </div>
    </aside>
  );
}
