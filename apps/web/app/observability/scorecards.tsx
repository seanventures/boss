"use client";

/**
 * Observability › Scorecards (docs/product/RUN_SCORECARDS.md, "See it", "Compare variants").
 *
 *   [Scores | Experiments]  [Builds Runs Steps Panels Research]  [Role Workflow Harness Model]  [7d 30d 90d]
 *   UNITS 42 · AVG SCORE 0.71 · DONE 88% · MERGED 64% · COST $31.20
 *   BY ROLE   key · n · score (mean, spread) · done · merged · cost · turns · steers
 *   WHAT'S COSTING YOU   lowest scores, most steers, most review rounds — each opens the run
 *
 *   Experiments: each A/B with both arms' n, mean ± sd, cost against its cap, the 95% interval of
 *   B − A, "Too close to call" until it clears zero, and Promote (one press, in Govern's log).
 *
 * Every number is the server's (services/scorecards.view, services/experiments). Scores are only
 * compared within one score version.
 */
import { GatedButton } from "../../components/gated";
import React, { useState } from "react";

import {
  HARNESS_NAMES, improveWrite, polledImprove,
  type Experiment, type ExperimentsView, type ScoreRow, type ScorecardsView,
} from "../../lib/improve";
import { usePolledRead } from "../../lib/log-stream";
import { useConsoleLive } from "../../lib/console-live";
import { showToast } from "../client-forms";
import type { ObsTabProps } from "./obs-home";
import { Empty, Sheet, Skeleton, ZoneHead, useUrlState, writeUrl } from "../infra/kit";
import "../improve/improve.css";

const KINDS = [
  { key: "build", label: "Builds", tip: "Factory builds and quick changes" },
  { key: "workflow_run", label: "Runs", tip: "Whole workflow runs" },
  { key: "step", label: "Steps", tip: "One workflow step each" },
  { key: "panel", label: "Panels", tip: "PRD panels" },
  { key: "research", label: "Research", tip: "Research dossiers" },
];
const GROUPS = [
  { key: "role", label: "Role" }, { key: "workflow", label: "Workflow" },
  { key: "harness", label: "Harness" }, { key: "model", label: "Model" },
];
const WINS = ["7d", "30d", "90d"];
const WHY: Record<string, { word: string; tip: string }> = {
  low: { word: "Low score", tip: "Among the lowest scores in this window" },
  steers: { word: "Steered", tip: "A person had to steer it" },
  rounds: { word: "Review rounds", tip: "Needed more review rounds than usual" },
};

type ScUrl = { mode: "scores" | "experiments"; kind: string; by: string; win: string };
function readScUrl(search: string): ScUrl {
  const p = new URLSearchParams(search);
  const kind = p.get("kind") ?? "build";
  const by = p.get("by") ?? "role";
  const win = p.get("swin") ?? "30d";
  return { mode: p.get("sc") === "experiments" ? "experiments" : "scores",
           kind: KINDS.some((k) => k.key === kind) ? kind : "build",
           by: GROUPS.some((g) => g.key === by) ? by : "role", win: WINS.includes(win) ? win : "30d" };
}

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
const usd = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `$${v.toFixed(2)}`);

function Seg({ items, value, onPick, label, testId }: {
  items: { key: string; label: string; tip?: string }[]; value: string; onPick: (k: string) => void; label: string; testId: string;
}) {
  return (
    <span className="inf-seg" role="radiogroup" aria-label={label} data-testid={testId}>
      {items.map((i) => (
        <button key={i.key} type="button" role="radio" aria-checked={value === i.key} className="inf-seg-btn"
                title={i.tip} data-testid={`${testId}-${i.key}`} onClick={() => onPick(i.key)}>{i.label}</button>
      ))}
    </span>
  );
}

/** Mean as a mark, ± one standard deviation as a band, on 0..1. SVG: no inline styles. */
function ScoreBar({ score, sd }: { score: number | null; sd: number | null }) {
  if (score === null) return <span className="im-score"><span className="im-score-v">—</span></span>;
  const lo = Math.max(0, score - (sd ?? 0));
  const hi = Math.min(1, score + (sd ?? 0));
  return (
    <span className="im-score">
      <svg viewBox="0 0 100 14" preserveAspectRatio="none" role="img"
           aria-label={`score ${score.toFixed(2)}${sd !== null ? `, spread ${sd.toFixed(2)}` : ""}`}>
        <rect className="im-score-track" x={0} y={5} width={100} height={4} rx={2} />
        <rect className="im-score-spread" x={lo * 100} y={3} width={Math.max(1, (hi - lo) * 100)} height={8} rx={3} />
        <rect className="im-score-mark" x={Math.max(0, score * 100 - 1.5)} y={1} width={3} height={12} rx={1} />
      </svg>
      <span className="im-score-v">{score.toFixed(2)}</span>
    </span>
  );
}

function Scores({ projectId, actor, u }: { projectId: string; actor: string; u: ScUrl }) {
  const live = useConsoleLive();
  const read = usePolledRead<ScorecardsView>({
    key: `${projectId}|${u.kind}|${u.by}|${u.win}`, pollMs: 60_000, paused: !live,
    read: polledImprove<ScorecardsView>("scorecards", { project_id: projectId, kind: u.kind, group: u.by, win: u.win }),
  });
  const [busy, setBusy] = useState(false);
  const v = read.data;
  const backfill = async () => {
    setBusy(true);
    const r = await improveWrite("backfill", { project_id: projectId, actor });
    setBusy(false);
    if (r.ok) { showToast("success", "History scored"); read.reload(); }
    else showToast("error", r.status === 403 ? "You can't change this" : "Not scored");
  };
  if (read.error && !v) return <Empty testId="sc-error"><span title={read.error}>Can&apos;t reach BOSS</span></Empty>;
  if (!v) return <Skeleton testId="sc-loading" />;
  const t = v.total;
  const groupLabel = GROUPS.find((g) => g.key === u.by)?.label ?? "Role";
  return (
    <>
      <div className="im-tiles" data-testid="sc-tiles">
        <div className="im-tile" title="Finished units in this window"><span className="im-tile-k">Units</span><span className="im-tile-v">{t.n}</span></div>
        <div className="im-tile" title={`Score v${v.score_version}, 0 to 1. Weights in Govern › Sharing.`}><span className="im-tile-k">Avg score</span><span className="im-tile-v">{num(t.score)}</span></div>
        <div className="im-tile" title="Completed, not failed, cancelled or rejected"><span className="im-tile-k">Done</span><span className="im-tile-v">{pct(t.done)}</span></div>
        <div className="im-tile" title="Of the ones that opened a pull request. Reverts outside BOSS are not seen."><span className="im-tile-k">Merged</span><span className="im-tile-v">{pct(t.merged)}</span></div>
        <div className="im-tile" title="Model money, not subscription use"><span className="im-tile-k">Cost</span><span className="im-tile-v">{usd(t.cost_usd)}</span></div>
      </div>
      {t.n === 0 ? (
        <Empty testId="sc-empty">
          <span>No scorecards yet</span>
          <GatedButton permission="system:operate" type="button" className="ag-btn" disabled={busy} data-testid="sc-backfill"
                  title="Score the runs BOSS already finished" onClick={() => void backfill()}>
            {busy ? "Scoring…" : "Score history"}
          </GatedButton>
        </Empty>
      ) : (
        <>
          <section className="obs-zone" aria-labelledby="sc-by-head">
            <ZoneHead id="sc-by-head" label={`BY ${groupLabel.toUpperCase()}`} count={v.rows.length}
                      tip={`Score v${v.score_version}; ${t.backfilled} scored from history`} />
            <div className="im-table" role="table" aria-label={`Scores by ${groupLabel}`} data-testid="sc-table">
              <div className="im-tr im-th" role="row">
                <span role="columnheader">{groupLabel}</span><span role="columnheader">N</span>
                <span role="columnheader" title="Mean, with one standard deviation either side">Score</span>
                <span role="columnheader">Done</span><span role="columnheader">Merged</span>
                <span role="columnheader">Cost</span><span role="columnheader" title="Average turns">Turns</span>
                <span role="columnheader" title="Average steers">Steers</span>
              </div>
              {v.rows.map((r: ScoreRow) => (
                <div className="im-tr" role="row" key={r.key} data-testid="sc-row">
                  <span className="im-key" role="cell" data-user-text="" title={r.label ?? r.key}>{r.label ?? r.key}</span>
                  <span role="cell">{r.n}</span>
                  <span role="cell"><ScoreBar score={r.score} sd={r.sd} /></span>
                  <span role="cell">{pct(r.done)}</span><span role="cell">{pct(r.merged)}</span>
                  <span role="cell">{usd(r.cost_usd)}</span><span role="cell">{num(r.turns, 0)}</span>
                  <span role="cell">{num(r.steers, 1)}</span>
                </div>
              ))}
            </div>
          </section>
          <section className="obs-zone" aria-labelledby="sc-cost-head">
            <ZoneHead id="sc-cost-head" label="WHAT'S COSTING YOU" count={v.costing.length}
                      tip="Lowest scores, most steers, most review rounds" />
            {v.costing.length ? (
              <ul className="im-costing" data-testid="sc-costing">
                {v.costing.map((c) => (
                  <li key={c.unit_id} className="im-cost-row" data-testid="sc-cost-row">
                    <span className="im-why" data-why={c.why} title={WHY[c.why]?.tip}>{WHY[c.why]?.word ?? c.why}</span>
                    <a href={c.href} data-user-text="" title={`${c.title} · ${c.state}${c.error_class ? ` · ${c.error_class}` : ""}`}>{c.title}</a>
                    <span title="Score">{num(c.score)}</span>
                    <span title={`${c.steers} steers · ${c.rounds} review rounds`}>{usd(c.cost_usd)}</span>
                  </li>
                ))}
              </ul>
            ) : <Empty testId="sc-costing-none">Nothing stands out</Empty>}
          </section>
        </>
      )}
    </>
  );
}

function DiffChart({ e }: { e: Experiment }) {
  const c = e.compare;
  if (c.low === null || c.high === null || c.diff === null) return null;
  const span = Math.max(0.2, Math.abs(c.low), Math.abs(c.high)) * 1.15;
  const x = (v: number) => 50 + (v / span) * 48;
  return (
    <svg className="im-diff" viewBox="0 0 100 26" preserveAspectRatio="none" role="img"
         aria-label={`B minus A: ${c.diff.toFixed(2)}, 95% interval ${c.low.toFixed(2)} to ${c.high.toFixed(2)}`}>
      <line className="im-diff-axis" x1={2} x2={98} y1={13} y2={13} />
      <line className="im-diff-zero" x1={50} x2={50} y1={3} y2={23} />
      <line className="im-diff-ci" x1={x(c.low)} x2={x(c.high)} y1={13} y2={13} />
      <circle className="im-diff-dot" cx={x(c.diff)} cy={13} r={3} />
    </svg>
  );
}

const STOP: Record<string, string> = {
  both_arms_capped: "Both arms hit their cost cap", arm_a_fails_gates: "Arm A failed gates more often",
  arm_b_fails_gates: "Arm B failed gates more often", reached_units: "Reached its units",
  stopped_by_operator: "Stopped by a person",
};

function PromoteSheet({ e, arm, projectId, actor, onClose, onDone }: {
  e: Experiment; arm: "a" | "b"; projectId: string; actor: string; onClose: () => void; onDone: () => void;
}) {
  const plan = e.promote?.[arm];
  const [anyway, setAnyway] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const go = async () => {
    setBusy(true);
    const r = await improveWrite("experiment_promote", { project_id: projectId, actor, id: e.id, arm, anyway });
    setBusy(false);
    if (r.ok) { showToast("success", "Winner promoted"); onDone(); onClose(); }
    else setError(r.status === 403 ? "You can't change this" : r.error);
  };
  const ready = Boolean(plan?.enough) || anyway;
  return (
    <Sheet title={`Promote arm ${arm.toUpperCase()}`} testId="sc-promote-sheet" onClose={onClose}>
      <div className="im-share">
        <span className="im-check" data-testid="sc-promote-change">
          <b>{plan?.label ?? e.knob_label}</b>
          <span data-user-text="">{String(plan?.from ?? "default")} → {String(plan?.to ?? "")}</span>
        </span>
        <span className="im-muted">This project&apos;s setting · in Govern changes</span>
        {plan && !plan.enough ? (
          <>
            <span className="im-err" role="status" data-testid="sc-promote-short" title={plan.why_not}>Not enough evidence</span>
            <label className="im-muted">
              <input type="checkbox" checked={anyway} onChange={(ev) => setAnyway(ev.target.checked)}
                     data-testid="sc-promote-anyway" /> Promote anyway
            </label>
          </>
        ) : null}
        <span className="im-actions">
          <GatedButton permission="improvement:manage" type="button" className="ag-btn ag-btn-primary" disabled={busy || !ready}
                  data-testid="sc-promote-yes" onClick={() => void go()}>{busy ? "Promoting…" : "Promote"}</GatedButton>
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          {error ? <span className="im-err" role="alert" title={error}>Not promoted</span> : null}
        </span>
      </div>
    </Sheet>
  );
}

function ExperimentCard({ e, projectId, actor, onChange }: { e: Experiment; projectId: string; actor: string; onChange: () => void }) {
  const [busy, setBusy] = useState("");
  const [promoting, setPromoting] = useState<"a" | "b" | null>(null);
  const act = async (op: "experiment_stop" | "experiment_promote", arm?: "a" | "b") => {
    if (op === "experiment_promote") { setPromoting(arm ?? "a"); return; }
    setBusy(arm ?? "stop");
    const r = await improveWrite(op, { project_id: projectId, actor, id: e.id, arm });
    setBusy("");
    if (r.ok) { showToast("success", op === "experiment_stop" ? "Experiment stopped" : "Winner promoted"); onChange(); }
    else showToast("error", r.status === 403 ? "You can't change this" : r.error);
  };
  const running = e.status === "running";
  const word = { running: "Running", stopped: "Stopped", finished: "Finished", promoted: "Promoted" }[e.status] ?? e.status;
  return (
    <article className="im-exp" data-testid="sc-exp">
      <header className="im-exp-head">
        <h4 className="im-exp-name" data-user-text="">{e.name}</h4>
        <span className="im-status" data-tone={running ? "green" : e.status === "promoted" ? "green" : "amber"}
              title={e.stop_reason ? STOP[e.stop_reason] ?? e.stop_reason : undefined}>{word}</span>
        <span className="im-verdict" data-tone={e.compare.verdict === "a" || e.compare.verdict === "b" ? "green" : undefined}
              title={e.compare.why} data-testid="sc-exp-verdict">{e.compare.label}</span>
        <span className="ag-spacer" />
        {running ? <GatedButton permission="improvement:manage" type="button" className="ag-btn" disabled={busy !== ""} onClick={() => void act("experiment_stop")}>Stop</GatedButton> : null}
      </header>
      <div className="im-arms">
        {(["a", "b"] as const).map((arm) => {
          const a = e.arms[arm];
          const winner = e.compare.verdict === arm || e.promoted_arm === arm;
          return (
            <div key={arm} className="im-arm" data-winner={winner ? "" : undefined} data-testid={`sc-arm-${arm}`}>
              <span className="im-arm-k">ARM {arm.toUpperCase()}{arm === "b" ? ` · ${Math.round(e.split_b * 100)}%` : ""}</span>
              <span className="im-arm-v" title={e.knob_label}>{(arm === "a" ? e.arm_a_label : e.arm_b_label) ?? String(arm === "a" ? e.arm_a : e.arm_b)}</span>
              <span className="im-arm-facts">
                <span title="Finished units">n {a.n}</span>
                <span title="Mean score ± one standard deviation">{num(a.mean)} ± {num(a.sd)}</span>
                <span title={`Cost cap $${e.arm_cost_cap_usd}`}>{usd(a.cost_usd)} / ${e.arm_cost_cap_usd}</span>
                <span title="Share that failed a gate or did not complete">fail {pct(a.gate_fail)}</span>
                {e.capped_arms.includes(arm) ? <span className="obs-bad">Capped</span> : null}
              </span>
              {e.status !== "promoted" ? (
                <GatedButton permission="improvement:manage" type="button" className={`ag-btn${winner ? " ag-btn-primary" : ""}`} disabled={busy !== ""}
                        data-testid={`sc-promote-${arm}`} title="Makes this the project's setting; recorded in Govern changes"
                        onClick={() => void act("experiment_promote", arm)}>Promote {arm.toUpperCase()}</GatedButton>
              ) : null}
            </div>
          );
        })}
      </div>
      <DiffChart e={e} />
      {promoting ? <PromoteSheet e={e} arm={promoting} projectId={projectId} actor={actor}
                                 onClose={() => setPromoting(null)} onDone={onChange} /> : null}
    </article>
  );
}

function NewExperiment({ view, projectId, actor, onDone }: { view: ExperimentsView; projectId: string; actor: string; onDone: () => void }) {
  const [f, setF] = useState({ target_kind: "role", target_key: "software_engineer", knob: "harness",
                               arm_a: "claude_code", arm_b: "codex", split: "50", units: "20", cap: "25" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const knob = view.knobs.find((k) => k.key === f.knob);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    const r = await improveWrite("experiment", {
      project_id: projectId, actor, target_kind: f.target_kind, target_key: f.target_key.trim(), knob: f.knob,
      arm_a: knob?.kind === "int" ? Number(f.arm_a) : f.arm_a, arm_b: knob?.kind === "int" ? Number(f.arm_b) : f.arm_b,
      split_b: Number(f.split) / 100, target_units: Number(f.units), arm_cost_cap_usd: Number(f.cap), name: "",
    });
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Experiment started");
    onDone();
  };
  const armInput = (k: "arm_a" | "arm_b", label: string) => knob?.choices ? (
    <label>{label}<select value={f[k]} onChange={set(k)} data-testid={`sc-new-${k}`}>
      {knob.choices.map((c) => <option key={c} value={c}>{HARNESS_NAMES[c] ?? c}</option>)}</select></label>
  ) : (
    <label>{label}<input type="number" min={knob?.min ?? 0} max={knob?.max ?? 5} value={f[k]} onChange={set(k)} data-testid={`sc-new-${k}`} /></label>
  );
  return (
    <form className="im-form" onSubmit={submit} data-testid="sc-new">
      <label title="Builds run as this agent, or inside runs of this workflow">Target
        <select value={f.target_kind} onChange={(ev) => {
          const kind = ev.target.value as "role" | "workflow";
          setF({ ...f, target_kind: kind, target_key: view.targets?.[kind]?.[0]?.key ?? "" });
        }}><option value="role">Agent</option><option value="workflow">Workflow</option></select></label>
      <label>{f.target_kind === "role" ? "Agent" : "Workflow"}
        <select value={f.target_key} onChange={set("target_key")} data-testid="sc-new-key">
          {(view.targets?.[f.target_kind as "role" | "workflow"] ?? [{ key: f.target_key, name: f.target_key }])
            .map((t) => <option key={t.key} value={t.key}>{t.name}</option>)}
        </select></label>
      <label title="Only settings that never change what needs approval">Vary
        <select value={f.knob} onChange={(e) => {
          const k = view.knobs.find((x) => x.key === e.target.value);
          setF({ ...f, knob: e.target.value, arm_a: k?.choices?.[0] ?? "2", arm_b: k?.choices?.[1] ?? "3" });
        }} data-testid="sc-new-knob">{view.knobs.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}</select></label>
      {armInput("arm_a", "Arm A")}
      {armInput("arm_b", "Arm B")}
      <label title="Share of new builds that get arm B">Split B %<input type="number" min={5} max={95} value={f.split} onChange={set("split")} /></label>
      <label title="Stops when this many have finished">Units<input type="number" min={2} max={1000} value={f.units} onChange={set("units")} /></label>
      <label title="An arm over this takes no more builds">Cap per arm $<input type="number" min={1} value={f.cap} onChange={set("cap")} /></label>
      <span className="im-form-end">
        <GatedButton permission="improvement:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="sc-new-start">Start</GatedButton>
        {error ? <span className="im-err" role="alert" title={error}>Not started</span> : null}
      </span>
    </form>
  );
}

function Experiments({ projectId, actor }: { projectId: string; actor: string }) {
  const live = useConsoleLive();
  const read = usePolledRead<ExperimentsView>({
    key: projectId, pollMs: 60_000, paused: !live, read: polledImprove<ExperimentsView>("experiments", { project_id: projectId }),
  });
  const [adding, setAdding] = useState(false);
  const v = read.data;
  if (read.error && !v) return <Empty testId="sc-exp-error"><span title={read.error}>Can&apos;t reach BOSS</span></Empty>;
  if (!v) return <Skeleton testId="sc-exp-loading" />;
  return (
    <section className="obs-zone" aria-labelledby="sc-exp-head">
      <ZoneHead id="sc-exp-head" label="EXPERIMENTS" count={v.items.filter((e) => e.status === "running").length}
                tip="Off until you start one. Never changes what needs approval.">
        <GatedButton permission="improvement:manage" type="button" className="ag-btn" data-testid="sc-exp-add" aria-expanded={adding}
                onClick={() => setAdding((a) => !a)}>{adding ? "Cancel" : "New experiment"}</GatedButton>
      </ZoneHead>
      {adding ? <NewExperiment view={v} projectId={projectId} actor={actor} onDone={() => { setAdding(false); read.reload(); }} /> : null}
      {v.items.length ? v.items.map((e) => <ExperimentCard key={e.id} e={e} projectId={projectId} actor={actor} onChange={read.reload} />)
        : !adding ? <Empty testId="sc-exp-none">No experiments yet</Empty> : null}
    </section>
  );
}

export function ScorecardsTab({ projectId, actor }: ObsTabProps) {
  const [u, reread] = useUrlState(readScUrl);
  const set = (patch: Record<string, string | null>) => { writeUrl(patch, "replace"); reread(); };
  return (
    <div className="im-sc" data-testid="sc">
      <div className="im-controls">
        <Seg items={[{ key: "scores", label: "Scores" }, { key: "experiments", label: "Experiments", tip: "A/B a harness or review rounds" }]}
             value={u.mode} label="View" testId="sc-mode" onPick={(k) => set({ sc: k === "scores" ? null : k })} />
        {u.mode === "scores" ? (
          <>
            <Seg items={KINDS} value={u.kind} label="Unit" testId="sc-kind" onPick={(k) => set({ kind: k === "build" ? null : k })} />
            <Seg items={GROUPS} value={u.by} label="Group by" testId="sc-by" onPick={(k) => set({ by: k === "role" ? null : k })} />
            <Seg items={WINS.map((w) => ({ key: w, label: w }))} value={u.win} label="Window" testId="sc-win"
                 onPick={(k) => set({ swin: k === "30d" ? null : k })} />
          </>
        ) : null}
        <span className="ag-spacer" />
        <a className="inf-link" href="/?area=budget&tab=sharing" title="See and change the score's weights">Weights ↗</a>
      </div>
      {u.mode === "scores" ? <Scores projectId={projectId} actor={actor} u={u} /> : <Experiments projectId={projectId} actor={actor} />}
    </div>
  );
}
