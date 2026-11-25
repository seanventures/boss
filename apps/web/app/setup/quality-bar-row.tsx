"use client";

/**
 * SETUP › QUALITY BAR (BOSSAPP-1362 phase 2): one choice, Prototype · Standard · Production, what
 * blocks and what becomes a follow-up in plain words (engine quality_bar.describe: the console
 * never writes its own copy), and what no preset changes (production always waits for a person).
 *
 * SETUP › ADVANCED › EACH GATE: every gate with its level over the preset (fold, never remove).
 * The floor (build, start, own tests, security, secrets) reads "Always blocks" and has no
 * choice. Both write project settings (PUT /v1/settings/quality_bar[_gates], settings:update).
 */
import React, { useState } from "react";
import {
  ArrowBendDownRight, ArrowCounterClockwise, CaretRight, Flask, Lock, SealCheck, ShieldCheck, XCircle,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { couldNot, reasonOf } from "../../lib/action-result";
import { fetchSetup, setupWrite } from "../../lib/setup-client";
import type { GateLevel, QualityBarView, QualityGate, QualityPreset } from "../../lib/setup-types";
import { SectionLabel } from "../section-label";
import { toast, useBoard } from "./parts";

export const PRESET_ICON: Record<QualityPreset, typeof Flask> = {
  prototype: Flask, standard: ShieldCheck, production: SealCheck,
};

const TIP = "How strict a build's checks are. What does not block becomes a follow-up BOSS fixes; "
  + "it is listed on the PR and the run.";

function useSave() {
  const { projectId, apply } = useBoard();
  const [busy, setBusy] = useState(false);
  const save = async (op: "quality_bar" | "quality_bar_gates", value: unknown, label: string) => {
    setBusy(true);
    let saved = false;
    try {
      await setupWrite(projectId, op, { value }, { label });
      saved = true;
      apply(await fetchSetup(projectId));
      toast(label);
    } catch (error) {
      toast(couldNot(`save ${label.split(":")[0]}`, reasonOf(error),
                     saved ? "It was saved; reload the page to see it." : "Nothing changed."), "error");
    } finally { setBusy(false); }
  };
  return { busy, save };
}

/** The three presets as one radio row (also New work's per-run chip reads PRESET_ICON). */
function PresetPicker({ qb, busy, onPick }: { qb: QualityBarView; busy: boolean; onPick: (p: QualityPreset) => void }) {
  return (
    <div className="su-line" role="radiogroup" aria-label="Quality bar">
      {qb.presets.map((p) => {
        const Icon = PRESET_ICON[p.key];
        const on = p.key === qb.bar;
        return (
          <GatedButton key={p.key} permission="settings:update" type="button" role="radio"
                       className={`su-btn su-btn-sm${on ? " su-btn-primary" : ""}`} aria-checked={on}
                       disabled={busy} onClick={() => onPick(p.key)} data-testid={`su-qb-${p.key}`}
                       title={p.line} data-tip={p.line}>
            <Icon size={14} weight={on ? "fill" : "regular"} aria-hidden />
            {p.label}{p.key === qb.default ? <span className="su-qb-default"> · default</span> : null}
          </GatedButton>
        );
      })}
    </div>
  );
}

export function QualityBarRow({ onEachGate }: { onEachGate?: () => void }) {
  const { view } = useBoard();
  const { busy, save } = useSave();
  const qb = view.quality_bar;
  if (!qb) return null;
  const preset = qb.presets.find((p) => p.key === qb.bar) ?? qb.presets[0];
  const changed = Object.keys(qb.overrides).length;
  return (
    <section className="su-row" data-testid="su-row-quality-bar" aria-label="Quality bar" id="su-quality-bar">
      <div className="su-row-label"><SectionLabel as="span" tip={TIP}>Quality bar</SectionLabel></div>
      <div className="su-row-body su-stack">
        <PresetPicker qb={qb} busy={busy}
                      onPick={(p) => { if (p !== qb.bar) void save("quality_bar", p, `Quality bar: ${qb.presets.find((x) => x.key === p)?.label ?? p}`); }} />
        <p className="su-qb-line" data-testid="su-qb-line"><b>{preset.label}.</b> {preset.line}</p>
        <div className="su-qb-cols">
          <div className="su-qb-col" data-testid="su-qb-blocks">
            <span className="su-qb-h"><XCircle size={14} weight="bold" aria-hidden className="su-qb-red" /> Blocks</span>
            <ul>{preset.blocks.map((b) => <li key={b}>{b}</li>)}</ul>
          </div>
          <div className="su-qb-col" data-testid="su-qb-followups">
            <span className="su-qb-h"><ArrowBendDownRight size={14} weight="bold" aria-hidden className="su-qb-blue" /> Follow-ups</span>
            <ul>{preset.follow_ups.map((b) => <li key={b}>{b}</li>)}</ul>
          </div>
        </div>
        <p className="su-qb-always" data-testid="su-qb-always"><Lock size={13} aria-hidden /> {qb.always}</p>
        {onEachGate ? (
          <span className="su-line">
            <button type="button" className="su-link" onClick={onEachGate} data-testid="su-qb-each-gate">
              Each gate{changed ? ` · ${changed} changed` : ""} <CaretRight size={12} aria-hidden /></button>
          </span>
        ) : null}
      </div>
    </section>
  );
}

const LEVEL_WORD: Record<GateLevel, string> = { block: "Blocks", follow_up: "Follow-up" };

function GateLine({ g, bar, busy, onSet }: { g: QualityGate; bar: QualityPreset; busy: boolean;
                                            onSet: (gate: string, level: GateLevel | "default") => void }) {
  if (g.floor) {
    return (
      <li className="su-qb-gate" data-testid={`su-qg-${g.gate}`} data-floor="">
        <span className="su-qb-gname">{g.label}</span>
        <span className="su-pill" title="Part of the floor: no preset or level relaxes it." data-tip="Part of the floor: no preset or level relaxes it.">
          <Lock size={12} aria-hidden /> Always blocks</span>
      </li>
    );
  }
  const options: { key: GateLevel | "default"; label: string; tip: string }[] = [
    { key: "default", label: `Preset · ${LEVEL_WORD[g.presets[bar]]}`, tip: g.note ?? `What ${bar} does with it` },
    { key: "block", label: "Blocks", tip: "A failure holds the build" },
    { key: "follow_up", label: "Follow-up", tip: "A failure is listed on the PR and the run; BOSS fixes it after" },
  ];
  return (
    <li className="su-qb-gate" data-testid={`su-qg-${g.gate}`}>
      <span className="su-qb-gname" title={g.note} data-tip={g.note}>{g.label}</span>
      <span className="su-line su-qb-levels" role="radiogroup" aria-label={g.label}>
        {options.map((o) => {
          const on = g.level === o.key;
          return (
            <GatedButton key={o.key} permission="settings:update" type="button" role="radio" aria-checked={on}
                         className={`su-btn su-btn-sm${on ? " su-btn-primary" : ""}`} disabled={busy}
                         onClick={() => { if (!on) onSet(g.gate, o.key); }} title={o.tip} data-tip={o.tip}
                         data-testid={`su-qg-${g.gate}-${o.key}`}>
              {o.key === "block" ? <XCircle size={13} aria-hidden /> : o.key === "follow_up"
                ? <ArrowBendDownRight size={13} aria-hidden /> : <ShieldCheck size={13} aria-hidden />}
              {o.label}
            </GatedButton>
          );
        })}
      </span>
    </li>
  );
}

export function QualityGatesRow() {
  const { view } = useBoard();
  const { busy, save } = useSave();
  const qb = view.quality_bar;
  if (!qb) return null;
  const set = (gate: string, level: GateLevel | "default") => {
    const next: Record<string, string> = { ...qb.overrides };
    if (level === "default") delete next[gate]; else next[gate] = level;
    const label = qb.gates.find((g) => g.gate === gate)?.label ?? gate;
    void save("quality_bar_gates", Object.keys(next).length ? next : null,
              `${label}: ${level === "default" ? "follows the preset" : LEVEL_WORD[level]}`);
  };
  const changed = Object.keys(qb.overrides).length;
  return (
    <section className="su-row" data-testid="su-row-quality-gates" aria-label="Each gate" id="su-quality-gates">
      <div className="su-row-label">
        <SectionLabel as="span" tip="Each check's level over the preset. The floor always blocks.">Each gate</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        <span className="su-line">
          <span className="su-faint">Preset: <b>{qb.presets.find((p) => p.key === qb.bar)?.label}</b>
            {changed ? ` · ${changed} gate${changed === 1 ? "" : "s"} changed` : " · no gate changed"}</span>
          {changed ? (
            <GatedButton permission="settings:update" type="button" className="su-btn su-btn-sm" disabled={busy}
                         onClick={() => void save("quality_bar_gates", null, "Each gate: all follow the preset")}
                         data-testid="su-qg-reset" title="Every gate follows the preset again">
              <ArrowCounterClockwise size={13} aria-hidden /> Reset</GatedButton>
          ) : null}
        </span>
        <ul className="su-qb-gates">
          {qb.gates.map((g) => <GateLine key={g.gate} g={g} bar={qb.bar} busy={busy} onSet={set} />)}
        </ul>
      </div>
    </section>
  );
}
