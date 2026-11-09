"use client";

/**
 * RESEARCH, cited dossiers (area `discovery`).
 *
 * Not a sidebar page: the usual way in is a workflow run's research step, whose panel shows
 * `DossierCompact` and links here for the full view. The Command Center's Build split, the
 * Factory and old links still land on this area. The look is the approved mockup
 * (boss-research-panels-mockup: research-home, dossier); styles in research/research.css.
 *
 *   Research home, hero (ask, context, Deep, Research →), totals, dossier cards with verdict
 *                   filters and search.
 *   Dossier      , hero with the phase stepper (live while running) and the verdict card;
 *                   candidate outcomes, cited signals, the critic, market and research brief.
 */
import React, { useActionState, useEffect, useMemo, useState, type ReactNode } from "react";
import { RateIt } from "./improve/rate-it";

import type {
  DiscoveryRun, DiscoveryFinding, DiscoveryListItem, DiscoveryOutcome, DiscoveryTotals, ModelAttention,
} from "@boss/api-client";

import { startDiscoveryAction, promoteOutcomeAction, deepenDiscoveryAction } from "./actions";
import { SubmitButton } from "./submit-button";
import { consoleHref } from "./url-intent";
import { Ago, Ring, Verdict, Stepper, How, useSearch, pushParams, clip } from "./research/bits";

import { callAction } from "../lib/call-action";
import { fetchDiscoveryDetail } from "../lib/discovery-detail";
import { sayLoadError } from "../lib/meeting-room-detail";
import { AreaLink } from "./area-link";
import {
  ago, briefLine, buildable, checkLabel, composerHref, domainOf, dossierTitle, dossierVerdict,
  outcomeBrief, pct, researchDoing, researchStages, usd, type VerdictFilter,
} from "../lib/research-panels";
import { useConsoleRefresh } from "./use-console-refresh";
import { useOnScreen } from "../lib/area-shown";

/** While a run is live, ask the console for a refresh. Asked, not issued: the console's one
 *  refresh coordinator coalesces it with every other panel's (lib/console-refresh.ts). */
function useLiveRefresh(active: boolean, intervalMs = 4000) {
  const refresh = useConsoleRefresh();
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => refresh("poll"), intervalMs);
    return () => clearInterval(t);
  }, [active, intervalMs, refresh]);
}

/** One dossier, fetched by id; re-read every 4s ONLY while it is still researching. A dossier
 *  that is not there is said once and not asked for again. */
function useDossier(id: string, stamp = ""): { run: DiscoveryRun | null; error: string | null } {
  const [run, setRun] = useState<DiscoveryRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mine = run && run.id === id ? run : null;
  const live = mine ? mine.status === "running" || mine.status === "queued" : false;
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    let cancelled = false;
    const load = () => fetchDiscoveryDetail(id).then((res) => {
      if (cancelled) return;
      if (res.ok) { setRun(res.run); setError(null); } else setError(res.error);
    });
    void load();
    if (!live || !onScreen) return () => { cancelled = true; };
    const t = setInterval(() => { void load(); }, 4000);
    return () => { cancelled = true; clearInterval(t); };
  }, [id, stamp, live, onScreen]);
  return { run: mine, error: mine ? null : error };
}

const isLive = (status: string) => status === "running" || status === "queued";

// --------------------------------------------------------------------------- //
// Actions

/** "Convene a panel →": promote one outcome into a PRD panel, then link into that room. */
function PromoteOutcome({ runId, idx, promotedTaskId, promotedMeetingId, small = false, label }: {
  runId: string; idx: number; promotedTaskId?: string; promotedMeetingId?: string; small?: boolean;
  label?: string;
}) {
  const search = useSearch();
  const [state, action, pending] = useActionState(callAction(promoteOutcomeAction, undefined, "discovery:create"), null);
  const meeting = promotedMeetingId ?? state?.meetingRunId;
  const taskId = promotedTaskId ?? state?.taskId;
  if (meeting || taskId) {
    const href = consoleHref(search, meeting ? { area: "meetings", meeting } : { area: "meetings", task: taskId });
    return (
      <a className={`rp-btn${small ? " sm" : ""}`} href={href} data-testid="dossier-panel-open">
        Open the panel</a>
    );
  }
  return (
    <form action={action}>
      <input type="hidden" name="run_id" value={runId} />
      <input type="hidden" name="outcome_index" value={idx} />
      <SubmitButton permission="discovery:create" className={`rp-btn primary${small ? " sm" : ""}`} disabled={pending}
                    data-testid="dossier-convene" title="Convene a PRD panel on this outcome">
        {pending ? "Convening…" : (label ?? "Convene a panel →")}
      </SubmitButton>
      {state?.error ? <span className="rp-err" role="alert"> {state.error}</span> : null}
    </form>
  );
}

function DeepenButton({ runId, small = false, primary = false }: { runId: string; small?: boolean; primary?: boolean }) {
  const search = useSearch();
  const [state, action, pending] = useActionState(callAction(deepenDiscoveryAction, undefined, "discovery:create"), null);
  if (state?.runId) {
    return (
      <a className={`rp-btn${small ? " sm" : ""}`} data-testid="dossier-deeper-started"
         href={consoleHref(search, { area: "discovery", run: state.runId })}>
        Deeper pass started</a>
    );
  }
  return (
    <form action={action}>
      <input type="hidden" name="run_id" value={runId} />
      <SubmitButton permission="discovery:create" className={`rp-btn${primary ? " primary" : ""}${small ? " sm" : ""}`} disabled={pending}
                    data-testid="dossier-deepen"
                    title="A multi-source scout sweep for a richer, more cited dossier">
        {pending ? "Deepening…" : "Deepen research"}
      </SubmitButton>
      {state?.error ? <span className="rp-err" role="alert"> {state.error}</span> : null}
    </form>
  );
}

/** Research the same seed again, a failed dossier's way forward. */
function ResearchAgain({ projectId, seed, context }: { projectId: string; seed: string; context?: string | null }) {
  const search = useSearch();
  const [state, action, pending] = useActionState(callAction(startDiscoveryAction, undefined, "discovery:create"), null);
  if (state?.runId) {
    return <a className="rp-btn" href={consoleHref(search, { area: "discovery", run: state.runId })}>Researching again</a>;
  }
  return (
    <form action={action}>
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="seed" value={seed} />
      {context ? <input type="hidden" name="context" value={context} /> : null}
      <SubmitButton permission="discovery:create" className="rp-btn primary" disabled={pending} data-testid="dossier-research-again">
        {pending ? "Starting…" : "Research it again"}
      </SubmitButton>
      {state?.error ? <span className="rp-err" role="alert"> {state.error}</span> : null}
    </form>
  );
}

// --------------------------------------------------------------------------- //
// Research home

function ResearchHero({ projectId, researchAvailable, discoveries, totals, onStarted }: {
  projectId: string;
  researchAvailable: boolean;
  discoveries: DiscoveryListItem[];
  /** The server's totals over EVERY dossier; the loaded page only when an older API sends none. */
  totals: DiscoveryTotals | null;
  onStarted: (runId: string) => void;
}) {
  const [state, action, pending] = useActionState(callAction(startDiscoveryAction, undefined, "discovery:create"), null);
  useEffect(() => { if (state?.runId) onStarted(state.runId); }, [state?.runId, onStarted]);
  const promising = totals?.promising ?? discoveries.filter((d) => dossierVerdict(d).filter === "promising").length;
  const signals = totals?.signals ?? discoveries.reduce((n, d) => n + (d.summary?.findings ?? 0), 0);
  const outcomes = totals?.outcomes ?? discoveries.reduce((n, d) => n + (d.summary?.outcomes ?? 0), 0);
  return (
    <section className="rp-hero" data-testid="research-hero">
      <div className="rp-aurora" aria-hidden />
      <div className="rp-hero-l">
        <div className="rp-area-name"><span className="rp-dot" aria-hidden />Research, cited dossiers in about 4 minutes</div>
        <h2 className="rp-h2">What should your team find out?</h2>
        {researchAvailable ? (
          <form action={action} className="rp-composer" data-testid="research-form">
            <input type="hidden" name="project_id" value={projectId} />
            <label className="rp-sr" htmlFor="rp-seed">The idea, problem or market to research</label>
            <textarea id="rp-seed" name="seed" required rows={2} data-testid="research-seed"
                      placeholder="An idea, a problem or a market, e.g. “Would small agencies pay for a tool that drafts replies to inbound leads?”" />
            <div className="rp-c-row">
              <label className="rp-sr" htmlFor="rp-context">Context</label>
              <input id="rp-context" name="context" className="rp-ctx"
                     placeholder="Context (optional), who you sell to, constraints" />
              <label className="rp-toggle" title="A multi-source scout sweep: richer, more cited, slower">
                <input type="checkbox" name="deep" data-testid="research-deep" /><i aria-hidden />Deep
              </label>
              <SubmitButton permission="discovery:create" className="rp-btn primary" disabled={pending} data-testid="research-start">
                {pending ? "Starting…" : "Research →"}
              </SubmitButton>
            </div>
            {state?.error ? <p className="rp-err" role="alert">Research did not start: {state.error}</p> : null}
          </form>
        ) : (
          <p className="rp-paused" data-testid="discovery-research-paused">
            New research is paused in Community until budget and recovery safeguards are ready.
            Saved dossiers stay below.
          </p>
        )}
        <How steps={["Sweep the web for signals", "A critic tries to refute it", "Outcomes you can pilot",
                     "An evidence gate decides"]} />
      </div>
      <div className="rp-stats" data-testid="research-totals">
        <div className="rp-stat"><b>{totals?.dossiers ?? discoveries.length}</b><span>dossiers</span></div>
        <div className="rp-stat"><b className="rp-good-t">{promising}</b><span>promising</span></div>
        <div className="rp-stat"><b>{signals}</b><span>cited signals</span></div>
        <div className="rp-stat"><b>{outcomes}</b><span>candidate outcomes</span></div>
      </div>
    </section>
  );
}

const FILTERS: Array<{ key: VerdictFilter; label: string }> = [
  { key: "all", label: "All" },
  { key: "promising", label: "Promising" },
  { key: "needs_more", label: "Needs more" },
  { key: "not_worth", label: "Not worth it" },
];

function DossierCard({ d, onOpen }: { d: DiscoveryListItem; onOpen: () => void }) {
  // THE CARD READS THE SUMMARY. The dossier is the detail, fetched when it is opened.
  const sum = d.summary;
  const v = dossierVerdict(d);
  const live = isLive(d.status);
  return (
    <button type="button" className="rp-card-link" onClick={onOpen} data-testid="dossier-card"
            data-status={d.status} title={(d.seed ?? d.title ?? "").slice(0, 400)}>
      <span className="rp-dtop">
        <Verdict tone={v.tone}>{v.label}</Verdict>
        <span className="rp-when"><Ago iso={d.created_at} /></span>
      </span>
      <b className="rp-dtitle">{dossierTitle(d)}</b>
      {d.status === "failed" ? (
        <span className="rp-card-note" data-tone="bad" data-testid="discovery-row-attention">
          {d.attention?.headline ?? clip(d.error ?? "The research stopped.", 140)}
        </span>
      ) : live ? (
        <span className="rp-dmeta">
          <Ring value={0} live size={40} />
          <span className="rp-dnums">
            <span><b>{researchStages(d).filter((s) => s.state === "done").length}</b> of 5 phases</span>
            <span>{researchDoing(d.phase)}</span>
          </span>
        </span>
      ) : (
        <span className="rp-dmeta">
          <Ring value={sum?.confidence ?? 0} size={40} />
          <span className="rp-dnums">
            <span><b>{sum?.findings ?? 0}</b> cited signals</span>
            <span><b>{sum?.outcomes ?? 0}</b> outcome{sum?.outcomes === 1 ? "" : "s"}{d.usage?.cost_usd_micros != null ? `, ${usd(d.usage.cost_usd_micros)}` : ""}</span>
          </span>
        </span>
      )}
      {d.attention && d.status !== "failed" ? (
        <span className="rp-card-note" data-tone="wait" data-testid="discovery-row-attention"> {d.attention.headline}</span>
      ) : null}
    </button>
  );
}

// --------------------------------------------------------------------------- //
export function DiscoveriesHome({
  projectId, orgId, discoveries, apiReachable, initialDiscoveryId,
  researchAvailable = true, totals = null, actor = "",
}: {
  projectId: string;
  /** Who is reading: their one-tap rating on a dossier is theirs. */
  actor?: string;
  orgId?: string;
  discoveries: DiscoveryListItem[];
  totals?: DiscoveryTotals | null;
  apiReachable: boolean;
  researchAvailable?: boolean;
  /** A dossier named in the URL, `?area=discovery&run=drun_…`, which is what the workflow's
   *  research step links to. Without this the id in the link was ignored and every "open the
   *  research" arrived at the list of every discovery the project had ever run. */
  initialDiscoveryId?: string | null;
}) {
  const [selected, setSelected] = useState<string | null>(initialDiscoveryId ?? null);
  // THE PROP IS READ ON EVERY NAVIGATION, NOT ONLY THE FIRST MOUNT.
  //
  // The console renders all fifty areas at once and shows one, so this component has ALREADY
  // mounted, with no dossier in the URL, long before the operator clicks "open the research"
  // on a workflow step. `useState(initialDiscoveryId)` captures its argument once, so the id
  // arriving in the URL changed the prop and nothing else.
  useEffect(() => {
    setSelected(initialDiscoveryId ?? null);
  }, [initialDiscoveryId]);
  // Back and Forward between the shelf and a dossier opened from it.
  useEffect(() => {
    const onPop = () => {
      const q = new URLSearchParams(window.location.search);
      if (q.get("area") !== "discovery") return;
      setSelected(q.get("discovery") ?? q.get("run") ?? null);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const open = React.useCallback((id: string | null) => {
    setSelected(id);
    pushParams({ run: id, discovery: null });
  }, []);
  const anyLive = discoveries.some((d) => isLive(d.status));
  useLiveRefresh(anyLive);

  // Dossiers past the first page ("Show more"): the list API pages by offset.
  const [more, setMore] = useState<DiscoveryListItem[]>([]);
  const [moreState, setMoreState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const all = useMemo(() => {
    const seen = new Set(discoveries.map((d) => d.id));
    return [...discoveries, ...more.filter((d) => !seen.has(d.id))];
  }, [discoveries, more]);
  const loadMore = React.useCallback(async () => {
    setMoreState("loading");
    try {
      const r = await fetch(`/v1/discoveries?project_id=${encodeURIComponent(projectId)}&limit=50&offset=${discoveries.length + more.length}`,
                            { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      // The list API sends summaries (discovery_service.list_discovery_runs), not dossiers.
      const page = ((await r.json()) as { discoveries?: DiscoveryListItem[] }).discoveries ?? [];
      setMore((cur) => [...cur, ...page]);
      setMoreState(page.length < 50 ? "done" : "idle");
    } catch {
      setMoreState("error");
    }
  }, [projectId, discoveries.length, more.length]);
  const [filter, setFilter] = useState<VerdictFilter>("all");
  const [query, setQuery] = useState("");
  const counts = useMemo(() => {
    if (totals) {
      return { all: totals.dossiers, promising: totals.promising, needs_more: totals.needs_more,
               not_worth: totals.not_worth } as Record<VerdictFilter, number>;
    }
    const c: Record<VerdictFilter, number> = { all: all.length, promising: 0, needs_more: 0, not_worth: 0 };
    for (const d of all) { const f = dossierVerdict(d).filter; if (f) c[f] += 1; }
    return c;
  }, [all, totals]);
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return all.filter((d) => (filter === "all" || dossierVerdict(d).filter === filter)
      && (!q || `${d.title ?? ""} ${d.seed ?? ""}`.toLowerCase().includes(q)));
  }, [all, filter, query]);

  if (selected) {
    return (
      <DiscoveryDetail id={selected} actor={actor} row={all.find((d) => d.id === selected) ?? null}
                       projectId={projectId} orgId={orgId} researchAvailable={researchAvailable}
                       onBack={() => open(null)} />
    );
  }

  return (
    <div className="rp" data-testid="research-home">
      <ResearchHero projectId={projectId} researchAvailable={researchAvailable} discoveries={all} totals={totals}
                    onStarted={open} />
      <section aria-label="Dossiers">
        <div className="rp-sec-h">
          <h3>Dossiers</h3>
          <div className="rp-filters" role="group" aria-label="Filter by verdict">
            {FILTERS.map((f) => (
              <button key={f.key} type="button" className="rp-f" aria-pressed={filter === f.key}
                      data-testid={`dossier-filter-${f.key}`} onClick={() => setFilter(f.key)}>
                {f.label} {counts[f.key]}
              </button>
            ))}
          </div>
          <span className="rp-spacer" />
          <input className="rp-search" type="search" placeholder="Search dossiers…" aria-label="Search dossiers"
                 value={query} onChange={(e) => setQuery(e.target.value)} data-testid="dossier-search" />
        </div>
        {!apiReachable ? (
          <div className="rp-empty" role="status"><b>The control plane is not answering.</b>
            Dossiers will show here once it is back.</div>
        ) : discoveries.length === 0 ? (
          <div className="rp-empty" data-testid="research-empty"><b>No dossiers yet.</b>
            {researchAvailable ? "Ask a question above, or run a workflow, whose first step researches it."
              : "Saved dossiers appear here."}
            <AreaLink area="delivery_workflows" className="rp-link">Start a workflow</AreaLink></div>
        ) : shown.length === 0 ? (
          <div className="rp-empty"><b>Nothing matches.</b>
            <button type="button" className="rp-link" onClick={() => { setFilter("all"); setQuery(""); }}>Show all</button></div>
        ) : (
          <>
            <div className="rp-grid">
              {shown.map((d) => <DossierCard key={d.id} d={d} onOpen={() => open(d.id)} />)}
            </div>
            {(totals ? all.length < totals.dossiers : discoveries.length >= 50) && moreState !== "done" ? (
              <div className="rp-more-row">
                <button type="button" className="rp-btn" onClick={() => void loadMore()} disabled={moreState === "loading"}
                        data-testid="dossiers-show-more">
                  {moreState === "loading" ? "Loading…" : moreState === "error" ? "Try again" : "Show more"}
                </button>
              </div>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// The dossier

function VerdictCard({ run, projectId, researchAvailable }: {
  run: DiscoveryRun; projectId: string; researchAvailable: boolean;
}) {
  const gate = run.state?.gate;
  const v = dossierVerdict(run, gate?.verdict);
  const live = isLive(run.status);
  if (live) {
    return (
      <div className="rp-vcard" data-tone="live" data-testid="dossier-verdict">
        <Verdict tone="live" big>Researching</Verdict>
        <Ring value={0} live size={92} big />
        <div className="rp-vsub">{researchDoing(run.phase)}</div>
        {run.attention ? <AttentionBanner attention={run.attention} /> : null}
      </div>
    );
  }
  if (run.status === "failed") {
    return (
      <div className="rp-vcard" data-tone="bad" data-testid="dossier-verdict">
        <Verdict tone="bad" big>Failed</Verdict>
        {run.attention ? <AttentionBanner attention={run.attention} />
          : <div className="rp-attn">{run.error ?? "The research stopped before a verdict."}</div>}
        {researchAvailable && run.seed ? (
          <div className="rp-vact"><ResearchAgain projectId={projectId} seed={run.seed} context={run.context} /></div>
        ) : null}
      </div>
    );
  }
  const promotions = run.state?.promotions ?? [];
  const best = bestOutcomeIndex(run.state?.candidate_outcomes ?? []);
  const promoted = promotions.find((p) => p.outcome_index === best);
  return (
    <div className="rp-vcard" data-tone={v.tone} data-testid="dossier-verdict">
      <Verdict tone={v.tone} big>{v.tone === "good" ? "◆ " : ""}{v.label}</Verdict>
      <Ring value={gate?.confidence ?? 0} size={92} big color="var(--rp-mint)" />
      <div className="rp-vsub">
        confidence{gate?.thesis_confidence != null ? `, thesis ${pct(gate.thesis_confidence)}%` : ""}
        {gate?.best_outcome_confidence != null ? `, best outcome ${pct(gate.best_outcome_confidence)}%` : ""}
        {gate?.cited_sources != null ? `, ${gate.cited_sources} sources` : ""}
      </div>
      {gate?.checks && Object.keys(gate.checks).length ? (
        <ul className="rp-checks" aria-label="Evidence gate">
          {Object.entries(gate.checks).map(([k, ok]) => (
            <li key={k} className="rp-chk" data-ok={ok ? "true" : "false"}>{ok ? "✓" : "✕"} {checkLabel(k)}</li>
          ))}
        </ul>
      ) : null}
      <div className="rp-vact">
        {buildable(run) && best >= 0 ? (
          <PromoteOutcome runId={run.id} idx={best} promotedTaskId={promoted?.task_id}
                          promotedMeetingId={promoted?.meeting_run_id} />
        ) : null}
        {researchAvailable ? <DeepenButton runId={run.id} primary={!buildable(run)} /> : null}
      </div>
    </div>
  );
}

/** The strongest outcome, by its own confidence (the first when none says). */
function bestOutcomeIndex(outcomes: DiscoveryOutcome[]): number {
  if (!outcomes.length) return -1;
  let best = 0;
  outcomes.forEach((o, i) => { if ((o.confidence_0_1 ?? 0) > (outcomes[best].confidence_0_1 ?? 0)) best = i; });
  return best;
}

function OutcomeCard({ o, index, rank, run, title }: {
  o: DiscoveryOutcome; index: number; rank: number; run: DiscoveryRun; title: string;
}) {
  const search = useSearch();
  const [open, setOpen] = useState(false);
  const promoted = (run.state?.promotions ?? []).find((p) => p.outcome_index === index);
  const best = rank === 0;
  const extra = o.validation_plan || o.wedge || o.rationale || (o.supporting_signals?.length ?? 0) > 0;
  return (
    <article className="rp-outcome" data-best={best ? "true" : undefined} data-testid="dossier-outcome">
      <div className="rp-o-head" data-open={open ? "true" : undefined}>
        <Ring value={o.confidence_0_1 ?? 0} size={54} color={best ? "var(--rp-mint)" : "var(--rp-periwinkle)"} />
        <div>
          <span className="rp-o-k">Outcome {rank + 1}{best ? ", strongest" : ""}</span>
          <b title={o.outcome}>{o.outcome ?? "An outcome"}</b>
        </div>
      </div>
      <div className="rp-o-grid">
        <div><small className="rp-cap">First slice</small><p title={o.first_prototype_slice}>{o.first_prototype_slice || "—"}</p></div>
        <div><small className="rp-cap">Pilot with</small><p title={o.target_pilot}>{o.target_pilot || "—"}</p></div>
      </div>
      {open && extra ? (
        <div className="rp-o-more" data-testid="dossier-outcome-plan">
          {o.validation_plan ? <div><small className="rp-cap">Validation plan</small><p>{o.validation_plan}</p></div> : null}
          {o.wedge ? <div><small className="rp-cap">Wedge</small><p>{o.wedge}</p></div> : null}
          {o.rationale ? <div><small className="rp-cap">Why</small><p>{o.rationale}</p></div> : null}
          {(o.supporting_signals?.length ?? 0) > 0 ? (
            <div><small className="rp-cap">Supporting signals</small>
              <ul className="rp-dis">{o.supporting_signals!.map((s, i) => <li key={i}>{s}</li>)}</ul></div>
          ) : null}
        </div>
      ) : null}
      <div className="rp-o-act">
        {buildable(run) ? (
          <PromoteOutcome runId={run.id} idx={index} small promotedTaskId={promoted?.task_id}
                          promotedMeetingId={promoted?.meeting_run_id} />
        ) : null}
        <a className="rp-btn sm" data-testid="dossier-build-it"
           href={composerHref(search, { title, brief: outcomeBrief(title, o) })}>Build it</a>
        {extra ? (
          <button type="button" className="rp-link" aria-expanded={open} onClick={() => setOpen((x) => !x)}>
            {open ? "Hide the plan" : "Validation plan"}
          </button>
        ) : null}
      </div>
    </article>
  );
}

function SignalCard({ f, full }: { f: DiscoveryFinding; full: boolean }) {
  const angle = String(f.angle || "other").toLowerCase();
  return (
    <article className="rp-sig">
      <span className="rp-ang" data-angle={angle}>{angle}</span>
      <p title={full ? undefined : f.finding}>{full ? f.finding : clip(f.finding, 150)}</p>
      {full && f.evidence ? <p className="rp-evidence">{f.evidence}</p> : null}
      {f.citation_url ? (
        <a className="rp-dom" href={f.citation_url} target="_blank" rel="noreferrer" title={f.citation_url}>
           {domainOf(f.citation_url)}
        </a>
      ) : null}
    </article>
  );
}

/** The market analysis as readable rows (nested objects and lists), never raw JSON. */
function AnalysisRows({ obj }: { obj: Record<string, unknown> }) {
  const row = (label: string, val: unknown): ReactNode => {
    if (val == null || val === "") return null;
    const L = label.replace(/_0_5$/, "").replace(/_/g, " ");
    if (Array.isArray(val)) return <div key={label}><b>{L}:</b> {val.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join("; ")}</div>;
    if (typeof val === "object") {
      return (
        <div key={label}>
          <b>{L}</b>
          <div className="rp-rows">{Object.entries(val as Record<string, unknown>).map(([k, v]) => row(k, v))}</div>
        </div>
      );
    }
    return <div key={label}><b>{L}:</b> {String(val)}</div>;
  };
  return <div className="rp-rows">{Object.entries(obj).map(([k, v]) => row(k, v))}</div>;
}

function str(x: unknown): string {
  return typeof x === "string" ? x : typeof x === "number" ? String(x) : "";
}

function CriticCard({ critique }: { critique: NonNullable<DiscoveryRun["state"]["critique"]> }) {
  const dis = critique.disconfirming_evidence ?? [];
  const claims = critique.claims ?? [];
  return (
    <div className="rp-card rp-critic" data-testid="dossier-critic">
      <div className="rp-card-h">
        <span className="rp-cr-ic" aria-hidden>Versus</span><b>The critic</b><span className="rp-spacer" />
        {critique.overall_confidence_0_1 != null ? (
          <span className="rp-n" data-tone="bad" title="The critic's confidence in the thesis">{pct(critique.overall_confidence_0_1)}%</span>
        ) : null}
      </div>
      {critique.biggest_risk ? (
        <p className="rp-risk"><small className="rp-cap">Biggest risk</small>{clip(critique.biggest_risk, 220)}</p>
      ) : null}
      {dis.length ? (
        <>
          <small className="rp-cap">Counter-evidence</small>
          <ul className="rp-dis">{dis.slice(0, 3).map((x, i) => <li key={i}>{clip(x, 150)}</li>)}</ul>
        </>
      ) : null}
      {claims.length || dis.length > 3 || (critique.biggest_risk ?? "").length > 220 ? (
        <details className="rp-fold">
          <summary>Everything the critic said{claims.length ? `, ${claims.length} claims` : ""}</summary>
          <div className="rp-claims">
            {critique.biggest_risk ? <p><b>Biggest risk:</b> {critique.biggest_risk}</p> : null}
            {claims.map((c, i) => (
              <p key={i}><span className="rp-claim-v" data-v={c.verdict}>{c.verdict}</span>{c.claim}{c.reason ? `, ${c.reason}` : ""}</p>
            ))}
            {dis.map((x, i) => <p key={`d${i}`}>• {x}</p>)}
          </div>
        </details>
      ) : null}
    </div>
  );
}

function MarketCard({ market }: { market: Record<string, unknown> }) {
  const problem = (market.problem ?? {}) as Record<string, unknown>;
  const problemLine = [problem.is_real === true ? "Real" : problem.is_real === false ? "Unproven" : "",
                       str(problem.frequency)].filter(Boolean).join(", ");
  const rows: Array<[string, string]> = [
    ["Problem", problemLine || clip(str(problem.statement), 110)],
    ["Segment", clip(str(market.target_segment), 110)],
    ["Size", clip(str(market.market_size), 90)],
    ["Timing", clip(str(market.timing), 110)],
  ].filter(([, v]) => v) as Array<[string, string]>;
  return (
    <div className="rp-card" data-testid="dossier-market">
      <div className="rp-card-h"><b>Market</b></div>
      {rows.map(([k, v]) => <div key={k} className="rp-mk"><small className="rp-cap">{k}</small><p>{v}</p></div>)}
      <details className="rp-fold">
        <summary>Full analysis</summary>
        <AnalysisRows obj={market} />
      </details>
    </div>
  );
}

function BriefCard({ brief }: { brief: NonNullable<DiscoveryRun["state"]["research_brief"]> }) {
  const qs = brief.questions ?? [];
  const sources = brief.sources_consulted ?? [];
  return (
    <div className="rp-card" data-testid="dossier-brief">
      <div className="rp-card-h"><b>Research brief</b></div>
      {brief.hypothesis ? <p className="rp-hyp" title={brief.hypothesis}>{clip(brief.hypothesis, 200)}</p> : null}
      <span className="rp-sub">{qs.length} question{qs.length === 1 ? "" : "s"}, {sources.length} source{sources.length === 1 ? "" : "s"} consulted</span>
      {qs.length || sources.length || (brief.hypothesis ?? "").length > 200 ? (
        <details className="rp-fold">
          <summary>The brief</summary>
          <div className="rp-qs">
            {(brief.hypothesis ?? "").length > 200 ? <p><b>Hypothesis:</b> {brief.hypothesis}</p> : null}
            {qs.length ? <ol>{qs.map((q, i) => <li key={i}>{q}</li>)}</ol> : null}
            {sources.length ? <p><b>Sources:</b> {sources.map((s) => domainOf(s) || s).join(", ")}</p> : null}
          </div>
        </details>
      ) : null}
    </div>
  );
}

/** One dossier. The row from the list paints the hero at once; the dossier itself is fetched
 *  here, by id, so a link to one older than the list still opens it, and re-read while live. */
function DiscoveryDetail({ id, row, projectId, actor = "", researchAvailable, onBack }: {
  id: string;
  row: DiscoveryListItem | null;
  projectId: string;
  actor?: string;
  orgId?: string;
  researchAvailable: boolean;
  onBack: () => void;
}) {
  const { run: loaded, error } = useDossier(id, row ? `${row.status}|${row.phase}|${row.updated_at ?? ""}` : "");
  const [allSignals, setAllSignals] = useState(false);
  const run: DiscoveryRun | null = loaded ?? (row ? { ...row, context: null, state: {} } : null);
  if (!run) {
    return (
      <div className="rp" data-testid="dossier">
        <button type="button" className="rp-back" onClick={onBack}>← All dossiers</button>
        <div className="rp-empty" data-testid={error ? "discovery-not-in-feed" : "discovery-dossier-loading"} role="status">
          {error ? <><b>This dossier isn&apos;t here any more.</b>{sayLoadError(error, "dossier")}</>
            : <b>Loading the dossier…</b>}
        </div>
      </div>
    );
  }
  const d = run.state ?? {};
  const live = isLive(run.status);
  const title = dossierTitle(run);
  const line = briefLine(run.seed);
  const outcomes = (d.candidate_outcomes ?? []).map((o, index) => ({ o, index }))
    .sort((a, b) => (b.o.confidence_0_1 ?? 0) - (a.o.confidence_0_1 ?? 0));
  const signals = d.signal_findings ?? [];
  const deep = Boolean((d as { deep?: boolean }).deep || row?.summary?.deep);
  return (
    <div className="rp" data-testid="dossier" data-status={run.status}>
      <button type="button" className="rp-back" onClick={onBack}>← All dossiers</button>
      <section className="rp-dhero">
        <div className="rp-dh-l">
          <div className="rp-area-name">
            <span className="rp-dot" data-tone={live ? undefined : run.status === "failed" ? "bad" : "idle"} aria-hidden />
            Dossier<Ago iso={run.created_at} prefix=", " />
            {run.usage?.cost_usd_micros != null ? `, ${usd(run.usage.cost_usd_micros)}` : ""}
            {deep ? ", Deep" : ""}
          </div>
          <h2 className="rp-dh-t">{title}</h2>
          {line ? <p className="rp-dh-s">{line}</p> : null}
          {run.context ? <p className="rp-sub">Context: {run.context}</p> : null}
          <Stepper stages={researchStages(run)} live={live} testId="discovery-flow" />
          {!researchAvailable ? (
            <p className="rp-sub">Deeper research is paused in Community; this dossier stays available.</p>
          ) : null}
        </div>
        <div className="rp-dh-r">
          <VerdictCard run={run} projectId={projectId} researchAvailable={researchAvailable} />
          {run.status === "completed" ? (
            <RateIt projectId={projectId} actor={actor} subject="research" id={run.id} testId="dossier-rate" />
          ) : null}
        </div>
      </section>

      {!loaded ? (
        <p className="rp-sub" data-testid="discovery-dossier-loading">
          {error ? sayLoadError(error, "dossier") : "Loading the dossier…"}
        </p>
      ) : null}

      {loaded && (outcomes.length || signals.length || d.critique || d.market_analysis || d.research_brief) ? (
        <div className="rp-dcols">
          <div className="rp-dmain">
            {outcomes.length ? (
              <>
                <div className="rp-sec-h"><h3>Candidate outcomes</h3><span className="rp-sub">What the evidence says to try first</span></div>
                {outcomes.map(({ o, index }, rank) => (
                  <OutcomeCard key={index} o={o} index={index} rank={rank} run={run} title={title} />
                ))}
              </>
            ) : !live ? (
              <div className="rp-empty"><b>No outcome cleared the bar.</b>Deepen the research, or read the critic.</div>
            ) : null}
            {signals.length ? (
              <>
                <div className="rp-sec-h" style={{ marginTop: 8, marginBottom: 0 }}>
                  <h3>Cited signals</h3><span className="rp-sub">{signals.length} from the web sweep</span>
                  <span className="rp-spacer" />
                  {signals.length > 6 || signals.some((f) => f.evidence) ? (
                    <button type="button" className="rp-link" aria-expanded={allSignals}
                            onClick={() => setAllSignals((x) => !x)} data-testid="dossier-all-signals">
                      {allSignals ? "Fewer" : `All ${signals.length} `}
                    </button>
                  ) : null}
                </div>
                <div className="rp-sigs">
                  {(allSignals ? signals : signals.slice(0, 6)).map((f, i) => <SignalCard key={i} f={f} full={allSignals} />)}
                </div>
              </>
            ) : null}
          </div>
          <aside className="rp-dside">
            {d.critique ? <CriticCard critique={d.critique} /> : null}
            {d.market_analysis && Object.keys(d.market_analysis).length ? <MarketCard market={d.market_analysis} /> : null}
            {d.research_brief?.hypothesis || d.research_brief?.questions?.length ? <BriefCard brief={d.research_brief} /> : null}
          </aside>
        </div>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------- //
/**
 * A research step's dossier, inside a workflow run's step panel: the verdict and what it means
 * first, the phases ticking while it runs, and the full dossier one press away.
 */
export function DossierCompact({ discoveryId, researchAvailable = true }: {
  discoveryId: string; researchAvailable?: boolean;
}) {
  const search = useSearch();
  const { run, error } = useDossier(discoveryId);
  const full = consoleHref(search, { area: "discovery", run: discoveryId });
  if (!run) {
    return (
      <div className="rp rp-compact" data-testid="dossier-compact">
        <div className="rp-cmp">
          <Ring value={0} live={!error} size={48} />
          <div className="rp-cmp-body">
            <span className={error ? "rp-err" : "rp-sub"}>{error ? `This dossier isn't here any more. ${sayLoadError(error, "dossier")}` : "Opening the dossier…"}</span>
          </div>
          <div className="rp-cmp-foot"><a className="rp-btn sm" href={full}>Open full view ↗</a></div>
        </div>
      </div>
    );
  }
  const d = run.state ?? {};
  const gate = d.gate;
  const live = isLive(run.status);
  const v = dossierVerdict(run, gate?.verdict);
  const outcomes = d.candidate_outcomes ?? [];
  const best = bestOutcomeIndex(outcomes);
  const signals = d.signal_findings?.length ?? 0;
  return (
    <div className="rp rp-compact" data-testid="dossier-compact" data-status={run.status}>
      <div className="rp-cmp">
        {live ? <Ring value={0} live size={64} /> : <Ring value={gate?.confidence ?? 0} size={64} color="var(--rp-mint)" />}
        <div className="rp-cmp-body">
          <div className="rp-cmp-top">
            <Verdict tone={v.tone} testId="dossier-compact-verdict">{v.label}</Verdict>
            <span className="rp-sub">
              {live ? researchDoing(run.phase)
                : run.status === "failed" ? "No verdict"
                : [gate?.cited_sources != null ? `${gate.cited_sources} sources` : signals ? `${signals} signals` : "",
                   `${outcomes.length} outcome${outcomes.length === 1 ? "" : "s"}`,
                   d.critique?.overall_confidence_0_1 != null ? `critic ${pct(d.critique.overall_confidence_0_1)}%` : "",
                   run.usage?.cost_usd_micros != null ? usd(run.usage.cost_usd_micros) : ""].filter(Boolean).join(", ")}
            </span>
          </div>
          <b className="rp-cmp-title">{dossierTitle(run)}</b>
          {best >= 0 ? <p className="rp-cmp-line" title={outcomes[best].outcome}><b>Best bet:</b> {outcomes[best].outcome}</p>
            : d.critique?.biggest_risk ? <p className="rp-cmp-line"><b>Biggest risk:</b> {d.critique.biggest_risk}</p> : null}
        </div>
        <div className="rp-cmp-foot"><Stepper stages={researchStages(run)} live={live} /></div>
        {!live && gate?.checks && Object.keys(gate.checks).length ? (
          <div className="rp-cmp-foot">
            <ul className="rp-checks" aria-label="Evidence gate">
              {Object.entries(gate.checks).map(([k, ok]) => (
                <li key={k} className="rp-chk" data-ok={ok ? "true" : "false"}>{ok ? "✓" : "✕"} {checkLabel(k)}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {run.attention ? <div className="rp-cmp-err"><AttentionBanner attention={run.attention} /></div>
          : run.status === "failed" && run.error ? <div className="rp-cmp-err"><div className="rp-attn">{run.error}</div></div> : null}
        <div className="rp-cmp-foot">
          <a className="rp-btn sm primary" href={full} data-testid="dossier-compact-open">Open full view ↗</a>
          {run.status === "completed" && researchAvailable ? <DeepenButton runId={run.id} small /> : null}
        </div>
      </div>
    </div>
  );
}

/**
 * WHY IT IS NOT MOVING, AND WHAT TO DO. A run that said "queued" while its worker was refused,
 * its budget was capped or its Claude login was rejected gave the operator nothing to act on.
 * The API now explains each of those (model_failures.explain); this puts it where they look.
 */
function AttentionBanner({ attention }: { attention: ModelAttention }) {
  const waiting = attention.state === "waiting";
  return (
    <div className="rp-attn" data-tone={waiting ? "wait" : undefined} role="status"
         data-testid="discovery-attention" data-kind={attention.kind}>
      <b>{waiting ? "Waiting" : "Warning"} {attention.headline}</b>
      <span>
        {attention.action}
        {attention.href ? <> <a href={attention.href}>Open</a></> : null}
      </span>
      {attention.detail ? <span>{attention.detail}</span> : null}
    </div>
  );
}
