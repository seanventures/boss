"use client";

/**
 * The Logs drawer (factory spec §4.5.8): the only place raw text appears. Tabs Build · Preview ·
 * Deploy. Build is this run's activity log, oldest first, filterable by agent; Preview is the
 * existing startup stream (same-origin SSE); Deploy is the deployment's live step list.
 */
import React, { useEffect, useId, useRef, useState } from "react";

import { LiveDeployment } from "../deploy-ui";
import { fetchBuildLog, type LogLine } from "./cockpit-actions";
import { clockOf, whoName } from "./cockpit-util";

export type LogTab = "build" | "preview" | "deploy";

function useBuildLog(projectId: string, runId: string, on: boolean): { lines: LogLine[] | null; failed: boolean } {
  const [lines, setLines] = useState<LogLine[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!on) return undefined;
    let alive = true;
    void fetchBuildLog(projectId, runId).then((got) => {
      if (!alive) return;
      if (got) { setLines(got); setFailed(false); } else setFailed(true);
    });
    return () => { alive = false; };
  }, [projectId, runId, on]);
  return { lines, failed };
}

/** The preview's startup output, streamed through the console's own origin. */
function usePreviewLog(previewId: string | null, on: boolean): { text: string; state: string } {
  const [text, setText] = useState("");
  const [state, setState] = useState("connecting");
  useEffect(() => {
    if (!on || !previewId || typeof EventSource === "undefined") return undefined;
    setState("connecting");
    const source = new EventSource(`/api/preview-logs?preview_id=${encodeURIComponent(previewId)}`);
    source.addEventListener("open", () => setState("streaming"));
    source.addEventListener("log", (event) => {
      setState("streaming");
      const data = (event as MessageEvent).data;
      try { setText((t) => (t + JSON.parse(data)).slice(-12000)); } catch { setText((t) => (t + data).slice(-12000)); }
    });
    source.addEventListener("end", () => { setState("ended"); source.close(); });
    source.onerror = () => { if (source.readyState === EventSource.CLOSED) setState("failed"); };
    return () => source.close();
  }, [previewId, on]);
  return { text, state };
}

export function LogsDrawer({ projectId, runId, previewId, deployId, tab, onTab, onClose }: {
  projectId: string; runId: string; previewId: string | null; deployId: string | null;
  tab: LogTab; onTab: (t: LogTab) => void; onClose: () => void;
}) {
  const headId = useId();
  const box = useRef<HTMLElement>(null);
  const [agent, setAgent] = useState("");
  const build = useBuildLog(projectId, runId, tab === "build");
  const preview = usePreviewLog(previewId, tab === "preview");
  useEffect(() => { box.current?.focus(); }, []);

  const buildLines = (build.lines ?? []).filter((l) => !agent || l.agent === agent);
  const agents = [...new Set((build.lines ?? []).map((l) => l.agent).filter(Boolean))];
  const text = tab === "build"
    ? buildLines.map((l) => `${clockOf(l.at)} ${l.agent ? `${whoName(l.agent)} ` : ""}${l.title}${l.body ? `\n${l.body}` : ""}`).join("\n")
    : tab === "preview" ? preview.text : "";

  const copy = () => { try { void navigator.clipboard?.writeText(text); } catch { /* nothing to do */ } };

  return (
    <section ref={box} className="fx-logs" role="region" aria-labelledby={headId} data-testid="fx-logs" tabIndex={-1}>
      <header className="fx-logs-head">
        <h2 id={headId} className="fx-sec-title">Logs</h2>
        <span role="tablist" aria-label="Which log" className="fx-ck-tabs">
          {(["build", "preview", "deploy"] as const).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} className="fx-ck-tab" data-testid={`fx-logs-tab-${t}`}
                    onClick={() => onTab(t)}>
              {t === "build" ? "Build" : t === "preview" ? "Preview" : "Deploy"}
            </button>
          ))}
        </span>
        {tab === "build" && agents.length > 1 ? (
          <select className="mr-select" aria-label="Agent" value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="">Everyone</option>
            {agents.map((a) => <option key={a} value={a}>{whoName(a)}</option>)}
          </select>
        ) : null}
        <span className="fx-spacer" />
        {tab !== "deploy" ? <button type="button" className="ag-link" onClick={copy} data-testid="fx-logs-copy">Copy</button> : null}
        <a className="ag-link" href={`?area=activity&run=${encodeURIComponent(runId)}`}>Open full log ↗</a>
        <button type="button" className="ag-link" onClick={onClose} aria-label="Close logs">×</button>
      </header>
      <div className="fx-logs-body">
        {tab === "build" ? (
          build.lines ? (
            buildLines.length ? <pre className="fx-log" data-user-text data-testid="fx-log-build">{text}</pre>
              : <span className="fx-muted">Nothing logged yet</span>
          ) : <span className="fx-muted">{build.failed ? "Can't reach BOSS" : "Loading"}</span>
        ) : null}
        {tab === "preview" ? (
          previewId
            ? <pre className="fx-log" data-user-text data-state={preview.state} data-testid="preview-log">
                {preview.text || (preview.state === "failed" ? "Lost the stream" : preview.state === "ended" ? "No output" : "Waiting")}
              </pre>
            : <span className="fx-muted">Not running</span>
        ) : null}
        {tab === "deploy" ? (deployId ? <LiveDeployment runId={deployId} /> : <span className="fx-muted">Not set up</span>) : null}
      </div>
    </section>
  );
}
