"use client";

/**
 * A rack (spec §4.3): one machine and what is placed on it. Head line (name · address · arch,
 * state on the right), meta line (`apps k of n` · `ports k of n` · `checked <ago>`), then either
 * the slot grid — one 168×64 tile per resident, dashed empty slots up to max_apps (≤ 8 drawn,
 * then `+n free`), or `+ Place app` when the machine sets no limit — or, while the machine is
 * still being set up, the Stage line with that stage's one action inline.
 *
 * Every glyph is a field the server sent (§5.4): the tile's dot and word are the target's
 * health, its mini strip is 32 × 45-minute buckets of probes, line 3 is the newest live deploy.
 * No CPU, no memory: BOSS does not measure them on a deploy host.
 */
import React, { useEffect, useRef } from "react";

import { STAGES, type HostStage, type Rack, type Resident, type StageAction } from "../../lib/infra-view";
import { PulseStrip } from "../observability/pulse-strip";
import { ago, localTime } from "./infra-util";
import { StateWord } from "./kit";

export const SLOTS_DRAWN = 8;
const CELL_S = 45 * 60;

/** Did this value change since the last render? (The ring pulse fires once on a real change.) */
function useChanged(value: string): boolean {
  const prev = useRef(value);
  const changed = prev.current !== value;
  useEffect(() => { prev.current = value; }, [value]);
  return changed;
}

// ── the stage line ──────────────────────────────────────────────────────────────────────────

/** Credential · Authorise · Verify · Provision · Ready — done ✓, the current one marked, the
 *  edge into the current stage draws once when the stage advances. */
export function StageLine({ stage, vertical, testId = "inf-stage-line" }: { stage: HostStage; vertical?: boolean; testId?: string }) {
  const prev = useRef(stage.index);
  const advanced = stage.index > prev.current;
  useEffect(() => { prev.current = stage.index; }, [stage.index]);
  return (
    <ol className="inf-stages" data-dir={vertical ? "col" : "row"} data-testid={testId}
        aria-label={`Setup: ${stage.label}, step ${stage.index} of 5`}>
      {STAGES.map((s, i) => {
        const at = i + 1;
        const state = at < stage.index ? "done" : at === stage.index ? "current" : "todo";
        return (
          <li key={s.key} className="inf-stage" data-state={state} title={s.label} aria-current={state === "current" ? "step" : undefined}
              data-new={advanced && at === stage.index ? "1" : undefined} data-anim={advanced && at === stage.index ? "" : undefined}>
            <span className="inf-stage-mark" aria-hidden>{state === "done" ? "✓" : at}</span>
            <span className="inf-stage-label">{s.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

// ── a resident tile (168×64) ────────────────────────────────────────────────────────────────

export function minutesLeft(expiresAt: string | null, now: number): number | null {
  if (!expiresAt) return null;
  const t = Date.parse(expiresAt);
  return Number.isFinite(t) ? Math.max(0, Math.round((t - now) / 60_000)) : null;
}

export function ResidentTile({ r, now, windowEnd, onOpen }: {
  r: Resident; now: number | null; windowEnd: string | null; onOpen: (r: Resident) => void;
}) {
  const changed = useChanged(r.health.word);
  const label = r.kind === "target" ? `${r.name}${r.env ? ` · ${r.env}` : ""}` : r.name;
  const end = windowEnd ? Date.parse(windowEnd) : NaN;
  const fromIso = Number.isFinite(end) ? new Date(end - r.strip.length * CELL_S * 1000).toISOString() : null;
  let line3: React.ReactNode = null;
  if (r.kind === "target") {
    line3 = r.lastDeploy?.sha7
      ? <span className="inf-tile-l3" title={`Deployed ${r.lastDeploy.sha7} · ${localTime(r.lastDeploy.at)} · ${r.lastDeploy.word}`}>
          <span className="inf-mono" data-copy="data">{r.lastDeploy.sha7}</span>{r.lastDeploy.at && now ? ` · ${ago(r.lastDeploy.at, now)}` : ""}
          {r.lastDeploy.failedAfter ? (
            <span className="inf-failed-after" data-testid="inf-tile-failed-after"
                  title={`Last attempt failed${now ? ` · ${ago(r.lastDeploy.failedAfter.at, now)} ago` : ""}`}>
              {" · ✗ "}{now ? ago(r.lastDeploy.failedAfter.at, now) : ""}<span className="ag-sr"> last attempt failed</span>
            </span>
          ) : null}
        </span>
      : <span className="inf-tile-l3 inf-muted" title="This target has no deploy yet">Never deployed</span>;
  } else if (r.kind === "preview") {
    const left = now !== null ? minutesLeft(r.expiresAt, now) : null;
    line3 = left !== null ? <span className="inf-tile-l3" title={r.expiresAt ? `Expires at ${localTime(r.expiresAt)}` : undefined}>{left}m left</span> : null;
  } else if (r.url) {
    line3 = (
      <a className="inf-link inf-tile-l3" href={r.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}
         data-testid="inf-tile-grafana">Grafana ↗</a>
    );
  }
  return (
    <article className="inf-tile" data-kind={r.kind} data-tone={r.health.tone} data-testid="inf-tile" data-id={r.id}
             onClick={() => onOpen(r)}>
      <button type="button" className="inf-tile-name" title={label} onClick={(e) => { e.stopPropagation(); onOpen(r); }}
              data-testid={`inf-tile-open-${r.id}`}>
        <span className="inf-trunc" data-copy="data">{label}</span>
      </button>
      <span className="inf-tile-l2">
        <StateWord w={r.health} testId="inf-tile-state" changed={changed} />
        {r.kind === "target" ? (
          <span className="inf-tile-strip">
            <PulseStrip cells={r.strip} fromIso={fromIso} cellS={CELL_S} label={label} span="24 hours" density="mini" />
          </span>
        ) : null}
      </span>
      {line3}
    </article>
  );
}

// ── the rack ────────────────────────────────────────────────────────────────────────────────

/** What a setting-up rack's inline action is called, and whether this install may do it. */
export function stageActionLabel(a: StageAction): string {
  return a === "generate_key" ? "Generate key" : a === "verify" ? "Verify" : a === "install_docker" ? "Install Docker"
    : a === "copy_command" ? "Copy command" : "";
}

export function RackView({ rack, now, windowEnd, canAct, busy, onHost, onResident, onPlace, onStage }: {
  rack: Rack; now: number | null; windowEnd: string | null;
  /** Which inline stage actions this install allows (precondition gates, read-only). */
  canAct: (a: StageAction) => boolean;
  busy: boolean;
  onHost: (id: string) => void;
  onResident: (r: Resident) => void;
  onPlace: ((hostId: string) => void) | null;
  onStage: (rack: Rack, a: StageAction) => void;
}) {
  const changed = useChanged(rack.state.word);
  const settingUp = !rack.thisComputer && rack.stage.stage !== "ready" && rack.state.tone !== "none";
  const free = rack.maxApps !== null ? Math.max(0, rack.maxApps - rack.apps) : 0;
  const drawn = Math.min(free, SLOTS_DRAWN);
  const action = rack.stageAction && canAct(rack.stageAction) ? rack.stageAction : null;
  return (
    <section className="inf-rack" data-testid={rack.thisComputer ? "inf-rack-this" : "inf-rack"} data-id={rack.id}
             data-tone={rack.state.tone} aria-label={rack.thisComputer ? `This computer, ${rack.name}` : rack.name}>
      <header className="inf-rack-head">
        <button type="button" className="inf-rack-name" onClick={() => onHost(rack.id)} data-testid={`inf-rack-open-${rack.id}`}
                title="Open this machine">
          {rack.thisComputer ? <span className="inf-rack-this">This computer</span> : null}
          <span className="inf-trunc" data-copy="data">{rack.name}</span>
        </button>
        {rack.address ? <span className="inf-mono inf-trunc inf-muted" data-copy="data" title={rack.address}>{rack.address}</span> : null}
        {rack.arch ? <span className="inf-muted">{rack.arch}</span> : null}
        <span className="ag-spacer" />
        <StateWord w={rack.state} testId="inf-rack-state" changed={changed} />
      </header>
      <div className="inf-rack-meta" data-testid="inf-rack-meta">
        <span title="Apps placed here, of the most this machine takes">
          {rack.maxApps !== null ? `apps ${rack.apps} of ${rack.maxApps}` : `${rack.apps} ${rack.apps === 1 ? "app" : "apps"}`}
        </span>
        {rack.portsTotal !== null ? (
          <span title="Ports held by apps and previews, of this machine's range">ports {rack.portsUsed} of {rack.portsTotal}</span>
        ) : null}
        {rack.checkedAt && now !== null ? <span title={localTime(rack.checkedAt)}>checked {ago(rack.checkedAt, now)}</span> : null}
      </div>
      {settingUp ? (
        <div className="inf-rack-setup">
          <StageLine stage={rack.stage} />
          <div className="inf-actions">
            {action ? (
              <button type="button" className="ag-btn ag-btn-primary" disabled={busy} data-testid={`inf-rack-stage-${action}`}
                      onClick={() => onStage(rack, action)}>{stageActionLabel(action)}</button>
            ) : null}
            <button type="button" className="inf-link" onClick={() => onHost(rack.id)} data-testid="inf-rack-details">Details ▾</button>
          </div>
        </div>
      ) : (
        <div className="inf-slots">
          {rack.residents.map((r) => (
            <ResidentTile key={`${r.kind}-${r.id}`} r={r} now={now} windowEnd={windowEnd} onOpen={onResident} />
          ))}
          {onPlace && rack.maxApps === null ? (
            <button type="button" className="inf-slot" data-testid="inf-place-app" onClick={() => onPlace(rack.id)}>+ Place app</button>
          ) : null}
          {Array.from({ length: drawn }, (_, i) => (
            onPlace
              ? <button key={i} type="button" className="inf-slot" aria-label="Place an app here" title="Place an app here"
                        data-testid="inf-slot-free" onClick={() => onPlace(rack.id)} />
              : <span key={i} className="inf-slot" aria-hidden data-testid="inf-slot-free" />
          ))}
          {free > drawn ? <span className="inf-slot-more" data-testid="inf-slot-more">+{free - drawn} free</span> : null}
        </div>
      )}
    </section>
  );
}

/** NOT PLACED: targets with no machine BOSS manages (registry, cloud). */
export function UnplacedRack({ residents, now, windowEnd, onResident }: {
  residents: Resident[]; now: number | null; windowEnd: string | null; onResident: (r: Resident) => void;
}) {
  return (
    <section className="inf-rack" data-kind="unplaced" data-testid="inf-unplaced" aria-label="Not placed">
      <header className="inf-rack-head">
        <h4 className="ag-zone-label" title="Targets not on a machine BOSS manages">NOT PLACED</h4>
        <span className="ag-zone-count">{residents.length}</span>
      </header>
      <div className="inf-slots">
        {residents.map((r) => <ResidentTile key={r.id} r={r} now={now} windowEnd={windowEnd} onOpen={onResident} />)}
      </div>
    </section>
  );
}
