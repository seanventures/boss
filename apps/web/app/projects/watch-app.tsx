"use client";

/**
 * PROJECT › HEALTH › WATCH THIS APP (docs/product/wireframes/agentic-factory/2-watch-an-app.html).
 *
 * The sheet: one input (a project app, a URL, or a few words about what matters) → the SRE
 * agent's Agent actions card (how it talks, what matters from your words, the watches with a
 * 7-day baseline and backtest, the one open question) → six boxes with popovers and the
 * alone / waits-for-you strip → one primary, Start watching. A watch's popover keeps the rule
 * builder as chips and the PromQL visible; the full builder stays one press away.
 *
 * The landing (WatchedCard) sits on the app's Health page: the diagram (read → collector →
 * first data → watches → verify the channel → on call), the Now line, the one gate (an outside
 * channel's code: Talking to the outside), the watches (a later edit asks why), and each
 * incident as a flow: on staging the agent acts alone; on prod the fix waits for one click.
 *
 * ONE PATH: the engine (services/watch_app.py) writes the app's alert rules, contact points and
 * routing through Observability's own functions; nothing here is a second monitoring stack.
 * "Wire it myself" opens Observability's Connect sheet with every old path.
 */
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  ArrowSquareOut, Bell, BookOpen, ChatCircle, Check, CheckCircle, Eye, Graph, GitPullRequest, HandPalm, Heartbeat, Lightning,
  MagicWand, MagnifyingGlass, PaperPlaneTilt, PencilSimple, Pulse, RocketLaunch, ShieldCheck, Timer, WaveSine, Wrench, Funnel,
  Globe, Folders, type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { Sheet } from "../infra/kit";
import { ErrorLine } from "../infra/infra-util";
import { ConnectSheet } from "../observability/connect-sheet";
import { Face } from "./kit";
import {
  changeWatch, KIND_WORDS, PARAM_WORDS, readWatch, resendCode, startWatching, TELL_KINDS, titleOf, verifyChannel,
  type Landing, type Night, type Tone, type Watch, type WatchKind, type WatchRead,
} from "../../lib/watch-app";
import { pxHref, writePxUrl } from "../../lib/project-experience";
import "../infra/infra.css";
import "../infra/add-server.css";
import "./watch-app.css";

const PAUSE_MS = 450;
const BEACON = { name: "Beacon", role_key: "sre" };
const ICONS: Record<string, Icon> = {
  "paper-plane": PaperPlaneTilt, timer: Timer, heartbeat: Heartbeat, wave: WaveSine, rocket: RocketLaunch, "magnifying-glass": MagnifyingGlass,
  graph: Graph, "git-pull-request": GitPullRequest, "check-circle": CheckCircle, "chat-circle": ChatCircle, bell: Bell,
};
const icon = (k: string, size = 13) => { const I = ICONS[k] ?? Bell; return <I size={size} aria-hidden />; };

type Box = "signals" | "collector" | "watches" | "oncall" | "tell" | "runbooks";
const BOX_TITLE: Record<Box, string> = { signals: "Signals", collector: "Collector", watches: "Watches", oncall: "On call", tell: "Tell", runbooks: "Runbooks" };
type Edit = { params: Record<string, number>; forS: number; tone: Tone; enabled: boolean };

/** `?watch=1` (or `watch=<target id>`) opens the sheet: the door any page links to. */
export function useWatchParam(): string | null {
  const read = () => (typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("watch"));
  const [v, setV] = useState<string | null>(null);
  useEffect(() => {
    const on = () => setV(read());
    on();
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  return v;
}
export const openWatch = (targetId?: string) => writePxUrl({ tab: "health", watch: targetId || "1" });
export const closeWatch = () => writePxUrl({ watch: null });

// ── a watch's 7-day line ──────────────────────────────────────────────────────────────────

function threshold(w: Watch): number | null {
  return w.kind === "error_rate" || w.kind === "latency_p95" ? (w.params.threshold ?? null) : w.kind === "health_check" ? (w.params.min_failures ?? null) : null;
}

export function Spark({ w }: { w: Watch }) {
  const vals = w.spark.map((v) => (v === null ? null : v));
  const thr = threshold(w);
  const known = vals.filter((v): v is number => v !== null);
  const top = Math.max(thr ?? 0, ...known, 1) * 1.15;
  const n = Math.max(vals.length, 2);
  const x = (i: number) => (i / (n - 1)) * 300;
  const y = (v: number) => 50 - (v / top) * 44;
  const pts = vals.map((v, i) => (v === null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(" ");
  return (
    <>
    <svg className="wa-spark" viewBox="0 0 300 54" preserveAspectRatio="none" role="img" data-testid={`wa-spark-${w.kind}`}
         aria-label={known.length ? `7 days of ${w.what}, the line at ${thr ?? "—"}` : `No 7-day data yet for ${w.what}`}>
      <line x1="0" x2="300" y1="52" y2="52" stroke="var(--border)" strokeWidth="1" />
      {thr !== null ? <line x1="0" x2="300" y1={y(thr)} y2={y(thr)} stroke="var(--amber)" strokeDasharray="4 3" strokeWidth="1" /> : null}
      {pts ? <polyline fill="none" stroke="var(--teal)" strokeWidth="1.6" points={pts} /> : null}
    </svg>
    {pts ? null : <span className="as-faint" data-testid={`wa-spark-empty-${w.kind}`}>No data in 7 days yet · the line fills as it reads</span>}
    </>
  );
}

/** The rule builder as chips: each number of the watch, editable in place. */
function Chips({ w, edit, set }: { w: Watch; edit: Edit; set: (e: Edit) => void }) {
  const words = PARAM_WORDS[w.kind] ?? {};
  const keys = Object.keys(words);
  return (
    <span className="wa-chips" data-testid={`wa-chips-${w.kind}`}>
      {keys.length ? keys.map((k) => (
        <label key={k} className="wa-num" title={words[k].label}>{words[k].label}
          <input inputMode="decimal" value={String(edit.params[k] ?? w.params[k] ?? "")} data-testid={`wa-p-${w.kind}-${k}`}
                 aria-label={`${w.what}: ${words[k].label}`}
                 onChange={(e) => { const v = Number(e.target.value.replace(/[^\d.]/g, "")); set({ ...edit, params: { ...edit.params, [k]: Number.isFinite(v) ? v : 0 } }); }} />
          {words[k].unit}</label>
      )) : <span className="as-faint">the newest deploy failed its health check</span>}
      <label className="wa-num" title="Held this long before it fires">for
        <input inputMode="numeric" value={String(Math.round(edit.forS / 60))} data-testid={`wa-p-${w.kind}-for`} aria-label={`${w.what}: held for minutes`}
               onChange={(e) => set({ ...edit, forS: (Number(e.target.value.replace(/\D/g, "")) || 0) * 60 })} />min</label>
      <span className="wa-seg" role="radiogroup" aria-label="Wake or tell">
        {(["wake", "tell"] as Tone[]).map((t) => (
          <button key={t} type="button" role="radio" aria-checked={edit.tone === t} data-testid={`wa-tone-${w.kind}-${t}`}
                  onClick={() => set({ ...edit, tone: t })}>{t}</button>
        ))}
      </span>
    </span>
  );
}

function WatchPop({ w, edit, set, tellWord, targetId, onRemove, onDone, why, setWhy, busy }: {
  w: Watch; edit: Edit; set: (e: Edit) => void; tellWord: string; targetId: string | null; onRemove: () => void; onDone: () => void;
  /** A later edit (on the landing) asks why; the sheet's first edit does not. */
  why?: string; setWhy?: (s: string) => void; busy?: boolean;
}) {
  const builder = targetId ? pxHref(`?area=observability&app=${encodeURIComponent(targetId)}&atab=alerts`) : null;
  const needsWhy = setWhy !== undefined;
  return (
    <div className="as-pop" role="dialog" aria-label={w.title} data-testid={`wa-pop-${w.kind}`}>
      <header className="as-pop-h">{icon(w.icon)} <b>{titleOf(w.kind, { ...w.params, ...edit.params }, w.title)}</b><span className="ag-spacer" />
        <span className="wa-tone" data-tone={edit.tone}>{edit.tone} · {tellWord}</span></header>
      <Spark w={w} />
      <div className="wa-kv">
        <span>When</span><Chips w={w} edit={edit} set={set} />
        <span>PromQL</span>
        <span className="wa-code" data-testid={`wa-promql-${w.kind}`}>{w.expr}</span>
        <span>Backtest</span><span className="as-faint" data-testid={`wa-backtest-${w.kind}`}>{w.backtest || w.sub}</span>
        <span>Then</span><span className="as-faint">The SRE agent triages; below the line it fixes alone · tells {tellWord}</span>
      </div>
      <footer className="wa-pop-f">
        {needsWhy ? <input className="wa-why" value={why ?? ""} onChange={(e) => setWhy?.(e.target.value)} placeholder="Why? (kept with the change)"
                           aria-label="Why this change" data-testid={`wa-why-${w.kind}`} /> : null}
        {builder ? <a className="inf-link" href={builder} data-testid={`wa-builder-${w.kind}`} title="The full rule builder, Edit PromQL, silences and history">
          ⋯ Full builder <ArrowSquareOut size={11} aria-hidden /></a> : null}
        <span className="ag-spacer" />
        <button type="button" className="as-btn" data-testid={`wa-remove-${w.kind}`} disabled={busy || (needsWhy && (why ?? "").trim().length < 3)}
                onClick={onRemove}>Remove</button>
        <button type="button" className="as-primary" data-testid={`wa-done-${w.kind}`} disabled={busy || (needsWhy && (why ?? "").trim().length < 3)}
                title={needsWhy && (why ?? "").trim().length < 3 ? "Say why first: every later change keeps its reason" : undefined}
                onClick={onDone}>Done</button>
      </footer>
    </div>
  );
}

// ── the sheet ─────────────────────────────────────────────────────────────────────────────

export function WatchSheet({ projectId, actor, initialTarget, onClose, onStarted }: {
  projectId: string; actor: string; initialTarget?: string | null; onClose: () => void; onStarted: (l: Landing) => void;
}) {
  const boxId = useId();
  const [text, setText] = useState("");
  const [forced, setForced] = useState<WatchKind | null>(null);
  const [targetId, setTargetId] = useState<string | null>(initialTarget && initialTarget !== "1" ? initialTarget : null);
  const [read, setRead] = useState<WatchRead | null>(null);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [night, setNight] = useState<Night | null>(null);
  const [alone, setAlone] = useState(true);
  // null: what the words said; "none": the person picked "Only you".
  const [tell, setTell] = useState<{ kind: string; name: string; url: string; address: string } | "none" | null>(null);
  const [openWatchKind, setOpenWatchKind] = useState<string | null>(null);
  const [open, setOpen] = useState<Box | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [wire, setWire] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => { const t = window.setTimeout(() => inputRef.current?.focus(), 30); return () => window.clearTimeout(t); }, []);

  // ── the read, once typing pauses (or at once for a picked app) ─────────────────────────
  const seq = useRef(0);
  useEffect(() => {
    const t = text.trim();
    if (!t && !targetId) { setRead(null); setReadError(null); setReading(false); return undefined; }
    const ctl = new AbortController();
    const mine = ++seq.current;
    setReading(true);
    const timer = window.setTimeout(() => {
      void readWatch(projectId, t, forced, targetId, ctl.signal).then((r) => {
        if (mine !== seq.current) return;
        setReading(false);
        if (r.ok) { setRead(r.data); setReadError(null); }
        else if (r.error) { setReadError(r.error); setRead(null); }
      });
    }, t ? PAUSE_MS : 0);
    return () => { window.clearTimeout(timer); ctl.abort(); };
  }, [projectId, text, forced, targetId]);

  const kind: WatchKind = forced ?? read?.kind ?? "project";
  const target = read?.target ?? null;
  const watches = read?.watches ?? [];
  const editOf = useCallback((w: Watch): Edit => edits[w.kind] ?? { params: w.params, forS: w.forS, tone: w.tone, enabled: true }, [edits]);
  const live = watches.filter((w) => editOf(w).enabled);
  const nightNow: Night = night ?? read?.question?.answer ?? "wake";
  const channel = tell === "none" ? null : tell ?? (read?.tell?.channel ? { kind: read.tell.channel.kind, name: read.tell.channel.name, url: "", address: read.tell.channel.address ?? "" } : null);
  const outside = !!channel?.kind;
  const tellWord = outside ? `${channel?.name || "your channel"} · you` : "you";
  const prod = !!target?.prod;
  const aloneNow = alone && !prod;

  const blocker = useMemo((): string | null => {
    if (kind !== "project") return "This one is wired by hand: Wire it myself keeps every path.";
    if (!text.trim() && !targetId) return "Name the app first: a project app, its URL, or a few words.";
    if (reading && !read) return "Reading it…";
    if (!target) return read?.word || "No app of this project matches yet.";
    if (!live.length) return "Keep at least one watch.";
    if (outside && channel && channel.kind !== "email" && !channel.url.trim() && read?.tell?.needsUrl !== false)
      return `Paste ${channel.name || "the channel"}'s incoming webhook URL in Tell, or tell only you.`;
    if (outside && channel?.kind === "email" && !/@/.test(channel.address || channel.name)) return "Give the email address in Tell.";
    return null;
  }, [kind, text, targetId, reading, read, target, live.length, outside, channel]);

  const submit = useCallback(async () => {
    if (busy) return;
    if (blocker) { setError(blocker); return; }
    if (!target) return;
    setError(null); setBusy(true);
    try {
      const r = await startWatching(projectId, {
        project_id: projectId, actor, target_id: target.targetId, words: text.trim(), night: nightNow, alone: aloneNow,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        watches: watches.map((w) => { const e = editOf(w); return { kind: w.kind, params: e.params, for_s: e.forS, tone: e.tone, enabled: e.enabled, words: w.words }; }),
        removed: watches.filter((w) => !editOf(w).enabled).map((w) => w.kind),
        tell: channel?.kind ? { kind: channel.kind, name: channel.name || channel.address, url: channel.url || undefined, address: channel.address || undefined } : {},
      });
      if (!r.ok) { setError(r.error); return; }
      onStarted(r.data);
    } finally {
      setBusy(false);
    }
  }, [busy, blocker, target, projectId, actor, text, nightNow, aloneNow, watches, editOf, channel, onStarted]);

  const keyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void submit(); }
  };
  const setEdit = (w: Watch, e: Edit) => setEdits((cur) => ({ ...cur, [w.kind]: e }));
  const addHint = () => {
    const h = hint.trim();
    if (!h) return;
    setText((t) => `${t.trim()}\n${h}`.trim()); setHint(""); setConfirmed(false);
  };

  if (wire) return <ConnectSheet projectId={projectId} actor={actor} onClose={() => setWire(false)} />;
  const wakeN = live.filter((w) => editOf(w).tone === "wake").length;
  return (
    <Sheet title="Watch an app" testId="wa-sheet" onClose={onClose} wide>
      <div className="as" onKeyDown={keyDown}>
        {/* 1 · one input */}
        <label className="as-input" htmlFor={boxId} data-testid="wa-input-box" title="Name an app, paste its URL, or say what matters. The SRE agent works out the rest.">
          <span className="as-input-ico" aria-hidden><Pulse size={20} weight="fill" /></span>
          <textarea id={boxId} ref={inputRef} className="as-text" rows={3} value={text} spellCheck={false} autoComplete="off" data-testid="wa-input"
                    placeholder={"HelpDesk staging.\nWake me if replies stop sending or the inbox gets slow.\nTell #support-eng on Slack."}
                    aria-label="A project app, a URL, or a few words about what matters"
                    onChange={(e) => { setText(e.target.value); setConfirmed(false); setError(null); }} />
          <span className="as-input-foot">
            <span>a project, a URL, or a few words · ⇧↵ new line</span>
            <span className="as-faint" data-testid="wa-reading">{reading ? "Beacon is reading the app…" : read ? "Beacon read it ↓" : ""}</span>
          </span>
        </label>
        <div className="wa-detect">
          <div className="as-pills" role="radiogroup" aria-label="What it is" data-testid="wa-kinds" title="Detected. Click to force another reading.">
            {(["project", "url", "elsewhere"] as WatchKind[]).map((k) => (
              <button key={k} type="button" role="radio" aria-checked={kind === k} className="as-pill" data-testid={`wa-kind-${k}`}
                      onClick={() => { setForced(k); setError(null); }}>
                {k === "project" ? <Folders size={13} aria-hidden /> : k === "url" ? <Globe size={13} aria-hidden /> : <Bell size={13} aria-hidden />}
                {KIND_WORDS[k]}
              </button>
            ))}
          </div>
          <span className="wa-matched" data-testid="wa-matched" aria-live="polite">
            {readError ? <span className="inf-error" data-testid="wa-read-error">{readError}</span>
              : target ? <>Matched <b>{target.label}</b>{target.address ? ` (${target.address})` : ""}</>
              : read ? <span>{read.word}</span> : <span className="as-faint">Waiting for an app</span>}
            {read && read.apps.length > 1 && kind === "project" ? (
              <select aria-label="Pick the app" data-testid="wa-app" value={target?.targetId ?? ""}
                      onChange={(e) => { setTargetId(e.target.value || null); setEdits({}); }}>
                {read.apps.map((a) => <option key={a.targetId} value={a.targetId}>{a.label}</option>)}
              </select>
            ) : null}
          </span>
        </div>

        {kind === "project" && target ? (
          <>
            {/* 2 · Agent actions */}
            <section className="as-card" data-confirmed={confirmed || undefined} aria-labelledby={`${boxId}-aa`} data-testid="wa-agent-actions">
              <header className="as-card-head">
                <Face agent={BEACON} size={28} />
                <span className="as-card-title">
                  <h3 id={`${boxId}-aa`}>Agent actions</h3>
                  <span className="as-faint">Beacon (SRE) read the app, its signals and 7 days of history</span>
                </span>
                <span className="as-tag" data-tone={reading ? "accent" : "green"} data-testid="wa-aa-state">{reading ? "reading" : "read"}</span>
              </header>
              <div className="wa-aa-grid">
                <div className="as-pop-b">
                  <div className="wa-f"><span className="as-label">How it talks</span>
                    <span className="wa-f-v" data-testid="wa-aa-signals">
                      <span className="as-tag" data-tone={read?.signals?.metrics ? "green" : "accent"}>{read?.signals?.metrics ? "FOUND" : "PROBE"}</span>
                      {read?.signals?.word}{read?.signals?.logs ? " · Docker logs" : ""} · <span className="wa-mark" title="Not needed for these watches">no traces</span></span></div>
                  <div className="wa-f"><span className="as-label">What matters</span>
                    <span className="wa-matters" data-testid="wa-aa-matters">
                      {(read?.matters.length ? read.matters : []).map((m) => (
                        <span key={m.kind} className="wa-matter">{icon(watches.find((w) => w.kind === m.kind)?.icon ?? "bell")}<span>{m.words}</span><em>your words</em></span>
                      ))}
                      {watches.filter((w) => !w.words).map((w) => (
                        <span key={w.kind} className="wa-matter">{icon(w.icon)}<span>{w.what}</span><em>BOSS default</em></span>
                      ))}
                    </span></div>
                  {read?.question ? (
                    <div className="wa-f"><span className="as-label">Open · 1</span>
                      <span className="wa-q" data-testid="wa-question"><span>{read.question.ask}</span>
                        <span className="wa-q-opts" role="radiogroup" aria-label="At night">
                          {read.question.options.map((o) => (
                            <button key={o.key} type="button" role="radio" className="wa-sug" aria-checked={nightNow === o.key} data-testid={`wa-night-${o.key}`}
                                    title={o.key === "alone" ? "The SRE agent fixes what it can alone and tells you in the morning" : undefined}
                                    onClick={() => setNight(o.key)}>{o.label}</button>
                          ))}
                        </span></span></div>
                  ) : null}
                </div>
                <div className="wa-f"><span className="as-label">Watches · {live.length}</span>
                  <div className="wa-watches" data-testid="wa-watches">
                    {watches.map((w) => {
                      const e = editOf(w);
                      return (
                        <React.Fragment key={w.kind}>
                          <button type="button" className="wa-watch" aria-expanded={openWatchKind === w.kind} data-off={!e.enabled || undefined}
                                  data-testid={`wa-watch-${w.kind}`} title={w.condition}
                                  onClick={() => setOpenWatchKind(openWatchKind === w.kind ? null : w.kind)}>
                            <span className="wa-watch-ico">{icon(w.icon)}</span>
                            <span className="wa-watch-t"><span className={w.words ? "wa-mark" : undefined} data-testid={`wa-watch-title-${w.kind}`}>{e.enabled ? titleOf(w.kind, { ...w.params, ...e.params }, w.title) : `${w.title} · removed`}</span><small>{w.sub}</small></span>
                            <span className="wa-tone" data-tone={e.tone}>{e.tone}</span>
                          </button>
                          {openWatchKind === w.kind ? (
                            <WatchPop w={w} edit={e} set={(x) => setEdit(w, x)} tellWord={tellWord} targetId={target.targetId}
                                      onRemove={() => { setEdit(w, { ...e, enabled: !e.enabled }); setOpenWatchKind(null); }}
                                      onDone={() => setOpenWatchKind(null)} />
                          ) : null}
                        </React.Fragment>
                      );
                    })}
                  </div></div>
              </div>
              <footer className="as-card-foot">
                <button type="button" className="as-btn" data-kind="green" aria-pressed={confirmed} data-testid="wa-looks-right" onClick={() => setConfirmed(true)}>
                  <CheckCircle size={14} aria-hidden /> {confirmed ? "Looks right ✓" : "Looks right"}</button>
                <button type="button" className="as-btn" data-testid="wa-fix-it" onClick={() => { setConfirmed(false); setOpen("watches"); }}>
                  <PencilSimple size={14} aria-hidden /> Fix it</button>
                <label className="wa-hint"><MagicWand size={13} aria-hidden />
                  <input value={hint} onChange={(e) => setHint(e.target.value)} data-testid="wa-hint" aria-label="A hint for the agent"
                         placeholder="Hint, e.g. “also wake me if it goes down”"
                         onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addHint(); } }} /></label>
              </footer>
            </section>

            {/* 3 · six boxes */}
            <section className="as-plan" aria-label="What BOSS will do" data-testid="wa-boxes">
              <header className="as-plan-head"><h3>What BOSS will do</h3>
                <span className="as-faint">Picked from the app and your words · click any box to change it</span></header>
              <div className="as-boxes">
                <BoxBtn k="signals" open={open} setOpen={setOpen} icon={<Lightning size={13} aria-hidden />} label="Signals" auto={read?.signals?.metrics ? "FOUND" : "AUTO"}
                        value={read?.signals?.value ?? "—"} sub={read?.signals?.sub ?? ""} />
                <BoxBtn k="collector" open={open} setOpen={setOpen} icon={<Funnel size={13} aria-hidden />} label="Collector" auto="AUTO"
                        value={read?.collector?.value ?? "—"} sub={read?.collector?.sub ?? ""} />
                <BoxBtn k="watches" open={open} setOpen={setOpen} icon={<Eye size={13} aria-hidden />} label="Watches" auto="AUTO"
                        value={`${live.length} watches`} sub={`${wakeN} wake · ${live.length - wakeN} tell`} />
                <BoxBtn k="oncall" open={open} setOpen={setOpen} icon={<Face agent={BEACON} size={14} />} label="On call"
                        value="SRE agent" sub={aloneNow ? "triage + fix alone" : prod ? "triage · prod waits for you" : "triage · fixes wait for you"} />
                <BoxBtn k="tell" open={open} setOpen={setOpen} icon={<Bell size={13} aria-hidden />} label="Tell" amber={outside && !read?.tell?.verified}
                        value={outside ? `${channel?.name || channel?.address} · you` : "you"}
                        sub={outside && !read?.tell?.verified ? `${TELL_KINDS.find((k) => k.key === channel?.kind)?.label ?? "Channel"}: verify once` : "console + your inbox"} />
                <BoxBtn k="runbooks" open={open} setOpen={setOpen} icon={<BookOpen size={13} aria-hidden />} label="Runbooks"
                        value={`${read?.runbooks.length ?? 0} built in`} sub="serving · app down" />
              </div>
              {open ? (
                <div className="as-pop" role="dialog" aria-label={BOX_TITLE[open]} data-testid={`wa-pop-${open}`}>
                  <header className="as-pop-h"><b>{BOX_TITLE[open]}</b><span className="ag-spacer" />
                    <button type="button" className="inf-link" data-testid="wa-pop-done" onClick={() => setOpen(null)}>Done</button></header>
                  {open === "signals" ? (
                    <div className="as-pop-b">
                      <span>{read?.signals?.word}. Nothing is pasted into the app and nothing is redeployed.</span>
                      <button type="button" className="inf-link" data-testid="wa-wire-from-signals" onClick={() => setWire(true)}>
                        <Wrench size={12} aria-hidden /> Wire it myself: OTel, Collector, remote write, /metrics, URL, alerts elsewhere</button>
                    </div>
                  ) : open === "collector" ? (
                    <div className="as-pop-b">
                      <span>{read?.collector?.kind === "prometheus" ? "BOSS's Prometheus already scrapes this project; Grafana draws it." : "BOSS's own scraper reads the app; deploy the Prometheus/Grafana stack from Sources when you want it."}</span>
                      <a className="inf-link" href={pxHref(`?area=observability&view=sources&project_id=${encodeURIComponent(projectId)}`)} data-testid="wa-sources-link">Sources ›</a>
                    </div>
                  ) : open === "watches" ? (
                    <div className="as-pop-b">
                      {watches.map((w) => { const e = editOf(w); return (
                        <label key={w.kind} className="inf-check"><input type="checkbox" checked={e.enabled} data-testid={`wa-on-${w.kind}`}
                          onChange={(ev) => setEdit(w, { ...e, enabled: ev.target.checked })} /> {w.title} <span className="as-faint">· {e.tone}</span></label>
                      ); })}
                      <span className="as-faint">Click a watch in Agent actions to change its numbers; the PromQL stays visible there.</span>
                    </div>
                  ) : open === "oncall" ? (
                    <div className="as-pop-b">
                      <span className="as-owner"><Face agent={BEACON} size={22} /> <b>{read?.onCall?.agent || "SRE"} agent</b> triages every watch.</span>
                      <label className="inf-check" title={prod ? "Production: a fix is a production deploy, which waits for a person" : undefined}>
                        <input type="checkbox" checked={aloneNow} disabled={prod} data-testid="wa-alone" onChange={(e) => setAlone(e.target.checked)} />
                        Fix alone below the line (investigate, fix PR, verify recovery)</label>
                      <span className="as-faint">{read?.boundary?.word}</span>
                      <a className="inf-link" href={pxHref("?area=budget&tab=autonomy")} data-testid="wa-decide-link">Where people decide ›</a>
                    </div>
                  ) : open === "tell" ? (
                    <TellPop channel={channel} you={read?.tell?.you ?? "you"} verified={!!read?.tell?.verified} set={(c) => setTell(c ?? "none")} />
                  ) : (
                    <div className="as-pop-b">
                      {(read?.runbooks ?? []).map((r) => <span key={r} className="as-chip as-mono">{r.replace("boss:runbook/", "")}</span>)}
                      <span className="as-faint">Built-in runbooks; a rule's own runbook URL wins (set it in the full builder).</span>
                    </div>
                  )}
                </div>
              ) : null}
              {read?.boundary ? <Strip b={read.boundary} alone={aloneNow} /> : null}
            </section>
          </>
        ) : null}

        {/* 4 · one primary */}
        <footer className="as-foot">
          <span className="as-chip as-mono" title="The read and the watches run on BOSS's own evaluator">{read?.cost || "≈ $0.20 a day · first data ≈ 1 min"}</span>
          <span className="as-faint"><ShieldCheck size={12} aria-hidden /> Prod and customers stay with you</span>
          <span className="ag-spacer" />
          <button type="button" className="inf-link" data-testid="wa-wire" title="OTel, Collector, remote write, /metrics, URL, other tool: the old paths, with tokens shown once"
                  onClick={() => setWire(true)}><Wrench size={12} aria-hidden /> Wire it myself</button>
          <GatedButton permission="observability:manage" projectId={projectId} className="as-primary" data-testid="wa-submit"
                       disabled={busy} data-blocked={blocker ? "1" : undefined} title={blocker ?? "Sets the watches, the channel and the SRE agent on call"}
                       onClick={() => void submit()}>
            <Eye size={14} aria-hidden /> {busy ? "Starting…" : "Start watching"} <kbd className="as-kbd" aria-hidden>⌘↵</kbd>
          </GatedButton>
        </footer>
        <ErrorLine error={error} testId="wa-refusal" />
      </div>
    </Sheet>
  );
}

function TellPop({ channel, you, verified, set }: {
  channel: { kind: string; name: string; url: string; address: string } | null; you: string; verified: boolean;
  set: (c: { kind: string; name: string; url: string; address: string } | null) => void;
}) {
  const c = channel ?? { kind: "", name: "", url: "", address: "" };
  const kind = TELL_KINDS.find((k) => k.key === c.kind) ?? TELL_KINDS[0];
  return (
    <div className="as-pop-b">
      <span className="wa-seg" role="radiogroup" aria-label="Tell" data-testid="wa-tell-kinds">
        {TELL_KINDS.map((k) => (
          <button key={k.key || "you"} type="button" role="radio" aria-checked={c.kind === k.key} data-testid={`wa-tell-${k.key || "you"}`}
                  onClick={() => set(k.key ? { ...c, kind: k.key } : null)}>{k.label}</button>
        ))}
      </span>
      <span className="as-faint">You always hear in the console{you && you !== "the console" ? ` and at ${you}` : ""}.</span>
      {c.kind ? (
        <div className="inf-form-row">
          <label className="inf-field"><span>{c.kind === "email" ? "Address" : "Channel"}</span>
            <input value={c.kind === "email" ? c.address || c.name : c.name} data-testid="wa-tell-name" autoComplete="off" spellCheck={false}
                   placeholder={c.kind === "email" ? "oncall@acme.io" : "#support-eng"}
                   onChange={(e) => set(c.kind === "email" ? { ...c, address: e.target.value, name: e.target.value } : { ...c, name: e.target.value })} /></label>
          {kind.needsUrl ? (
            <label className="inf-field" title="Write-only: it goes to the secret store and is never shown again"><span>Incoming webhook URL</span>
              <input type="password" value={c.url} data-testid="wa-tell-url" autoComplete="new-password" placeholder="https://hooks.slack.com/…"
                     onChange={(e) => set({ ...c, url: e.target.value })} /></label>
          ) : null}
        </div>
      ) : null}
      {c.kind ? <span className="as-faint" data-testid="wa-tell-gate"><HandPalm size={12} aria-hidden /> {verified ? "Already verified." :
        "BOSS sends it a 6-digit code; entering it is the one step that waits for you (Talking to the outside)."}</span> : null}
    </div>
  );
}

function Strip({ b, alone }: { b: WatchRead["boundary"] & object; alone: boolean }) {
  return (
    <div className="wa-strip" data-testid="wa-strip">
      <span className="wa-strip-l">Alone</span>
      {(alone ? b.alone : b.alone.slice(0, 1)).map((x) => <span key={x.key} className="wa-cx" data-testid={`wa-alone-${x.key}`}>{icon(x.icon, 11)}{x.label}</span>)}
      <span className="wa-strip-l" data-waits>Waits for you</span>
      {(alone ? b.waits : [{ key: "fix", label: "every fix", title: "Production deploy", why: "" }, ...b.waits.filter((w) => w.key !== "fix")]).map((x) => (
        <span key={x.key} className="wa-cx" data-waits data-testid={`wa-waits-${x.key}`} title={`${x.title}${x.why ? ` · ${x.why}` : ""} (Where people decide)`}>
          <HandPalm size={11} aria-hidden />{x.label}</span>
      ))}
    </div>
  );
}

function BoxBtn({ k, open, setOpen, icon: ic, label, value, sub, auto, amber }: {
  k: Box; open: Box | null; setOpen: (b: Box | null) => void; icon: React.ReactNode; label: string; value: string; sub: string; auto?: string; amber?: boolean;
}) {
  return (
    <button type="button" className="as-box" aria-expanded={open === k} data-amber={amber || undefined} data-testid={`wa-box-${k}`}
            onClick={() => setOpen(open === k ? null : k)} title={`${label}: ${value}`}>
      <span className="as-box-h">{ic}<span className="as-label">{label}</span>{auto ? <span className="as-auto">{auto}</span> : null}</span>
      <b className="as-box-v">{value}</b>
      <span className="as-box-s">{sub}</span>
    </button>
  );
}

// ── where it lands: the app's Health page ─────────────────────────────────────────────────

const STEP_ICON: Record<string, React.ReactNode> = {
  read: <MagnifyingGlass size={16} aria-hidden />, collector: <Funnel size={16} aria-hidden />, data: <Pulse size={16} aria-hidden />,
  watches: <Eye size={16} aria-hidden />, verify: <HandPalm size={16} aria-hidden />, oncall: <Eye size={16} aria-hidden />,
};

export function WatchedCard({ l, projectId, actor, onChanged }: { l: Landing; projectId: string; actor: string; onChanged: (l: Landing) => void }) {
  const [code, setCode] = useState("");
  const [said, setSaid] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [openKind, setOpenKind] = useState<string | null>(null);
  const [edit, setEdit] = useState<Edit | null>(null);
  const [why, setWhy] = useState("");
  const tellWord = l.tell ? `${l.tell.name} · you` : "you";
  const act = async (p: Promise<{ ok: true; data: Landing } | { ok: false; error: string }>, ok?: string) => {
    setBusy(true); setSaid(null);
    try {
      const r = await p;
      if (r.ok) { onChanged(r.data); setSaid(ok ?? null); return true; }
      setSaid(r.error); return false;
    } finally { setBusy(false); }
  };
  return (
    <section className="px-card wa-land" aria-labelledby={`wa-l-${l.targetId}`} data-testid={`wa-landing-${l.targetId}`}>
      <div className="wa-land-h">
        <span className="as-input-ico" style={{ width: 28, height: 28 }} aria-hidden><Heartbeat size={15} /></span>
        <h2 id={`wa-l-${l.targetId}`}>{l.label}</h2>
        <span className="as-tag" data-tone="green" data-testid="wa-l-state">watched</span>
        <span className="ag-spacer" />
        <span className="wa-oncall"><Face agent={BEACON} size={18} /> Beacon on call · {l.nightWord.toLowerCase()}</span>
      </div>
      <ol className="ss-flow" aria-label="Watching" data-testid="wa-l-steps" style={{ ["--n" as string]: l.steps.length } as React.CSSProperties}>
        {l.steps.map((s) => (
          <li key={s.key} className="ss-node" data-state={s.state} data-testid={`wa-l-step-${s.key}`}>
            <span className="ss-dot">{s.state === "done" ? <Check size={16} weight="bold" aria-hidden /> : STEP_ICON[s.key] ?? <Eye size={16} aria-hidden />}</span>
            <b>{s.label}</b><span>{s.detail}</span>
          </li>
        ))}
      </ol>
      <div className="ss-now" data-tone={l.now.tone} data-testid="wa-l-now"><Face agent={BEACON} size={18} /><b>Now</b>
        <span className="ss-now-text" data-testid="wa-l-now-text">{l.now.text}</span></div>
      {l.gate ? (
        <div className="wa-gate" data-testid="wa-l-gate">
          <span className="as-hand" aria-hidden>✋</span>
          <span className="wa-gate-t"><b>Verify {l.gate.name}</b> · {l.gate.word}</span>
          <input className="wa-code-in" inputMode="numeric" maxLength={6} value={code} aria-label={`The code sent to ${l.gate.name}`} data-testid="wa-l-code"
                 placeholder="000000" onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} />
          <GatedButton permission="observability:manage" projectId={projectId} className="wa-btn-amber" data-testid="wa-l-verify"
                       disabled={busy || code.length !== 6} title={code.length !== 6 ? "Enter the 6 digits" : "Allow messages to this channel"}
                       onClick={() => void act(verifyChannel(projectId, actor, l.targetId, code), `${l.gate?.name} verified.`).then((ok) => ok && setCode(""))}>
            Enter code</GatedButton>
          <GatedButton permission="observability:manage" projectId={projectId} className="inf-link" data-testid="wa-l-resend" disabled={busy}
                       onClick={() => void act(resendCode(projectId, actor, l.targetId), "A new code is on its way.")}>Send again</GatedButton>
        </div>
      ) : null}
      {said ? <span className="as-faint" role="status" data-testid="wa-l-said">{said}</span> : null}
      <div className="wa-watches" data-testid="wa-l-watches">
        {l.watches.map((w) => (
          <React.Fragment key={w.ruleId ?? w.kind}>
            <button type="button" className="wa-watch" aria-expanded={openKind === w.kind} data-testid={`wa-l-watch-${w.kind}`} title={w.condition}
                    onClick={() => { const opening = openKind !== w.kind; setOpenKind(opening ? w.kind : null); setEdit(opening ? { params: w.params, forS: w.forS, tone: w.tone, enabled: true } : null); setWhy(""); }}>
              <span className="wa-watch-ico">{icon(w.icon)}</span>
              <span className="wa-watch-t"><span>{w.title}</span><small>{w.state === "firing" ? "firing now" : w.state ? w.state.replace(/_/g, " ") : "quiet"} · {w.from}</small></span>
              <span className="wa-tone" data-tone={w.tone} data-state={w.state === "firing" ? "firing" : undefined}>{w.state === "firing" ? "firing" : w.tone}</span>
            </button>
            {openKind === w.kind && edit && w.ruleId ? (
              <WatchPop w={w} edit={edit} set={setEdit} tellWord={tellWord} targetId={l.targetId} why={why} setWhy={setWhy} busy={busy}
                        onRemove={() => void act(changeWatch(projectId, actor, l.targetId, { rule_id: w.ruleId!, reason: why, remove: true }), "Removed.")
                          .then((ok) => ok && setOpenKind(null))}
                        onDone={() => void act(changeWatch(projectId, actor, l.targetId, { rule_id: w.ruleId!, reason: why, params: edit.params, for_s: edit.forS, tone: edit.tone }), "Saved, with its why.")
                          .then((ok) => ok && setOpenKind(null))} />
            ) : null}
          </React.Fragment>
        ))}
      </div>
      <Strip b={l.boundary} alone={l.alone} />
      {l.incidents.length ? (
        <div className="wa-flows" data-testid="wa-l-incidents">
          {l.incidents.map((i) => <IncidentFlow key={i.id} i={i} alone={l.alone} prod={l.boundary.prod} env={l.env} />)}
        </div>
      ) : null}
    </section>
  );
}

const short = (s: string, n = 42) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** An incident as the path it takes: the signal → SRE triage → on the list? → alone or you. */
function IncidentFlow({ i, alone, prod, env }: { i: Landing["incidents"][number]; alone: boolean; prod: boolean; env: string }) {
  const resolved = i.status === "resolved" || i.status === "dismissed";
  return (
    <div className="wa-flow" data-testid={`wa-l-inc-${i.id}`} aria-label={`Incident: ${i.title}`}>
      <span className="wa-fl-n" data-t="signal"><b><Lightning size={11} weight="bold" aria-hidden /> {i.title}</b><small>{env}</small></span><span className="wa-fl-w" />
      <span className="wa-fl-n" data-t="agent" title={i.triage || i.status}><b>SRE triage</b><small>{short(i.triage || i.status)}</small></span><span className="wa-fl-w" />
      <span className="wa-fl-n" data-t="decision"><b>On the list?</b><small>{prod ? "prod · yes" : `${env} · no`}</small></span><span className="wa-fl-w" />
      {resolved ? <span className="wa-fl-n" data-t="done"><b>✓ Recovered</b><small>{alone ? "alone" : "after you"}</small></span>
        : alone ? <span className="wa-fl-n" data-t="done" title={i.response || undefined} data-testid={`wa-l-inc-response-${i.id}`}>
            <b>Fix alone</b><small>{i.response ? short(i.response) : "investigate · fix PR"}</small></span>
        : <span className="wa-fl-n" data-t="you"><b>✋ You approve</b><small>1 click</small></span>}
    </div>
  );
}

/** The door on Health: "Watch this app" opens the sheet; nothing else to learn. */
export function WatchDoor({ projectId, watched }: { projectId: string; watched: number }) {
  return (
    <div className="wa-cta" data-testid="px-h-watch-row">
      <GatedButton permission="observability:manage" projectId={projectId} className="wa-cta-btn" data-testid="px-h-watch"
                   title="One input: the SRE agent sets up the watches and goes on call" onClick={() => openWatch()}>
        <Pulse size={14} aria-hidden /> {watched ? "Watch another app" : "Watch this app"}</GatedButton>
      <span className="as-faint">{watched ? `${watched} app${watched > 1 ? "s" : ""} watched · Beacon on call` : "One input; the SRE agent sets it up and goes on call"}</span>
    </div>
  );
}
