"use client";

/**
 * The group drawer (spec §4.10, `model=<group>:<key>`): one model / stage / agent / runner over
 * the window — its stats from the Models view, then RECENT CALLS straight from the ledger
 * (GET /v1/llm-calls, 20 at a time, `Show older` pages by offset). Each call: time · face ·
 * stage · model · in/out · ms · ok/✗ · retries · `Build ↗`. `Logs ↗` opens the Activity log on
 * `llm_call`.
 */
import React, { useCallback, useEffect, useState } from "react";

import { buildHref, logsHref } from "../../lib/infra-view";
import { compact, llmCallsUrl, ms, parseLlmCall, pct, usd, type Group, type LlmCall, type ModelGroupRow } from "../../lib/obs-view";
import { RunFace, nameOf } from "../agents/run-face";
import { plainClick } from "../agents/live-util";
import { Drawer, DrawerSection, Skeleton } from "../infra/kit";
import { goInfra } from "./obs-util";
import { obsRead } from "./obs-write";

export const PAGE = 20;

/** `model=<group>:<key>` — the key may itself hold a colon (`guest:codex`). */
export function readModelParam(v: string | null): { group: Group; key: string } | null {
  if (!v) return null;
  const i = v.indexOf(":");
  if (i <= 0) return null;
  const g = v.slice(0, i);
  if (!["model", "stage", "role", "engine"].includes(g)) return null;
  return { group: g as Group, key: v.slice(i + 1) };
}

export function groupLabel(row: Pick<ModelGroupRow, "label" | "roleKey" | "guest">, group: Group): string {
  if (group === "role" && row.roleKey) return nameOf(row.roleKey).name;
  return row.label;
}

export function spendWord(spendMicros: number, included: boolean, estimated: boolean): { text: string; tip?: string } {
  if (spendMicros > 0) return { text: `${usd(spendMicros)}${estimated ? " est." : ""}`, tip: estimated ? "Some prices were estimated, not billed" : undefined };
  if (included) return { text: "Included", tip: "Ran on a subscription. No metered spend." };
  return { text: "—", tip: "No metered spend" };
}

export function GroupDrawer({ projectId, group, keyName, row, since, onClose }: {
  projectId: string; group: Group; keyName: string; row: ModelGroupRow | null; since: string | null; onClose: () => void;
}) {
  const [calls, setCalls] = useState<LlmCall[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const load = useCallback(async (off: number) => {
    setLoading(true);
    const body = await obsRead<{ items?: unknown[]; total?: number }>(llmCallsUrl(projectId, {
      since: since ?? undefined, [group]: keyName, offset: off, limit: PAGE,
    }));
    setLoading(false);
    const items = (body?.items ?? []).map(parseLlmCall);
    setCalls((prev) => (off === 0 ? items : [...(prev ?? []), ...items]));
    setTotal(typeof body?.total === "number" ? body.total : items.length);
    setOffset(off);
  }, [projectId, group, keyName, since]);
  useEffect(() => { void load(0); }, [load]);

  const name = row ? groupLabel(row, group) : keyName;
  const spend = row ? spendWord(row.spendMicros, row.included, row.estimated) : null;
  const head = (
    <span className="obs-dhead">
      {group === "role" && row?.roleKey ? <RunFace roleKey={row.roleKey} size={24} /> : null}
      <span className="obs-dhead-name" data-copy="data" data-user-text>{name}</span>
      {row?.guest ? <span className="obs-chip" title="A guest runner">guest</span> : null}
    </span>
  );
  return (
    <Drawer label={`${name} calls`} testId="obs-group-drawer" width={560} onClose={onClose} head={head}>
      {row ? (
        <div className="obs-facts" data-testid="obs-group-stats">
          <span className="obs-fact"><span className="obs-fact-k">Calls</span><span className="obs-fact-v inf-num">{row.calls.toLocaleString("en-US")}</span></span>
          <span className="obs-fact"><span className="obs-fact-k">Tokens</span><span className="obs-fact-v inf-num">{compact(row.tokensIn)} in · {compact(row.tokensOut)} out</span></span>
          <span className="obs-fact" title={spend?.tip}><span className="obs-fact-k">Spend</span><span className="obs-fact-v inf-num">{spend?.text}</span></span>
          <span className="obs-fact"><span className="obs-fact-k">Avg</span><span className="obs-fact-v inf-num">{ms(row.avgMs)}</span></span>
          <span className="obs-fact"><span className="obs-fact-k">p95</span><span className="obs-fact-v inf-num">{ms(row.p95Ms)}</span></span>
          <span className="obs-fact" title="Calls that failed after retries, of all calls">
            <span className="obs-fact-k">Errors</span><span className="obs-fact-v inf-num">{row.calls ? pct(row.failed / row.calls) : "—"} · {row.failed}</span>
          </span>
          <span className="obs-fact"><span className="obs-fact-k">Retries</span><span className="obs-fact-v inf-num">{row.retries}</span></span>
        </div>
      ) : null}
      <DrawerSection label="RECENT CALLS" testId="obs-recent-calls" actions={
        <a className="inf-link" href={logsHref("llm_call", null, since)} data-testid="obs-group-logs"
           onClick={(e) => { if (plainClick(e)) { e.preventDefault(); goInfra(logsHref("llm_call", null, since)); } }}>Logs ↗</a>
      }>
        {calls === null ? <Skeleton testId="obs-recent-loading" /> : calls.length === 0 ? <span className="inf-empty">No calls in this window</span> : (
          <ul className="obs-list obs-calls">
            {calls.map((c) => (
              <li key={c.id} className="obs-call" data-testid="obs-call-row" data-ok={c.ok ? "1" : "0"}>
                <span className="inf-num inf-muted" title={new Date(c.at).toLocaleString()}>
                  {new Date(c.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}
                </span>
                <RunFace roleKey={c.roleKey} variant={c.roleKey ? "agent" : "boss"} size={20} />
                <span className="inf-trunc" data-copy="data">{c.stage ?? "—"}</span>
                <span className="inf-trunc inf-mono inf-faint" data-copy="data">{c.model ?? "—"}</span>
                <span className="inf-num" title="Tokens in / out">{compact(c.tokensIn)}/{compact(c.tokensOut)}</span>
                <span className="inf-num">{ms(c.latencyMs)}</span>
                <span className={c.ok ? "obs-ok" : "obs-bad"} title={c.ok ? "OK" : c.error ?? "Failed"} aria-label={c.ok ? "OK" : "Failed"}>{c.ok ? "ok" : "✗"}</span>
                <span className="inf-num inf-faint" title="Retries">{c.retries ? `↻${c.retries}` : ""}</span>
                {c.runId ? (
                  <a className="inf-link" href={buildHref(c.runId)} title="Open the build that made this"
                     onClick={(e) => { if (plainClick(e)) { e.preventDefault(); goInfra(buildHref(c.runId!)); } }}>Build ↗</a>
                ) : <span />}
              </li>
            ))}
          </ul>
        )}
        {calls && calls.length < total ? (
          <button type="button" className="ag-btn obs-older" data-testid="obs-calls-older" disabled={loading}
                  onClick={() => void load(offset + PAGE)}>Show older</button>
        ) : null}
      </DrawerSection>
    </Drawer>
  );
}
