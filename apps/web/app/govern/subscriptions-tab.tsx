"use client";

/**
 * Govern › Subscriptions (BOSSAPP-1342): each connected plan (Claude Code, Codex, ...), its 5-hour
 * and weekly windows with % left and the reset in the viewer's own time, the status, which jobs
 * route there and what BOSS does at the limit. The numbers are the router's own (the same
 * ones Setup's headroom strip shows). A reading BOSS has not heard from in a while says how old
 * it is instead of passing for current.
 */
import React from "react";

import { localWith } from "../../lib/local-time";
import type { SubscriptionSource, SubscriptionsView } from "../../lib/govern";

const ORDER = ["exhausted", "near_limit", "tight", "plenty"];
/** "3 h ago" from when BOSS last heard the number: computed here so the read stays cacheable. */
function ago(iso: string | null): string {
  const ms = iso ? Date.now() - Date.parse(iso) : NaN;
  if (!Number.isFinite(ms)) return "";
  const m = Math.max(0, Math.round(ms / 60000));
  return m < 2 ? "just now" : m < 60 ? `${m} min ago` : m < 36 * 60 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
}
const reset = (iso: string | null) => localWith(iso, { weekday: "short", hour: "numeric", minute: "2-digit" });

/** The banner on every Govern tab while an account is Tight, Near limit or at its limit. */
export function SubscriptionBanner({ view, onOpen }: { view: SubscriptionsView | null; onOpen: () => void }) {
  const hot = (view?.sources ?? []).filter((s) => s.state !== "plenty")
    .sort((a, b) => ORDER.indexOf(a.state) - ORDER.indexOf(b.state));
  if (!hot.length) return null;
  const worst = hot[0];
  return (
    <div className="gv-sub-banner" role="status" data-state={worst.state} data-testid="gv-sub-banner">
      <span><strong>{worst.label}: {worst.state_label.toLowerCase()}.</strong>{" "}
        {worst.state === "exhausted" ? worst.at_limit.words
          : `${worst.left_pct ?? "?"}% of the week left${worst.resets_at ? `, resets ${reset(worst.resets_at)}` : ""}.`}
        {hot.length > 1 ? ` ${hot.length - 1} more account${hot.length > 2 ? "s" : ""} need attention.` : ""}</span>
      <button type="button" className="ag-btn" data-testid="gv-sub-banner-open" onClick={onOpen}>See subscriptions</button>
    </div>
  );
}

function Card({ s }: { s: SubscriptionSource }) {
  const fresh = s.freshness;
  return (
    <section className="gv-sub" data-testid={`gv-sub-${s.engine}`} data-state={s.state}>
      <header className="gv-sub-head">
        <h3 className="gv-sub-name">{s.label}</h3>
        <span className="gv-sub-pill" data-state={s.state} data-testid={`gv-sub-state-${s.engine}`}>{s.state_label}</span>
        {s.login.state === "expired" ? <span className="gv-sub-faint">{s.login.words}</span> : null}
        {fresh.stale && fresh.as_of ? (
          <span className="gv-sub-faint" data-testid={`gv-sub-age-${s.engine}`}
                title="BOSS has not heard this account's numbers since; they may have moved.">last reported {ago(fresh.as_of)}</span>
        ) : null}
      </header>
      {s.windows.length ? s.windows.map((w) => (
        <div className="gv-sub-win" key={w.kind} data-testid={`gv-sub-window-${s.engine}-${w.kind}`}>
          <span>{w.label}</span>
          <span className="gv-sub-meter" role="meter" aria-label={`${s.label} ${w.label} left`}
                aria-valuemin={0} aria-valuemax={100} aria-valuenow={w.left_pct ?? undefined}>
            <i data-state={w.state} style={{ width: `${w.left_pct ?? 0}%` }} />
          </span>
          <span>{w.state === "exhausted" ? "Limit reached" : `${w.left_pct}% left`}
            {w.resets_at ? <span className="gv-sub-faint"> · resets {reset(w.resets_at)}</span> : null}</span>
        </div>
      )) : (
        <span className="gv-sub-faint">{s.basis === "unknown" ? "No usage reported yet." : `${s.left_pct ?? "?"}% left`}</span>
      )}
      <span className="gv-sub-faint" data-testid={`gv-sub-jobs-${s.engine}`}>
        {s.jobs.length ? `Used by: ${s.jobs.join(", ")}` : "No job is routed here."}
      </span>
      <span data-testid={`gv-sub-atlimit-${s.engine}`}><strong>At the limit:</strong> {s.at_limit.words}</span>
    </section>
  );
}

export function SubscriptionsTab({ view, error }: { view: SubscriptionsView | null; error: string }) {
  if (!view) return <div className="gv-skel" aria-busy={!error || undefined}>{error || null}</div>;
  if (!view.sources.length) {
    return <p className="gv-sub-faint" data-testid="gv-sub-empty">No subscription is connected. Connect Claude Code or Codex in Setup to see its limits here.</p>;
  }
  const sources = [...view.sources].sort((a, b) => ORDER.indexOf(a.state) - ORDER.indexOf(b.state));
  return <div className="gv-subs" data-testid="gv-subs">{sources.map((s) => <Card key={s.key} s={s} />)}</div>;
}
