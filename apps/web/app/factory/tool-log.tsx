"use client";

/**
 * Each tool call in one line (build page): `✓ Bash  pytest -q tests/test_auth.py  1.5s  exit 1`.
 * The command or the path, how long it took, the exit code or the error's first line. A failed
 * call opens by itself and shows its error output (redacted on the server); consecutive reads
 * of one kind fold into `Read ×4`. `All` / `Failures` choose what the list shows.
 */
import { CaretDown, CaretRight, Check, X } from "@phosphor-icons/react";
import React, { useEffect, useRef, useState } from "react";

import { groupTools, msWord, outcomeWord, parseToolLog, type ToolCall, type ToolLog } from "../../lib/build-detail";

/** GET /v1/code-tasks/{id}/tool-log, read again when the build's tool-call count changes. */
export function useToolLog(projectId: string, runId: string, version: string): { log: ToolLog | null; failed: boolean } {
  const [log, setLog] = useState<ToolLog | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let gone = false;
    void (async () => {
      try {
        const r = await fetch(`/v1/code-tasks/${encodeURIComponent(runId)}/tool-log?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (!r.ok) { if (!gone) setFailed(true); return; }
        const got = parseToolLog(await r.json());
        if (!gone) { setLog(got); setFailed(false); }
      } catch {
        if (!gone) setFailed(true);
      }
    })();
    return () => { gone = true; };
  }, [projectId, runId, version]);
  return { log, failed };
}

function Mark({ ok }: { ok: boolean | null }) {
  if (ok === null) return <span className="fx-tl-mark" data-ok="running" aria-label="Running">…</span>;
  return ok
    ? <span className="fx-tl-mark" data-ok="true" aria-label="Worked"><Check size={10} weight="bold" aria-hidden /></span>
    : <span className="fx-tl-mark" data-ok="false" aria-label="Failed"><X size={10} weight="bold" aria-hidden /></span>;
}

function CallLine({ call, open, onToggle }: { call: ToolCall; open: boolean; onToggle: () => void }) {
  const body = call.ok === false ? call.error : null;
  const outcome = outcomeWord(call);
  return (
    <li className="fx-tl-item" data-ok={call.ok === null ? "running" : String(call.ok)} data-call={call.i}
        data-testid={`fx-call-${call.i}`}>
      <button type="button" className="fx-tl-line" aria-expanded={body ? open : undefined} disabled={!body}
              onClick={body ? onToggle : undefined} title={call.arg ?? undefined}>
        <Mark ok={call.ok} />
        <span className="fx-tl-tool">{call.tool}</span>
        <span className="fx-tl-arg fx-mono" data-user-text>{call.arg ?? ""}</span>
        <span className="fx-tl-ms">{msWord(call.ms)}</span>
        {outcome ? <span className="fx-tl-out" data-user-text>{outcome}</span> : null}
        {body ? (open ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />)
          : <span className="fx-tl-chev" aria-hidden />}
      </button>
      {body && open ? (
        <pre className="fx-tl-err fx-mono" data-testid={`fx-call-err-${call.i}`} data-user-text>{body}{call.clipped ? "\n…" : ""}</pre>
      ) : null}
    </li>
  );
}

function ReadsLine({ tool, calls }: { tool: string; calls: ToolCall[] }) {
  const [open, setOpen] = useState(false);
  const ms = calls.reduce((n, c) => n + (c.ms ?? 0), 0);
  const names = calls.map((c) => c.arg ?? "").filter(Boolean);
  return (
    <li className="fx-tl-item" data-ok="true" data-group={tool} data-testid={`fx-reads-${calls[0].i}`}>
      <button type="button" className="fx-tl-line" aria-expanded={open} onClick={() => setOpen((v) => !v)}
              title={names.join("\n")}>
        <Mark ok />
        <span className="fx-tl-tool">{tool} ×{calls.length}</span>
        <span className="fx-tl-arg fx-mono" data-user-text>{names.slice(0, 2).join(", ")}{names.length > 2 ? ` +${names.length - 2}` : ""}</span>
        <span className="fx-tl-ms">{msWord(ms || null)}</span>
        {open ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
      </button>
      {open ? (
        <ul className="fx-tl-sub">
          {calls.map((c) => (
            <li key={c.i} className="fx-tl-item" data-ok="true" data-call={c.i}>
              <span className="fx-tl-line" title={c.arg ?? undefined}>
                <Mark ok />
                <span className="fx-tl-tool">{c.tool}</span>
                <span className="fx-tl-arg fx-mono" data-user-text>{c.arg ?? ""}</span>
                <span className="fx-tl-ms">{msWord(c.ms)}</span>
                <span className="fx-tl-chev" aria-hidden />
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export type ToolListProps = {
  calls: ToolCall[];
  /** A call search jumped to: shown, opened and scrolled to. */
  focus?: number | null;
  testId?: string;
};

/** One act's tool calls. Failures open by default; `Failures` shows only them. */
export function ToolList({ calls, focus = null, testId }: ToolListProps) {
  const failures = calls.filter((c) => c.ok === false);
  const [only, setOnly] = useState(false);
  const [closed, setClosed] = useState<Set<number>>(() => new Set());
  const [opened, setOpened] = useState<Set<number>>(() => new Set());
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focus === null) return;
    setOpened((s) => new Set(s).add(focus));
    setClosed((s) => { const n = new Set(s); n.delete(focus); return n; });
    window.requestAnimationFrame(() => {
      root.current?.querySelector(`[data-call="${focus}"]`)?.scrollIntoView({ block: "center" });
    });
  }, [focus]);
  const shown = only ? failures : calls;
  const isOpen = (c: ToolCall) => (c.ok === false ? !closed.has(c.i) : opened.has(c.i));
  const toggle = (c: ToolCall) => {
    if (c.ok === false) setClosed((s) => { const n = new Set(s); if (n.has(c.i)) n.delete(c.i); else n.add(c.i); return n; });
    else setOpened((s) => { const n = new Set(s); if (n.has(c.i)) n.delete(c.i); else n.add(c.i); return n; });
  };
  return (
    <div ref={root} className="fx-tl" data-testid={testId}>
      {failures.length ? (
        <span className="fx-tl-filter" role="group" aria-label="Show">
          <button type="button" className="fx-chip" aria-pressed={!only} onClick={() => setOnly(false)}
                  data-testid="fx-tl-all">All {calls.length}</button>
          <button type="button" className="fx-chip" aria-pressed={only} onClick={() => setOnly(true)}
                  data-testid="fx-tl-failures" title="Only the calls that failed">Failures {failures.length}</button>
        </span>
      ) : null}
      <ul className="fx-tl-list">
        {(only ? shown.map((call) => ({ kind: "one" as const, call })) : groupTools(shown)).map((g) => g.kind === "one"
          ? <CallLine key={g.call.i} call={g.call} open={isOpen(g.call) || focus === g.call.i} onToggle={() => toggle(g.call)} />
          : g.calls.some((c) => c.i === focus)
            ? g.calls.map((c) => <CallLine key={c.i} call={c} open={false} onToggle={() => toggle(c)} />)
            : <ReadsLine key={g.calls[0].i} tool={g.tool} calls={g.calls} />)}
      </ul>
    </div>
  );
}
