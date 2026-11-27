"use client";

/**
 * Govern › Subscriptions (BOSSAPP-1342): each connected plan (Claude Code, Codex, ...), its 5-hour
 * and weekly windows with % left and the reset in the viewer's own time, the status, which jobs
 * route there and what BOSS does at the limit. The numbers are the router's own (the same
 * ones Setup's headroom strip shows). A reading BOSS has not heard from in a while says how old
 * it is instead of passing for current. An engine with several accounts (a pool, BOSSAPP-1343)
 * shows each account under its pool, the primary marked and the one taking new work named.
 */
import React from "react";
import { ArrowRight, Crown, Gauge, UsersThree } from "@phosphor-icons/react";

import { consoleHref } from "../url-intent";
import { localWith, useLocalTime } from "../../lib/local-time";
import type { SubscriptionSource, SubscriptionsView } from "../../lib/govern";

const ORDER = ["exhausted", "near_limit", "tight", "plenty"];
/** "3 h ago" from when BOSS last heard the number: computed here so the read stays cacheable. */
function ago(iso: string | null, nowMs: number): string {
  const ms = iso && nowMs > 0 ? nowMs - Date.parse(iso) : NaN;
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
      <button type="button" className="ag-btn" data-testid="gv-sub-banner-open" onClick={onOpen}>
        <Gauge size={14} aria-hidden />See subscriptions</button>
    </div>
  );
}

/** A card's test id: the engine for a single login, engine + seat for a pool's accounts. */
const tidOf = (s: SubscriptionSource) => (s.pool ? `${s.engine}-${s.pool.position}` : s.engine);

function Card({ s }: { s: SubscriptionSource }) {
  const lt = useLocalTime();
  const fresh = s.freshness;
  const tid = tidOf(s);
  return (
    <section className="gv-sub" data-testid={`gv-sub-${tid}`} data-state={s.state}>
      <header className="gv-sub-head">
        <h3 className="gv-sub-name">{s.label}</h3>
        <span className="gv-sub-pill" data-state={s.state} data-testid={`gv-sub-state-${tid}`}>{s.state_label}</span>
        {s.pool?.primary ? (
          <span className="gv-sub-tag" data-testid={`gv-sub-primary-${tid}`} title="Leads while it has plenty of room">
            <Crown size={12} weight="bold" aria-hidden />Primary</span>
        ) : null}
        {s.pool && s.taking_new_work ? (
          <span className="gv-sub-tag" data-tone="accent" data-testid={`gv-sub-next-${tid}`}
                title="The account a new build is given now">
            <ArrowRight size={12} weight="bold" aria-hidden />Takes new work</span>
        ) : null}
        {s.login.state === "expired" ? <span className="gv-sub-faint">{s.login.words}</span> : null}
        {fresh.stale && fresh.as_of && lt.now() > 0 ? (
          <span className="gv-sub-faint" data-testid={`gv-sub-age-${tid}`}
                title="BOSS has not heard this account's numbers since; they may have moved.">last reported {ago(fresh.as_of, lt.now())}</span>
        ) : null}
      </header>
      {s.windows.length ? s.windows.map((w) => (
        <div className="gv-sub-win" key={w.kind} data-testid={`gv-sub-window-${tid}-${w.kind}`}>
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
      <span className="gv-sub-faint" data-testid={`gv-sub-jobs-${tid}`}>
        {s.jobs.length ? `Used by: ${s.jobs.join(", ")}` : "No job is routed here."}
      </span>
      <span data-testid={`gv-sub-atlimit-${tid}`}><strong>At the limit:</strong> {s.at_limit.words}</span>
    </section>
  );
}

export function SubscriptionsTab({ view, error }: { view: SubscriptionsView | null; error: string }) {
  if (!view) return <div className="gv-skel" aria-busy={!error || undefined}>{error || null}</div>;
  if (!view.sources.length) {
    return <p className="gv-sub-faint" data-testid="gv-sub-empty">No subscription is connected. Connect Claude Code or Codex in Setup to see its limits here.</p>;
  }
  const sources = [...view.sources].sort((a, b) => ORDER.indexOf(a.state) - ORDER.indexOf(b.state));
  // ACCOUNT POOLS (BOSSAPP-1343): a pool's accounts sit together under the pool, in seat order,
  // at the place of its worst account; single logins stay one card each.
  const groups: { pool: SubscriptionSource["pool"]; items: SubscriptionSource[] }[] = [];
  for (const s of sources) {
    const at = s.pool ? groups.find((g) => g.pool?.id === s.pool!.id) : undefined;
    if (at) at.items.push(s); else groups.push({ pool: s.pool, items: [s] });
  }
  return (
    <div className="gv-subs" data-testid="gv-subs">
      {groups.map((g) => (g.pool ? <Pool key={g.pool.id} pool={g.pool} items={g.items} /> : <Card key={g.items[0].key} s={g.items[0]} />))}
    </div>
  );
}

function Pool({ pool, items }: { pool: NonNullable<SubscriptionSource["pool"]>; items: SubscriptionSource[] }) {
  const seats = [...items].sort((a, b) => (a.pool?.position ?? 0) - (b.pool?.position ?? 0));
  const [href, setHref] = React.useState("");
  // Built from the URL after mount (keeps org=), so the server render and the first paint agree.
  React.useEffect(() => { setHref(consoleHref(window.location.search, { area: "setup", focus: "models" })); }, []);
  const n = pool.size;
  return (
    <section className="gv-sub-pool" data-testid={`gv-sub-pool-${items[0].engine}`} aria-label={`${pool.name}: ${n} accounts`}>
      <header className="gv-sub-pool-head">
        <UsersThree size={16} weight="bold" aria-hidden />
        <h3 className="gv-sub-name">{pool.name}</h3>
        <span className="gv-sub-faint" data-testid={`gv-sub-pool-size-${items[0].engine}`}>
          {n} account{n === 1 ? "" : "s"}{pool.scope === "org" ? " · whole org" : ""}</span>
        <a className="ag-btn gv-sub-pool-go" href={href || undefined} data-testid={`gv-sub-pool-manage-${items[0].engine}`}
           title="Add, remove or reorder this pool's accounts in Setup › Models">
          <UsersThree size={14} aria-hidden />Manage accounts</a>
      </header>
      {seats.map((s) => <Card key={s.key} s={s} />)}
    </section>
  );
}
