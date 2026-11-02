"use client";

/**
 * THE AGENT'S OWN STEPS, as an agent design is drawn (design §4.7.2; the
 * reference "1. Incident Intake Agent" screen). Four bands:
 *
 *   Knowledge Graph vN · read only        the kinds the agent reads, in their domain colours
 *   <Agent> · steps                       its numbered steps, from the stage before to the next
 *   Context Graph vN · read and write     the kinds it writes (hexagons), and context it reads
 *   External systems · via MCP            the outside systems its steps call
 *
 * Grey lines are reads, blue lines writes, dotted lines MCP calls; every line belongs to ONE
 * numbered step. Select a step or a node to light only its lines. "Show the last run" marks
 * each node and each step with what the run's graph record says it did.
 *
 * Everything drawn comes from the stage's contract (`steps`, `reads`, `writes`, `calls`), the
 * project's kinds (labels, domain colours, layers) and the last run: no ids, no sample data.
 */
import React, { useMemo, useState } from "react";

import {
  kindKey, kindLabel, type AgentStepDef, type KindMeta, type PipelineStage,
} from "../../../lib/pipeline-model";
import type { TracedKind } from "../../../lib/workflow-api";

type Overlay = "declared" | "done" | "missing" | "undeclared";
type MapNode = {
  key: string; kind: string; label: string; band: "kg" | "cg";
  color: string; ink: string; overlay: Overlay; traced?: TracedKind; written: boolean;
};
type SystemNode = { key: string; mcp: string; label: string; tools: string[]; made: number | null; missing: boolean };
type Line = { id: string; step: number; node: string; kind: "read" | "write" | "call";
              x1: number; y1: number; x2: number; y2: number; overlay?: Overlay };
type Selection = { type: "step"; n: number } | { type: "node"; key: string } | null;

const W = 1400;
const PAD = 16;
const BAND_GAP = 14;
const NODE_H = 34;
const HEX_H = 38;
const R = 22;

/** The storage layer's colour, for a kind no pack colours: rules red, code amber, ops teal… */
const LAYER_COLOR: Record<string, string> = {
  rules: "#b8443a", code: "#b7791f", ops: "#1f8a99", lifecycle: "#2f6db3",
  knowledge: "#5b6b82", context: "#4b4fae",
};

function inkFor(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return "#ffffff";
  const n = parseInt(m[1], 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.42 ? "#111827" : "#ffffff";
}

function textWidth(label: string, px = 13): number {
  return Math.ceil(label.length * px * 0.6) + 30;
}

/** Centred rows of nodes, wrapping at the band's width. */
function layoutRow(widths: number[], top: number, rowH: number, gap = 14): { x: number; y: number }[] {
  const max = W - 2 * PAD - 60;
  const rows: number[][] = [[]];
  let used = 0;
  widths.forEach((w, i) => {
    if (rows[rows.length - 1].length && used + w > max) { rows.push([]); used = 0; }
    rows[rows.length - 1].push(i);
    used += w + gap;
  });
  const at: { x: number; y: number }[] = new Array(widths.length);
  rows.forEach((row, r) => {
    const width = row.reduce((s, i) => s + widths[i], 0) + gap * (row.length - 1);
    let x = (W - width) / 2;
    for (const i of row) { at[i] = { x, y: top + r * (rowH + gap) }; x += widths[i] + gap; }
  });
  return at;
}

function bandRows(at: { y: number }[]): number {
  return new Set(at.map((p) => p.y)).size || 1;
}

/** Two short lines for a step's name under its circle. */
function twoLines(name: string, max = 16): [string, string] {
  const words = name.split(/\s+/);
  let first = "";
  let i = 0;
  while (i < words.length && (first + " " + words[i]).trim().length <= max) { first = (first + " " + words[i]).trim(); i += 1; }
  if (!first) { first = words[0]; i = 1; }
  const rest = words.slice(i).join(" ");
  return [first, rest.length > max + 4 ? `${rest.slice(0, max + 3)}…` : rest];
}

/** A step's name wrapped to the room between two step circles, word by word, in up to three
 *  lines, so neighbouring names never run into each other ("Upstream lineag|Regulatory reach").
 *  A single word longer than the room breaks at a hyphen or underscore, else stands alone. */
export function wrapToWidth(name: string, maxChars: number): string[] {
  const max = Math.max(6, maxChars);
  const words = name.split(/\s+/).filter(Boolean).flatMap((w) => {
    if (w.length <= max) return [w];
    const parts = w.split(/(?<=[-_])/);
    return parts.length > 1 ? parts : [w];
  });
  const lines: string[] = [];
  for (const w of words) {
    const last = lines[lines.length - 1];
    const joiner = last && /[-_]$/.test(last) ? "" : " ";
    if (last !== undefined && (last + joiner + w).length <= max) lines[lines.length - 1] = last + joiner + w;
    else lines.push(w);
  }
  if (lines.length <= 3) return lines;
  return [...lines.slice(0, 2), `${lines.slice(2).join(" ").slice(0, max - 1)}…`];
}

/** How many characters of a step name fit between two circles `gap` apart. */
const STEP_NAME_PX = 6.8;

function hexagon(x: number, y: number, w: number, h: number): string {
  const d = h / 2;
  return [`${x + d},${y}`, `${x + w - d},${y}`, `${x + w},${y + h / 2}`, `${x + w - d},${y + h}`,
          `${x + d},${y + h}`, `${x},${y + h / 2}`].join(" ");
}

function humanTool(tool: string): string {
  return String(tool ?? "").replace(/[_-]+/g, " ").trim();
}

function systemLabel(mcp: string): string {
  const words = kindLabel(mcp.replace(/_mock$/i, "")).replace(/^Dq\b/, "DQ");
  return words.length <= 4 ? words.toUpperCase() : words;
}

export function AgentStepsMap({ stage, steps, kinds, contextKinds, kgVersion, cgVersion, overlay }: {
  stage: PipelineStage;
  steps: AgentStepDef[];
  /** Labels, domain colours and layers of the kinds, from the designs view. */
  kinds: Record<string, KindMeta>;
  /** Kinds this pipeline treats as Context Graph: any stage writes them, or they are stored there. */
  contextKinds: Set<string>;
  kgVersion: number | null;
  cgVersion: number | null;
  /** The last run's trace for this stage, when "Show the last run" is on. */
  overlay: { reads: TracedKind[]; writes: TracedKind[]; calls: { tool: string; server?: string; count: number }[];
             callsRecorded?: boolean } | null;
}) {
  const [selected, setSelected] = useState<Selection>(null);
  const contract = stage.contract!;

  const model = useMemo(() => {
    const meta = (kind: string): KindMeta | undefined =>
      kinds[kind] ?? Object.entries(kinds).find(([k]) => kindKey(k) === kindKey(kind))?.[1];
    const writes = new Set(contract.writes.map((w) => kindKey(w.kind)));
    const isContext = (kind: string) => writes.has(kindKey(kind)) || contextKinds.has(kindKey(kind))
      || meta(kind)?.layer === "context";
    const traced = (side: "reads" | "writes") =>
      new Map((overlay?.[side] ?? []).map((t) => [kindKey(t.kind), t]));
    const readsDone = traced("reads");
    const writesDone = traced("writes");

    const nodes: MapNode[] = [];
    const seen = new Set<string>();
    const add = (kind: string, written: boolean, undeclared = false) => {
      const key = kindKey(kind);
      if (seen.has(key)) {
        if (written) nodes.find((n) => n.key === key)!.written = true;
        return;
      }
      seen.add(key);
      const m = meta(kind);
      const band: "kg" | "cg" = isContext(kind) ? "cg" : "kg";
      const color = band === "cg" ? LAYER_COLOR.context : (m?.color || LAYER_COLOR[m?.layer ?? "knowledge"] || LAYER_COLOR.knowledge);
      const t = written ? writesDone.get(key) ?? readsDone.get(key) : readsDone.get(key) ?? writesDone.get(key);
      nodes.push({ key, kind, label: m?.label || kindLabel(kind), band, color, ink: band === "cg" ? "#ffffff" : inkFor(color),
                   overlay: !overlay ? "declared" : undeclared ? "undeclared" : t ? "done" : "missing",
                   ...(t ? { traced: t } : {}), written });
    };
    contract.reads.forEach((r) => add(r.kind, false));
    contract.writes.forEach((w) => add(w.kind, true));
    if (overlay) {
      overlay.reads.forEach((t) => { if (!seen.has(kindKey(t.kind))) add(t.kind, false, true); });
      overlay.writes.forEach((t) => { if (!seen.has(kindKey(t.kind))) add(t.kind, true, true); });
    }

    const systems: SystemNode[] = [];
    for (const call of contract.calls) {
      const key = kindKey(call.mcp);
      let sys = systems.find((s) => s.key === key);
      if (!sys) { sys = { key, mcp: call.mcp, label: systemLabel(call.mcp), tools: [], made: null, missing: false }; systems.push(sys); }
      if (call.tool && !sys.tools.includes(call.tool)) sys.tools.push(call.tool);
    }
    for (const step of steps) {
      for (const call of step.calls) {
        const key = kindKey(call.mcp);
        if (!systems.some((s) => s.key === key)) systems.push({ key, mcp: call.mcp, label: systemLabel(call.mcp), tools: [], made: null, missing: false });
      }
    }
    if (overlay) {
      for (const sys of systems) {
        // A recorded call is this system's when it names the system, or one of its tools.
        const count = overlay.calls
          .filter((c) => (c.server ? kindKey(c.server) === sys.key : sys.tools.includes(c.tool)))
          .reduce((s, c) => s + c.count, 0);
        sys.made = count || null;
        sys.missing = !count && !!overlay.callsRecorded;
      }
    }

    // Geometry.
    const kg = nodes.filter((n) => n.band === "kg");
    const cg = nodes.filter((n) => n.band === "cg");
    const kgTop = PAD;
    const kgAt = layoutRow(kg.map((n) => textWidth(n.label)), kgTop + 46, NODE_H, 12);
    const kgH = 46 + bandRows(kgAt) * (NODE_H + 12) + 8;
    const stTop = kgTop + kgH + BAND_GAP;
    const stH = 150;
    const cy = stTop + 70;
    const cgTop = stTop + stH + BAND_GAP;
    const cgAt = layoutRow(cg.map((n) => textWidth(n.label, 13) + 16), cgTop + 56, HEX_H, 26);
    // A labelled Context Graph edge between two kinds (MATCHED_INCIDENT) curves under them.
    const cgKeys = new Set(cg.map((n) => n.key));
    const cgLinks = (contract.links ?? []).filter((l) => cgKeys.has(kindKey(l.from)) && cgKeys.has(kindKey(l.to)));
    const cgH = 56 + bandRows(cgAt) * (HEX_H + 26) + 8 + (cgLinks.some((l) => kindKey(l.from) !== kindKey(l.to)) ? 34 : 0);
    const exTop = cgTop + cgH + BAND_GAP;
    const exAt = layoutRow(systems.map((s) => textWidth(s.label) + 6), exTop + 52, NODE_H, 40);
    const exH = systems.length ? 52 + NODE_H + 30 : 0;
    const H = (systems.length ? exTop + exH : cgTop + cgH) + PAD;

    const x0 = 210;
    const x1 = W - 210;
    const gap = steps.length > 1 ? (x1 - x0) / (steps.length - 1) : 0;
    const stepX = new Map(steps.map((s, i) => [s.n, steps.length > 1 ? x0 + i * gap : W / 2]));
    const stepGap = steps.length > 1 ? gap : 0;

    const kgPos = new Map(kg.map((n, i) => [n.key, { ...kgAt[i], w: textWidth(n.label) }]));
    const cgPos = new Map(cg.map((n, i) => [n.key, { ...cgAt[i], w: textWidth(n.label, 13) + 16 }]));
    const exPos = new Map(systems.map((s, i) => [s.key, { ...exAt[i], w: textWidth(s.label) + 6 }]));

    const lines: Line[] = [];
    // Spread the ends that meet one circle so the lines read apart.
    const spread = (count: number, i: number) => (count <= 1 ? 0 : (i - (count - 1) / 2) * Math.min(9, 30 / count));
    for (const step of steps) {
      const sx = stepX.get(step.n)!;
      const tops = step.reads.filter((k) => kgPos.has(kindKey(k)));
      const bottoms = [...step.reads.filter((k) => cgPos.has(kindKey(k))), ...step.writes, ...step.calls.map((c) => c.mcp)];
      tops.forEach((kind, i) => {
        const p = kgPos.get(kindKey(kind))!;
        const node = nodes.find((n) => n.key === kindKey(kind));
        lines.push({ id: `read-${step.n}-${kindKey(kind)}`, step: step.n, node: kindKey(kind), kind: "read",
                     x1: p.x + p.w / 2, y1: p.y + NODE_H, x2: sx + spread(tops.length, i), y2: cy - R - 3, overlay: node?.overlay });
      });
      let b = 0;
      for (const kind of step.reads.filter((k) => cgPos.has(kindKey(k)))) {
        const p = cgPos.get(kindKey(kind))!;
        const node = nodes.find((n) => n.key === kindKey(kind));
        lines.push({ id: `read-${step.n}-${kindKey(kind)}`, step: step.n, node: kindKey(kind), kind: "read",
                     x1: p.x + p.w / 2, y1: p.y, x2: sx + spread(bottoms.length, b++), y2: cy + R + 3, overlay: node?.overlay });
      }
      for (const kind of step.writes) {
        const p = cgPos.get(kindKey(kind));
        if (!p) continue;
        const node = nodes.find((n) => n.key === kindKey(kind));
        lines.push({ id: `write-${step.n}-${kindKey(kind)}`, step: step.n, node: kindKey(kind), kind: "write",
                     x1: sx + spread(bottoms.length, b++), y1: cy + R, x2: p.x + p.w / 2, y2: p.y - 2, overlay: node?.overlay });
      }
      for (const call of step.calls) {
        const p = exPos.get(kindKey(call.mcp));
        if (!p) continue;
        lines.push({ id: `call-${step.n}-${kindKey(call.mcp)}-${kindKey(call.tool)}`, step: step.n, node: `sys:${kindKey(call.mcp)}`,
                     kind: "call", x1: sx + spread(bottoms.length, b++), y1: cy + R, x2: p.x + p.w / 2, y2: p.y - 2 });
      }
    }
    return { nodes, kg, cg, systems, kgPos, cgPos, exPos, lines, stepX, stepGap, cgLinks, kgTop, kgH, stTop, stH, cy, cgTop, cgH, exTop, exH, H };
  }, [contract, steps, kinds, contextKinds, overlay]);

  const { lines } = model;
  const lineOn = (l: Line) => !selected || (selected.type === "step" ? l.step === selected.n : l.node === selected.key);
  const nodeOn = (key: string) => !selected
    || (selected.type === "node" ? selected.key === key : lines.some((l) => l.step === selected.n && l.node === key));
  const stepOn = (n: number) => !selected
    || (selected.type === "step" ? selected.n === n : lines.some((l) => l.node === selected.key && l.step === n));
  const toggle = (next: Selection) => setSelected((cur) => (JSON.stringify(cur) === JSON.stringify(next) ? null : next));
  const press = (next: Selection, label: string) => ({
    role: "button" as const, tabIndex: 0, "aria-label": label,
    "aria-pressed": JSON.stringify(selected) === JSON.stringify(next),
    onClick: () => toggle(next),
    onKeyDown: (e: React.KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(next); } },
  });

  // Per step on the last run: every kind it names was read or written → done; some → partly.
  const stepRun = (step: AgentStepDef): "done" | "partly" | "missing" | null => {
    if (!overlay) return null;
    const named = [...step.reads, ...step.writes].map(kindKey);
    if (!named.length) return null;
    const hit = named.filter((k) => model.nodes.find((n) => n.key === k)?.overlay === "done").length;
    return hit === named.length ? "done" : hit ? "partly" : "missing";
  };

  const kgTitle = bandTitle("Knowledge Graph", kgVersion, "read only");
  const cgTitle = bandTitle("Context Graph", cgVersion, "read and write");
  const { kgTop, kgH, stTop, stH, cy, cgTop, cgH, exTop, exH, H } = model;

  const selStep = selected?.type === "step" ? steps.find((s) => s.n === selected.n) : undefined;
  const selNode = selected?.type === "node" ? model.nodes.find((n) => n.key === selected.key) : undefined;
  const selSystem = selected?.type === "node" && selected.key.startsWith("sys:")
    ? model.systems.find((s) => `sys:${s.key}` === selected.key) : undefined;
  const label = (kind: string) => model.nodes.find((n) => n.key === kindKey(kind))?.label ?? kindLabel(kind);

  return (
    <div className="pl-steps" data-testid="agent-steps-map">
      <p className="pl-hint">Select a step number or a node to highlight what it reads and writes.</p>
      <svg className="pl-steps-map" viewBox={`0 0 ${W} ${H}`} role="group" data-testid="steps-map"
           aria-label={`${stage.title}: ${steps.length} steps, what each reads and writes`}
           data-overlay-on={overlay ? "true" : "false"} data-selected={selected ? "true" : undefined}>
        <defs>
          <marker id="pls-read" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" className="pls-head-read" />
          </marker>
          <marker id="pls-write" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" className="pls-head-write" />
          </marker>
          <marker id="pls-flow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" className="pls-head-flow" />
          </marker>
        </defs>

        {/* Bands */}
        <rect className="pls-band" x={PAD} y={kgTop} width={W - 2 * PAD} height={kgH} rx={10} />
        <text className="pls-band-label" x={W - PAD - 18} y={kgTop + 26} textAnchor="end" data-testid="band-kg">{kgTitle}</text>
        <rect className="pls-band" x={PAD} y={stTop} width={W - 2 * PAD} height={stH} rx={10} />
        <text className="pls-band-label" x={W - PAD - 18} y={stTop + 26} textAnchor="end" data-testid="band-steps">{`${stage.title} · steps`}</text>
        <rect className="pls-band" x={PAD} y={cgTop} width={W - 2 * PAD} height={cgH} rx={10} />
        <text className="pls-band-label" x={W - PAD - 18} y={cgTop + 26} textAnchor="end" data-testid="band-cg">{cgTitle}</text>
        {model.systems.length ? (
          <>
            <rect className="pls-band" x={PAD} y={exTop} width={W - 2 * PAD} height={exH} rx={10} />
            <text className="pls-band-label" x={W - PAD - 18} y={exTop + 26} textAnchor="end" data-testid="band-mcp">External systems · via MCP</text>
          </>
        ) : null}
        {!model.kg.length ? (
          <text className="pls-empty" x={W / 2} y={kgTop + 62} textAnchor="middle">Reads nothing from the Knowledge Graph</text>
        ) : null}

        {/* Lines, under the nodes */}
        <g data-testid="steps-lines">
          {lines.map((l) => (
            <line key={l.id} data-testid={`line-${l.id}`} className={`pls-line pls-line-${l.kind}`}
                  data-on={selected ? (lineOn(l) ? "true" : "false") : undefined}
                  data-overlay={l.overlay && l.overlay !== "declared" ? l.overlay : undefined}
                  x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2}
                  markerEnd={l.kind === "write" ? "url(#pls-write)" : l.kind === "read" ? "url(#pls-read)" : "url(#pls-read)"} />
          ))}
        </g>

        {/* The flow: from the stage before, the numbered steps, to the next stage */}
        <g data-testid="steps-flow">
          {stage.from ? (
            <g className="pls-anchor" data-testid="steps-from">
              <rect x={PAD + 14} y={cy - 24} width={150} height={48} rx={24} />
              {twoLines(stage.from, 18).filter(Boolean).map((t, i, all) => (
                <text key={i} x={PAD + 89} y={cy + (all.length > 1 ? -4 + i * 16 : 5)} textAnchor="middle">{t}</text>
              ))}
            </g>
          ) : null}
          {stage.to ? (
            <g className="pls-anchor" data-testid="steps-to">
              <rect x={W - PAD - 164} y={cy - 24} width={150} height={48} rx={24} />
              {twoLines(`To ${stage.to}`, 18).filter(Boolean).map((t, i, all) => (
                <text key={i} x={W - PAD - 89} y={cy + (all.length > 1 ? -4 + i * 16 : 5)} textAnchor="middle">{t}</text>
              ))}
            </g>
          ) : null}
          {steps.map((s, i) => {
            const x = model.stepX.get(s.n)!;
            const prev = i > 0 ? model.stepX.get(steps[i - 1].n)! : stage.from ? PAD + 164 : null;
            return prev !== null ? (
              <line key={`f-${s.n}`} className="pls-flow" x1={prev + (i > 0 ? R + 2 : 0)} y1={cy} x2={x - R - 4} y2={cy} markerEnd="url(#pls-flow)" />
            ) : null;
          })}
          {steps.length && stage.to ? (
            <line className="pls-flow" x1={model.stepX.get(steps[steps.length - 1].n)! + R + 2} y1={cy}
                  x2={W - PAD - 168} y2={cy} markerEnd="url(#pls-flow)" />
          ) : null}
          {steps.map((s) => {
            const x = model.stepX.get(s.n)!;
            const room = model.stepGap ? model.stepGap - 10 : 220;
            const nameLines = wrapToWidth(s.name, Math.floor(room / STEP_NAME_PX));
            const run = stepRun(s);
            return (
              <g key={s.n} className="pls-step" data-testid={`step-${s.n}`}
                 data-on={selected ? (stepOn(s.n) ? "true" : "false") : undefined}
                 data-run={run ?? undefined}
                 {...press({ type: "step", n: s.n }, `Step ${s.n}: ${s.name}`)}>
                <circle cx={x} cy={cy} r={R} />
                <text x={x} y={cy + 5} textAnchor="middle" className="pls-step-n">{s.n}</text>
                {nameLines.map((line, i) => (
                  <text key={i} x={x} y={cy + R + 18 + i * 14} textAnchor="middle" className="pls-step-name"
                        data-testid={`step-name-${s.n}-${i}`}>{line}</text>
                ))}
              </g>
            );
          })}
        </g>

        {/* Knowledge Graph nodes */}
        <g data-testid="steps-kg">
          {model.kg.map((n) => {
            const p = model.kgPos.get(n.key)!;
            return (
              <g key={n.key} className="pls-node pls-node-kg" data-testid={`map-node-${n.key}`}
                 data-on={selected ? (nodeOn(n.key) ? "true" : "false") : undefined}
                 data-overlay={n.overlay !== "declared" ? n.overlay : undefined}
                 {...press({ type: "node", key: n.key }, `${n.label}, Knowledge Graph, read`)}>
                <rect x={p.x} y={p.y} width={p.w} height={NODE_H} rx={7} style={{ fill: n.color }} />
                <text x={p.x + p.w / 2} y={p.y + 22} textAnchor="middle" style={{ fill: n.ink }}>{n.label}</text>
                {n.overlay === "done" || n.overlay === "undeclared" ? (
                  <text x={p.x + p.w - 2} y={p.y - 4} textAnchor="end" className="pls-count">{`read ${n.traced?.count ?? 0}`}</text>
                ) : null}
              </g>
            );
          })}
        </g>

        {/* Context Graph edges the agent writes, labelled: a self-link loops beside its node */}
        <g data-testid="steps-cg-links">
          {model.cgLinks.map((l) => {
            const a = model.cgPos.get(kindKey(l.from))!;
            const b = model.cgPos.get(kindKey(l.to))!;
            const key = `${kindKey(l.from)}-${kindKey(l.rel)}-${kindKey(l.to)}`;
            const on = !selected || (selected.type === "node" && [kindKey(l.from), kindKey(l.to)].includes(selected.key));
            if (a === b) {
              const x = a.x + 6; const top = a.y + 6; const bottom = a.y + HEX_H - 6;
              return (
                <g key={key} data-testid={`cg-link-${key}`} data-on={selected ? (on ? "true" : "false") : undefined}>
                  <path className="pls-link" markerEnd="url(#pls-write)"
                        d={`M ${x} ${top} C ${x - 46} ${top - 26}, ${x - 46} ${bottom + 26}, ${x} ${bottom}`} />
                  <text className="pls-link-label" x={x - 44} y={a.y + HEX_H / 2 + 4} textAnchor="end">{l.rel}</text>
                </g>
              );
            }
            const x1 = a.x + a.w / 2; const x2 = b.x + b.w / 2;
            const y1 = a.y + HEX_H; const y2 = b.y + HEX_H; const dip = Math.max(y1, y2) + 34;
            return (
              <g key={key} data-testid={`cg-link-${key}`} data-on={selected ? (on ? "true" : "false") : undefined}>
                <path className="pls-link" markerEnd="url(#pls-write)"
                      d={`M ${x1} ${y1} C ${x1} ${dip}, ${x2} ${dip}, ${x2} ${y2 + 2}`} />
                <text className="pls-link-label" x={(x1 + x2) / 2} y={dip - 4} textAnchor="middle">{l.rel}</text>
              </g>
            );
          })}
        </g>

        {/* Context Graph nodes */}
        <g data-testid="steps-cg">
          {model.cg.map((n) => {
            const p = model.cgPos.get(n.key)!;
            return (
              <g key={n.key} className="pls-node pls-node-cg" data-testid={`map-node-${n.key}`}
                 data-on={selected ? (nodeOn(n.key) ? "true" : "false") : undefined}
                 data-overlay={n.overlay !== "declared" ? n.overlay : undefined}
                 {...press({ type: "node", key: n.key }, `${n.label}, Context Graph, ${n.written ? "written" : "read"}`)}>
                <polygon points={hexagon(p.x, p.y, p.w, HEX_H)} />
                <text x={p.x + p.w / 2} y={p.y + 24} textAnchor="middle">{n.label}</text>
                {n.overlay === "done" || n.overlay === "undeclared" ? (
                  <text x={p.x + p.w / 2} y={p.y + HEX_H + 16} textAnchor="middle" className="pls-count">
                    {`${n.written ? "wrote" : "read"} ${n.traced?.count ?? 0}`}
                  </text>
                ) : null}
              </g>
            );
          })}
          {!model.cg.length ? (
            <text className="pls-empty" x={W / 2} y={cgTop + 80} textAnchor="middle">Writes nothing to the Context Graph</text>
          ) : null}
        </g>

        {/* External systems */}
        <g data-testid="steps-mcp">
          {model.systems.map((s) => {
            const p = model.exPos.get(s.key)!;
            return (
              <g key={s.key} className="pls-node pls-node-sys" data-testid={`map-system-${s.key}`}
                 data-on={selected ? (nodeOn(`sys:${s.key}`) ? "true" : "false") : undefined}
                 data-overlay={s.made ? "done" : s.missing ? "missing" : undefined}
                 {...press({ type: "node", key: `sys:${s.key}` }, `${s.label}, an outside system called over MCP`)}>
                <rect x={p.x} y={p.y} width={p.w} height={NODE_H} rx={4} />
                <text x={p.x + p.w / 2} y={p.y + 22} textAnchor="middle">{s.label}</text>
                {s.made ? <text x={p.x + p.w / 2} y={p.y + NODE_H + 15} textAnchor="middle" className="pls-count">{`called ${s.made}`}</text> : null}
              </g>
            );
          })}
        </g>
      </svg>

      <div className="pl-legend pls-legend" aria-hidden data-testid="steps-legend">
        <span><i className="pls-key-kg" />Knowledge Graph node (domain colour)</span>
        <span><i className="pls-key-cg" />Context Graph node</span>
        <span><i className="pls-key-sys" />External system (via MCP)</span>
        <span><i className="pl-key pl-key-read" />Reads</span>
        <span><i className="pl-key pl-key-write" />Writes</span>
        <span><i className="pl-key pl-key-call" />MCP call</span>
        {overlay ? (
          <>
            <span><i className="pl-dot" data-overlay="done" />Done as declared</span>
            <span><i className="pl-dot" data-overlay="missing" />Declared, not done</span>
            <span><i className="pl-dot" data-overlay="undeclared" />Done, not declared</span>
          </>
        ) : null}
      </div>

      {selStep ? (
        <div className="pl-node-detail pls-detail" data-testid="step-detail" role="status">
          <div className="pls-detail-title">{`Step ${selStep.n} · ${selStep.name}`}</div>
          {selStep.explain ? <p>{selStep.explain}</p> : null}
          <ul>
            {selStep.reads.length ? <li><b>Reads</b> {selStep.reads.map(label).join(", ")}</li> : null}
            {selStep.writes.length ? <li><b>Writes</b> {selStep.writes.map(label).join(", ")}</li> : null}
            {selStep.calls.length ? (
              <li><b>Calls</b> {selStep.calls.map((c) => `${systemLabel(c.mcp)}${c.tool ? ` (${humanTool(c.tool)})` : ""}`).join(", ")}</li>
            ) : null}
            {overlay ? <li><b>On the last run</b> {stepRunWords(selStep, model.nodes)}</li> : null}
          </ul>
        </div>
      ) : selNode ? (
        <div className="pl-node-detail pls-detail" data-testid="map-node-detail" role="status">
          <div className="pls-detail-title">
            {selNode.label}{selNode.band === "kg" ? " · Knowledge Graph, read only" : " · Context Graph"}
          </div>
          <p>{nodeWords(selNode, steps)}</p>
        </div>
      ) : selSystem ? (
        <div className="pl-node-detail pls-detail" data-testid="map-node-detail" role="status">
          <div className="pls-detail-title">{`${selSystem.label} · outside system, via MCP`}</div>
          <p>
            {`Called by step${callers(selSystem.key, steps).length === 1 ? "" : "s"} ${callers(selSystem.key, steps).join(", ") || "none"}`}
            {selSystem.tools.length ? `: ${selSystem.tools.map(humanTool).join(", ")}` : ""}
            {selSystem.made ? `. The last run called it ${selSystem.made}×.` : selSystem.missing ? ". The last run did not call it." : "."}
          </p>
        </div>
      ) : null}
    </div>
  );
}

/** "Knowledge Graph v3 · read only"; without a version, "Knowledge Graph · read only". */
export function bandTitle(graph: string, version: number | null, access: string): string {
  return `${graph}${version ? ` v${version}` : ""} · ${access}`;
}

function callers(key: string, steps: AgentStepDef[]): number[] {
  return steps.filter((s) => s.calls.some((c) => kindKey(c.mcp) === key)).map((s) => s.n);
}

function nodeWords(node: MapNode, steps: AgentStepDef[]): string {
  const readers = steps.filter((s) => s.reads.some((k) => kindKey(k) === node.key)).map((s) => s.n);
  const writers = steps.filter((s) => s.writes.some((k) => kindKey(k) === node.key)).map((s) => s.n);
  const parts: string[] = [];
  if (readers.length) parts.push(`Read by step${readers.length === 1 ? "" : "s"} ${readers.join(", ")}`);
  if (writers.length) parts.push(`written by step${writers.length === 1 ? "" : "s"} ${writers.join(", ")}`);
  if (!parts.length) parts.push(node.overlay === "undeclared" ? "Not in the design" : "Declared by the agent; no step names it");
  let run = "";
  if (node.overlay === "done" && node.traced) {
    run = ` The last run ${node.written ? "wrote" : "read"} ${node.traced.count}`
      + (node.traced.decision_types.length ? ` (${node.traced.decision_types.join(", ")})` : "")
      + (node.traced.examples.length ? `: ${node.traced.examples.join(", ")}` : "") + ".";
  } else if (node.overlay === "missing") {
    run = " The last run did not do this.";
  } else if (node.overlay === "undeclared" && node.traced) {
    run = ` The last run ${node.written ? "wrote" : "read"} ${node.traced.count} without the design saying so.`;
  }
  const sentence = parts.join("; ");
  return `${sentence[0].toUpperCase()}${sentence.slice(1)}.${run}`;
}

function stepRunWords(step: AgentStepDef, nodes: MapNode[]): string {
  const named = [...new Set([...step.reads, ...step.writes].map(kindKey))];
  if (!named.length) return "this step names no graph kind.";
  const done = named.filter((k) => nodes.find((n) => n.key === k)?.overlay === "done");
  const missing = named.filter((k) => !done.includes(k)).map((k) => nodes.find((n) => n.key === k)?.label ?? k);
  if (!missing.length) return "everything it reads and writes was done.";
  return `${done.length} of ${named.length} done; not done: ${missing.join(", ")}.`;
}
