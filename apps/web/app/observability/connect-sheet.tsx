"use client";

/**
 * Observability › Connect an app (BOSSAPP-598, PRD §6.1): one question, how does it talk?
 *
 *   How does it talk?   ( ) BOSS builds it  (•) OpenTelemetry  ( ) A collector  ( ) Prometheus (remote write)
 *                       ( ) It has /metrics ( ) Just a URL     ( ) It already alerts elsewhere [ Alertmanager ▾ ]
 *   Name [ checkout-api ]   Env [ prod ]   (URL [ https://… ] for /metrics and URL)
 *                                                                        [ Cancel ] [ Connect ]
 *   → PASTE THESE (shown once)   OTEL_EXPORTER_OTLP_ENDPOINT  https://…   [Copy]
 *                                OTEL_EXPORTER_OTLP_HEADERS   Authorization=Bearer …  [Copy]
 *   → LIVE CHECK   Waiting for the first data… → "12 series from checkout-api; …; 3 rules proposed"
 *                  (after 2 minutes still nothing: what to check)
 *
 * The API says what each path needs and refuses with a sentence (422), shown as is.
 */
import React, { useEffect, useRef, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  ALERT_TOOLS, CONNECT_PATH, CONNECT_PATHS, connectNeeds, liveUrl, nameRequired, parseConnected, parseLive,
  type AlertTool, type ConnectPath, type Connected, type Live,
} from "../../lib/monitoring";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { CopyButton } from "../infra/infra-util";
import { Sheet } from "../infra/kit";
import { obsRead, obsWrite } from "./obs-write";

export const LIVE_POLL_MS = 3_000;
export const LIVE_HINT_AFTER_MS = 120_000;
/** Polling stops after this; the word stays. */
const LIVE_GIVE_UP_MS = 15 * 60_000;

function LiveCheck({ projectId, serviceId, since }: { projectId: string; serviceId: string; since: string }) {
  const [live, setLive] = useState<Live | null>(null);
  const [late, setLate] = useState(false);
  const started = useRef(Date.now());
  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      const body = await obsRead<unknown>(liveUrl(projectId, serviceId, since));
      if (stop) return;
      const l = body ? parseLive(body) : null;
      if (l) setLive(l);
      const age = Date.now() - started.current;
      if (age >= LIVE_HINT_AFTER_MS) setLate(true);
      if (l?.arrived) { notifyChanged("observability", { service: serviceId }); return; }
      if (age < LIVE_GIVE_UP_MS) timer = setTimeout(() => void tick(), LIVE_POLL_MS);
    };
    void tick();
    return () => { stop = true; if (timer) clearTimeout(timer); };
  }, [projectId, serviceId, since]);
  return (
    <div className="obs-zone" data-testid="obs-connect-live" aria-live="polite">
      <span className="obs-rlabel">Live check</span>
      {live?.arrived ? (
        <span className="obs-line" data-testid="obs-connect-arrived">
          <span className="obs-adot" data-tone="green" aria-hidden />
          <span className="obs-strong" data-copy="data">{live.word}</span>
        </span>
      ) : late ? (
        <span className="obs-zone" data-testid="obs-connect-hint">
          <span className="obs-line"><span className="obs-adot" data-tone="amber" aria-hidden />
            <span className="obs-strong">Nothing yet after 2 minutes</span></span>
          <span className="inf-muted" data-copy="data">{live?.hint ?? live?.word ?? "Check the values above were pasted, and that the app can reach BOSS."}</span>
        </span>
      ) : (
        <span className="obs-line inf-muted" data-testid="obs-connect-waiting">
          <span className="obs-adot" data-tone="grey" aria-hidden />Waiting for the first data…
        </span>
      )}
    </div>
  );
}

function PasteBlock({ c }: { c: Connected }) {
  if (!c.paste || (!c.paste.values.length && !c.paste.block)) return null;
  return (
    <div className="obs-zone" data-testid="obs-connect-paste">
      <span className="obs-rlabel">Paste these</span>
      {c.shownOnce ? (
        <span className="obs-chip" data-tone="amber" data-testid="obs-connect-once"
              title="BOSS keeps only a hash of the token. Lost it? Rotate it in Sources.">
          Shown once: copy them now
        </span>
      ) : null}
      <ul className="obs-list">
        {c.paste.values.map((v) => (
          <li key={v.name} className="obs-line" data-testid="obs-connect-value">
            <span className="inf-mono obs-strong" data-copy="data">{v.name}</span>
            <code className="inf-mono inf-trunc obs-url" title={v.value} data-copy="data">{v.value}</code>
            <span className="ag-spacer" />
            <CopyButton text={v.value} tip={`Copy ${v.name}`} testId={`obs-connect-copy-${v.name}`} />
          </li>
        ))}
      </ul>
      {c.paste.block ? (
        <>
          <pre className="obs-promql" data-copy="data" data-testid="obs-connect-block">{c.paste.block}</pre>
          <span className="obs-line"><span className="ag-spacer" />
            <CopyButton text={c.paste.block} label="Copy block" testId="obs-connect-copy-block" /></span>
        </>
      ) : null}
    </div>
  );
}

export function ConnectSheet({ projectId, actor, onClose }: { projectId: string; actor: string; onClose: () => void }) {
  const [path, setPath] = useState<ConnectPath>("otel");
  const [tool, setTool] = useState<AlertTool>("alertmanager");
  const [name, setName] = useState("");
  const [env, setEnv] = useState("prod");
  const [url, setUrl] = useState("");
  const [every, setEvery] = useState("300");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ c: Connected; since: string } | null>(null);
  const needs = connectNeeds(path, path === "alerts" ? tool : null);

  const connect = async () => {
    setBusy(true);
    setError(null);
    const since = new Date().toISOString();
    const body: Record<string, unknown> = { project_id: projectId, actor, path, environment: env.trim() || "prod" };
    if (needs.name && name.trim()) body.name = name.trim();
    if (needs.url) body.url = url.trim();
    if (path === "alerts") body.tool = tool;
    if (needs.heartbeat) body.heartbeat_every_s = Number(every) || 300;
    const r = await obsWrite(CONNECT_PATH, body, { scope: `obs:connect:${path}`, label: "Connect" });
    setBusy(false);
    if (!r.ok) { setError(r.error ?? "Not connected"); return; }
    const c = parseConnected(r.body);
    showToast("success", c.word || "Connected");
    notifyChanged("observability", { connect: path });
    setDone({ c, since });
  };
  const missing = (nameRequired(path) && !name.trim()) || (needs.url && !url.trim());

  return (
    <Sheet title="Connect an app" testId="obs-connect-sheet" onClose={onClose}>
      {done ? (
        <div className="obs-rform" data-testid="obs-connect-done">
          <span className="obs-strong" data-testid="obs-connect-word" data-copy="data">{done.c.word}</span>
          <PasteBlock c={done.c} />
          {done.c.serviceId && done.c.path !== "boss" ? (
            <LiveCheck projectId={projectId} serviceId={done.c.serviceId} since={done.since} />
          ) : null}
          <div className="inf-actions"><span className="ag-spacer" />
            <button type="button" className="ag-btn ag-btn-primary" data-testid="obs-connect-close" onClick={onClose}>Done</button></div>
        </div>
      ) : (
        <div className="obs-rform">
          {/* ONE APP'S WATCHES LIVE ON ITS OWN PAGE (Project › Health › Watch this app): one input,
              the SRE agent sets it up. This sheet stays the "Wire it myself" path. */}
          <a className="inf-link" data-testid="obs-connect-watch" href={`?area=project&project_id=${encodeURIComponent(projectId)}&tab=health&watch=1`}
             title="One input: the SRE agent reads the app and proposes the watches">
            Watching one of this project&apos;s apps? Project › Health › Watch this app ›</a>
          <fieldset className="obs-zone obs-connect-paths" data-testid="obs-connect-paths">
            <legend className="obs-rlabel">How does it talk?</legend>
            {CONNECT_PATHS.map((p) => (
              <label key={p.key} className="obs-rcheck" title={p.tip}>
                <input type="radio" name="obs-connect-path" checked={path === p.key} data-testid={`obs-connect-${p.key}`}
                       onChange={() => { setPath(p.key); setError(null); }} />
                <span>{p.label}</span>
              </label>
            ))}
          </fieldset>
          {path === "boss" ? (
            <span className="inf-muted" data-testid="obs-connect-boss-note">
              Nothing to paste: BOSS watches what it builds and deploys. Connect to see what it watches now.
            </span>
          ) : null}
          {path === "alerts" ? (
            <label className="inf-field">
              <span>Tool</span>
              <select value={tool} onChange={(e) => setTool(e.target.value as AlertTool)} data-testid="obs-connect-tool">
                {ALERT_TOOLS.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
            </label>
          ) : null}
          {needs.name ? (
            <div className="obs-rrow obs-connect-row">
              <label className="inf-field">
                <span>{nameRequired(path) ? "Name" : "Name (optional)"}</span>
                <input value={name} onChange={(e) => setName(e.target.value)} data-testid="obs-connect-name" placeholder="checkout-api"
                       autoComplete="off" spellCheck={false} title="The service name alerts and rules will carry" />
              </label>
              <label className="inf-field">
                <span>Env</span>
                <input value={env} onChange={(e) => setEnv(e.target.value)} data-testid="obs-connect-env" autoComplete="off"
                       spellCheck={false} title="prod, staging, …" />
              </label>
            </div>
          ) : null}
          {needs.url ? (
            <label className="inf-field">
              <span>{path === "metrics_url" ? "Metrics URL" : "URL"}</span>
              <input value={url} onChange={(e) => setUrl(e.target.value)} data-testid="obs-connect-target" inputMode="url"
                     autoComplete="off" spellCheck={false}
                     placeholder={path === "metrics_url" ? "https://checkout.example.com/metrics" : "https://checkout.example.com/health"}
                     title={path === "url" ? "BOSS calls it every minute and alerts when it stops answering" : "Prometheus text format"} />
            </label>
          ) : null}
          {needs.heartbeat ? (
            <label className="inf-field">
              <span>Expect a check-in every (seconds)</span>
              <input type="number" min={30} value={every} onChange={(e) => setEvery(e.target.value)} data-testid="obs-connect-every"
                     title="Fires when no check-in arrives in this time" />
            </label>
          ) : null}
          {error ? <span className="inf-error" data-testid="obs-connect-error" data-copy="data">{error}</span> : null}
          <div className="inf-actions">
            <span className="ag-spacer" />
            <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
            <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-connect-go"
                         disabled={busy || missing} aria-busy={busy || undefined} onClick={connect}>
              {busy ? "Connecting…" : "Connect"}
            </GatedButton>
          </div>
        </div>
      )}
    </Sheet>
  );
}
