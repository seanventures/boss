"use client";

/**
 * Observability › System (spec §4.11; the stability work's real data): BOSS's own machine.
 *
 *   BOSS MACHINE      build 4c1d2e9 · 2h          This BOSS container's memory cap ⓘ
 *    Memory ███████░░ 78% High   CPU ██░░ 0.41/core OK   Disk ████░ 44% OK   Swap 0 MB OK
 *    New OOM kills 0
 *   WORK   Queue 3 · Leased 2 · Running 2 · In flight 4                      Command Center ↗
 *   STALLS · 24H 2   ▯▯▯▮▯▯▯▯▯▮▯   + the 8 newest
 *   FINDINGS 2       AttentionCards (system_health) — options, confirm and act are the card's
 *   WHAT BOSS DID    the system doctor's recent actions
 *
 * Every number is a reading the server sends: a gauge draws a bar only where a percent (or load
 * per core against 1.0) exists, and says `Not measurable here` where the machine gave nothing.
 * The gauges are measured inside the BOSS container — never your deploy hosts.
 */
import React, { useEffect, useRef, useState } from "react";

import type { Band, System } from "../../lib/obs-view";
import { buildHref } from "../../lib/infra-view";
import { AttentionCard } from "../agents/attention-card";
import { ago, plainClick, useNow } from "../agents/live-util";
import type { ObsTabProps } from "./obs-home";
import { leaveTo } from "./obs-util";
import { Empty, Skeleton, TableToggle, ZoneHead } from "../infra/kit";

// ── a gauge ─────────────────────────────────────────────────────────────────────────────────

/** The bar's fill as a share 0..1 — only from a real percent, or load per core against 1.0. */
export function gaugeShare(kind: "memory" | "cpu" | "disk" | "swap", b: Band | null): number | null {
  if (!b || b.value === null) return null;
  if (kind === "cpu") return Math.max(0, Math.min(1, b.value));
  if (b.unit === "%") return Math.max(0, Math.min(1, b.value / 100));
  return null;
}
export function gaugeValue(kind: "memory" | "cpu" | "disk" | "swap", b: Band): string {
  if (b.value === null) return "—";
  if (kind === "cpu") return `${b.value.toFixed(2)}/core`;
  if (b.unit === "%") return `${Math.round(b.value)}%`;
  return `${Math.round(b.value).toLocaleString("en-US")} ${b.unit || "MB"}`;
}

const GAUGES: { kind: "memory" | "cpu" | "disk" | "swap"; label: string }[] = [
  { kind: "memory", label: "Memory" }, { kind: "cpu", label: "CPU" }, { kind: "disk", label: "Disk" }, { kind: "swap", label: "Swap" },
];

function Gauge({ kind, label, b }: { kind: "memory" | "cpu" | "disk" | "swap"; label: string; b: Band | null }) {
  if (!b || b.value === null) {
    return (
      <div className="obs-gauge" data-testid={`obs-gauge-${kind}`} data-absent="1">
        <span className="obs-gauge-k">{label}</span>
        <span className="inf-faint" title={b?.tip ?? "This machine gave no reading for it"} data-testid={`obs-gauge-${kind}-absent`}>Not measurable here</span>
      </div>
    );
  }
  const share = gaugeShare(kind, b);
  return (
    <div className="obs-gauge" data-testid={`obs-gauge-${kind}`} data-tone={b.tone}>
      <span className="obs-gauge-k">{label}</span>
      {share !== null ? (
        <svg className="obs-gauge-bar" viewBox="0 0 100 6" preserveAspectRatio="none" role="img"
             aria-label={`${label} ${gaugeValue(kind, b)}${kind === "cpu" ? " of one core" : ""}`}>
          <rect className="obs-gauge-track" x={0} y={0} width={100} height={6} rx={3} />
          <rect className="obs-gauge-fill" data-tone={b.tone} x={0} y={0} width={Math.max(1.5, share * 100)} height={6} rx={3} />
        </svg>
      ) : null}
      <span className="obs-gauge-v inf-num">{gaugeValue(kind, b)}</span>
      <span className="obs-gauge-word" data-tone={b.tone} title={b.tip}>{b.word}</span>
    </div>
  );
}

/** Is that area on this console's rail (the edition has it)? */
function useOnRail(area: string): boolean {
  const [on, setOn] = useState(false);
  useEffect(() => { setOn(Boolean(document.querySelector(`[data-area-nav="${area}"]`))); }, [area]);
  return on;
}

// ── stalls ──────────────────────────────────────────────────────────────────────────────────

function StallStrip({ strip, fromMs }: { strip: number[]; fromMs: number }) {
  const [table, setTable] = useState(false);
  const n = Math.max(1, strip.length);
  const W = n * 10;
  const cellMs = (24 * 3600_000) / n;
  const hhmm = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const total = strip.reduce((a, b) => a + b, 0);
  return (
    <div className="obs-stallstrip">
      <div className="obs-chart-bar">
        <svg className="obs-cells" viewBox={`0 0 ${W} 12`} preserveAspectRatio="none" role="img" data-testid="obs-stall-strip"
             aria-label={`Stalls, 24 hours: ${total} in ${strip.filter((x) => x > 0).length} periods`}>
          {strip.map((c, i) => (
            <rect key={i} className="obs-stallcell" data-k={c > 0 ? "stall" : "none"} x={i * 10 + 1} y={c > 0 ? 0 : 1} width={8} height={c > 0 ? 12 : 10} rx={1.5}
                  vectorEffect="non-scaling-stroke">
              <title>{`${hhmm(fromMs + i * cellMs)} · ${c ? `${c} ${c === 1 ? "stall" : "stalls"}` : "No stalls"}`}</title>
            </rect>
          ))}
        </svg>
        <TableToggle on={table} onToggle={() => setTable((v) => !v)} testId="obs-stall-table-toggle" />
      </div>
      {table ? (
        <table className="inf-table" data-testid="obs-stall-table">
          <thead><tr><th scope="col">From</th><th scope="col">Stalls</th></tr></thead>
          <tbody>{strip.map((c, i) => <tr key={i}><td>{hhmm(fromMs + i * cellMs)}</td><td>{c}</td></tr>)}</tbody>
        </table>
      ) : null}
    </div>
  );
}

function Stalls({ s, now }: { s: System; now: number | null }) {
  const fromMs = (now ?? Date.now()) - 24 * 3600_000;
  return (
    <section className="obs-zone" aria-labelledby="obs-stalls-head" data-testid="obs-stalls">
      <ZoneHead id="obs-stalls-head" label="STALLS · 24H" count={s.stalls.total} />
      <StallStrip strip={s.stalls.strip} fromMs={fromMs} />
      {s.stalls.rows.length === 0 ? <Empty testId="obs-stalls-none">No stalls</Empty> : (
        <ul className="obs-list">
          {s.stalls.rows.map((r, i) => (
            <li key={`${r.at}-${i}`} className="obs-line" data-testid="obs-stall-row">
              <span className="inf-num inf-muted" title={new Date(r.at).toLocaleString()}>
                {new Date(r.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}
              </span>
              <span className="obs-strong">{r.word}</span>
              {r.subject ? <span className="inf-trunc inf-muted" data-copy="data" data-user-text>{r.subject}</span> : null}
              {r.did ? (r.shadow
                ? <span className="inf-faint" title="Watching only. Nothing was changed." data-testid="obs-stall-shadow">would {r.did} (shadow)</span>
                : <span className="inf-muted">{r.did} done</span>) : null}
              <span className="ag-spacer" />
              {r.runId ? (
                <a className="inf-link" href={buildHref(r.runId)} title="Open the build that made this"
                   onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(buildHref(r.runId!)); } }}>Build ↗</a>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ── the tab ─────────────────────────────────────────────────────────────────────────────────

export function SystemTab({ projectId, actor, orgId, system }: ObsTabProps) {
  const s = system.data;
  const now = useNow(30_000);
  const shepherd = useOnRail("shepherd");
  const sandboxes = useOnRail("sandboxes");
  // A finding enters with motion once, when its id is new since the last poll (§5.2).
  const seen = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!s) return;
    const ids = s.findings.map((f) => f.id);
    if (seen.current) setFresh(new Set(ids.filter((id) => !seen.current!.has(id))));
    seen.current = new Set(ids);
  }, [s]);

  if (system.loading && !s) return <Skeleton testId="obs-system-loading" />;
  if (!s) return <Empty testId="obs-system-error">Can&apos;t read BOSS&apos;s machine</Empty>;
  const m = s.machine;
  const scopeTip = m.scopeLabel
    ? `${m.scopeLabel}. Measured inside BOSS, not your deploy hosts`
    : "Measured inside the BOSS container, not your deploy hosts";

  return (
    <div className="obs-system" data-testid="obs-system">
      {s.partial.length ? (
        <div className="inf-partial" role="status" data-testid="obs-system-partial" title={`Not answered: ${s.partial.join(", ")}`}>
          Some checks didn&apos;t run
        </div>
      ) : null}

      <section className="obs-zone obs-machine" aria-labelledby="obs-machine-head" data-testid="obs-machine">
        <ZoneHead id="obs-machine-head" label="BOSS MACHINE">
          {s.build.sha7 ? (
            <span className="inf-faint inf-num" title={s.build.builtAt ? `Built ${new Date(s.build.builtAt).toLocaleString()}` : undefined} data-testid="obs-build">
              build <span className="inf-mono" data-copy="data">{s.build.sha7}</span>{s.build.builtAt && now !== null ? ` · ${ago(s.build.builtAt, now)}` : ""}
            </span>
          ) : null}
          <span className="obs-scope" title={scopeTip} data-testid="obs-machine-scope">
            <span className="inf-trunc" data-copy="data">{m.scopeLabel ?? "BOSS container"}</span>
            <span aria-hidden className="obs-info">ⓘ</span>
          </span>
        </ZoneHead>
        <div className="obs-gauges">
          {GAUGES.map((g) => <Gauge key={g.kind} kind={g.kind} label={g.label} b={m.bands[g.kind]} />)}
        </div>
        {s.oomKillsNew !== null ? (
          <span className="obs-line inf-num" data-testid="obs-oom" title="Processes the kernel killed for memory since the last reading">
            <span className="inf-muted">New OOM kills</span>
            <span className={s.oomKillsNew > 0 ? "obs-bad obs-strong" : "obs-strong"}>{s.oomKillsNew}</span>
          </span>
        ) : null}
      </section>

      <section className="obs-zone" aria-labelledby="obs-work-head" data-testid="obs-work">
        <ZoneHead id="obs-work-head" label="WORK">
          <a className="inf-link" href="?area=overview" onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo("?area=overview"); } }}>Command Center ↗</a>
        </ZoneHead>
        <span className="obs-line obs-work inf-num">
          <span><span className="inf-muted">Queue</span> {s.work.queue}</span>
          <span><span className="inf-muted">Leased</span> {s.work.leased}</span>
          <span><span className="inf-muted">Running</span> {s.work.running}</span>
          <span><span className="inf-muted">In flight</span> {s.work.inFlight}</span>
          {s.work.buildsAtOnce ? (
            // The admission label is a sentence ("Builds at once: 2 (auto) · 1 queued…"): the
            // line names the setting once and shows its value.
            <span title={s.work.buildsAtOnce}><span className="inf-muted">At once</span>{" "}
              {s.work.buildsAtOnce.replace(/^Builds at once:\s*/i, "").split(" · ")[0]}</span>
          ) : null}
        </span>
      </section>

      <Stalls s={s} now={now} />

      <section className="obs-zone" aria-labelledby="obs-findings-head" data-testid="obs-findings">
        <ZoneHead id="obs-findings-head" label="FINDINGS" count={s.findings.length} />
        {s.findings.length === 0 ? <Empty testId="obs-findings-none">Nothing found</Empty> : (
          <div className="obs-findings">
            {s.findings.map((f) => (
              <div key={f.id} className="obs-finding" data-new={fresh.has(f.id) ? "1" : undefined} data-anim={fresh.has(f.id) ? "1" : undefined}
                   data-testid="obs-finding">
                <AttentionCard item={f} projectId={projectId} actor={actor} orgId={orgId ?? undefined}
                               onRefresh={system.refresh} onDone={system.refresh} />
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="obs-zone" aria-labelledby="obs-did-head" data-testid="obs-actions">
        <ZoneHead id="obs-did-head" label="WHAT BOSS DID" count={s.actions.length} />
        {s.actions.length === 0 ? <Empty>Nothing yet</Empty> : (
          <ul className="obs-list">
            {s.actions.map((a, i) => (
              <li key={`${a.at}-${i}`} className="obs-line" data-testid="obs-action-row">
                <span className="inf-num inf-muted" title={new Date(a.at).toLocaleString()}>{ago(a.at, now) || "—"}</span>
                <span className="obs-strong">{a.action}</span>
                {a.target ? <span className="inf-mono inf-faint inf-trunc" data-copy="data">{a.target}</span> : null}
                {a.outcome ? <span className="inf-muted inf-trunc" data-copy="data">{a.outcome}</span> : null}
                <span className="ag-spacer" />
                {a.improved === true ? <span className="obs-ok" title="The reading improved after">Helped</span> : null}
                {a.improved === false ? <span className="obs-bad" title="The reading did not improve">No change</span> : null}
                <span className="obs-chip" title={a.automatic ? "BOSS did this on its own" : "A person chose this"}>{a.automatic ? "Auto" : "Chosen"}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="obs-line obs-foot">
        {s.advice ? (
          <span className="obs-advice" data-testid="obs-system-advice" title={s.advice.tip}>
            <span className="obs-strong" data-copy="data">{s.advice.title}</span>
            {s.advice.href ? (
              <a className="inf-link" href={s.advice.href} onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(s.advice!.href!); } }}>Open</a>
            ) : null}
          </span>
        ) : null}
        <span className="ag-spacer" />
        {sandboxes ? <a className="inf-link" href="?area=sandboxes" onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo("?area=sandboxes"); } }}>Sandboxes ↗</a> : null}
        {shepherd ? <a className="inf-link" href="?area=shepherd" onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo("?area=shepherd"); } }}>Shepherd ↗</a> : null}
      </div>
    </div>
  );
}
