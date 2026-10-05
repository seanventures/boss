"use client";

/**
 * A preview's log (`preview=<id>`, spec §4.7). While the preview is live it streams, the way
 * Factory's startup log does (EventSource on /api/preview-logs, which proxies the API's SSE with
 * the key added server-side); once it has ended it is read once (GET /v1/previews/{id}/logs).
 */
import React, { useEffect, useState } from "react";

import { buildHref, type PreviewRow } from "../../lib/infra-view";
import { goConsole } from "./deploy-actions";
import { CopyButton, ErrorLine } from "./infra-util";
import { Drawer, DrawerSection, StateWord } from "./kit";

const MAX_CHARS = 64_000;

/** The log as text from whatever the API answered: SSE-ish text, `{lines}`, `{log}` or `{text}`. */
export function logText(body: unknown): string {
  if (typeof body === "string") return body;
  const b = (body ?? {}) as { lines?: unknown; log?: unknown; text?: unknown; output?: unknown };
  if (Array.isArray(b.lines)) return b.lines.map(String).join("\n");
  for (const v of [b.log, b.text, b.output]) if (typeof v === "string") return v;
  return "";
}

function useStream(previewId: string, on: boolean) {
  const [text, setText] = useState("");
  const [state, setState] = useState<"connecting" | "streaming" | "ended" | "failed">("connecting");
  useEffect(() => {
    if (!on || typeof EventSource === "undefined") return undefined;
    setState("connecting");
    const source = new EventSource(`/api/preview-logs?preview_id=${encodeURIComponent(previewId)}`);
    source.addEventListener("open", () => setState("streaming"));
    source.addEventListener("log", (event) => {
      setState("streaming");
      const data = (event as MessageEvent).data as string;
      let line = data;
      try { line = String(JSON.parse(data)); } catch { /* raw */ }
      setText((t) => (t + (t && !t.endsWith("\n") && !line.startsWith("\n") ? "\n" : "") + line).slice(-MAX_CHARS));
    });
    source.addEventListener("end", () => { setState("ended"); source.close(); });
    source.onerror = () => { if (source.readyState === EventSource.CLOSED) setState("failed"); };
    return () => source.close();
  }, [previewId, on]);
  return { text, state };
}

function useOnce(previewId: string, on: boolean) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!on) return;
    let gone = false;
    void (async () => {
      try {
        const r = await fetch(`/v1/previews/${encodeURIComponent(previewId)}/logs`, { cache: "no-store" });
        const raw = await r.text();
        if (gone) return;
        if (!r.ok) { setError(`The API answered ${r.status}.`); return; }
        let body: unknown = raw;
        try { body = JSON.parse(raw); } catch { /* text */ }
        setText(logText(body).slice(-MAX_CHARS));
      } catch (e) {
        if (!gone) setError(e instanceof Error ? e.message : "The API did not answer.");
      }
    })();
    return () => { gone = true; };
  }, [previewId, on]);
  return { text, error };
}

export function PreviewLogDrawer({ previewId, row, onClose }: { previewId: string; row: PreviewRow | null; onClose: () => void }) {
  const live = !!row && (row.state === "running" || row.state === "starting");
  const stream = useStream(previewId, live);
  const once = useOnce(previewId, !live);
  const text = live ? stream.text : once.text ?? "";
  const head = (
    <span className="inf-dd-head">
      <strong className="inf-trunc" data-copy="data">{row ? `${row.app}${row.branch ? ` · ${row.branch}` : ""}` : "Preview"}</strong>
      {row ? <StateWord w={{ word: row.word, tone: row.tone }} /> : null}
    </span>
  );
  return (
    <Drawer label="Preview log" testId="inf-preview-log" width={520} onClose={onClose} head={head}>
      <DrawerSection label="Log" testId="inf-preview-log-body" actions={(
        <span className="inf-actions">
          {text ? <CopyButton text={text} label="Copy" /> : null}
          {row?.runId ? (
            <a className="inf-link" href={buildHref(row.runId)} title="Open the build that made this"
               onClick={(e) => { if (!(e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0)) { e.preventDefault(); goConsole(buildHref(row.runId as string)); } }}>
              Build ↗
            </a>
          ) : null}
        </span>
      )}>
        {live && stream.state === "connecting" && !text ? <span className="inf-muted">Connecting…</span> : null}
        {live && stream.state === "failed" && !text ? <span className="inf-muted">Stream closed</span> : null}
        {!live ? <ErrorLine error={once.error} /> : null}
        {text ? <pre className="inf-dlog" data-copy="data" tabIndex={0} data-testid="inf-preview-log-text">{text}</pre>
          : (!live && once.text !== null) || (live && stream.state === "ended") ? <span className="inf-muted">No log yet</span> : null}
      </DrawerSection>
    </Drawer>
  );
}
