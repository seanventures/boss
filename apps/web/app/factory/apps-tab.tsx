"use client";

/**
 * Factory › Apps (factory spec §4.6): one row per app — who built it, its last state, its
 * preview, where it is deployed, how many builds. Pressing a row lists its builds, newest first;
 * `↗` opens the newest build's cockpit.
 *
 * The rows come from `GET /v1/factory/apps` (the server groups runs into apps). Until a server
 * answers that, they are grouped here from the build list with the same rule
 * (app-grouping.ts), and only what that list holds is shown: no faces, no deploys guessed.
 */
import { CaretDown, CaretRight } from "@phosphor-icons/react";
import React, { useEffect, useMemo, useState } from "react";

import type { CodeTaskRun, PreviewRec } from "@boss/api-client";
import { arr, num, obj, opt, str } from "../../lib/factory-floor";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AgFaceStack } from "../agents/agent-face";
import { ago, useNow } from "../agents/live-util";
import { groupRunsIntoApps } from "../app-grouping";
import { appIdentity } from "../app-identity";
import { previewWebHref } from "../../lib/browser-links";

export type AppBuild = { runId: string; attempt: number; word: string; at?: string };
export type AppRow = {
  key: string; app: string; builders: string[]; word: string;
  preview: { status: string; url?: string } | null;
  deploys: { env: string; ok: boolean }[];
  builds: AppBuild[];
  rank: number;
};

/** The fallback's words for a run's status: only what the run row says. */
function runWord(r: CodeTaskRun): string {
  const s = String(r.status ?? "");
  if (s === "running" || s === "queued" || s === "repairing") return "Building";
  if (s === "completed") return r.pull_request_url ? "PR open" : "Done · no PR";
  if (s === "failed") return "Failed";
  if (s === "cancelled") return "Stopped";
  if (s === "blocked") return "Blocked";
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
}

function rankOf(word: string): number {
  if (word === "Blocked" || word.startsWith("Needs")) return 0;
  if (word === "Building") return 1;
  return 2;
}

export function appsFromRuns(runs: CodeTaskRun[], previews: PreviewRec[]): AppRow[] {
  return groupRunsIntoApps(runs).map((members) => {
    const newest = members[0];
    const live = previews.find((p) => members.some((m) => m.id === p.run_id)
      && (p.status === "running" || p.status === "starting" || p.status === "verified"));
    const builds = members.map((m, i) => ({
      runId: m.id, attempt: Number(((m.finalize_ctx ?? {}) as Record<string, unknown>).attempt ?? members.length - i) || members.length - i,
      word: runWord(m), at: (m as unknown as { created_at?: string }).created_at,
    }));
    const word = runWord(newest);
    return {
      key: newest.id, app: appIdentity(newest).name, builders: [], word,
      preview: live ? { status: live.status === "starting" ? "starting" : "running", url: previewWebHref(live.id) } : null,
      deploys: [], builds, rank: rankOf(word),
    };
  }).sort((a, b) => a.rank - b.rank);
}

export function parseApps(w: unknown): AppRow[] {
  return arr(obj(w).apps).map((x) => {
    const o = obj(x);
    const p = o.preview ? obj(o.preview) : null;
    const word = str(o.word);
    return {
      key: str(o.newest_run_id ?? o.key ?? o.app), app: str(o.app), builders: arr(o.builders).map(String), word,
      preview: p && opt(p.status) ? { status: str(p.status), url: opt(p.url) } : null,
      deploys: arr(o.deploys).map((d) => { const y = obj(d); return { env: str(y.env), ok: y.ok === true }; }),
      builds: arr(o.builds).map((b) => {
        const y = obj(b);
        return { runId: str(y.run_id), attempt: num(y.attempt, 1), word: str(y.word), at: opt(y.at) };
      }),
      rank: rankOf(word),
    };
  });
}

/** `/v1/factory/apps` once per open, or null when no server answers it (then the fallback). */
function useServerApps(projectId: string): AppRow[] | null {
  const [apps, setApps] = useState<AppRow[] | null>(null);
  useEffect(() => {
    let stop = false;
    void (async () => {
      try {
        const r = await fetch(`/v1/factory/apps?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" });
        if (!r.ok || stop) return;
        setApps(parseApps(await r.json()));
      } catch { /* the fallback stands */ }
    })();
    return () => { stop = true; };
  }, [projectId]);
  return apps;
}

export type AppsTabProps = {
  projectId: string;
  runs: CodeTaskRun[];
  previews: PreviewRec[];
  query?: string;
  onOpen: (runId: string, from: HTMLElement | null) => void;
  onNew: () => void;
};

export function AppsTab({ projectId, runs, previews, query = "", onOpen, onNew }: AppsTabProps) {
  useIdentityVersion();
  const now = useNow(30_000);
  const served = useServerApps(projectId);
  const fallback = useMemo(() => appsFromRuns(runs, previews), [runs, previews]);
  const apps = (served ?? fallback).filter((a) => !query.trim() || a.app.toLowerCase().includes(query.trim().toLowerCase()));
  const [open, setOpen] = useState<string | null>(null);

  if (!apps.length) {
    return (
      <div className="fx-apps" data-testid="fx-apps">
        <div className="ag-empty fx-empty" data-testid="fx-no-apps">
          <span>No apps yet</span>
          <button type="button" className="ag-btn ag-btn-primary" onClick={onNew}>+ New build</button>
        </div>
      </div>
    );
  }
  return (
    <div className="fx-apps" data-testid="fx-apps">
      <ul className="fx-rows" aria-label="Apps">
        {apps.map((a) => {
          const expanded = open === a.key;
          const newest = a.builds[0]?.runId ?? a.key;
          return (
            <li key={a.key} className="fx-app" data-testid={`fx-app-${a.key}`}>
              <div className="fx-row fx-app-row">
                <span className="fx-row-name">
                  <button type="button" className="fx-row-open ag-trunc" aria-expanded={expanded}
                          onClick={() => setOpen(expanded ? null : a.key)}>
                    {expanded ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
                    <span data-user-text>{a.app}</span>
                  </button>
                </span>
                <span className="fx-app-faces">
                  {a.builders.length ? <AgFaceStack roleKeys={a.builders} size={20} max={4} /> : null}
                </span>
                <span className="fx-row-now"><span className="fx-word">{a.word}</span></span>
                <span className="fx-app-preview">
                  {a.preview?.url && a.preview.status === "running" ? (
                    <a className="ag-link" href={a.preview.url} target="_blank" rel="noreferrer">Preview ↗</a>
                  ) : a.preview?.status === "starting" ? <span>Starting…</span> : null}
                </span>
                <span className="fx-app-deploys">
                  {a.deploys.map((d) => <span key={d.env} title={d.ok ? "Deployed" : "Not deployed"}>{d.env} {d.ok ? "✓" : "—"}</span>)}
                </span>
                <span className="fx-app-count" title="Builds of this app">{a.builds.length} build{a.builds.length === 1 ? "" : "s"}</span>
                <button type="button" className="ag-icon-btn fx-row-go" aria-label={`Open ${a.app}`} title="Open the newest build"
                        onClick={(e) => onOpen(newest, e.currentTarget.closest("li"))}>↗</button>
              </div>
              {expanded ? (
                <ul className="fx-app-builds" aria-label={`Builds of ${a.app}`}>
                  {a.builds.map((b) => (
                    <li key={b.runId}>
                      <button type="button" className="fx-app-build" data-testid={`fx-app-build-${b.runId}`}
                              onClick={(e) => onOpen(b.runId, e.currentTarget)}>
                        <span>Attempt {b.attempt}</span>
                        <span aria-hidden> · </span><span className="fx-word">{b.word}</span>
                        {b.at ? <><span aria-hidden> · </span><span>{ago(b.at, now)}</span></> : null}
                        <span aria-hidden> ↗</span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
