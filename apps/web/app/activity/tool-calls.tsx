"use client";

/**
 * Activity › Tool calls (BOSSAPP-819): the append-only tool-call audit, one row per call BOSS
 * decided. Filter chips (decision, outcome, screen verdict; a cell's value with +), a keyset
 * "Older" page, Export as NDJSON, and a drawer for one call: its redacted arguments and digest,
 * the approval it carried, the injection screen's verdict and the policy that matched.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DownloadSimple, Plus, ShieldWarning, X } from "@phosphor-icons/react";

import { clock, dayLabel, utcTitle } from "../../lib/log-time";
import {
  DECISION_WORD, fetchToolCalls, toneOf, toolCallExportHref,
  type ToolCall, type ToolCallFilterKey, type ToolCallFilters,
} from "../../lib/tool-calls";
import { consoleHref } from "../url-intent";

const FILTER_LABEL: Record<ToolCallFilterKey, string> = {
  decision: "Decision", outcome: "Outcome", screen: "Screen", tool: "Tool", server_id: "Server", run_id: "Run",
};

/** The quick chips: one click filters, a second clears. */
const CHIPS: ReadonlyArray<{ key: ToolCallFilterKey; value: string; label: string; tip: string }> = [
  { key: "decision", value: "allow", label: "Allowed", tip: "Calls policy let run" },
  { key: "decision", value: "ask", label: "Asked", tip: "Calls that needed a person" },
  { key: "outcome", value: "refused", label: "Refused", tip: "Calls that did not run" },
  { key: "screen", value: "suspicious", label: "Suspicious", tip: "Results the injection screen flagged" },
];

function took(ms: number): string {
  if (!ms) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function ToolCalls({ projectId }: { projectId: string }) {
  const [filters, setFilters] = useState<ToolCallFilters>({});
  const [items, setItems] = useState<ToolCall[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "idle" | "failed">("loading");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<ToolCall | null>(null);
  const abort = useRef<AbortController | null>(null);
  const key = useMemo(() => JSON.stringify(filters), [filters]);

  const load = useCallback(async (cursor: string | null) => {
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    setState("loading");
    const got = await fetchToolCalls(projectId, filters, cursor, ctl.signal);
    if (ctl.signal.aborted) return;
    if (!got.ok) { setState("failed"); setError(got.error); return; }
    setItems((prev) => (cursor ? [...prev, ...got.data.items] : got.data.items));
    setNext(got.data.next);
    setState("idle");
  }, [projectId, filters]);

  useEffect(() => {
    setOpen(null);
    void load(null);
    return () => abort.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, key]);

  const toggle = (k: ToolCallFilterKey, value: string) =>
    setFilters((f) => {
      const copy = { ...f };
      if (copy[k] === value) delete copy[k]; else copy[k] = value;
      return copy;
    });
  const active = Object.entries(filters).filter(([, v]) => v) as Array<[ToolCallFilterKey, string]>;

  return (
    <div className="tc" data-testid="tool-calls">
      <header className="tc-head">
        <h2 className="tc-title">Tool calls</h2>
        <span className="tc-count" data-testid="tc-count">
          {items.length}{next ? "+" : ""} {items.length === 1 && !next ? "call" : "calls"}
        </span>
        <span className="tc-spacer" />
        <a className="ag-btn" href={toolCallExportHref(projectId, filters)} download
           title="This filter as NDJSON" data-testid="tc-export">
          <DownloadSimple size={14} aria-hidden /> Export
        </a>
      </header>
      <div className="tc-chips" role="group" aria-label="Filters" data-testid="tc-chips">
        {CHIPS.map((c) => (
          <button key={`${c.key}:${c.value}`} type="button" className="tc-chip" title={c.tip}
                  aria-pressed={filters[c.key] === c.value} data-testid={`tc-chip-${c.value}`}
                  onClick={() => toggle(c.key, c.value)}>{c.label}</button>
        ))}
        {active.filter(([k, v]) => !CHIPS.some((c) => c.key === k && c.value === v)).map(([k, v]) => (
          <button key={k} type="button" className="tc-chip" aria-pressed title="Remove this filter"
                  data-testid={`tc-token-${k}`} onClick={() => toggle(k, v)}>
            {FILTER_LABEL[k]}: <span data-user-text>{v}</span> <X size={10} weight="bold" aria-hidden />
          </button>
        ))}
      </div>
      {state === "failed" ? (
        <p className="tc-empty" role="alert" data-testid="tc-error">{error || "Can't reach BOSS"}</p>
      ) : null}
      {state !== "failed" && !items.length ? (
        <p className="tc-empty" data-testid="tc-empty">
          {state === "loading" ? "Loading…" : active.length ? "No calls match these filters" : "No tool calls yet"}
        </p>
      ) : null}
      {items.length ? (
        <div className="tc-table-wrap">
          <table className="tc-table" data-testid="tc-table">
            <thead>
              <tr><th>When</th><th>Tool</th><th>Agent</th><th>Decision</th><th>Outcome</th><th>Screen</th><th className="tc-num">Took</th></tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id} data-tone={toneOf(c)} aria-selected={open?.id === c.id} tabIndex={0}
                    data-testid="tc-row" onClick={() => setOpen(c)}
                    onKeyDown={(e) => { if (e.key === "Enter") setOpen(c); }}>
                  <td title={utcTitle(c.recordedAt)}>{dayLabel(c.recordedAt)} {clock(c.recordedAt)}</td>
                  <td className="tc-tool" data-user-text>
                    {c.tool}
                    <button type="button" className="tc-plus" aria-label={`Only ${c.tool}`} title="Filter for this tool"
                            onClick={(e) => { e.stopPropagation(); toggle("tool", c.tool); }}>
                      <Plus size={9} weight="bold" aria-hidden />
                    </button>
                  </td>
                  <td data-user-text>{c.agent ?? ""}</td>
                  <td>{DECISION_WORD[c.decision] ?? c.decision}</td>
                  <td><span className="tc-pill" data-tone={toneOf(c)}>{c.outcome}</span></td>
                  <td>{c.screen?.verdict === "suspicious"
                    ? <span className="tc-pill" data-tone="warn"><ShieldWarning size={11} aria-hidden /> suspicious</span>
                    : c.screen?.verdict ?? ""}</td>
                  <td className="tc-num">{took(c.latencyMs)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {next ? (
        <button type="button" className="ag-btn tc-more" data-testid="tc-more" disabled={state === "loading"}
                onClick={() => void load(next)}>Older</button>
      ) : null}
      {open ? <ToolCallDrawer call={open} onClose={() => setOpen(null)}
                              onFilter={(k, v) => { toggle(k, v); setOpen(null); }} /> : null}
    </div>
  );
}

function ToolCallDrawer({ call, onClose, onFilter }: {
  call: ToolCall; onClose: () => void; onFilter: (k: ToolCallFilterKey, v: string) => void;
}) {
  const search = typeof window === "undefined" ? "" : window.location.search;
  const facts: Array<{ label: string; value: string; filter?: ToolCallFilterKey }> = [
    { label: "When", value: `${dayLabel(call.recordedAt)} ${clock(call.recordedAt)}` },
    { label: "Decision", value: DECISION_WORD[call.decision] ?? call.decision, filter: "decision" },
    { label: "Outcome", value: call.outcome, filter: "outcome" },
  ];
  if (call.agent) facts.push({ label: "Agent", value: call.agent });
  if (call.principal) facts.push({ label: "For", value: call.principal });
  if (call.serverId) facts.push({ label: "Server", value: call.serverId, filter: "server_id" });
  if (call.runId) facts.push({ label: "Run", value: call.runId, filter: "run_id" });
  if (call.latencyMs) facts.push({ label: "Took", value: took(call.latencyMs) });
  const rawFor: Record<string, string> = { Decision: call.decision };
  return (
    <aside className="lx-drawer tc-drawer" role="dialog" aria-label="Tool call" tabIndex={-1}
           data-testid="tc-drawer" onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <header className="lx-drawer-head">
        <h2 className="lx-drawer-title" data-user-text>{call.tool}</h2>
        <span className="lx-spacer" />
        <button type="button" className="ag-link" aria-label="Close" data-testid="tc-drawer-close" onClick={onClose}>
          <X size={13} weight="bold" aria-hidden />
        </button>
      </header>
      <div className="lx-drawer-body">
        <dl className="lx-facts">
          {facts.map((f) => (
            <div key={f.label} className="lx-fact">
              <dt>{f.label}</dt>
              <dd>
                <span className="lx-fact-value" data-user-text>{f.value}</span>
                {f.filter ? (
                  <span className="lx-fact-acts">
                    <button type="button" aria-label={`Only ${f.label}`} title="Filter for this"
                            onClick={() => onFilter(f.filter!, rawFor[f.label] ?? f.value)}>
                      <Plus size={10} weight="bold" aria-hidden />
                    </button>
                  </span>
                ) : null}
              </dd>
            </div>
          ))}
        </dl>
        {call.decisionId ? (
          <nav className="lx-links" aria-label="Open">
            <a className="ag-link" data-testid="tc-decision-link" title="The approval this call carried"
               href={consoleHref(search, { area: "decisions", decision: call.decisionId })}>Decision ↗</a>
          </nav>
        ) : null}
        <section className="tc-sec" data-testid="tc-screen">
          <h3>Injection screen</h3>
          {call.screen ? (
            <p>
              <span className="tc-pill" data-tone={call.screen.verdict === "suspicious" ? "warn" : "ok"}>
                {call.screen.verdict ?? "unknown"}
              </span>
              {call.screen.mode ? <span className="tc-muted"> · {call.screen.mode}</span> : null}
              {call.screen.rules?.length ? <span className="tc-muted"> · {call.screen.rules.join(", ")}</span> : null}
            </p>
          ) : <p className="tc-muted">No result was screened (the call did not deliver one)</p>}
        </section>
        <section className="tc-sec" data-testid="tc-policy">
          <h3>Policy</h3>
          {call.matchedPolicyIds.length
            ? <p data-user-text>{call.matchedPolicyIds.join(", ")}</p>
            : <p className="tc-muted">No rule matched</p>}
          {call.explanation ? <p className="tc-why" data-user-text data-testid="tc-explanation">{call.explanation}</p> : null}
        </section>
        <section className="tc-sec" data-testid="tc-args">
          <h3>Arguments <span className="tc-muted">(redacted)</span></h3>
          <pre className="lx-data" data-user-text>{JSON.stringify(call.argsRedacted, null, 2)}</pre>
          {call.argsSha256 ? <p className="tc-sha" title="sha256 of the exact call" data-testid="tc-sha">sha256 {call.argsSha256}</p> : null}
        </section>
      </div>
    </aside>
  );
}

/** Activity's two views: the log of everything, and the tool-call audit. `?activity_tab=tools`. */
export function ActivityTabs({ log, projectId }: { log: React.ReactNode; projectId: string }) {
  const [tab, setTab] = useState<"log" | "tools">("log");
  useEffect(() => {
    try {
      if (new URLSearchParams(window.location.search).get("activity_tab") === "tools") setTab("tools");
    } catch { /* no URL: the log */ }
  }, []);
  const choose = (next: "log" | "tools") => {
    setTab(next);
    try {
      const params = new URLSearchParams(window.location.search);
      if (next === "tools") params.set("activity_tab", "tools"); else params.delete("activity_tab");
      const qs = params.toString();
      window.history.replaceState(null, "", `${window.location.pathname}${qs ? `?${qs}` : ""}`);
    } catch { /* a sandboxed frame keeps the in-memory tab */ }
  };
  return (
    <>
      <div className="tc-tabs" role="tablist" aria-label="Activity views" data-testid="activity-tabs">
        <button type="button" role="tab" aria-selected={tab === "log"} className="tc-tab"
                data-testid="activity-tab-log" onClick={() => choose("log")}>Log</button>
        <button type="button" role="tab" aria-selected={tab === "tools"} className="tc-tab"
                data-testid="activity-tab-tools" title="Every tool call BOSS decided"
                onClick={() => choose("tools")}>Tool calls</button>
      </div>
      {tab === "tools" ? <ToolCalls projectId={projectId} /> : log}
    </>
  );
}
