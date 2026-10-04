/**
 * Factory › Floor: the frozen contract (factory spec §4.2-§4.4, §6.2; Phase 0).
 *
 * engine/boss_engine/services/factory_view.py holds the same shapes. The wire is snake_case;
 * the parsers below are the only place it is mapped, and they never invent a field the server
 * did not send: no percent, no ETA, no guessed face (`roleKey: null` stays null). Every word a
 * build shows (`word`, `tip`, station `word`) is the server's — the web maps none of its own.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { onChanged } from "../app/changed";
import { isConsoleLive, subscribeConsoleLive } from "./console-live";
import { parseAttention, type AttentionItem } from "./agents-live-board";

// ── Stations (§4.3) ─────────────────────────────────────────────────────────────────────────

export type StationKey = "plan" | "you_plan" | "tests" | "build" | "check" | "review" | "pr" | "preview" | "ship";
export type StationStatus = "pending" | "working" | "needs_you" | "done" | "stuck" | "skipped" | "waiting";
/** agent: roleKey's face · hollow: hollowRole's face, pending · boss: BOSS glyph · you: You glyph · none: empty slot. */
export type StationFace = "agent" | "hollow" | "boss" | "you" | "none";

export type Seat = { actId: string; roleKey: string | null; seat: number; status: StationStatus; verdict: string | null };
export type StationLink = { label: string; url?: string };

export type Station = {
  key: StationKey;
  label: string;
  status: StationStatus;
  face: StationFace;
  roleKey: string | null;
  hollowRole: string | null;
  actId: string | null;
  actIds: string[];
  seats: Seat[];
  /** `Done` · `Coding` · `Next` · `Stuck` · `Skipped` · `Waiting` · `Needs you` — the server's. */
  word: string;
  tip?: string;
  startedAt?: string;
  endedAt?: string;
  /** On the edge INTO this station. */
  edgeLabel?: string;
  rounds: number;
  loopWith: string | null;
  links: StationLink[];
};

// ── One state per build (§4.4) ──────────────────────────────────────────────────────────────

export type BuildStateKey =
  | "cancelled" | "failed" | "needs_plan" | "needs_waiver" | "budget_refused" | "blocked"
  | "budget_wait" | "quiet" | "queued" | "building" | "pr_open" | "merged" | "deployed" | "done_no_pr";

export type PreviewFlag = { status: "starting" | "running" | "failed"; url?: string };
export type Origin = { kind: "workflow_run" | "meeting" | "discovery" | string; id: string; label: string };

export type BuildRow = {
  runId: string;
  app: string;
  title: string;
  attempt: number;
  rootRunId: string;
  actor?: string;
  state: BuildStateKey;
  word: string;
  tip?: string;
  verb: string;
  station: StationKey | null;
  stations: Station[];
  /** Stations done of stations planned. Never a percent. */
  k: number;
  n: number;
  rounds: number;
  startedAt?: string;
  stationSince?: string;
  updatedAt?: string;
  /** Metered model spend so far; 0 when unmetered (and then never shown). */
  costUsd: number;
  metered: boolean;
  attentionId: string | null;
  preview: PreviewFlag | null;
  origin: Origin | null;
  /** BOSS's own housekeeping build (the Captain's quality pass). */
  internal?: boolean;
};

export type FloorOutcome = {
  kind: "pr_opened" | "pr_merged" | "preview" | "deployed" | "done_no_pr";
  runId: string; app: string;
  /** The builder; null (deploys) → the BOSS glyph, never a guessed agent. */
  roleKey: string | null;
  text: string; url?: string; at: string;
};

export type StoppedRow = {
  runId: string; app: string; title: string;
  state: "failed" | "cancelled";
  word: string; tip?: string;
  roleKey: string | null; station: StationKey | null;
  retryable: boolean; retryReason?: string;
  at: string;
};

export type PreBuild = { meetingRunId: string; title: string; seats: string[]; at?: string };

export type FloorCounts = { building: number; needsYou: number; shipped7d: number; stopped: number; apps: number };

export type Floor = {
  projectId: string; etag: string; generatedAt: string;
  counts: FloorCounts;
  attention: AttentionItem[];
  builds: BuildRow[];
  outcomes: FloorOutcome[];
  stopped: StoppedRow[];
  preBuild: PreBuild[];
};

// ── Parsers (the only place the wire is mapped) ─────────────────────────────────────────────

type Wire = Record<string, unknown>;
export const obj = (v: unknown): Wire => (v && typeof v === "object" && !Array.isArray(v) ? v as Wire : {});
export const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
export const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
export const opt = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);
export const num = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
export const roleOf = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

const STATION_STATUSES = new Set(["pending", "working", "needs_you", "done", "stuck", "skipped", "waiting"]);
const FACES = new Set(["agent", "hollow", "boss", "you", "none"]);
const stationStatus = (v: unknown): StationStatus => (STATION_STATUSES.has(str(v)) ? str(v) as StationStatus : "pending");

export function parseStation(w: unknown): Station {
  const o = obj(w);
  const roleKey = roleOf(o.role_key);
  const hollowRole = roleOf(o.hollow_role);
  let face = (FACES.has(str(o.face)) ? str(o.face) : "") as StationFace;
  // A face the server did not send is never drawn: an agent face needs a role, a hollow one too.
  if (face === "agent" && !roleKey) face = "none";
  if (face === "hollow" && !hollowRole) face = "none";
  if (!face) face = roleKey ? "agent" : "none";
  return {
    key: str(o.key) as StationKey, label: str(o.label), status: stationStatus(o.status), face,
    roleKey, hollowRole: face === "hollow" ? hollowRole : null,
    actId: roleOf(o.act_id), actIds: arr(o.act_ids).map(String),
    seats: arr(o.seats).map((s) => {
      const x = obj(s);
      return { actId: str(x.act_id), roleKey: roleOf(x.role_key), seat: num(x.seat, 1),
               status: stationStatus(x.status), verdict: opt(x.verdict) ?? null };
    }),
    word: str(o.word), tip: opt(o.tip), startedAt: opt(o.started_at), endedAt: opt(o.ended_at),
    edgeLabel: opt(o.edge_label), rounds: num(o.rounds), loopWith: roleOf(o.loop_with),
    links: arr(o.links).map((l) => { const x = obj(l); return { label: str(x.label), url: opt(x.url) }; }),
  };
}

export function parsePreviewFlag(w: unknown): PreviewFlag | null {
  const o = obj(w);
  const status = str(o.status);
  if (status !== "starting" && status !== "running" && status !== "failed") return null;
  return { status, url: opt(o.url) };
}

export function parseOrigin(w: unknown): Origin | null {
  const o = obj(w);
  if (!opt(o.id) && !opt(o.label)) return null;
  return { kind: str(o.kind), id: str(o.id), label: str(o.label) };
}

export function parseBuildRow(w: unknown): BuildRow {
  const o = obj(w);
  const n = num(o.n);
  return {
    runId: str(o.run_id), app: str(o.app), title: str(o.title), attempt: num(o.attempt, 1),
    rootRunId: str(o.root_run_id, str(o.run_id)), actor: opt(o.actor),
    state: str(o.state, "building") as BuildStateKey, word: str(o.word), tip: opt(o.tip),
    verb: str(o.verb, "Working"), station: (opt(o.station) as StationKey | undefined) ?? null,
    stations: arr(o.stations).map(parseStation),
    k: Math.min(num(o.k), n), n, rounds: num(o.rounds),
    startedAt: opt(o.started_at), stationSince: opt(o.station_since), updatedAt: opt(o.updated_at),
    costUsd: o.metered === false ? 0 : num(o.cost_usd), metered: o.metered === true,
    attentionId: opt(o.attention_id) ?? null,
    preview: o.preview ? parsePreviewFlag(o.preview) : null,
    origin: o.origin ? parseOrigin(o.origin) : null,
    internal: o.internal === true || undefined,
  };
}

export function parseFloor(w: unknown): Floor {
  const o = obj(w);
  const c = obj(o.counts);
  return {
    projectId: str(o.project_id), etag: str(o.etag), generatedAt: str(o.generated_at),
    counts: { building: num(c.building), needsYou: num(c.needs_you), shipped7d: num(c.shipped_7d),
              stopped: num(c.stopped), apps: num(c.apps) },
    attention: arr(o.attention).map(parseAttention),
    builds: arr(o.builds).map(parseBuildRow),
    outcomes: arr(o.outcomes).map((x) => {
      const y = obj(x);
      return { kind: str(y.kind, "pr_opened") as FloorOutcome["kind"], runId: str(y.run_id), app: str(y.app),
               roleKey: roleOf(y.role_key), text: str(y.text), url: opt(y.url), at: str(y.at) };
    }),
    stopped: arr(o.stopped).map((x) => {
      const y = obj(x);
      return { runId: str(y.run_id), app: str(y.app), title: str(y.title),
               state: (str(y.state) === "cancelled" ? "cancelled" : "failed") as StoppedRow["state"],
               word: str(y.word), tip: opt(y.tip), roleKey: roleOf(y.role_key),
               station: (opt(y.station) as StationKey | undefined) ?? null,
               retryable: y.retryable === true, retryReason: opt(y.retry_reason), at: str(y.at) };
    }),
    preBuild: arr(o.pre_build).map((x) => {
      const y = obj(x);
      return { meetingRunId: str(y.meeting_run_id), title: str(y.title), seats: arr(y.seats).map(String), at: opt(y.at) };
    }),
  };
}

// ── The poll: one hook, ETag, pause- and visibility-aware ───────────────────────────────────

export const FLOOR_POLL_MS = 8000;
const MISSING_MS = 60_000;

export type Polled<T> = {
  data: T | null;
  /** The last failure's reason, while the answer could not be read. Cleared by an answer. */
  error: string | null;
  /** True until the first answer (or failure). */
  loading: boolean;
  refresh: () => void;
};

/**
 * GET `url` every `everyMs(data)` while the page is visible, the console is Live and `enabled`.
 * If-None-Match makes an unchanged answer a 304 and no re-render. A change made anywhere in
 * the console (`onChanged`) asks now. The Floor and the cockpit both poll through this.
 */
export function usePolled<T>(url: string | null, parse: (w: unknown) => T,
                             everyMs: (data: T | null) => number): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const etag = useRef<string | null>(null);
  const kick = useRef<() => void>(() => {});
  const latest = useRef<T | null>(null);
  const parser = useRef(parse);
  parser.current = parse;
  const cadence = useRef(everyMs);
  cadence.current = everyMs;

  useEffect(() => {
    etag.current = null;
    latest.current = null;
    setData(null);
    setError(null);
    setLoading(true);
    if (!url) return undefined;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inflight = false;
    let gen = 0;
    const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";

    const tick = async (mine: number) => {
      if (stopped || mine !== gen) return;
      let wait = cadence.current(latest.current);
      if (!hidden() && isConsoleLive() && !inflight) {
        inflight = true;
        try {
          const r = await fetch(url, {
            cache: "no-store", headers: etag.current ? { "If-None-Match": etag.current } : {},
          });
          if (stopped) return;
          if (r.status === 304) {
            setError(null);
          } else if (r.ok) {
            etag.current = r.headers.get("etag");
            const parsed = parser.current(await r.json());
            latest.current = parsed;
            setData(parsed);
            setError(null);
            wait = cadence.current(parsed);
          } else {
            if (r.status === 404 || r.status === 405) wait = MISSING_MS;
            let said = "";
            try { said = String(((await r.json()) as { detail?: unknown }).detail ?? ""); } catch { /* none */ }
            setError(said || `The API answered ${r.status}.`);
          }
        } catch (e) {
          if (!stopped) setError(e instanceof Error ? e.message : "The API did not answer.");
        } finally {
          inflight = false;
          if (!stopped) setLoading(false);
        }
      }
      if (!stopped && mine === gen) timer = setTimeout(() => void tick(mine), wait);
    };
    const restart = () => {
      if (stopped || hidden() || !isConsoleLive()) return;
      if (timer) clearTimeout(timer);
      gen += 1;
      void tick(gen);
    };
    kick.current = restart;
    const onVis = () => restart();
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVis);
    const offChanged = onChanged(() => restart());
    const offPause = subscribeConsoleLive((on) => { if (on) restart(); });
    void tick(gen);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVis);
      offChanged();
      offPause();
    };
  }, [url]);

  const refresh = useCallback(() => kick.current(), []);
  return { data, error, loading: loading && data === null && !error, refresh };
}

/** `viewer` is the signed-in person: what they did reads "you" (`Stopped by you`). */
export function floorUrl(projectId: string, viewer?: string): string {
  return `/v1/factory/floor?project_id=${encodeURIComponent(projectId)}`
    + (viewer ? `&viewer=${encodeURIComponent(viewer)}` : "");
}

export function useFactoryFloor(projectId: string, enabled = true, viewer?: string): Polled<Floor> {
  return usePolled(projectId && enabled ? floorUrl(projectId, viewer) : null, parseFloor, () => FLOOR_POLL_MS);
}
