"use client";

/**
 * S3 · STORAGE under Govern › Infrastructure (BOSSAPP-1156): what takes the room, where it is
 * heading, and what keeps it tidy. Four tiles (Free now · Growth · Full in · Last cleanup), the
 * used-by-type bar, the 30-day trend with the two forecasts (if nothing changed / with the
 * retention rules) and the "full" line, the thresholds, the retention rules, every host.
 *
 * It is loud only when it should be: green above the alert, amber under 20 % (the quiet notice),
 * red under 15 % and at the 10 % hard floor. Read from GET /v1/infra/storage/summary every 60 s
 * (docs/infra/INFRA_AGENT_API.md §4); hand-drawn SVG like observability/spark.tsx. Tokens only.
 */
import React, { useId, useState } from "react";
import { ArrowRight, Bell, Broom, Clock, Cpu, Database, HardDrives, Laptop, Shield, TrendUp, Warning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  CLEAN_PERMISSION, RULES_PERMISSION, cleanNow, daysWord, fullInDays, gb, reportPatch, setRuleEnabled, storageTone, useInfraIncident, useStorageSummary, whenWord,
  type HostStorageRow, type StorageSummary,
} from "../../lib/infra-storage";
import { showToast } from "../client-forms";
import { StripLink } from "./infra-strip-banner";

const TYPE_TONES: Record<string, string> = {
  backups: "red", images: "amber", pg_db: "blue", database: "blue", build_cache: "violet", caches: "violet", pg_temp: "violet",
  volumes: "indigo", logs: "grey", other: "teal", docker_vm_unreclaimed: "grey",
};
const HOST_ICON = (h: HostStorageRow) => (/laptop|mac/i.test(h.label) ? <Laptop size={14} aria-hidden /> : /kvm/i.test(h.label) ? <Cpu size={14} aria-hidden /> : <HardDrives size={14} aria-hidden />);

/** The stacked bar's segments as percentages of the disk; `free` is drawn striped by the bar. */
export function segments(s: Pick<StorageSummary, "byType" | "totalGb">): { key: string; label: string; gb: number; pct: number }[] {
  if (!(s.totalGb > 0)) return [];
  return s.byType.filter((t) => t.gb > 0 && t.key !== "free").map((t) => ({ key: t.key, label: t.label, gb: t.gb, pct: Math.max(0.4, (t.gb / s.totalGb) * 100) }));
}

/** An SVG path over `values`, y scaled from `lo` (the chart's floor) to `total` (the disk, the
 *  "full" line at the top), x from `x0` to `x1`. */
export function linePath(values: number[], total: number, x0: number, x1: number, h: number, lo = 0): string {
  if (values.length < 2 || !(total > lo)) return "";
  const step = (x1 - x0) / (values.length - 1);
  const span = total - lo;
  return values.map((v, i) => `${i ? "L" : "M"}${(x0 + i * step).toFixed(1)},${(h - ((Math.max(lo, Math.min(v, total)) - lo) / span) * (h - 6) - 3).toFixed(1)}`).join("");
}

/** The chart's floor: a little under the lowest reading, never below zero, so the month's growth
 *  is a visible slope and "full" stays the top line. */
export function chartFloor(values: number[], total: number): number {
  const finite = values.filter((v) => Number.isFinite(v));
  if (!finite.length) return 0;
  const lo = Math.min(...finite);
  return Math.max(0, Math.floor(lo - (total - lo) * 0.6));
}

/** The two forecasts from today, 30 days out: straight on at today's growth; and at the growth
 *  the rules leave (free / with_rules_days). */
export function forecastLines(s: Pick<StorageSummary, "trend" | "totalGb" | "freeGb" | "growthGbPerDay" | "withRulesDays">): { flat: number[]; rules: number[] } {
  const last = s.trend.length ? s.trend[s.trend.length - 1].usedGb : s.totalGb - s.freeGb;
  const flat = Array.from({ length: 30 }, (_, i) => Math.min(s.totalGb, last + Math.max(0, s.growthGbPerDay) * i));
  const slope = s.withRulesDays !== null && s.withRulesDays > 0 ? s.freeGb / s.withRulesDays : 0;
  const rules = Array.from({ length: 30 }, (_, i) => Math.min(s.totalGb, last + slope * i));
  return { flat, rules };
}

function TrendForecast({ s }: { s: StorageSummary }) {
  const W = 600, H = 150, MID = 320;
  const usedValues = s.trend.map((p) => p.usedGb);
  const { flat, rules } = forecastLines(s);
  const lo = chartFloor([...usedValues, ...rules], s.totalGb);
  const used = linePath(usedValues, s.totalGb, 20, MID, H, lo);
  const flatPath = linePath(flat, s.totalGb, MID, W - 20, H, lo);
  const rulesPath = linePath(rules, s.totalGb, MID, W - 20, H, lo);
  const said = `Used ${gb(s.totalGb - s.freeGb)} of ${gb(s.totalGb)} over 30 days, growing ${gb(s.growthGbPerDay)} a day; ${s.fullInDays !== null ? `full in ${daysWord(s.fullInDays)} if nothing changed` : "not filling"}; with the retention rules ${s.withRulesDays !== null ? `full in ${daysWord(s.withRulesDays)}` : "it stays level"}.`;
  return (
    <figure className="sc-trend" data-testid="sc-trend">
      <figcaption className="sc-trend-head">
        <h3>Trend and forecast</h3>
        <span className="sc-legend">
          <span data-line="used">used</span>
          <span data-line="flat">if nothing changed</span>
          <span data-line="rules">with retention rules</span>
        </span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={said} className="sc-svg">
        <line className="sc-full" x1={20} x2={W - 20} y1={3} y2={3} />
        <line className="sc-today" x1={MID} x2={MID} y1={0} y2={H} />
        {used ? <path className="sc-used" d={used} vectorEffect="non-scaling-stroke" /> : null}
        {flatPath ? <path className="sc-flat" d={flatPath} vectorEffect="non-scaling-stroke" /> : null}
        {rulesPath ? <path className="sc-rules" d={rulesPath} vectorEffect="non-scaling-stroke" /> : null}
      </svg>
      <span className="sc-axis"><span>{s.trend.length ? "30 days ago" : "no readings yet"}</span><span>today</span><span>full · {gb(s.totalGb)}</span></span>
    </figure>
  );
}

function HostRow({ h, active, onPick }: { h: HostStorageRow; active: boolean; onPick: () => void }) {
  const tone = h.level === "critical" || h.level === "floor" ? "red" : h.level === "warn" ? "amber" : "green";
  const W = 90, H = 22;
  const vals = h.spark.length ? h.spark : [h.freePct];
  const max = Math.max(100, ...vals);
  const step = vals.length > 1 ? W / (vals.length - 1) : 0;
  const d = vals.map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${(H - 1 - (v / max) * (H - 2)).toFixed(1)}`).join("");
  return (
    <li className="sc-host" data-active={active || undefined} data-tone={tone} data-testid="sc-host">
      <button type="button" className="sc-host-name" onClick={onPick} aria-pressed={active} title={`Show ${h.label}`}>
        {HOST_ICON(h)}<code>{h.label}</code>
      </button>
      <svg className="sc-host-bar" viewBox="0 0 100 1" preserveAspectRatio="none" role="img" aria-label={`${h.freePct} % free`}><rect x="0" y="0" width={Math.max(0, Math.min(100, h.freePct))} height="1" /></svg>
      <span className="sc-host-pct">{Math.round(h.freePct)} % free</span>
      <span className="ia-chip" data-tone={tone}><Clock size={11} aria-hidden />{daysWord(h.fullInDays)}</span>
      <svg className="sc-host-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden><path d={d} vectorEffect="non-scaling-stroke" /></svg>
    </li>
  );
}

export function StorageCardView({ s, projectId, orgId, sample, pendingDecisionId, onHost, onClean, onRule, cleaning }: {
  s: StorageSummary; projectId: string; orgId?: string | null; sample?: boolean;
  /** The open incident's tier 3 question, if it is waiting. */
  pendingDecisionId?: string | null;
  onHost: (hostId: string) => void; onClean: () => void; onRule: (rule: StorageSummary["rules"][number]) => void; cleaning: boolean;
}) {
  const tone = s.level === "floor" || s.level === "critical" ? "red" : s.level === "warn" ? "amber" : storageTone(s.freePct, s.thresholds);
  const days = fullInDays(s.freeGb, s.growthGbPerDay, s.fullInDays);
  const segs = segments(s);
  const atFloor = s.level === "floor" || s.freePct <= s.thresholds.floorPct;
  const id = useId();
  const pendingLink = pendingDecisionId ? (
    <StripLink patch={{ area: "decisions", decision: pendingDecisionId, decision_filter: "needs_action", role: null }} orgId={orgId} className="sc-link" testId="sc-pending">
      1 approval waiting <ArrowRight size={11} aria-hidden /></StripLink>
  ) : null;
  return (
    <div className="sc" data-testid="storage-card" data-tone={tone}>
      <header className="sc-head">
        <span className="sc-crumb">Govern <span aria-hidden>›</span> Infrastructure</span>
        <span className="sc-title-row">
          <span>
            <h2 className="sc-title">Storage</h2>
            <div className="sc-sub">What takes the room, where it is heading, and what keeps it tidy.</div>
          </span>
          <span className="sc-spacer" />
          <label className="sc-host-pick" title="Which machine">
            <span className="ag-sr">Host</span>
            <select className="input" value={s.hostId} onChange={(e) => onHost(e.target.value)} data-testid="sc-host-select">
              {(s.hosts.some((h) => h.hostId === s.hostId) ? s.hosts : [{ hostId: s.hostId, label: s.hostLabel }, ...s.hosts]).map((h) => <option key={h.hostId} value={h.hostId}>{h.label}</option>)}
            </select>
          </label>
          <GatedButton permission={CLEAN_PERMISSION} projectId={projectId} className="sc-btn sc-btn-primary" data-testid="sc-clean"
                       disabled={cleaning} onClick={onClean} title="Tier 1 now: caches, old images, temp files, orphan volumes, logs. Dry run first, nothing paused.">
            <Broom size={14} weight="bold" aria-hidden /> Clean now
          </GatedButton>
          {sample ? <span className="ia-sample">Sample data</span> : null}
        </span>
      </header>

      {tone !== "green" ? (
        <div className="sc-alert" role="status" data-tone={tone} data-testid="sc-alert">
          {tone === "red" ? <Warning size={14} weight="bold" aria-hidden /> : <Bell size={14} weight="bold" aria-hidden />}
          {atFloor ? <><b>{Math.round(s.freePct)} % free, at the {s.thresholds.floorPct} % hard floor.</b> Builds and upgrades wait until the Infra Agent frees space.</>
            : tone === "red" ? <><b>{Math.round(s.freePct)} % free, {s.freePct < s.thresholds.criticalPct ? "under" : "at"} the {s.thresholds.criticalPct} % critical line.</b> The Infra Agent is on it; see the Command Center.</>
            : <><b>{Math.round(s.freePct)} % free, {s.freePct < s.thresholds.warnPct ? "under" : "at"} the {s.thresholds.warnPct} % notice.</b> The Infra Agent runs the safe cleanup early; nothing is paused.</>}
          {pendingLink}
        </div>
      ) : pendingDecisionId ? (
        <div className="sc-alert" role="status" data-tone="amber" data-testid="sc-alert"><Bell size={14} weight="bold" aria-hidden /><b>A deletion is waiting for you.</b>{pendingLink}</div>
      ) : null}

      <div className="sc-tiles" data-testid="sc-tiles">
        <div className="sc-tile" data-tone={tone} data-testid="sc-tile-free">
          <span className="sc-tile-label"><HardDrives size={13} aria-hidden /> Free now</span>
          <b className="sc-tile-n">{Math.round(s.freePct)} %</b>
          <span className="sc-tile-sub">{gb(s.freeGb)} of {gb(s.totalGb)}{s.stale ? <> · <span className="sc-stale">reading is over 2 min old</span></> : s.sampledAt ? ` · ${whenWord(s.sampledAt)}` : ""}</span>
        </div>
        <div className="sc-tile" data-tone={s.growthGbPerDay > 0 ? "amber" : "green"} data-testid="sc-tile-growth">
          <span className="sc-tile-label"><TrendUp size={13} aria-hidden /> Growth</span>
          <b className="sc-tile-n">{s.growthGbPerDay > 0 ? "+" : ""}{s.growthGbPerDay.toFixed(1)} GB/day</b>
          <span className="sc-tile-sub">{s.windowDays}-day average</span>
        </div>
        <div className="sc-tile" data-tone={days !== null && days <= (s.thresholds.criticalDays ?? 3) ? "red" : days !== null && days <= 14 ? "amber" : "green"} data-testid="sc-tile-full">
          <span className="sc-tile-label"><Clock size={13} aria-hidden /> Full in</span>
          <b className="sc-tile-n">{daysWord(days)}</b>
          <span className="sc-tile-sub">{s.withRulesDays !== null && days !== null && s.withRulesDays > days ? `${daysWord(s.withRulesDays)} with the rules` : "if nothing changed"}</span>
        </div>
        <div className="sc-tile" data-testid="sc-tile-cleanup">
          <span className="sc-tile-label"><Broom size={13} aria-hidden /> Last cleanup</span>
          <b className="sc-tile-n">{s.lastCleanup ? whenWord(s.lastCleanup.at) : "none yet"}</b>
          <span className="sc-tile-sub">{s.lastCleanup ? <>Infra Agent · {gb(s.lastCleanup.freedGb)} freed{s.lastCleanup.incidentId ? <> · <StripLink patch={reportPatch(s.lastCleanup.incidentId, s.hostId)} orgId={orgId} className="sc-link" testId="sc-last-report">report</StripLink></> : null}</> : "Nothing removed yet on this host"}</span>
        </div>
      </div>

      <section className="sc-card" aria-labelledby={`${id}-bt`}>
        <header className="sc-card-head">
          <h3 id={`${id}-bt`}>Used by type</h3>
          <span className="sc-faint">{s.sampledAt ? `sampled ${whenWord(s.sampledAt)}` : ""}</span>
          <span className="sc-spacer" />
          {s.docker ? <span className="ia-chip" data-tone="amber" data-testid="sc-docker" title="Docker's disk image holds more than its containers use; the trim hands the difference back to the host">
            <Database size={11} aria-hidden />Docker VM {gb(s.docker.unreclaimedGb)} unreclaimed</span> : null}
        </header>
        <svg className="sc-bar" viewBox="0 0 100 1" preserveAspectRatio="none" role="img" aria-label={`${segs.map((x) => `${x.label} ${gb(x.gb)}`).join(", ")}; ${gb(s.freeGb)} free`} data-testid="sc-bar">
          {segs.reduce<{ x: number; nodes: React.ReactNode[] }>((acc, x) => {
            acc.nodes.push(<rect key={x.key} className="sc-seg" data-tone={TYPE_TONES[x.key] ?? "grey"} x={acc.x} y="0" width={x.pct} height="1"><title>{`${x.label} · ${gb(x.gb)}`}</title></rect>);
            acc.x += x.pct;
            return acc;
          }, { x: 0, nodes: [] }).nodes}
        </svg>
        <ul className="sc-legend-types">
          {segs.map((x) => <li key={x.key}><span className="sc-swatch" data-tone={TYPE_TONES[x.key] ?? "grey"} aria-hidden />{x.label} <code>{gb(x.gb)}</code></li>)}
          {!segs.length ? <li className="sc-faint">No reading by type yet.</li> : null}
        </ul>
      </section>

      <section className="sc-card">
        <TrendForecast s={s} />
        <div className="sc-thresholds" data-testid="sc-thresholds">
          <span className="ia-chip" data-tone="amber"><Bell size={11} aria-hidden />Alert at {s.thresholds.warnPct} % free</span>
          <span className="ia-chip" data-tone="red"><Warning size={11} aria-hidden />Critical at {s.thresholds.criticalPct} % free</span>
          <span className="ia-chip" data-tone="red"><Shield size={11} aria-hidden />Hard floor {s.thresholds.floorPct} %</span>
          <span className="ia-chip" data-tone="green"><Broom size={11} aria-hidden />Resume at {s.thresholds.targetPct} %</span>
          <span className="sc-spacer" />
          <span className="sc-faint" data-testid="sc-thresholds-note" title="PUT /v1/infra/storage/rules carries thresholds; the editor is a follow-up (INFRA_CONSOLE_GAPS.md)">Set by the install</span>
        </div>
      </section>

      <section className="sc-card" aria-labelledby={`${id}-rules`}>
        <header className="sc-card-head"><h3 id={`${id}-rules`}>Retention rules</h3><span className="sc-faint">Layer 1 · no model, no asking</span></header>
        <ul className="sc-rules" data-testid="sc-rules">
          {s.rules.map((r) => (
            <li key={r.id} className="sc-rule" data-enabled={r.enabled} data-source={r.source} data-testid="sc-rule">
              <span className="sc-rule-name">{r.label}{r.source === "learned" ? <span className="ia-chip" data-tone="violet" title="Proposed by the Infra Agent after a cause was seen twice, approved by a person">learned</span> : null}</span>
              <span className="ia-chip" data-tone="grey">{r.words}</span>
              <GatedButton permission={RULES_PERMISSION} projectId={projectId} className="sc-toggle" data-testid="sc-rule-toggle" aria-pressed={r.enabled}
                           onClick={() => onRule(r)} title={r.enabled ? "On · press to turn off" : "Off · press to turn on"}>{r.enabled ? "On" : "Off"}</GatedButton>
            </li>
          ))}
          {!s.rules.length ? <li className="sc-faint">No retention rules yet. The first cleanup proposes them.</li> : null}
        </ul>
        <div className="sc-note"><Shield size={13} aria-hidden /> Only things BOSS made and labelled are ever touched. Your own containers and data are never cleaned.</div>
      </section>

      {s.hosts.length ? (
        <section className="sc-card" aria-labelledby={`${id}-hosts`}>
          <header className="sc-card-head"><h3 id={`${id}-hosts`} className="sc-label">All hosts</h3></header>
          <ul className="sc-hosts">{s.hosts.map((h) => <HostRow key={h.hostId} h={h} active={h.hostId === s.hostId} onPick={() => onHost(h.hostId)} />)}</ul>
        </section>
      ) : null}
    </div>
  );
}

/** The card, live. `hostId` from the URL (`?host=`); the switcher writes it back. */
export function StorageCard({ projectId, actor, orgId, hostId, onHost }: {
  projectId: string; actor: string; orgId?: string | null; hostId: string | null; onHost: (hostId: string) => void;
}) {
  const { summary, error, loading, refresh, sample } = useStorageSummary(projectId, hostId);
  const { incident } = useInfraIncident(projectId);
  const [cleaning, setCleaning] = useState(false);
  if (!summary) {
    return (
      <div className="sc" data-testid="storage-card" data-empty>
        <header className="sc-head">
          <span className="sc-crumb">Govern <span aria-hidden>›</span> Infrastructure</span>
          <h2 className="sc-title">Storage</h2>
        </header>
        {loading ? <div className="gv-skel" aria-busy="true" /> : (
          <div className="sc-alert" data-tone="grey" role="status" data-testid="sc-unavailable">
            <Warning size={14} aria-hidden />
            {error ? <>Storage readings are not available yet. <span className="sc-faint">{error}</span></> : "No storage readings for this host yet. The storage watch samples every 30 seconds once the host helper runs."}
          </div>
        )}
      </div>
    );
  }
  const onClean = async () => {
    if (cleaning) return;
    setCleaning(true);
    const r = await cleanNow(summary.hostId);
    setCleaning(false);
    if (!r.ok) { showToast("error", r.error ?? "Could not start the cleanup"); return; }
    showToast("success", "Cleaning the safe tier now. Dry run first; nothing is paused.");
    refresh();
  };
  const onRule = async (rule: StorageSummary["rules"][number]) => {
    const r = await setRuleEnabled(rule, !rule.enabled);
    if (!r.ok) { showToast("error", r.error ?? "Not saved"); return; }
    showToast("success", `${rule.label}: ${rule.enabled ? "off" : "on"}`);
    refresh();
  };
  const pending = incident && incident.status !== "resolved" && incident.hostId === summary.hostId ? incident.askDecisionId : null;
  return <StorageCardView s={summary} projectId={projectId} orgId={orgId} sample={sample} pendingDecisionId={pending} onHost={onHost}
                          onClean={() => void onClean()} onRule={(r) => void onRule(r)} cleaning={cleaning} />;
}
