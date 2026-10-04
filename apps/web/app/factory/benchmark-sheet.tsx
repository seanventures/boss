"use client";

/**
 * `+ New build ▾ › From a benchmark` (factory spec §4.1, walk D12): the pinned example apps as
 * Factory rows — a name, `Build`, and `Open ↗` once one exists. The words follow the Factory's
 * copy rules (labels of three words or fewer; the long sentences live in tooltips), where the
 * old catalogue spoke in paragraphs about "governed projects".
 */
import type { QaBenchmarkApp } from "@boss/api-client";
import React, { useState } from "react";

import { previewWebHref } from "../../lib/browser-links";
import { callAction } from "../../lib/call-action";
import { startQaBenchmarkAppAction } from "../actions";
import { showToast } from "../client-forms";

export type Launched = Record<string, { run_id?: string; error?: string; kind?: "meeting" | "code" | "existing" }>;

export function BenchmarkSheetBody({ benchmarks, builtIds, projectId, actor, owner, repo, launched, onLaunched, onOpen }: {
  benchmarks: QaBenchmarkApp[];
  builtIds: Set<string>;
  projectId: string; actor: string; owner?: string; repo?: string;
  launched: Launched;
  onLaunched: (key: string, runId?: string, error?: string, kind?: "meeting" | "code" | "existing") => void;
  onOpen: (runId: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  if (!benchmarks.length) return <span className="fx-muted" data-testid="fx-no-benchmarks">No benchmarks</span>;

  async function launch(b: QaBenchmarkApp, again: boolean) {
    if (busy) return;
    setBusy(b.id);
    const data = new FormData();
    data.set("project_id", projectId);
    data.set("actor", actor);
    data.set("benchmark_id", b.id);
    data.set("owner", owner ?? "");
    data.set("repo", repo ?? "");
    if (again) data.set("force", "true");
    let out: { run_id?: string; kind?: "meeting" | "code" | "existing"; error?: string } | undefined;
    try {
      out = await callAction(startQaBenchmarkAppAction)(data);
      if (out?.run_id) showToast("success", out.kind === "meeting" ? "Plan started" : "Build started");
    } finally {
      setBusy(null);
      onLaunched(b.id, out?.run_id, out?.run_id ? undefined : (out?.error ?? "No build came back"), out?.kind);
    }
  }

  return (
    <ul className="fx-bench" data-testid="fx-bench-list" aria-label="Benchmarks">
      {benchmarks.map((b) => {
        const got = launched[b.id];
        const builtRun = got?.run_id ?? b.built?.run_id ?? null;
        const built = !!builtRun || builtIds.has(b.id);
        const preview = b.built?.preview_id ? previewWebHref(b.built.preview_id) : null;
        const about = [b.tagline, b.summary, b.focus.length ? `Exercises ${b.focus.join(", ")}` : ""].filter(Boolean).join(" · ");
        return (
          <li key={b.id} className="fx-bench-row" data-testid={`fx-bench-${b.id}`}>
            <span className="fx-bench-name ag-trunc" title={about} data-user-text>{b.name}</span>
            <span className="fx-bench-tag ag-trunc" title={about} data-user-text>{b.tagline}</span>
            <span className="fx-spacer" />
            {got?.error ? <span className="fx-err" role="alert" title={got.error}>Couldn&apos;t start</span> : null}
            {built ? (
              preview
                ? <a className="ag-btn" href={preview} target="_blank" rel="noreferrer" title="Open the running app">Open ↗</a>
                : builtRun && got?.kind !== "meeting"
                  ? <button type="button" className="ag-btn" onClick={() => onOpen(builtRun)} title="Open its build">Open ↗</button>
                  : null
            ) : null}
            <button type="button" className={built ? "ag-btn" : "ag-btn ag-btn-primary"} disabled={busy !== null}
                    aria-busy={busy === b.id || undefined} data-testid={`fx-bench-build-${b.id}`}
                    title={built ? "A second copy. It costs a full build." : "Plan it with the panel, then build it"}
                    onClick={() => void launch(b, built)}>
              {busy === b.id ? "Starting…" : built ? "Build again" : "Build"}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
