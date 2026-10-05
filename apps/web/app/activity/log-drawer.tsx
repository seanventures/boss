"use client";

/**
 * One line, opened (spec §4.2.7): a right-hand drawer over the list (a full sheet on a phone),
 * not a box below the table. Its facts with `+`/`−` to filter for or out, the places it leads,
 * `Show around this` (the lines before and after it in the same run), the body with today's
 * clip truth, the redacted data with Copy, and `Copy link` (the permalink, `row=`).
 */
import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Copy, LinkSimple, Minus, Plus, X } from "@phosphor-icons/react";

import type { ActivityLogItem } from "../../lib/activity-log";
import { fetchContext, type LogContext } from "../../lib/log-api";
import { clock, dayLabel, took, utcTitle } from "../../lib/log-time";
import type { LogToken, LogTokenKey } from "../../lib/log-url";
import { usd } from "../../lib/money";
import { getAct } from "../../lib/agent-world";
import type { Act } from "../../lib/agent-world-types";
import { showToast } from "../client-forms";
import { ActCard } from "../agents/act-card";
import { nameOf, RunFace } from "../agents/run-face";
import { LogLine } from "./log-rows";
import {
  LEVEL_LABEL, SERVICE_LABEL, STREAM_LABEL, costOf, durationOf, linksOf, flatten, type DisplayRow,
} from "./log-model";
import { useIdentityVersion } from "../../lib/use-agent-identities";

type Fact = { label: string; value: React.ReactNode; title?: string; token?: LogToken };

function factsOf(item: ActivityLogItem, labels: Record<string, string>): Fact[] {
  const facts: Fact[] = [
    { label: "When", value: `${dayLabel(item.createdAt)} ${clock(item.createdAt)}`, title: utcTitle(item.createdAt) },
    { label: "Stream", value: STREAM_LABEL[item.source] ?? item.source, token: { key: "src", value: item.source } },
    { label: "Kind", value: item.kind, token: item.kind ? { key: "kind", value: item.kind } : undefined },
    { label: "Status", value: item.status, token: { key: "status", value: item.status } },
  ];
  if (item.agent) {
    facts.push({ label: "Agent", token: { key: "agent", value: item.agent },
                 value: <span className="lx-fact-face"><RunFace roleKey={item.agent} size={16} /><span data-user-text>{nameOf(item.agent).name}</span></span> });
  }
  if (item.actor) facts.push({ label: "Actor", value: item.actor, token: { key: "actor", value: item.actor } });
  if (item.service) {
    facts.push({ label: "Process", value: SERVICE_LABEL[item.service] ?? item.service,
                 token: { key: "svc", value: item.service } });
  }
  // The pipeline stage an agent turn or model call ran in (D13), and a deploy's app (D16:
  // an app is not a process, so it is named here rather than under Process).
  if ((item.source === "agent_turn" || item.source === "llm_call") && item.stage) {
    facts.push({ label: "Stage", value: item.stage.replace(/_/g, " "), title: item.stage });
  }
  if (item.source === "deployment" && item.data.app) {
    facts.push({ label: "App", value: String(item.data.app), token: { key: "subject", value: String(item.data.app) },
                 title: item.data.environment ? `${String(item.data.environment)}` : undefined });
  }
  const step = item.source === "workflow_step" ? item.stage : String(item.data.step_id ?? "");
  if (step && item.workflowRunId) {
    facts.push({ label: "Step", value: labels[`${item.workflowRunId}/${step}`] || step, title: step });
  }
  if (item.entityId) {
    facts.push({ label: "Entity", value: labels[item.entityId] || item.entityType || item.entityId,
                 title: `${item.entityType} ${item.entityId}`.trim() });
  }
  const dur = took(durationOf(item));
  if (dur) facts.push({ label: "Duration", value: dur });
  const cost = costOf(item);
  if (cost > 0) facts.push({ label: "Cost", value: usd(cost), title: "Metered model spend" });
  return facts;
}

const FILTERABLE: ReadonlySet<LogTokenKey> = new Set(["src", "kind", "status", "agent", "actor", "svc", "subject"]);
const NEGATABLE_HERE: ReadonlySet<LogTokenKey> = new Set(["src", "agent"]);

export function LogDrawer({ item, labels, gone, projectId, viewer, onClose, onFilter, onPrev, onNext }: {
  item: ActivityLogItem | null;
  /** Who is reading, so the work card names a person "you" only for them. */
  viewer: string;
  labels: Record<string, string>;
  /** A deep link to a line that is not there any more (pruned, or another project's). */
  gone: boolean;
  projectId: string;
  onClose: () => void;
  onFilter: (token: LogToken) => void;
  onPrev?: () => void;
  onNext?: () => void;
}) {
  useIdentityVersion(); // names land when the install's agents load
  const box = useRef<HTMLElement>(null);
  const [around, setAround] = useState<LogContext | null>(null);
  const [aroundState, setAroundState] = useState<"idle" | "loading" | "failed">("idle");
  const [dataOpen, setDataOpen] = useState(false);
  const [act, setAct] = useState<Act | null>(null);

  useEffect(() => { setAround(null); setAroundState("idle"); setDataOpen(false); setAct(null); }, [item?.id]);
  useEffect(() => { box.current?.focus(); }, [item?.id, gone]);

  const search = typeof window === "undefined" ? "" : window.location.search;
  const copyLink = () => {
    try {
      void navigator.clipboard?.writeText(window.location.href);
      showToast("success", "Link copied");
    } catch { /* the address bar holds the same link */ }
  };
  const showAround = async () => {
    if (!item) return;
    setAroundState("loading");
    const got = await fetchContext(projectId, item.id);
    if (got.ok && got.data) { setAround(got.data); setAroundState("idle"); } else setAroundState("failed");
  };
  const openWork = async () => {
    const actId = String(item?.data.act_id ?? "");
    if (!actId) return;
    if (act) { setAct(null); return; }
    const r = await getAct(projectId, actId);
    if (r.ok) setAct(r.data);
  };

  const sheet = useSheet();
  const drawer = (
    <aside ref={box} className="lx-drawer" role="dialog" aria-modal={sheet || undefined} aria-label="Line" tabIndex={-1}
           data-testid="lx-drawer"
           onKeyDown={(e) => {
             if (e.key === "Escape") { e.preventDefault(); onClose(); }
             else if ((e.key === "j" || e.key === "ArrowDown") && onNext && !(e.target as Element).closest("input, textarea")) { e.preventDefault(); onNext(); }
             else if ((e.key === "k" || e.key === "ArrowUp") && onPrev && !(e.target as Element).closest("input, textarea")) { e.preventDefault(); onPrev(); }
           }}>
      <header className="lx-drawer-head">
        {item ? (
          <span className="lx-lvl" data-level={item.level}>{LEVEL_LABEL[item.level] ?? item.level}</span>
        ) : null}
        <h2 className="lx-drawer-title" data-user-text>{item ? item.title || item.kind : "This line is gone"}</h2>
        <span className="lx-spacer" />
        {item ? (
          <button type="button" className="ag-link" data-testid="lx-copy-link" onClick={copyLink}>
            <LinkSimple size={12} aria-hidden /> Copy link
          </button>
        ) : null}
        <button type="button" className="ag-link" aria-label="Close" data-testid="lx-drawer-close" onClick={onClose}>
          <X size={13} weight="bold" aria-hidden />
        </button>
      </header>
      {gone || !item ? (
        <div className="lx-drawer-gone" data-testid="lx-row-gone" title="Older than the retention window">This line is gone</div>
      ) : (
        <div className="lx-drawer-body">
          <dl className="lx-facts">
            {factsOf(item, labels).map((f) => (
              <div key={f.label} className="lx-fact">
                <dt>{f.label}</dt>
                <dd title={f.title}>
                  <span className="lx-fact-value" data-user-text>{f.value}</span>
                  {f.token && FILTERABLE.has(f.token.key) ? (
                    <span className="lx-fact-acts">
                      <button type="button" aria-label={`Only ${f.label}`} title="Filter for this"
                              onClick={() => onFilter(f.token!)}><Plus size={10} weight="bold" aria-hidden /></button>
                      {NEGATABLE_HERE.has(f.token.key) ? (
                        <button type="button" aria-label={`Not ${f.label}`} title="Filter this out"
                                onClick={() => onFilter({ ...f.token!, negate: true })}><Minus size={10} weight="bold" aria-hidden /></button>
                      ) : null}
                    </span>
                  ) : null}
                </dd>
              </div>
            ))}
          </dl>
          <nav className="lx-links" aria-label="Open">
            {linksOf(item, search).map((l) => (
              <a key={l.key} className="ag-link" href={l.href} title={l.tip} data-testid={`lx-link-${l.key}`}>{l.label}</a>
            ))}
            {item.data.act_id ? (
              <button type="button" className="ag-link" data-testid="lx-link-work" aria-expanded={!!act}
                      title="The signed work card" onClick={() => void openWork()}>Work ↗</button>
            ) : null}
          </nav>
          {act ? <div className="lx-work"><ActCard act={act} viewer={viewer} inline onClose={() => setAct(null)} /></div> : null}
          <div className="lx-around">
            {around ? (
              <AroundLines context={around} labels={{ ...labels, ...around.labels }} anchor={item.id} />
            ) : (
              <button type="button" className="ag-btn" data-testid="lx-around" title="Lines just before and after, same run"
                      disabled={aroundState === "loading"} onClick={() => void showAround()}>
                {aroundState === "failed" ? "Can't reach BOSS" : "Show around this"}
              </button>
            )}
          </div>
          {item.body ? (
            <section className="lx-body-sec">
              {item.data.truncated === true ? (
                <span className="lx-clip" title="A clipped line is not a short one" data-testid="lx-clip">
                  Clipped when stored · {Number(item.data.content_bytes ?? 0).toLocaleString()} bytes captured
                </span>
              ) : null}
              <pre className="lx-text" data-user-text data-testid="lx-body">{item.body}</pre>
            </section>
          ) : null}
          <section className="lx-data-sec">
            {Object.keys(item.data).length ? (
              <>
                <span className="lx-data-head">
                  <button type="button" className="ag-link" aria-expanded={dataOpen} data-testid="lx-data-toggle"
                          onClick={() => setDataOpen((o) => !o)}>Data {dataOpen ? "▾" : "▸"}</button>
                  <button type="button" className="ag-link" title="Copy the redacted data"
                          onClick={() => { try { void navigator.clipboard?.writeText(JSON.stringify(item.data, null, 2)); showToast("success", "Copied"); } catch { /* nothing */ } }}>
                    <Copy size={12} aria-hidden /> Copy
                  </button>
                </span>
                {dataOpen ? <pre className="lx-data" data-user-text data-testid="lx-data">{JSON.stringify(item.data, null, 2)}</pre> : null}
              </>
            ) : <span className="lx-muted">No data</span>}
          </section>
        </div>
      )}
    </aside>
  );
  // ON A PHONE THE DRAWER IS A FULL-SCREEN SHEET (D11). The console's panel stack carries a
  // transform, which traps `position: fixed` inside it (it drew 13px in and taller than the
  // screen); the sheet is portalled to <body>, inside an `.lx` so the Log's styles still apply.
  return sheet && typeof document !== "undefined"
    ? createPortal(<div className="lx lx-sheet-host">{drawer}</div>, document.body)
    : drawer;
}

/** Below 720px (the Log's phone breakpoint) the drawer is a sheet over the whole console. */
function useSheet(): boolean {
  const [sheet, setSheet] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const q = window.matchMedia("(max-width: 719px)");
    const on = () => setSheet(q.matches);
    on();
    q.addEventListener?.("change", on);
    return () => q.removeEventListener?.("change", on);
  }, []);
  return sheet;
}

function AroundLines({ context, labels, anchor }: { context: LogContext; labels: Record<string, string>; anchor: string }) {
  const rows = flatten(context.items, { byRun: false, open: new Set(), labels })
    .filter((r): r is Extract<DisplayRow, { kind: "line" }> => r.kind === "line");
  return (
    <div className="lx-around-list" data-testid="lx-around-list" role="listbox" aria-label="Lines around this one">
      {rows.map((row) => (
        <div key={row.key} className="lx-around-row" data-anchor={row.item.id === anchor || undefined}>
          <LogLine row={{ ...row, top: 0 }} labels={labels} selected={row.item.id === anchor} fresh={false}
                   onOpen={() => {}} onFold={() => {}} />
        </div>
      ))}
    </div>
  );
}
