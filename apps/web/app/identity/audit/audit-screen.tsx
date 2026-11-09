"use client";

/**
 * THE SECURITY AUDIT LOG (mockup g, IDENTITY.md §8, §9.9).
 *
 * Header: the chain's verification (seq, when), Verify now and Export (step-up). A filter bar
 * (search with tokens, who, event group, kind, time, more), a dense table grouped by day, and the
 * detail drawer: who, for whom, by which chain, why it was decided, the event as stored (already
 * redacted by the API, rendered as data) and its place in the hash chain.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight, Check, DownloadSimple, Funnel, MagnifyingGlass, ShieldCheck, ShieldWarning, UsersThree, X, DotsThree, CaretRight,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../../agent-avatar";
import { CanButton, useGuardedCall } from "../../../components/identity/identity-provider";
import { Face, Pill, clock, useNow } from "../../../components/identity/kit";
import { IdentityError, idGet, idPost } from "../../../lib/identity/api";
import type { AuditEvent, AuditOutcome, AuditPage } from "../../../lib/identity/types";
import { showToast } from "../../client-forms";
import { JsonView } from "../json-view";
import { ErrorState, type ReadFailure } from "../../../components/identity/states";
import { eventWords, REQUESTS_PATH } from "../../../lib/identity/words";
import { formatUtcStamp } from "../../../lib/datetime";
import { plainText } from "../../../lib/identity/plain";
import { publicAddress, sourceWords } from "../../../lib/identity/address";
import { EMPTY_FILTERS, EVENT_GROUPS, apiQuery, inGroup, parseSearch, queryFrom, searchText, type AuditFilters, type EventGroup } from "./audit-query";

type Kind = "all" | "people" | "agents" | "keys" | "system";
const KINDS: { id: Kind; label: string }[] = [
  { id: "all", label: "Kind: people, agents, keys" }, { id: "people", label: "Kind: people" },
  { id: "agents", label: "Kind: agents" }, { id: "keys", label: "Kind: keys" }, { id: "system", label: "Kind: system" },
];
type Range = "1h" | "24h" | "7d" | "all";
const RANGES: { id: Range; label: string; ms: number }[] = [
  { id: "1h", label: "Last hour", ms: 3_600_000 }, { id: "24h", label: "Last 24 h", ms: 86_400_000 },
  { id: "7d", label: "Last 7 days", ms: 7 * 86_400_000 }, { id: "all", label: "All time", ms: Infinity },
];

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
function dayLabel(iso: string, now: number): string {
  const d = new Date(iso);
  const key = (t: Date) => t.toISOString().slice(0, 10);
  const today = new Date(now);
  const yesterday = new Date(now - 86_400_000);
  const base = `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
  if (key(d) === key(today)) return `TODAY, ${base}`;
  if (key(d) === key(yesterday)) return `YESTERDAY, ${base}`;
  return base;
}

/** Save the export bundle the API returns inline (it has no download URL). */
function saveJson(data: unknown, name: string): void {
  try {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch { /* no download here (tests, old browsers) */ }
}

const OUTCOME_TONE: Record<AuditOutcome, "ok" | "bad" | "warn"> = { ok: "ok", denied: "bad", failed: "warn", notice: "warn" };

function kindOf(e: AuditEvent): Kind {
  if (e.actor.kind === "agent") return "agents";
  if (e.actor.kind === "service_account" || e.event_type.startsWith("api_key.")) return "keys";
  if (e.actor.kind === "user") return "people";
  return "system";
}

function Actor({ e }: { e: AuditEvent }) {
  return (
    <span className="idn-actor">
      {e.actor.kind === "agent" ? <AgentAvatar agent={e.actor.id.replace(/^agt_/, "")} size={20} tip={e.actor.name} />
        : <Face name={e.actor.kind === "unknown" ? "?" : e.actor.name} tone={e.actor.hue} size={22} />}
      <b>{e.actor.name}</b>
      {e.on_behalf_of ? <span className="idn-via">for {e.on_behalf_of.name}</span> : null}
    </span>
  );
}

function shortHash(h: string): string {
  return h.length > 16 ? `${h.slice(0, 4)} ${h.slice(4, 8)} ${h.slice(8, 12)} … ${h.slice(-4)}` : h;
}

function Drawer({ e, onFilter }: { e: AuditEvent; onFilter: (f: Partial<AuditFilters>) => void }) {
  const decided = e.outcome === "denied" ? "Why it was denied" : e.outcome === "failed" ? "Why it failed" : "Why it was allowed";
  const requestId = typeof e.raw.request_id === "string" ? e.raw.request_id : null;
  return (
    <aside className="idn-drawer" data-outcome={e.outcome} aria-labelledby="idn-dr-h" data-testid="audit-drawer">
      <div className="idn-dr-h">
        <div className="idn-evc" title={e.event_type}>{eventWords(e.event_type)}, #{e.seq.toLocaleString("en-US")}</div>
        <h2 id="idn-dr-h" data-copy="data">{plainText(e.headline)}</h2>
        {e.event_type === "access.requested" ? (
          <a className="idn-link" href={REQUESTS_PATH} title="Grant or decline it in Access requests" data-testid="audit-review-request">Review request</a>
        ) : null}
        <div className="idn-row idn-dim idn-small">
          <span title={requestId ? `Request ${requestId}` : undefined}>{formatUtcStamp(e.at)} UTC</span>
        </div>
      </div>
      <div className="idn-dr-b">
        <div>
          <div className="idn-label idn-lbl">Who, for whom, by which chain</div>
          <div className="idn-mini-chain" data-testid="drawer-chain">
            {(e.chain.length ? e.chain : [{ id: e.actor.id, name: e.actor.name, run: null, kind: e.actor.kind === "agent" ? "agent" as const : "user" as const }]).map((c, i, all) => (
              <React.Fragment key={`${c.id}-${i}`}>
                <span className="idn-n">
                  {c.kind === "agent" ? <AgentAvatar agent={c.id.replace(/^agt_/, "")} size={20} tip={c.name} />
                    : <Face name={c.name} tone={e.on_behalf_of?.id === c.id ? e.on_behalf_of.hue : e.actor.hue} size={22} />}
                  {c.name}{c.run ? <small title={c.run}>{plainText(c.run) || "a run"}</small> : null}
                </span>
                {i < all.length - 1 ? <ArrowRight size={12} className="idn-dim" aria-label="then" /> : null}
              </React.Fragment>
            ))}
          </div>
        </div>
        {e.why.length ? (
          <div>
            <div className="idn-label idn-lbl">{decided}</div>
            <div className="idn-why2" data-testid="drawer-why">
              {e.why.map((w) => (
                <div key={w.text}>
                  <span className={w.held === false ? "idn-bad" : w.held ? "idn-ok" : "idn-dim"} aria-label={w.held === false ? "refused" : w.held ? "held" : "detail"}>
                    {w.held === false ? <X size={14} aria-hidden /> : w.held ? <Check size={14} aria-hidden /> : <DotsThree size={14} aria-hidden />}
                  </span>
                  <span data-copy="data" title={w.text}>{plainText(w.text)}</span>
                </div>
              ))}
            </div>
          </div>
        ) : null}
        <div data-raw-ok>
          <div className="idn-label idn-lbl">Event, as stored (redacted)</div>
          <JsonView data={e.raw} label="The stored event, redacted" />
        </div>
        <div>
          <div className="idn-label idn-lbl">Integrity</div>
          <div className="idn-hash" data-copy="data">
            <span>prev</span><span>{shortHash(e.prev_hash)}</span>
            <span>hash</span><span>{shortHash(e.hash)}</span>
            <span>status</span><span>links to #{(e.seq - 1).toLocaleString("en-US")} ✓</span>
          </div>
        </div>
        <div className="idn-row">
          {e.run_id ? <a className="idn-btn sm" href={`/?area=code_tasks&run=${encodeURIComponent(e.run_id)}`}>Open the run</a> : null}
          {e.grant_id ? <a className="idn-btn sm" href={`/identity/agents/delegations/${encodeURIComponent(e.grant_id)}`}>Open the grant</a> : null}
          {e.token_jti ? <button type="button" className="idn-btn sm" title={`Token ${e.token_jti}`} onClick={() => onFilter({ q: e.token_jti! })}>Everything with this token</button> : null}
        </div>
      </div>
    </aside>
  );
}

export function AuditScreen({ initial, initialFilters, initialEvent, verifyOnOpen = false, failure = null }: {
  initial: AuditPage | null; initialFilters: AuditFilters; initialEvent: string | null; verifyOnOpen?: boolean;
  /** Why the first read failed: said in words, never drawn as an empty log. */
  failure?: ReadFailure | null;
}) {
  const now = useNow();
  const [page, setPage] = useState<AuditPage | null>(initial);
  const [filters, setFilters] = useState<AuditFilters>(initialFilters);
  const [group, setGroup] = useState<EventGroup>("all");
  const [kind, setKind] = useState<Kind>("all");
  const [range, setRange] = useState<Range>("all");
  const [more, setMore] = useState(Boolean(initialFilters.grant));
  const [loading, setLoading] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [linked, setLinked] = useState<AuditEvent | null>(null);
  const guarded = useGuardedCall();
  const events = page?.events ?? [];
  const rangeMs = RANGES.find((r) => r.id === range)!.ms;
  const firstPick = initialEvent ?? events.find((e) => e.outcome === "denied" && e.why.length)?.id ?? events.find((e) => e.outcome === "denied")?.id ?? events[0]?.id ?? null;
  const [selected, setSelected] = useState<string | null>(firstPick);
  const rowRefs = useRef<Map<string, HTMLTableRowElement>>(new Map());

  // Names  ids from what is on screen, for `chain:Sam` and the person chip.
  const names = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of events) {
      m.set(e.actor.id, e.actor.name.split(" ")[0]);
      if (e.on_behalf_of) m.set(e.on_behalf_of.id, e.on_behalf_of.name.split(" ")[0]);
    }
    return m;
  }, [events]);
  const nameOf = useCallback((id: string) => names.get(id) ?? null, [names]);
  const idOf = useCallback((name: string) => [...names.entries()].find(([, n]) => n.toLowerCase() === name.toLowerCase())?.[0] ?? null, [names]);
  const [text, setText] = useState(() => searchText(initialFilters, () => null));

  const load = useCallback(async (f: AuditFilters, view = { group, kind, rangeMs }, cursor: string | null = null) => {
    setLoading(true);
    try {
      const next = await idGet<AuditPage>(`/v1/identity/audit?${apiQuery(f, view, new Date().getTime(), cursor)}`);
      // "Load more" appends the older page; a new filter replaces it.
      setPage((p) => (cursor && p ? { ...next, events: [...p.events, ...next.events] } : next));
      try {
        const url = new URL(window.location.href);
        url.search = queryFrom(f);
        window.history.replaceState(null, "", url.toString());
      } catch { /* no history here */ }
    } catch (e) {
      showToast("error", e instanceof IdentityError ? e.message : "Not loaded");
    } finally {
      setLoading(false);
    }
  }, [group, kind, rangeMs]);

  const apply = (f: AuditFilters) => {
    setFilters(f);
    setText(searchText(f, nameOf));
    load(f);
  };
  /** A dropdown changed: ask the API for that slice (it filters event prefix, kind and time). */
  const reslice = (view: { group: EventGroup; kind: Kind; rangeMs: number }) => { load(filters, view); };

  // A deep link (?event=sev_N) to an event not on this page is read by id for the drawer.
  useEffect(() => {
    if (!initialEvent || events.some((e) => e.id === initialEvent)) return;
    let live = true;
    idGet<AuditEvent>(`/v1/identity/audit/${encodeURIComponent(initialEvent)}`)
      .then((e) => { if (live) setLinked(e); })
      .catch(() => undefined);
    return () => { live = false; };
    // Only for the event the page was opened on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialEvent]);

  // Verifying walks the whole chain under audit:export, which asks for a fresh sign-in (step-up):
  // a press opens the step-up sheet (guarded) instead of failing with "Confirm it's you"; the
  // automatic check on opening (?verify=1) never prompts and stays quiet when step-up is needed.
  const verify = useCallback(async (pressed = true) => {
    setVerifying(true);
    const call = () => idPost<{ ok: boolean; seq: number; verified_at: string | null; first_break: number | null }>("/v1/identity/audit/verify", {});
    try {
      const r = pressed ? await guarded("Verifying reads the whole security log.", call) : await call();
      setPage((p) => (p ? { ...p, chain: { verified_at: r.verified_at, seq: r.seq, ok: r.ok, first_break: r.first_break } } : p));
      showToast(r.ok ? "success" : "error", r.ok ? "Chain verified" : "Chain broken");
    } catch (e) {
      const quiet = e instanceof IdentityError && (e.code === "step_up_cancelled" || (!pressed && e.code === "step_up_required"));
      if (!quiet) showToast("error", e instanceof IdentityError ? e.message : "Not verified");
    } finally {
      setVerifying(false);
    }
  }, [guarded]);

  useEffect(() => { if (verifyOnOpen) verify(false); }, [verifyOnOpen, verify]);

  const exportLog = async () => {
    try {
      const r = await guarded("Exporting copies the whole security log.", () =>
        idPost<{ url: string | null; events: number; anchor: string | null; bundle?: unknown }>("/v1/identity/audit/export", {}));
      if (r.url) window.location.assign(r.url);
      else if (r.bundle) saveJson(r.bundle, `boss-audit-${new Date().toISOString().slice(0, 10)}.json`);
      showToast("success", `Exported ${r.events.toLocaleString("en-US")} events`);
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) showToast("error", e instanceof IdentityError ? e.message : "Not exported");
    }
  };

  const shown = useMemo(() => {
    const span = RANGES.find((r) => r.id === range)!.ms;
    return events.filter((e) => inGroup(e.event_type, group) && (kind === "all" || kindOf(e) === kind) && now - Date.parse(e.at) <= span);
  }, [events, group, kind, range, now]);

  const days = useMemo(() => {
    const out: { label: string; events: AuditEvent[] }[] = [];
    for (const e of shown) {
      const label = dayLabel(e.at, now);
      const last = out[out.length - 1];
      if (last && last.label === label) last.events.push(e); else out.push({ label, events: [e] });
    }
    return out;
  }, [shown, now]);

  const current = shown.find((e) => e.id === selected) ?? (linked && linked.id === selected ? linked : null);
  const pick = (id: string) => {
    setSelected(id);
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("event", id);
      window.history.replaceState(null, "", url.toString());
    } catch { /* no history here */ }
  };
  const move = (id: string, delta: number) => {
    const i = shown.findIndex((e) => e.id === id);
    const next = shown[i + delta];
    if (next) { pick(next.id); rowRefs.current.get(next.id)?.focus(); }
  };

  const chain = page?.chain;
  return (
    <>
      <header className="idn-ah">
        <div>
          <h1>Security audit log</h1>
        </div>
        {/* Never verified is not "verified": the Overview said "not verified yet" while this badge
            said "Chain verified", because the API answers ok: true until a first check runs. */}
        {chain ? (
          <span className="idn-verified" data-ok={chain.ok} data-pending={(chain.ok && !chain.verified_at) || undefined} role="status" data-testid="chain-status">
            {chain.ok && chain.verified_at ? <ShieldCheck size={14} aria-hidden /> : <ShieldWarning size={14} aria-hidden />}
            {!chain.ok ? `Chain broken at #${chain.first_break}` : chain.verified_at ? "Chain verified" : "Not verified yet"}, <span title="Events in the hash chain">{chain.seq.toLocaleString("en-US")} events</span>
            {chain.verified_at ? <>, {clock(chain.verified_at).slice(0, 5)}</> : null}
          </span>
        ) : null}
        <span className="idn-spacer" />
        <div className="idn-acts">
          <CanButton permission="audit:export" className="idn-btn" onClick={() => verify(true)} disabled={verifying} tip="Walk the hash chain and report the first break" data-testid="verify-chain">
            <Check size={15} aria-hidden /> {verifying ? "Verifying…" : "Verify now"}
          </CanButton>
          <CanButton permission="audit:export" className="idn-btn" onClick={exportLog} tip="Asks you to confirm it's you first" data-testid="export-audit">
            <DownloadSimple size={15} aria-hidden /> Export, step-up
          </CanButton>
        </div>
      </header>

      <form className="idn-fb" role="search" aria-label="Filter events" onSubmit={(ev) => { ev.preventDefault(); apply(parseSearch(text, idOf)); }}>
        <label className="idn-q">
          <MagnifyingGlass size={15} aria-hidden />
          <span className="idn-sr">Search events, people, agents and grants</span>
          <input value={text} onChange={(ev) => setText(ev.target.value)} placeholder="Search events, people, agents, grants…" data-testid="audit-search" />
          {text ? <button type="button" aria-label="Clear search" onClick={() => apply({ ...EMPTY_FILTERS })}><X size={13} aria-hidden /></button> : null}
        </label>
        {filters.actor ? (
          <button type="button" className="idn-fchip" aria-pressed="true" onClick={() => apply({ ...filters, actor: "" })} title="Remove this filter">
            <UsersThree size={14} aria-hidden /> {nameOf(filters.actor) ?? filters.actor} + agents for {nameOf(filters.actor) ?? "them"} <X size={11} aria-hidden />
          </button>
        ) : null}
        <label className="idn-fchip idn-select">
          <span className="idn-sr">Event group</span>
          <select value={group} onChange={(ev) => { const g = ev.target.value as EventGroup; setGroup(g); reslice({ group: g, kind, rangeMs }); }}>
            {EVENT_GROUPS.map((g) => <option key={g.id} value={g.id}>{g.label}</option>)}
          </select>
        </label>
        <label className="idn-fchip idn-select">
          <span className="idn-sr">Principal kind</span>
          <select value={kind} onChange={(ev) => { const k = ev.target.value as Kind; setKind(k); reslice({ group, kind: k, rangeMs }); }}>
            {KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
        </label>
        <label className="idn-fchip idn-select">
          <span className="idn-sr">Time</span>
          <select value={range} onChange={(ev) => { const r = ev.target.value as Range; setRange(r); reslice({ group, kind, rangeMs: RANGES.find((x) => x.id === r)!.ms }); }}>
            {RANGES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
          </select>
        </label>
        <button type="button" className="idn-fchip" aria-pressed={more} aria-expanded={more} onClick={() => setMore((v) => !v)}>
          <Funnel size={14} aria-hidden /> More
        </button>
        {more ? (
          <div className="idn-fb-more">
            <label className="idn-field">
              <span className="idn-label">Outcome</span>
              <select className="idn-input" value={filters.outcome} onChange={(ev) => apply({ ...filters, outcome: ev.target.value })}>
                <option value="">Any</option><option value="denied">Denied</option><option value="failed">Failed</option>
                <option value="denied,failed">Denied or failed</option><option value="notice">Notice</option><option value="ok">OK</option>
              </select>
            </label>
            <label className="idn-field">
              <span className="idn-label">Grant</span>
              <input className="idn-input" value={filters.grant} placeholder="From a grant's page" title="Open a grant and use its audit link" onChange={(ev) => setFilters({ ...filters, grant: ev.target.value })}
                onBlur={() => apply(filters)} />
            </label>
          </div>
        ) : null}
      </form>

      {!page && failure ? <ErrorState what="the audit log" failure={failure} /> : null}
      {!page && !failure ? <ErrorState what="the audit log" failure={{ status: 500, code: "error" }} /> : null}
      <div className="idn-split" aria-busy={loading}>
        <div className="idn-lt">
          <div className="idn-scroll">
            <table role="grid" aria-label="Security events" aria-rowcount={shown.length} data-testid="audit-table">
              <colgroup>
                <col className="idn-c-time" /><col className="idn-c-event" /><col className="idn-c-actor" /><col /><col className="idn-c-result" /><col className="idn-c-from" />
              </colgroup>
              <thead><tr><th>Time</th><th>Event</th><th>Actor</th><th>What</th><th>Result</th><th>From</th></tr></thead>
              <tbody>
                {days.map((d) => (
                  <React.Fragment key={d.label}>
                    <tr className="idn-day" aria-hidden><td colSpan={6}>{d.label}</td></tr>
                    {d.events.map((e) => (
                      <tr key={e.id} ref={(el) => { if (el) rowRefs.current.set(e.id, el); else rowRefs.current.delete(e.id); }}
                        tabIndex={0} aria-selected={e.id === selected} data-outcome={e.outcome} data-testid={`event-${e.id}`}
                        onClick={() => pick(e.id)}
                        onKeyDown={(ev) => {
                          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(e.id); }
                          if (ev.key === "ArrowDown") { ev.preventDefault(); move(e.id, 1); }
                          if (ev.key === "ArrowUp") { ev.preventDefault(); move(e.id, -1); }
                        }}>
                        <td className="idn-tm">{clock(e.at)}</td>
                        <td className="idn-evn" title={e.event_type}>{eventWords(e.event_type)}</td>
                        <td><Actor e={e} /></td>
                        <td className="idn-tgt" title={e.what} data-copy="data">{e.what === e.event_type ? "" : plainText(e.what)}</td>
                        <td><Pill tone={OUTCOME_TONE[e.outcome]}>{e.outcome}</Pill></td>
                        <td className="idn-ipx" data-copy="data" title={publicAddress(e.source) ? undefined : e.source}>{sourceWords(e.source)}</td>
                      </tr>
                    ))}
                  </React.Fragment>
                ))}
                {!shown.length && page ? <tr><td colSpan={6} className="idn-dim">{page.events.length ? "No events match" : "No security events yet"}</td></tr> : null}
              </tbody>
            </table>
            {page?.cursor ? (
              <button type="button" className="idn-btn sm ghost" disabled={loading} data-testid="audit-more"
                onClick={() => load(filters, { group, kind, rangeMs }, page.cursor)}>{loading ? "Loading…" : "Load older events"}</button>
            ) : null}
          </div>
        </div>
        {current ? <Drawer e={current} onFilter={(f) => apply({ ...EMPTY_FILTERS, ...f })} /> : (
          <aside className="idn-card idn-dim idn-row" data-outcome="ok"><CaretRight size={14} aria-hidden /> Pick an event to see why</aside>
        )}
      </div>
    </>
  );
}
