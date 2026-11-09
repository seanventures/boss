"use client";

/**
 * SELF-FIXES (docs/product/BOSS_FIXES_ITSELF.md §10.3): every fix BOSS made to itself, and the
 * autonomy it bought.
 *
 *   Self-fixes                                     Autonomy ▁▂▂▅▇ 62% this week ⓘ
 *   Status       Fix                                   Area    Run        Upstream
 *   ● Verified   retire app from the old shared project deploy  wfr_f383…  Not sent [Send]
 *   ● Needs you  planner reads the app folder           build   wfr_9a1c…  , 
 *
 *   One fix (?sfx=): problem → diagnosis → fix → tests → review → approved → installed →
 *   resumed → upstream, each with when and what, and Diff, Output, Remove, Send to BOSS team.
 *
 * Recorded bugs a self-fix did not start for are listed under it, each with "Fix it" (a person
 * starts the self-fix from the recommendation).
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { useConsoleLive } from "../../lib/console-live";
import { usePolledRead } from "../../lib/log-stream";
import { showToast } from "../client-forms";
import { consoleHref } from "../url-intent";
import {
  CHIP_TONE, chipOf, micros, pct, polledSelfFix, readSelfFix, selfFixWrite,
  type AutonomyView, type DiffView, type ListView, type OneView, type RecommendationsView, type SelfFixItem,
} from "../../lib/self-fix";
import { localWith } from "../../lib/local-time";
import { AUTONOMY_TIP } from "./run-autonomy";
import { SendSheet } from "./send-sheet";

export const SELF_FIXES_POLL_MS = 15_000;

const UPSTREAM: Record<SelfFixItem["upstream_state"], string> = {
  local_only: "Not sent", previewed: "Previewed", sent: "Sent", merged: "Merged", closed: "Closed",
  duplicate_of: "Duplicate",
};

function readSfx(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("sfx");
}
function setSfx(id: string | null): void {
  try {
    const q = new URLSearchParams(window.location.search);
    q.set("area", "self_fixes");
    if (id) q.set("sfx", id); else q.delete("sfx");
    window.history.replaceState(null, "", `${window.location.pathname}?${q.toString()}`);
  } catch { /* the in-memory state still moves */ }
}
const runHref = (runId: string) =>
  consoleHref(typeof window === "undefined" ? "" : window.location.search, { area: "delivery_workflows", wfr: runId });

/** A sparkline of the weekly autonomy rate: one bar per week, height = the share BOSS solved. */
export function Sparkline({ rates }: { rates: Array<number | null> }) {
  const bars = rates.slice(-8);
  return (
    <span className="sfx-spark" aria-hidden data-testid="sfx-spark">
      {bars.map((r, i) => (
        <span key={i} className="sfx-spark-bar" style={{ height: `${Math.max(8, Math.round((r ?? 0) * 100))}%` }}
              data-empty={r === null || undefined} />
      ))}
    </span>
  );
}

export function AutonomyTile({ view }: { view: AutonomyView | null }) {
  if (!view) return null;
  const last = view.trend.length ? view.trend[view.trend.length - 1] : null;
  const rate = last ? last.rate : view.totals.rate;
  const t = view.totals;
  const tip = `${AUTONOMY_TIP} Over ${view.window}: ${t.problems} problems, BOSS ${t.by_boss}, people ${t.by_person}`
    + `, outside agents ${t.by_outside_agent}, open ${t.open}. BOSS-side only: ${pct(view.boss_side?.rate)}.`;
  return (
    <span className="sfx-autonomy" data-testid="sfx-autonomy" title={tip}>
      <span className="sfx-sub">Autonomy</span>
      <Sparkline rates={view.trend.map((w) => w.rate)} />
      <strong data-testid="sfx-autonomy-rate">{pct(rate)}</strong>
      <span className="sfx-sub">this week ⓘ</span>
    </span>
  );
}

function Lineage({ one, projectId, actor, onSend, onChanged, onBack }: {
  one: OneView; projectId: string; actor: string; onSend: () => void; onChanged: () => void; onBack: () => void;
}) {
  const sfx = one.self_fix;
  const chip = chipOf(sfx.chip);
  const [diff, setDiff] = useState<DiffView | null>(null);
  const [show, setShow] = useState<"" | "diff" | "output">("");
  const [busy, setBusy] = useState(false);
  const toggleDiff = async () => {
    setShow((s) => (s === "diff" ? "" : "diff"));
    if (diff) return;
    const got = await readSelfFix<DiffView>("diff", { project_id: projectId, id: sfx.id });
    if (got.ok && !got.notModified) setDiff(got.data);
  };
  const remove = async () => {
    setBusy(true);
    const got = await selfFixWrite("remove", { project_id: projectId, actor, id: sfx.id });
    setBusy(false);
    if (got.ok) { showToast("success", "Removal needs approval"); onChanged(); }
    else showToast("error", got.error);
  };
  const removable = sfx.stack_state === "on_stack";
  const sendable = chip === "Verified" && (sfx.upstream_state === "local_only" || sfx.upstream_state === "previewed");
  return (
    <section className="sfx-one" data-testid="sfx-one" aria-label="One self-fix">
      <header className="sfx-one-head">
        <button type="button" className="ag-link" data-testid="sfx-back" onClick={onBack}
                title="All self-fixes">← All</button>
        <h3 className="sfx-one-title" data-user-text>{sfx.pr?.title || sfx.class_signature}</h3>
        <span className={`cap-badge tone-${CHIP_TONE[chip].tone}`} data-testid="sfx-one-chip" title={CHIP_TONE[chip].tip}>{chip}</span>
      </header>
      <div className="sfx-strip-line" title={one.line}>{one.line}</div>
      <ol className="sfx-lineage" data-testid="sfx-lineage">
        {one.lineage.map((s) => (
          <li key={s.key} className="sfx-lineage-step" data-state={s.state} data-testid={`sfx-lineage-${s.key}`}>
            <span className="sfx-lineage-label">{s.label}</span>
            <span className="sfx-lineage-detail" title={s.detail} data-user-text>
              {s.href ? <a className="ag-link" href={s.href}>{s.detail || "Open"}</a> : (s.detail || "—")}
            </span>
            {s.at ? <time className="sfx-sub" dateTime={s.at}>{localWith(s.at, { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" })}</time> : null}
          </li>
        ))}
      </ol>
      <div className="sfx-sheet-actions">
        <button type="button" className="ag-btn" data-testid="sfx-one-diff" aria-expanded={show === "diff"}
                onClick={toggleDiff} title="The change and its tests">Diff</button>
        <button type="button" className="ag-btn" data-testid="sfx-one-output" aria-expanded={show === "output"}
                onClick={() => setShow((s) => (s === "output" ? "" : "output"))} title="Every event, in order">Output</button>
        {removable ? (
          <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn"
                       data-testid="sfx-one-remove" disabled={busy} onClick={remove}
                       title="Rebuild BOSS without this fix (after approval)">Remove</GatedButton>
        ) : null}
        {sendable ? (
          <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn sfx-primary"
                       data-testid="sfx-one-send" onClick={onSend}
                       title="Preview the pull request for the BOSS team; nothing leaves until you send">Send to BOSS team</GatedButton>
        ) : null}
        {sfx.upstream_url ? <a className="ag-link" href={sfx.upstream_url} target="_blank" rel="noreferrer">Upstream ↗</a> : null}
      </div>
      {show === "diff" ? (
        <div className="sfx-diff" data-testid="sfx-one-diff-view">
          {diff ? (
            <>
              <div className="sfx-sub">
                {(diff.tests ?? []).map((t) => <span key={t} title="Fails on the base, passes with the fix">✗→✓ {t} </span>)}
              </div>
              <pre data-user-text>{diff.diff || "No diff recorded"}</pre>
            </>
          ) : "Loading…"}
        </div>
      ) : null}
      {show === "output" ? (
        <ol className="sfx-events" data-testid="sfx-one-events">
          {one.events.map((e, i) => (
            <li key={i} title={`${e.event_type}, ${e.actor}`} data-user-text>{e.data?.sentence || e.event_type}</li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

export function SelfFixesHome({ projectId, actor }: { projectId: string; actor: string }) {
  const live = useConsoleLive();
  const [sfx, setPicked] = useState<string | null>(null);
  const [sending, setSending] = useState<string | null>(null);
  useEffect(() => {
    setPicked(readSfx());
    const on = () => setPicked(readSfx());
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  const pick = (id: string | null) => { setSfx(id); setPicked(id); };

  const list = usePolledRead<ListView>({
    key: projectId, pollMs: SELF_FIXES_POLL_MS, paused: !live,
    read: polledSelfFix<ListView>("list", { project_id: projectId }),
  });
  const autonomy = usePolledRead<AutonomyView>({
    key: projectId, pollMs: 60_000, paused: !live,
    read: polledSelfFix<AutonomyView>("autonomy", { project_id: projectId, window: "90d" }),
  });
  const recs = usePolledRead<RecommendationsView>({
    key: projectId, pollMs: 60_000, paused: !live,
    read: polledSelfFix<RecommendationsView>("recommendations", { project_id: projectId }),
  });
  const one = usePolledRead<OneView>({
    key: `${projectId}:${sfx ?? ""}`, pollMs: SELF_FIXES_POLL_MS, paused: !live, enabled: !!sfx,
    read: polledSelfFix<OneView>("one", { project_id: projectId, id: sfx ?? "" }),
  });
  const items = list.data?.self_fixes ?? [];
  const open = (recs.data?.recommendations ?? []).filter((r) => r.status === "open" && !r.self_fix_id);
  const start = async (recommendation_id: string) => {
    const got = await selfFixWrite("start", { project_id: projectId, actor, recommendation_id });
    if (got.ok) { showToast("success", "Self-fix started"); list.reload(); recs.reload(); }
    else showToast("error", got.error);
  };

  return (
    <div className="sfx-home" data-testid="sfx-home">
      <header className="gv-head">
        <h2 className="gv-title" title="Bugs in BOSS's own code that BOSS fixed, tested and installed with your approval">Self-fixes</h2>
        <span className="gv-spacer" />
        <AutonomyTile view={autonomy.data} />
      </header>
      {sfx && one.data ? (
        <Lineage one={one.data} projectId={projectId} actor={actor} onSend={() => setSending(sfx)} onBack={() => pick(null)}
                 onChanged={() => { one.reload(); list.reload(); }} />
      ) : sfx && one.error ? (
        <div role="alert" data-testid="sfx-one-error">{one.error}</div>
      ) : (
        <>
          {list.error && !list.data ? <div className="ag-empty" role="alert" title={list.error}>Can&apos;t reach BOSS</div> : null}
          {list.data && !items.length ? (
            <div className="ag-empty" data-testid="sfx-empty"
                 title="When a step fails on a bug in BOSS itself, BOSS fixes it here">No self-fixes yet</div>
          ) : null}
          {items.length ? (
            <table className="sfx-table" data-testid="sfx-list">
              <thead>
                <tr><th>Status</th><th>Fix</th><th>Area</th><th>Run</th><th>Upstream</th></tr>
              </thead>
              <tbody>
                {items.map((it) => {
                  const chip = chipOf(it.chip);
                  const canSend = chip === "Verified" && it.upstream_state === "local_only";
                  return (
                    <tr key={it.id} data-testid="sfx-row" data-chip={chip}>
                      <td><span className={`cap-badge tone-${CHIP_TONE[chip].tone}`} title={CHIP_TONE[chip].tip}
                                data-testid="sfx-row-chip">{chip}</span></td>
                      <td>
                        <button type="button" className="ag-link sfx-row-open" data-testid="sfx-row-open"
                                title={it.line} onClick={() => pick(it.id)}>
                          {it.pr?.title || it.class_signature}
                        </button>
                        {chip === "Refused" && it.refusal ? (
                          <span className="sfx-sub" title={it.refusal_detail || it.refusal}> ({it.refusal.replace(/_/g, " ")})</span>
                        ) : null}
                      </td>
                      <td>{it.area}</td>
                      <td>{it.run_id ? <a className="ag-link" href={runHref(it.run_id)} title={it.run_id}>{it.run_id.slice(0, 8)}…</a> : "—"}</td>
                      <td data-testid="sfx-row-upstream">
                        {it.upstream_url ? <a className="ag-link" href={it.upstream_url} target="_blank" rel="noreferrer">{UPSTREAM[it.upstream_state] ?? it.upstream_state} ↗</a>
                          : chip === "Verified" ? UPSTREAM[it.upstream_state] ?? it.upstream_state : "—"}
                        {canSend ? (
                          <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn"
                                       data-testid="sfx-row-send" onClick={() => setSending(it.id)}
                                       title="Preview what the BOSS team would get">Send</GatedButton>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : null}
          {open.length ? (
            <details className="sfx-recs" data-testid="sfx-recs">
              <summary title="Bugs BOSS recorded in its own code that it did not fix by itself">
                Recorded, not fixed, {open.length}
              </summary>
              <ul>
                {open.map((r) => (
                  <li key={r.id} data-testid="sfx-rec">
                    <span data-user-text title={r.root_cause || r.symptom || undefined}>{r.title}</span>
                    <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn"
                                 data-testid="sfx-rec-start" onClick={() => start(r.id)}
                                 title="Let BOSS try to fix this itself; you approve the install">Fix it</GatedButton>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </>
      )}
      {sending ? (
        <SendSheet projectId={projectId} actor={actor} selfFixId={sending}
                   onClose={() => setSending(null)} onSent={() => { list.reload(); one.reload(); }} />
      ) : null}
    </div>
  );
}
