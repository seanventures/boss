"use client";

/**
 * A build's cockpit (factory spec §4.5): one page, not twelve stacked blocks.
 *
 *   header      ← Floor, app, state word, k of n, elapsed, cost, Steer, Stop, ⋯
 *               title, attempt n ▾, from <origin> , faces, Credits
 *   needs you   the Floor's own AttentionCards for this build (absent when nothing waits)
 *   the Line    stations staffed by faces; a bay filters the Story, a face opens its Act card
 *   Story       what each agent did, oldest first          │ Outputs: 8 rows, one open at a time
 *   Logs ▾      the only place raw text appears
 *
 * Everything said is the server's (`/v1/code-tasks/{id}/cockpit`, lib/cockpit.ts): the state
 * word, `k of n`, each station's word, each output's word. Elapsed ticks here from `started_at`;
 * there is no percent and no ETA anywhere.
 */
import { AVATAR_STATES } from "../../lib/avatar-kit/states";
import { logHref } from "../../lib/log-url";
import { DotsThree, SpinnerGap } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { AvatarState } from "../../lib/agent-world-types";
import { useCockpit, type Cockpit as CockpitData } from "../../lib/cockpit";
import type { StationKey } from "../../lib/factory-floor";
import { openDockAbout } from "../../lib/dock-subject";
import { previewWebHref } from "../../lib/browser-links";
import { isConsoleLive, subscribeConsoleLive } from "../../lib/console-live";
import { callAction } from "../../lib/call-action";
import { GatedButton } from "../../components/gated";
import { retryCodeTaskWithStateAction } from "../actions";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { AgFaceStack } from "../agents/agent-face";
import { AttentionCard } from "../agents/attention-card";
import { useNow, useBand } from "../agents/live-util";
import { CreditsButton } from "../agents/run-credits";
import { useRunChain } from "../agents/run-acts";
import { nameOf } from "../agents/run-face";
import { ChangeIt } from "./change-it";
import { ResultReview } from "./result-review";
import { addFeatureHref } from "./apps-tab";
import { AppSetupSheet } from "../../components/v2/templates/setup-sheet";
import { stopBuild, steerBuild } from "./cockpit-actions";
import { RateIt, ReasonChips, sendReasons } from "../improve/rate-it";
import { ShareRunSheet } from "../improve/share-run";
import { ActPopover, StationFaceView, areaHref, money, secondsWord, spanOf } from "./cockpit-util";
import { DiagnosisFlow, WhyLine } from "../../components/stuck-work";
import { isWaitingForGitHub } from "../../lib/github-wait";
import { LogsDrawer, type LogTab } from "./logs-drawer";
import { Outputs, defaultOpen, type OutputKey } from "./outputs";
import { StationLine, bayWord } from "./station-line";
import { NowBand } from "./now-band";
import { useToolLog } from "./tool-log";
import { Story, bayLabel } from "./story";
import { plural } from "../../lib/factory-floor";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { ChangeSetCockpit, useRunChangeSet } from "../multirepo/change-set-cockpit";
import { LearnedStrip } from "../knowledge/learned-strip";
import { useAreaKeydown } from "../../lib/area-keys";

export type CockpitProps = {
  projectId: string;
  /** The build (code task run id) from `?build=`. */
  runId: string;
  /** `?logs=build|preview|deploy`: open the Logs drawer on that tab (a "Live log" link). */
  initialLogs?: LogTab | null;
  actor: string;
  orgId?: string | null;
  /** `← Floor` / Esc: the shell clears `build=` and restores focus to the opening row. */
  onClose: () => void;
  /** Picking another attempt switches `build=`. */
  onOpenBuild: (runId: string) => void;
};

const AGENT_STATES = new Set<string>(AVATAR_STATES);
const quietish = (s: string) => s === "quiet" || s === "budget_wait" || s === "blocked";

function originHref(kind: string, id: string): string {
  if (kind === "workflow_run") return areaHref({ area: "delivery_workflows", wfr: id });
  if (kind === "meeting") return areaHref({ area: "meetings", meeting: id });
  // The research dossier: `?area=discovery&run=drun_…` (discoveries-ui.tsx reads `run`).
  return areaHref({ area: "discovery", run: id });
}

/**
 * What to ask BOSS about THIS build, from what it recorded (the old page's suggestions): the
 * findings it shipped past, the checks that never ran or only advised, a mutation the tests
 * missed, criteria nobody proved, why an attempt failed. Nothing is suggested without its fact.
 */
export function askSuggestions(c: Pick<CockpitData, "app" | "brief" | "lineage" | "failure" | "run">): string[] {
  const b = c.brief;
  const out: string[] = [];
  if (b.deferredFindings) out.push(`Fix the ${b.deferredFindings} deferred review finding${b.deferredFindings === 1 ? "" : "s"} on ${c.app}`);
  if (b.notRun.length) out.push(`No ${b.notRun.join(", ")} ran on this build, what would they most likely find?`);
  else if (b.advisoryFailing) out.push(`${b.advisoryFailing} checks failed but shipped as advisory, which should block next time?`);
  if (b.mutationCaught === false) out.push("The tests missed an injected bug, where are they weakest?");
  if (b.criteria.length) out.push(`Which of the ${b.criteria.length} acceptance criteria are not actually covered by a test?`);
  const broken = c.lineage.find((l) => l.state === "failed");
  if (b.error || broken) out.push(`Why did ${broken && broken.runId !== c.run.id ? `attempt ${broken.attempt}` : "this build"} fail${b.error ? `, ${b.error.slice(0, 60)}` : ""}?`);
  out.push(`Given the brief and review notes for ${c.app}, what should we build next, and why?`);
  return out.slice(0, 5);
}

/** What a retry reuses, said before the press (the old Retry's tooltip, mirroring the server's
 *  _repair_needs_write_tests): it resumes, it does not rebuild from scratch. */
const TEST_GATES = new Set(["tests_strong_enough", "tests_written_first", "coder_did_not_edit_tests",
  "written_tests_pass", "existing_tests_intact"]);
export function retryTip(c: Pick<CockpitData, "outputs">): string {
  const failing = (c.outputs.checks?.gates ?? []).filter((g) => !g.ok && !g.waivedBy).map((g) => g.name);
  const reusesTests = failing.length > 0 && !failing.some((g) => TEST_GATES.has(g));
  return `Resumes on the same branch, reusing the plan${reusesTests ? " and the tests" : ""}`;
}

function usePaused(): boolean {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    setPaused(!isConsoleLive());
    return subscribeConsoleLive((on) => setPaused(!on));
  }, []);
  return paused;
}

export function Cockpit({ projectId, runId, actor, onClose, onOpenBuild, initialLogs }: CockpitProps) {
  const { data, error, loading, refresh } = useCockpit(projectId, runId, actor);
  // A build that is one lane of a multi-repo change opens the change's cockpit (every lane, one
  // set of PRs); an ordinary build is answered 404 and keeps this page exactly as it was.
  const changeSet = useRunChangeSet(projectId, runId);
  // Before the build has loaded (or while BOSS can't be reached) Esc still goes back.
  useEffect(() => {
    if (data || changeSet) return undefined;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); onClose(); } };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [data, changeSet, onClose]);
  if (changeSet) {
    return <ChangeSetCockpit key={changeSet.id} projectId={projectId} changeSetId={changeSet.id} initial={changeSet}
                             actor={actor} onClose={onClose} />;
  }
  if (!data) {
    if (error && !loading) {
      return (
        <section className="fx-cockpit" data-testid="fx-cockpit" data-run={runId}>
          <button type="button" className="ag-link fx-back" onClick={onClose}>← Builds</button>
          <span className="fx-unreachable" data-testid="fx-unreachable">
            <span>Can&apos;t reach BOSS</span>
            <button type="button" className="ag-btn" title={error} onClick={refresh}>Retry</button>
          </span>
        </section>
      );
    }
    return <CockpitSkeleton runId={runId} onClose={onClose} />;
  }
  return <CockpitView key={data.run.id} c={data} projectId={projectId} runId={runId} actor={actor}
                      onClose={onClose} onOpenBuild={onOpenBuild} refresh={refresh} initialLogs={initialLogs ?? null} />;
}

function CockpitSkeleton({ runId, onClose }: { runId: string; onClose: () => void }) {
  const labels = ["Brief", "Pull request", "Checks", "Tests", "Changes", "Comments", "Preview", "Deploy", "Cost"];
  return (
    <section className="fx-cockpit" data-testid="fx-cockpit" data-run={runId} aria-busy="true">
      <span className="ag-sr" role="status">Loading</span>
      <button type="button" className="ag-link fx-back" onClick={onClose}>← Builds</button>
      <div className="fx-skel-line" aria-hidden>{Array.from({ length: 8 }, (_, i) => <span key={i} className="fx-skel-bay" />)}</div>
      <div className="fx-body">
        <div className="fx-story" aria-hidden>{Array.from({ length: 4 }, (_, i) => <span key={i} className="fx-skel-bar" />)}</div>
        <ul className="fx-outputs fx-out-list">
          {labels.map((l) => <li key={l} className="fx-out"><span className="fx-out-head"><span className="fx-out-label">{l}</span><span className="fx-out-value">{"—"}</span></span></li>)}
        </ul>
      </div>
    </section>
  );
}

function CockpitView({ c, projectId, runId, actor, onClose, onOpenBuild, refresh, initialLogs = null }: {
  c: CockpitData; projectId: string; runId: string; actor: string;
  onClose: () => void; onOpenBuild: (runId: string) => void; refresh: () => void;
  initialLogs?: LogTab | null;
}) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const h1 = useRef<HTMLHeadingElement>(null);
  const root = useRef<HTMLElement>(null);
  const now = useNow(1000);
  const band = useBand();
  const paused = usePaused();
  const menuId = useId();
  const [filter, setFilter] = useState<StationKey | null>(null);
  const [open, setOpen] = useState<OutputKey | null>(() => defaultOpen(c));
  // A "Live log" link (`?logs=build`) lands with this build's log open, not on a closed drawer.
  const [logs, setLogs] = useState<LogTab | null>(initialLogs);
  const [menu, setMenu] = useState(false);
  const [attempts, setAttempts] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  // STOP IS INSTANT (walk D1): the press says `Stopped by you` at once; the server's answer
  // confirms it on the next read, or the press is undone with its sentence.
  const [stopped, setStopped] = useState(false);
  const [steering, setSteering] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [guidance, setGuidance] = useState("");
  const [steerChips, setSteerChips] = useState<string[]>([]);
  const [sharing, setSharing] = useState(false);
  const [diagnostics, setDiagnostics] = useState(false);
  const [pending, setPending] = useState<{ id: string; text: string; at: string }[]>([]);
  const [changeIt, setChangeIt] = useState(false);
  const [reviewChange, setReviewChange] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const [pop, setPop] = useState<{ actId: string; anchor: HTMLElement | null } | null>(null);
  const [announce, setAnnounce] = useState("");
  const lastWord = useRef(c.state.word);
  const lineWrap = useRef<HTMLDivElement>(null);
  // Outputs stick below the sticky Line: its real height, measured (walk D6).
  useEffect(() => {
    const el = lineWrap.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const set = () => root.current?.style.setProperty("--fx-line-h", `${Math.round(el.getBoundingClientRect().height)}px`);
    set();
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const quietOpened = useRef(false);
  const gPressed = useRef(false);

  // Opening the cockpit focuses its title (spec §7 Focus).
  useEffect(() => { h1.current?.focus({ preventScroll: true }); }, []);

  // One polite announcement per state word change; never the `Now:` line.
  useEffect(() => {
    if (c.state.word !== lastWord.current) {
      lastWord.current = c.state.word;
      setAnnounce(c.state.word);
    }
  }, [c.state.word]);

  // A quiet build opens Logs on its own, once.
  useEffect(() => {
    if (c.state.state === "quiet" && !quietOpened.current) {
      quietOpened.current = true;
      setLogs((t) => t ?? "build");
    }
  }, [c.state.state]);

  // A newly failed check opens Checks; the PR opens when it starts waiting on you.
  const lastState = useRef(c.state.state);
  useEffect(() => {
    if (lastState.current !== c.state.state) {
      lastState.current = c.state.state;
      const d = defaultOpen(c);
      if (d) setOpen(d);
    }
  }, [c]);

  const onFace = useCallback((actId: string, anchor?: HTMLElement) => setPop({ actId, anchor: anchor ?? null }), []);

  const doStop = async () => {
    setConfirmStop(false);
    setErr("");
    setStopped(true);
    showToast("success", "Build stopped");
    const r = await stopBuild(runId, actor);
    // 409: it had already ended, nothing to undo; the next read says how.
    if (!r.ok && r.status !== 409) { setStopped(false); setErr(r.error ?? ""); }
    notifyChanged("code-tasks");
    refresh();
  };

  const doRetry = async () => {
    setMenu(false);
    setBusy("retry");
    setErr("");
    const form = new FormData();
    form.set("run_id", runId);
    form.set("actor", actor);
    const r = await callAction(retryCodeTaskWithStateAction, undefined, "code_task:retry")(null, form);
    setBusy(null);
    if (r?.error) { setErr(r.error); return; }
    showToast("success", r?.attempt ? `Attempt ${r.attempt} started` : "Attempt started");
    notifyChanged("code-tasks");
    if (r?.runId) onOpenBuild(r.runId);
  };

  const doSteer = async () => {
    const text = guidance.trim();
    if (!text || busy) return;
    setBusy("steer");
    setErr("");
    const r = await steerBuild(runId, actor, text);
    setBusy(null);
    if (r.ok) {
      // The steer's reasons ride with it (optional, quiet): why the agent needed steering.
      void sendReasons(projectId, actor, "steer", runId, steerChips);
      setSteerChips([]);
      setPending((p) => [...p, { id: `pending-${Date.now()}`, text, at: new Date().toISOString() }]);
      setGuidance("");
      setSteering(false);
      showToast("success", "Guidance queued");
      refresh();
    } else setErr(r.error ?? "");
  };

  const copyLink = () => {
    setMenu(false);
    try { void navigator.clipboard?.writeText(window.location.href); showToast("success", "Link copied"); } catch { /* nothing */ }
  };

  // Keyboard (spec §7): L toggles Logs, G then O focuses Outputs, Esc closes drawer → clears the
  // filter → back to the Floor. Never while typing.
  useAreaKeydown((e: KeyboardEvent) => {
    const t = e.target as HTMLElement | null;
    if (e.altKey || e.metaKey || e.ctrlKey) return;
    if (t?.closest?.("input, textarea, select, [contenteditable='true']")) {
      if (e.key === "Escape" && steering) { setSteering(false); e.preventDefault(); }
      return;
    }
    if (e.defaultPrevented) return;
    const key = e.key;
    if (key === "Escape") {
      if (pop || changeIt) return;
      e.preventDefault();
      if (menu || attempts || confirmStop) { setMenu(false); setAttempts(false); setConfirmStop(false); return; }
      if (logs) { setLogs(null); return; }
      if (filter) { setFilter(null); return; }
      onClose();
      return;
    }
    if (key === "l" || key === "L") { setLogs((v) => (v ? null : "build")); return; }
    if (key === "g" || key === "G") { gPressed.current = true; window.setTimeout(() => { gPressed.current = false; }, 800); return; }
    if ((key === "o" || key === "O") && gPressed.current) {
      gPressed.current = false;
      root.current?.querySelector<HTMLElement>(".fx-outputs")?.focus();
    }
  }, { target: "document" });

  const settled = c.state.state === "cancelled" || c.state.state === "failed";
  const s = stopped && !settled
    ? { ...c.state, state: "cancelled" as const, word: "Stopped by you", tip: "Keeps the work so far. Retry resumes it." }
    : c.state;
  const stoppable = c.stoppable && !stopped;
  const still = paused || quietish(s.state) || s.state === "cancelled";
  const elapsed = c.startedAt ? spanOf(c.startedAt, c.live ? null : c.run.updatedAt ?? null, c.live ? now : null) : "";
  const current = c.stations.find((st) => st.key === s.station);
  const currentAgent = current?.roleKey ? nameOf(current.roleKey).name : "the agents";
  const faceStates = new Map<string, AvatarState>(c.agents.map((a) => [a.roleKey,
    (still && (a.state === "working" || a.state === "needs_you") ? "idle" : AGENT_STATES.has(a.state) ? a.state : "idle") as AvatarState]));
  const attempt = c.lineage.find((l) => l.runId === c.run.id)?.attempt ?? c.run.attempt;
  // Builds of this app beyond this lineage (a follow-up, a change on its branch): the menu
  // lists them all, so it names itself by count.
  const sameApp = c.lineage.some((l) => !l.sameLineage);
  // CREDITS ARE THIS BUILD'S ACTS (its own chain, /v1/code-tasks/{id}/acts), not the workflow
  // run that started it, which credited one agent and $0.00 while Outputs said five.
  const { chain } = useRunChain(projectId, c.run.id, c.live);
  const latestDeploy = (c.outputs.deploy.items[0]?.id as string | undefined) ?? null;
  const stopTip = "Keeps the work so far. Retry resumes it.";
  // The tool log, read again whenever the build has made more tool calls.
  const toolCount = c.story.reduce((n, r) => n + (r.type === "act" ? r.toolCalls : 0), 0);
  const { log } = useToolLog(projectId, c.run.id, `${toolCount}:${c.live}:${c.run.updatedAt ?? ""}`);
  const steerTip = "Guide the agents. They read it between stations.";

  return (
    <section ref={root} className="fx-cockpit" data-testid="fx-cockpit" data-run={c.run.id} data-state={s.state}
             data-still={still || undefined} data-band={band}>
      <span className="ag-sr" aria-live="polite" data-testid="fx-announce">{announce}</span>
      <header className="fx-ck-head">
        <div className="fx-head-1">
          <button type="button" className="ag-link fx-back" onClick={onClose} data-testid="fx-back">← Builds</button>
          <h1 ref={h1} tabIndex={-1} className="fx-ck-app ag-trunc" title={c.app} data-user-text>{c.app}</h1>
          {/* THE EVERYDAY JOB, IN THE OPEN: a feature added to this app, built on its branch with its
              tests kept. It used to be ⋯ › Change it; that sheet stays for a rebuild. */}
          <a className="ag-btn fx-add-feature" data-testid="fx-add-feature"
             href={addFeatureHref(c.run.id)}
             title="New build, adding to this app: its branch, its PR, its tests">＋ Add a feature</a>
          {/* THE APP'S SETTINGS, IN THE OPEN: secrets, deploy settings and the sign-in guide were
              behind Factory › Apps › ⋯ › Setup, which an operator asked for twice could not find. */}
          <button type="button" className="ag-btn" data-testid="fx-app-settings"
                  title="Its settings, connectors and sign-in setup"
                  onClick={() => setSettingsOpen(true)}>Settings</button>
          <span className="fx-state" data-state={s.state} data-testid="fx-state">
            <span className="fx-state-word" title={s.tip || undefined}>{s.word}</span>
            {s.n ? <span className="fx-k" title="Stations done. BOSS doesn't guess finish times." data-testid="fx-k">{s.k} of {s.n}</span> : null}
            {elapsed ? <span className="fx-elapsed" data-testid="fx-elapsed">{elapsed}</span> : null}
            {c.metered && c.costUsd > 0 ? <span className="fx-cost" title="Metered model spend so far" data-testid="fx-cost">{money(c.costUsd)}</span> : null}
            {s.preview === "running" && c.outputs.preview ? (
              <a className="fx-flag" href={previewWebHref(c.outputs.preview.id)}
                 target="_blank" rel="noreferrer" data-testid="fx-preview-flag">Preview live</a>
            ) : null}
            {paused ? <span className="fx-flag" title="Live updates are paused. Press Live in the top bar.">Paused</span> : null}
          </span>
          <span className="fx-spacer" />
          <span className="fx-actions">
            {stoppable ? (
              <GatedButton permission="code_task:steer" type="button" className="ag-btn fx-wide-only" title={steerTip} aria-expanded={steering}
                      data-testid="fx-steer" onClick={() => setSteering((v) => !v)}>Steer</GatedButton>
            ) : null}
            {stoppable ? (
              <span className="fx-pop-anchor">
                <GatedButton permission="code_task:cancel" type="button" className="ag-btn fx-wide-only" title={stopTip} data-testid="fx-stop"
                        disabled={busy !== null} onClick={() => setConfirmStop(true)}>
                  {busy === "stop" ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}Stop
                </GatedButton>
              </span>
            ) : null}
            <span className="fx-pop-anchor">
              <button type="button" className="ag-btn ag-btn-icon" aria-label="More" aria-haspopup="menu" aria-expanded={menu}
                      aria-controls={menu ? menuId : undefined} data-testid="fx-more" onClick={() => setMenu((v) => !v)}>
                <DotsThree size={16} weight="bold" aria-hidden />
              </button>
              {menu ? (
                <span id={menuId} role="menu" className="fx-ck-menu" data-testid="fx-ck-menu">
                  {stoppable ? <GatedButton permission="code_task:steer" type="button" role="menuitem" className="fx-phone-only" onClick={() => { setMenu(false); setSteering(true); }}>Steer</GatedButton> : null}
                  {stoppable ? <GatedButton permission="code_task:cancel" type="button" role="menuitem" className="fx-phone-only" title={stopTip} onClick={() => { setMenu(false); setConfirmStop(true); }}>Stop</GatedButton> : null}
                  {c.retryable ? <GatedButton permission="code_task:retry" type="button" role="menuitem" data-testid="fx-menu-retry" title={retryTip(c)} onClick={() => void doRetry()}>Retry</GatedButton> : null}
                  <button type="button" role="menuitem" data-testid="fx-menu-change" onClick={() => { setMenu(false); setChangeIt(true); }}>Change it</button>
                  <button type="button" role="menuitem" data-testid="fx-menu-ask" onClick={() => {
                    setMenu(false);
                    // The dock's starters for THIS build (agent-ui.tsx reads them by run id).
                    try { window.sessionStorage.setItem(`boss.agent.suggests.${c.run.id}`, JSON.stringify(askSuggestions(c))); } catch { /* private mode */ }
                    openDockAbout({ subject: { kind: "code_task", id: c.run.id }, question: "", role: null, history: "push" });
                  }}>Ask BOSS</button>
                  <a role="menuitem" href={logHref({ run: c.run.id })}>Activity log </a>
                  <button type="button" role="menuitem" onClick={copyLink}>Copy link</button>
                  <GatedButton permission="telemetry:share" type="button" role="menuitem" data-testid="fx-menu-diagnostics"
                               onClick={() => { setMenu(false); setDiagnostics(true); }}>Download diagnostics</GatedButton>
                  <button type="button" role="menuitem" data-testid="fx-menu-share"
                          title="Send this run, redacted and edited by you, to the BOSS project"
                          onClick={() => { setMenu(false); setSharing(true); }}>Share this run</button>
                </span>
              ) : null}
            </span>
            {confirmStop ? (
              <span role="alertdialog" aria-label="Stop this build?" className="fx-confirm" data-testid="fx-stop-confirm">
                <span>Stop this build?</span>
                <GatedButton permission="code_task:cancel" type="button" className="ag-btn" data-tone="reject" title={stopTip} data-testid="fx-stop-yes" onClick={() => void doStop()}>Stop</GatedButton>
                <button type="button" className="ag-link" onClick={() => setConfirmStop(false)}>Cancel</button>
              </span>
            ) : null}
          </span>
        </div>
        <div className="fx-head-2">
          <span className="fx-ck-title ag-trunc" title={c.run.title} data-user-text>{c.run.title}</span>
          {c.lineage.length > 1 ? (
            <span className="fx-pop-anchor">
              <button type="button" className="ag-link" aria-haspopup="menu" aria-expanded={attempts} data-testid="fx-attempts"
                      title="Every build of this app: retries, follow-ups, changes" onClick={() => setAttempts((v) => !v)}>
                {sameApp ? `${plural(c.lineage.length, "build")} ▾` : `attempt ${attempt} ▾`}
              </button>
              {attempts ? (
                <span role="menu" className="fx-ck-menu" data-testid="fx-attempts-menu">
                  {c.lineage.map((l) => (
                    <button key={l.runId} type="button" role="menuitem" aria-current={l.runId === c.run.id || undefined}
                            data-testid={`fx-attempt-${l.runId}`} title={l.title || undefined}
                            onClick={() => { setAttempts(false); if (l.runId !== c.run.id) onOpenBuild(l.runId); }}>
                      {l.sameLineage || !l.kind ? `Attempt ${l.attempt}` : l.kind}, {l.word}
                      {l.prNumber ? `, PR #${l.prNumber}` : ""}
                      {c.metered && l.costUsd > 0 ? `, ${money(l.costUsd)}` : ""}
                      {l.at ? `, ${secondsWord(Math.max(0, Math.round(((now ?? Date.parse(l.at)) - Date.parse(l.at)) / 1000)))}` : ""}
                    </button>
                  ))}
                </span>
              ) : null}
            </span>
          ) : null}
          {c.origin ? (
            <a className="ag-link" href={originHref(c.origin.kind, c.origin.id)} data-testid="fx-origin">
              from <span data-user-text>{c.origin.label}</span></a>
          ) : null}
          {!stoppable ? <RateIt projectId={projectId} actor={actor} subject="build" id={c.run.id} testId="fx-rate" /> : null}
          <span className="fx-spacer" />
          {c.agents.length ? <AgFaceStack roleKeys={c.agents.map((a) => a.roleKey)} size={24} max={6} states={faceStates} /> : null}
          <CreditsButton chain={chain} runId={c.run.id} projectId={projectId} viewer={actor} title={c.app}
                         live={c.live} autoOpen={false} className="ag-btn fx-credits" />
        </div>
        {steering ? (
          <form className="fx-steer" onSubmit={(e) => { e.preventDefault(); void doSteer(); }} data-testid="fx-steer-box">
            <input className="input" autoFocus value={guidance} onChange={(e) => setGuidance(e.target.value)}
                   placeholder={`Tell ${currentAgent} …`} aria-label="Guidance" data-testid="fx-steer-input" />
            <GatedButton permission="code_task:steer" type="submit" className="ag-btn ag-btn-primary" disabled={!guidance.trim() || busy !== null}>Send</GatedButton>
            <ReasonChips value={steerChips} onChange={setSteerChips} testId="fx-steer-why" />
          </form>
        ) : null}
        {sharing ? <ShareRunSheet projectId={projectId} actor={actor} unitKind="build" unitId={c.run.id}
                                  onClose={() => setSharing(false)} /> : null}
        {diagnostics ? <ShareRunSheet projectId={projectId} actor={actor} unitKind="build" unitId={c.run.id}
                                      localOnly onClose={() => setDiagnostics(false)} /> : null}
        {err ? <span className="fx-err" role="alert" data-user-text data-testid="fx-error">{err}</span> : null}
        {settingsOpen ? <AppSetupSheet projectId={projectId} runId={c.run.id} appName={c.app}
                                       tab="settings" onClose={() => setSettingsOpen(false)} /> : null}
      </header>
      {/* WAITING FOR WHAT, said under the header (never a bare "Queued"), and what the Shepherd
          found when it stopped moving (stuck_work). */}
      {c.why && (!c.diagnosis || isWaitingForGitHub(c.why.code)) ? <div className="fx-why"><WhyLine why={c.why} testId="fx-why" /></div> : null}
      <DiagnosisFlow d={c.diagnosis} testId="fx-diagnosis" />

      {c.attention.length ? (
        <div className="fx-strip" data-testid="fx-strip" aria-label="Needs you" role="region">
          {c.attention.map((item) => (
            <AttentionCard key={item.id} item={item} projectId={projectId} actor={actor} still={still}
                           onDone={refresh} onRefresh={refresh} />
          ))}
        </div>
      ) : null}

      <div className="fx-line-wrap" ref={lineWrap}>
        <StationLine stations={c.stations} density="header" still={still} now={now} viewer={actor}
                     selected={filter} onBay={(k) => setFilter((f) => (f === k ? null : k))} />
      </div>
      <ol className="fx-stations-v" aria-label="Stations" data-testid="fx-stations-v">
        {c.stations.map((st) => (
          <li key={st.key} className="fx-vstation" data-status={st.status} aria-label={bayLabel(st)}>
            <button type="button" className="fx-vstation-btn" onClick={() => setFilter((f) => (f === st.key ? null : st.key))}>
              <span className="fx-vstation-label">{st.label}</span>
              <StationFaceView station={st} size={24} still={still} />
              {/* The same word, clock rule and reason as the header line (bayWord). */}
              <span className="fx-vstation-word" title={bayWord(st, now).tip}>{bayWord(st, now).text}</span>
            </button>
          </li>
        ))}
      </ol>

      <NowBand c={c} now={now} />
      <LearnedStrip projectId={projectId} actor={actor} runId={c.run.id} live={c.live} />
      {c.run.status === "completed" ? <ResultReview key={c.run.id} runId={c.run.id}
        onChangeRequested={(note) => { setReviewChange(note); setChangeIt(true); }} /> : null}

      <div className="fx-body">
        <Story log={log} projectId={projectId} runId={c.run.id} viewer={actor} rows={c.story} stations={c.stations} now={c.now}
               comments={c.outputs.comments.items} filter={filter} onClearFilter={() => setFilter(null)}
               onFace={onFace} onLogs={() => setLogs("build")} pending={pending} still={still} />
        <Outputs projectId={projectId} actor={actor} cockpit={c} open={open} onOpen={setOpen}
                 onLogs={(t) => setLogs(t)} onRefresh={refresh} />
      </div>

      <button type="button" className="ag-link fx-logs-toggle" aria-expanded={!!logs} data-testid="fx-logs-toggle"
              onClick={() => setLogs((v) => (v ? null : "build"))}>
        Logs {logs ? "▴" : "▾"}
      </button>
      {/* Portalled to <body>: on a phone it is a full-screen sheet, which a fixed box inside a
          filtered ancestor never was (walk D5: 2521px tall, top −1677). */}
      {logs ? createPortal(
        <LogsDrawer projectId={projectId} runId={c.run.id} previewId={c.outputs.preview?.id ?? null}
                    previewStatus={c.outputs.preview?.status} deployId={latestDeploy}
                    tab={logs} onTab={setLogs} onClose={() => setLogs(null)} />, document.body,
      ) : null}
      {changeIt ? <ChangeIt projectId={projectId} runId={c.run.id} actor={actor} onClose={() => { setChangeIt(false); setReviewChange(""); }} onStarted={onOpenBuild}
                            initialMessage={reviewChange}
                            app={c.app} repo={c.brief.repo ?? ""}
                            previewId={c.outputs.preview?.status === "running" ? c.outputs.preview.id : null} /> : null}
      {pop ? <ActPopover projectId={projectId} actId={pop.actId} viewer={actor} anchor={pop.anchor} onClose={() => setPop(null)} /> : null}
    </section>
  );
}
