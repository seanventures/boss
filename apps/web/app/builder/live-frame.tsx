"use client";

/**
 * The running app beside the chat, in a device frame, with Publish (BOSSAPP-1241).
 * The caller supplies the build. This screen never starts one and never calls GitHub.
 * Publishing to GitHub shows the existing not-connected error.
 */
import React, { useState } from "react";

export const NOT_CONNECTED = "Connect GitHub first: a new project reaches a repository through the "
  + "organisation's GitHub App (Setup › GitHub).";

export type LiveChatLine = { role: string; text: string };

export type LiveBuild = {
  id: string;
  title?: string;
  status?: string;
  appUrl?: string | null;
  app_url?: string | null;
  chat?: LiveChatLine[];
  error?: string | null;
  fake?: boolean;
};

export type PublishResult = {
  ok: boolean;
  code: string;
  message: string;
  dedupe_key: string;
  recorded: boolean;
  where?: string;
};

const DEVICES = [
  { id: "phone", label: "Phone", width: 390, height: 844 },
  { id: "tablet", label: "Tablet", width: 834, height: 1194 },
  { id: "desktop", label: "Desktop", width: 1280, height: 800 },
] as const;

const STILL_GOING = new Set(["building", "queued", "running"]);

function words(text: string): number {
  return text.replace(/[·▸•|]/g, " ").trim().split(/\s+/).filter(Boolean).length;
}

function rawUrl(build: LiveBuild): string | null {
  const raw = build.appUrl ?? build.app_url;
  return typeof raw === "string" ? raw : null;
}

function runningUrl(build: LiveBuild): string | null {
  const raw = rawUrl(build);
  if (!raw) return null;
  const url = raw.trim();
  if (!url) return null;
  const head = url.slice(0, 8).toLowerCase();
  if (head.startsWith("https://") || head.startsWith("http://")) return url;
  return null;
}

function dedupeKey(id: string): string {
  return `bossapp-1241:${id}:publish`;
}

export function LiveFrame({
  build = null,
  publishTo = "local",
  onPublish,
}: {
  build?: LiveBuild | null;
  publishTo?: string;
  onPublish?: (result: PublishResult) => void;
}) {
  const [deviceId, setDeviceId] = useState<string>("desktop");
  const [result, setResult] = useState<PublishResult | null>(null);
  const [recorded, setRecorded] = useState(false);
  const device = DEVICES.find((item) => item.id === deviceId) ?? DEVICES[2];
  const id = build?.id?.trim() ?? "";
  const url = build ? runningUrl(build) : null;
  const status = (build?.status || (build ? "building" : "empty")).trim().toLowerCase();
  const still = STILL_GOING.has(status);
  let error = build?.error?.trim() || "";
  if (build && rawUrl(build)?.trim() && !url && !error) error = "The app address was refused.";
  if (status === "failed" && !error) error = "The build failed.";
  if (build && !id && !error) error = "A build needs an id.";
  const canPublish = Boolean(url) && Boolean(id);
  const title = build?.title?.trim() ?? "";
  const chat = (build?.chat ?? []).filter((line) => line.text.trim());
  const lamp = still
    ? "Still going"
    : status === "failed"
      ? "Build failed"
      : status === "succeeded"
        ? "Build finished"
        : build
          ? "In progress"
          : "No build";

  function press() {
    if (!build || !id) return;
    const key = dedupeKey(id);
    const destination = (publishTo || "local").trim().toLowerCase();
    if (destination === "github") {
      const next: PublishResult = {
        ok: false,
        code: "github_not_connected",
        message: NOT_CONNECTED,
        dedupe_key: key,
        recorded: false,
      };
      setResult(next);
      onPublish?.(next);
      return;
    }
    if (recorded) {
      setResult({
        ok: true,
        code: "already_published",
        message: "Already published.",
        dedupe_key: key,
        recorded: false,
        where: "local",
      });
      return;
    }
    if (!url) {
      const next: PublishResult = {
        ok: false,
        code: "no_app",
        message: "Nothing is running yet.",
        dedupe_key: key,
        recorded: false,
      };
      setResult(next);
      onPublish?.(next);
      return;
    }
    const next: PublishResult = {
      ok: true,
      code: "published",
      message: "Published.",
      dedupe_key: key,
      recorded: true,
      where: "local",
    };
    setRecorded(true);
    setResult(next);
    onPublish?.(next);
  }

  const prose = result !== null && words(result.message) > 6;

  return (
    <section className="blf" data-testid="blf-root" aria-label="Live frame">
      <style>{CSS}</style>
      <div
        className="blf-split"
        data-testid="blf-split"
        data-layout="beside"
        data-still-going={still ? "true" : "false"}
        data-fake={build?.fake ? "true" : "false"}
      >
        <section className="blf-chat" data-testid="blf-chat" aria-label="Chat">
          <header className="blf-chat-head">
            <span className="blf-kicker">Chat</span>
            {title
              ? <h1 className="blf-title" data-user-text>{title}</h1>
              : <h1 className="blf-title">Live frame</h1>}
          </header>
          <ol className="blf-log">
            {chat.length === 0
              ? <li><span className="blf-empty">No messages yet.</span></li>
              : chat.map((line, index) => (
                <li key={`${line.role}-${index}`} className={line.role === "you" ? "blf-you" : "blf-boss"}>
                  <span className="blf-who">{line.role === "you" ? "You" : "BOSS"}</span>
                  <p data-user-text>{line.text}</p>
                </li>
              ))}
          </ol>
          {still && url
            ? <p className="blf-note" data-testid="blf-note">The build is still going. This is the app already running.</p>
            : null}
        </section>
        <section className="blf-stage" data-testid="blf-stage" aria-label="Running app">
          <div className="blf-bar">
            <span className={still ? "blf-lamp is-on" : "blf-lamp"} data-testid="blf-lamp" data-on={still ? "true" : "false"} aria-hidden="true" />
            <span className="blf-lamp-word">{lamp}</span>
            <div className="blf-devices" role="radiogroup" aria-label="Device frame">
              {DEVICES.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="radio"
                  aria-checked={item.id === device.id}
                  className="blf-device"
                  data-testid={`blf-device-${item.id}`}
                  title={`Show the ${item.label.toLowerCase()} frame`}
                  onClick={() => setDeviceId(item.id)}
                >{item.label}</button>
              ))}
            </div>
            <button
              type="button"
              className="blf-publish"
              data-testid="blf-publish"
              title="Publish the running app"
              disabled={!canPublish}
              onClick={press}
            >Publish</button>
          </div>
          {!canPublish ? <span className="blf-reason" data-testid="blf-publish-reason">Nothing is running yet.</span> : null}
          {result
            ? prose
              ? <p className="blf-result" data-testid="blf-publish-result" role={result.ok ? undefined : "alert"}>{result.message}</p>
              : <span className="blf-result" data-testid="blf-publish-result">{result.message}</span>
            : null}
          {error ? <p className="blf-error" data-testid="blf-error" role="alert">{error}</p> : null}
          <div
            className={`blf-bezel is-${device.id}`}
            data-testid="blf-bezel"
            data-device={device.id}
            data-width={device.width}
            data-height={device.height}
          >
            <div className="blf-chrome" aria-hidden="true">
              {device.id === "desktop" ? <span className="blf-dots"><i /><i /><i /></span> : null}
              {device.id === "phone" ? <span className="blf-notch" /> : null}
              <span className="blf-readout" data-copy="data">{device.width} × {device.height}</span>
            </div>
            <div className="blf-screen">
              {url
                ? <iframe data-testid="blf-app" title="Running app" src={url} sandbox="allow-scripts allow-forms allow-popups" referrerPolicy="no-referrer" />
                : <span className="blf-glass-empty">No app yet</span>}
            </div>
          </div>
        </section>
      </div>
    </section>
  );
}

export default LiveFrame;

const CSS = `
.blf {
  --blf-ink: #1b262c;
  --blf-sage: #e6f0e8;
  --blf-brass: #c4923a;
  --blf-tide: #1e6b64;
  --blf-glass: #f4f8f5;
  --blf-fault: #6e2e28;
  --blf-fault-ink: #f8ece9;
  background: var(--blf-ink);
  color: #d7e3dc;
  font-family: "Avenir Next", "Segoe UI", sans-serif;
}
.blf-split {
  display: grid;
  grid-template-columns: minmax(240px, 340px) minmax(0, 1fr);
  min-height: 640px;
}
.blf-chat {
  background: var(--blf-sage);
  color: var(--blf-ink);
  padding: 1.25rem 1.2rem 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.blf-kicker {
  display: block;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--blf-tide);
}
.blf-title {
  margin: 0.3rem 0 0;
  font-family: "Iowan Old Style", Palatino, "Palatino Linotype", "Book Antiqua", serif;
  font-size: 1.7rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1.15;
}
.blf-log { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.9rem; }
.blf-who {
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--blf-tide);
}
.blf-you .blf-who { color: #6a5424; }
.blf-log p, .blf-note, .blf-error, .blf-result { margin: 0.2rem 0 0; }
.blf-note { margin: 0; color: #23423c; max-width: 28ch; }
.blf-stage {
  padding: 1rem 1.25rem 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-width: 0;
}
.blf-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem; }
.blf-lamp {
  width: 9px; height: 9px; border-radius: 50%;
  background: #5c6568;
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.4);
}
.blf-lamp.is-on {
  background: var(--blf-brass);
  box-shadow: 0 0 0 3px rgba(196, 146, 58, 0.28);
  animation: blf-pulse 1.8s ease-in-out infinite;
}
.blf-lamp-word { font-size: 0.82rem; letter-spacing: 0.04em; }
.blf-devices {
  display: inline-flex;
  border: 1px solid rgba(230, 240, 232, 0.22);
  border-radius: 999px;
  overflow: hidden;
}
.blf-device {
  background: transparent;
  color: #d7e3dc;
  border: 0;
  padding: 0.35rem 0.7rem;
  font: 500 0.82rem/1 "Avenir Next", "Segoe UI", sans-serif;
  cursor: pointer;
}
.blf-device[aria-checked="true"] { background: var(--blf-tide); color: #f4faf8; }
.blf-publish {
  margin-left: auto;
  background: var(--blf-brass);
  color: var(--blf-ink);
  border: 0;
  border-radius: 999px;
  padding: 0.45rem 0.95rem;
  font: 600 0.92rem/1 "Avenir Next", "Segoe UI", sans-serif;
  cursor: pointer;
}
.blf-publish:disabled { background: #3a4448; color: #8b9694; cursor: not-allowed; }
.blf-publish:focus-visible, .blf-device:focus-visible {
  outline: 2px solid #f0d48a;
  outline-offset: 2px;
}
.blf-reason { color: #d5e0da; font-size: 0.92rem; }
.blf-error {
  background: var(--blf-fault);
  color: var(--blf-fault-ink);
  padding: 0.55rem 0.75rem;
  border-radius: 8px;
}
.blf-bezel {
  background: linear-gradient(180deg, #303a3e, #1c2528);
  padding: 14px 14px 18px;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08), 0 18px 40px rgba(0, 0, 0, 0.28);
}
.blf-bezel.is-phone { width: min(100%, 250px); border-radius: 36px; margin-inline: auto; }
.blf-bezel.is-tablet { width: min(100%, 440px); border-radius: 28px; margin-inline: auto; }
.blf-bezel.is-desktop { width: 100%; border-radius: 16px; }
.blf-chrome {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 22px;
  margin-bottom: 8px;
  color: #c5d2cb;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.72rem;
}
.blf-notch {
  width: 72px; height: 14px; border-radius: 0 0 10px 10px; background: #141c1f; display: block;
}
.blf-dots { display: flex; gap: 5px; }
.blf-dots i { width: 8px; height: 8px; border-radius: 50%; background: #8d6a32; display: block; }
.blf-readout { margin-left: auto; }
.blf-screen {
  background: var(--blf-glass);
  min-height: 280px;
  border-radius: 12px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
.blf-bezel.is-phone .blf-screen { min-height: 420px; border-radius: 22px; }
.blf-screen iframe { width: 100%; height: 100%; min-height: inherit; border: 0; background: #fff; }
.blf-glass-empty { color: #5d6b66; font-size: 0.92rem; }
@keyframes blf-pulse { 50% { opacity: 0.45; } }
@media (prefers-reduced-motion: reduce) {
  .blf-lamp.is-on { animation: none; }
}
@media (max-width: 720px) {
  .blf-split { grid-template-columns: 1fr; }
}
`;
