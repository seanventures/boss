"use client";

/**
 * YOUR APP (operator, 2026-11-19: "for the Recipe why there are no previews showing up now?").
 *
 * The project's app, always one press away: the Overview's card (top, beside the Journey), and a
 * small "Open app" on the projects list card and Command Center's project rows. It says which
 * version it shows — main after a merge, else the latest ready change — and one button opens it.
 * With no preview running, Open app starts one through the console's one write path
 * (startPreviewAction: consent and step-up included), says "Starting… about a minute", and opens
 * it; the link itself shows "Starting your app…" until the app serves. A preview BOSS stopped for
 * idle is never shown as gone: it is asleep and wakes on Open.
 */
import React, { useState } from "react";
import { AppWindow, ArrowSquareOut, Globe, Moon, Play } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { callAction } from "../../lib/call-action";
import { browserHref } from "../../lib/browser-links";
import { appInitials, opensDirectly, type YourApp } from "../../lib/your-app";
import { startPreviewAction } from "../actions";
import { notifyChanged } from "../changed";
import { Ico, Tag } from "./kit";
import "./your-app.css";

/** Open a link in a new tab. A blocked popup cannot be told apart (noopener gives no handle),
 *  so after a start the card also keeps the link. */
function openTab(href: string): void {
  try { window.open(browserHref(href), "_blank", "noopener,noreferrer"); } catch { /* the link stays */ }
}

/** "Main, as merged in change #2" → "as merged in change #2": the words after the label. */
const versionRest = (words: string) => { const i = words.indexOf(", "); return i >= 0 ? words.slice(i + 2) : ""; };

export type OpenAppState = { busy: boolean; starting: boolean; error: string; link: string };

/** THE ONE OPEN: running → open it; anything else → start it (one write path), then open it. */
export function useOpenApp(app: YourApp, actor?: string, onStarted?: () => void) {
  const [st, setSt] = useState<OpenAppState>({ busy: false, starting: false, error: "", link: "" });
  const open = async () => {
    if (opensDirectly(app)) { openTab(app.href); return; }
    setSt({ busy: true, starting: true, error: "", link: "" });
    const form = new FormData();
    form.set("run_id", app.run_id);
    if (actor) form.set("actor", actor);
    let res: { url?: string; error?: string; note?: string; id?: string; starting?: boolean } | undefined;
    try {
      res = await callAction(startPreviewAction, undefined, "preview:manage")(null, form);
    } catch (e) {
      res = { error: e instanceof Error ? e.message : String(e) };
    }
    // A CLIENT TIMEOUT IS NOT A VERDICT: the preview builds on after the console stops waiting.
    if (res?.error && /did not answer within/i.test(res.error)) res = { starting: true };
    if (res?.error) { setSt({ busy: false, starting: false, error: res.error, link: "" }); return; }
    notifyChanged("previews");
    onStarted?.();
    const link = res?.url ?? "";
    if (link) openTab(link);
    setSt({ busy: false, starting: Boolean(res?.starting) || !res?.url, error: "", link });
  };
  return { ...st, open };
}

const STATE_TONE: Record<YourApp["state"], "green" | "amber" | "teal" | "idle" | "red"> = {
  running: "green", starting: "teal", asleep: "idle", stopped: "idle", failed: "amber", none: "idle",
};
const STATE_WORD: Record<YourApp["state"], string> = {
  running: "Running", starting: "Starting", asleep: "Asleep", stopped: "Stopped", failed: "Didn't start", none: "Not started",
};

/** The Overview's card: the app's tile, its version, its state, and one Open app. */
export function YourAppCard({ app, projectId, actor, onStarted }: { app: YourApp; projectId: string; actor?: string; onStarted?: () => void }) {
  const o = useOpenApp(app, actor, onStarted);
  const starting = o.starting || app.state === "starting";
  const state = starting ? "starting" : app.state;
  const line = o.error ? "" : starting ? "Starting… about a minute. It opens in a new tab." : app.words;
  return (
    <section className="px-card ya-card" aria-labelledby="px-ov-app" data-testid="px-ov-app" data-state={state}>
      <div className="ya-body">
        <div className="ya-thumb" aria-hidden data-state={state}>
          <span>{appInitials(app.name)}</span>
          {state === "asleep" ? <Moon size={14} weight="fill" className="ya-badge" /> : null}
        </div>
        <div className="ya-main">
          <div className="px-card-h ya-h">
            <Ico icon={AppWindow} c="teal" />
            <h2 id="px-ov-app">Your app</h2>
            <Tag tone={STATE_TONE[state]} title={app.words}>{STATE_WORD[state]}</Tag>
          </div>
          <div className="ya-name" data-testid="px-ov-app-name">{app.name}</div>
          <div className="ya-version px-faint" data-testid="px-ov-app-version" title={app.title ? `Built by: ${app.title}` : undefined}>
            Shows <b>{app.version_label}</b>{versionRest(app.version_words) ? ` · ${versionRest(app.version_words)}` : ""}
          </div>
          <small className="px-faint ya-line" data-testid="px-ov-app-line" role="status" aria-live="polite">{line}</small>
          {o.error ? <small className="ya-err" role="alert" data-testid="px-ov-app-error" title={o.error}>Couldn't start it: {o.error.slice(0, 140)}</small> : null}
        </div>
        <div className="ya-actions">
          <GatedButton permission="preview:manage" projectId={projectId} className="px-btn" data-kind="primary" data-testid="px-ov-app-open"
                       disabled={o.busy} onClick={() => void o.open()}>
            {opensDirectly(app) ? <ArrowSquareOut size={14} weight="bold" aria-hidden /> : <Play size={14} weight="fill" aria-hidden />}
            {o.busy ? "Starting…" : "Open app"}
          </GatedButton>
          {o.link ? <a className="px-more" href={browserHref(o.link)} target="_blank" rel="noreferrer noopener" data-testid="px-ov-app-link">Open in a new tab ↗</a> : null}
          {app.live_url ? (
            <a className="px-more ya-live" href={app.live_url} target="_blank" rel="noreferrer noopener" data-testid="px-ov-app-live">
              <Globe size={11} aria-hidden /> Try it live ↗</a>
          ) : null}
        </div>
      </div>
    </section>
  );
}

/** The small "Open app" on a projects list card and a Command Center project row. */
export function OpenAppChip({ app, projectId, actor, testid }: { app: YourApp; projectId: string; actor?: string; testid?: string }) {
  const o = useOpenApp(app, actor);
  const starting = o.starting || app.state === "starting";
  return (
    <span className="ya-chip" data-testid={testid ?? `open-app-${projectId}`} data-state={starting ? "starting" : app.state}
          onClick={(e) => e.stopPropagation()}>
      <GatedButton permission="preview:manage" projectId={projectId} className="ya-chip-btn" disabled={o.busy}
                   title={o.error ? `Couldn't start it: ${o.error}` : `${app.name} · ${app.version_label} · ${app.words}`}
                   onClick={(e) => { e.stopPropagation(); void o.open(); }}>
        {app.state === "asleep" && !starting ? <Moon size={11} weight="fill" aria-hidden /> : <AppWindow size={11} weight="bold" aria-hidden />}
        {o.busy || starting ? "Starting…" : "Open app"}
      </GatedButton>
    </span>
  );
}
