"use client";

import React, { useEffect, useRef, useState } from "react";

import { apiWrite } from "../../lib/api-write";

/**
 * Bench preview for one mobile frame.
 * Darkroom teal, brass chassis, contact sheet. Not a device farm, not a store.
 */

export type Platform = "ios" | "android";

export type PreviewFrame = {
  platform: Platform;
  index: number;
  chrome: "notch" | "punch";
  bars: number[];
};

export type UploadReceipt = {
  id: string;
  dedupe_key: string;
  status: "accepted";
  device_farm: false;
  real_store: false;
};

export type UploadBody = {
  platform: Platform;
  artifactId: string;
  signingKey: string;
};

export type UploadResult =
  | { ok: true; receipt: UploadReceipt }
  | { ok: false; error: string };

const SEED: Record<Platform, number> = { ios: 3, android: 7 };

export function paintFrame(platform: Platform, index: number): PreviewFrame {
  const seed = SEED[platform];
  const bars = [0, 1, 2, 3, 4].map((slot) => ((index * seed + slot * 17) % 9) + 2);
  return {
    platform,
    index,
    chrome: platform === "ios" ? "notch" : "punch",
    bars,
  };
}

export async function uploadSignedBuild(body: UploadBody): Promise<UploadResult> {
  const response = await apiWrite(
    "/v1/builder/store-uploads",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platform: body.platform,
        artifact_id: body.artifactId,
        signing_key: body.signingKey,
      }),
    },
    { press: { scope: "emulator-frame", label: "Upload signed build" } },
  );
  let data: Record<string, unknown> = {};
  try {
    data = (await response.json()) as Record<string, unknown>;
  } catch {
    data = {};
  }
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "The upload was refused.";
    return { ok: false, error: detail };
  }
  if (data.status !== "accepted" || typeof data.dedupe_key !== "string" || typeof data.id !== "string") {
    return { ok: false, error: "The upload did not return a receipt." };
  }
  return {
    ok: true,
    receipt: {
      id: data.id,
      dedupe_key: data.dedupe_key,
      status: "accepted",
      device_farm: false,
      real_store: false,
    },
  };
}

function Bars({ bars, className }: { bars: number[]; className: string }) {
  return (
    <span className={className} aria-hidden="true">
      {bars.map((height, slot) => (
        <i key={slot} style={{ height: `${height * 8}px` }} />
      ))}
    </span>
  );
}

export function EmulatorFrame({
  artifactId = "build-preview",
  frameIntervalMs = 800,
  upload = uploadSignedBuild,
}: {
  artifactId?: string;
  frameIntervalMs?: number;
  upload?: (body: UploadBody) => Promise<UploadResult>;
}) {
  const [platform, setPlatform] = useState<Platform>("ios");
  const [frames, setFrames] = useState<PreviewFrame[]>(() => [paintFrame("ios", 1)]);
  const [signingKey, setSigningKey] = useState("");
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<UploadReceipt | null>(null);
  const saved = useRef<{ fingerprint: string; receipt: UploadReceipt } | null>(null);
  const busy = useRef(false);

  useEffect(() => {
    const id = setInterval(() => {
      setFrames((current) => {
        const last = current[current.length - 1];
        if (!last || last.index >= 24) return current;
        return [...current, paintFrame(platform, last.index + 1)].slice(-4);
      });
    }, frameIntervalMs);
    return () => clearInterval(id);
  }, [platform, frameIntervalMs]);

  function choose(next: Platform) {
    if (next === platform) return;
    setPlatform(next);
    setFrames([paintFrame(next, 1)]);
  }

  async function send() {
    const key = signingKey.trim();
    if (!key) {
      setError("A signing key is required. Nothing was uploaded.");
      setReceipt(null);
      return;
    }
    const fingerprint = `${platform}:${artifactId}:${key}`;
    if (saved.current?.fingerprint === fingerprint) {
      setError("");
      setReceipt(saved.current.receipt);
      return;
    }
    if (busy.current) return;
    busy.current = true;
    setError("");
    try {
      const result = await upload({ platform, artifactId, signingKey: key });
      if (!result.ok) {
        setReceipt(null);
        setError(result.error);
        return;
      }
      saved.current = { fingerprint, receipt: result.receipt };
      setReceipt(result.receipt);
    } catch (err) {
      setReceipt(null);
      setError(err instanceof Error ? err.message : "The upload failed.");
    } finally {
      busy.current = false;
    }
  }

  const live = frames[frames.length - 1] ?? paintFrame(platform, 1);

  return (
    <section
      className="emf"
      data-surface="preview"
      data-device-farm="false"
      data-real-store="false"
    >
      <style>{CSS}</style>
      <header className="emf-head">
        <span className="emf-kicker">Bench preview</span>
        <h1>One frame, on the bench</h1>
        <p>This glass is a local preview, not a device farm.</p>
      </header>
      <div className="emf-stage">
        <div className="emf-strip" aria-hidden="true">
          {frames.map((frame) => (
            <span className="emf-cell" key={`${frame.platform}-${frame.index}`}>
              <Bars bars={frame.bars} className="emf-cell-bars" />
            </span>
          ))}
        </div>
        <div className="emf-chassis" data-platform={platform}>
          <div
            className="emf-glass"
            data-frame-index={live.index}
            data-frame-platform={live.platform}
            data-bars={live.bars.join(",")}
          >
            <span className={live.chrome === "notch" ? "emf-notch" : "emf-punch"} />
            <span className="emf-screen-name">{live.chrome === "notch" ? "Notch glass" : "Punch glass"}</span>
            <Bars bars={live.bars} className="emf-bars" />
            <span className="emf-local">Local pixels</span>
          </div>
          <span className="emf-lamp" aria-hidden="true" />
        </div>
      </div>
      <p className="emf-index" aria-live="polite">Frame {live.index}</p>
      <div className="emf-switch" role="radiogroup" aria-label="Platform">
        <button
          type="button"
          role="radio"
          aria-checked={platform === "ios"}
          data-platform-choice="ios"
          title="Show the iOS preview"
          onClick={() => choose("ios")}
        >
          iOS
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={platform === "android"}
          data-platform-choice="android"
          title="Show the Android preview"
          onClick={() => choose("android")}
        >
          Android
        </button>
      </div>
      <form
        className="emf-upload"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <label>
          Signing key
          <input
            type="password"
            autoComplete="off"
            value={signingKey}
            title="Paste the signing key"
            aria-label="Signing key"
            onChange={(event) => setSigningKey(event.target.value)}
          />
        </label>
        <button type="submit" title="Send this build once">
          Upload signed build
        </button>
      </form>
      <p className="emf-honest">Upload goes to a fake store endpoint, not a real store.</p>
      {error ? <p role="alert">{error}</p> : null}
      {receipt ? (
        <p data-receipt={receipt.dedupe_key}>The fake store accepted this upload.</p>
      ) : null}
    </section>
  );
}

const CSS = `
.emf {
  --emf-bench: #16343a;
  --emf-brass: #c6a15b;
  --emf-brass-deep: #8a6233;
  --emf-ink: #e7f0ea;
  --emf-glass: #f3efe4;
  --emf-glass-ink: #1a3f38;
  --emf-lamp: #e7b23a;
  --emf-refusal: #d4533a;
  box-sizing: border-box;
  max-width: 440px;
  margin: 0;
  padding: 22px 22px 18px;
  background: var(--emf-bench);
  color: var(--emf-ink);
  font-family: "Avenir Next", "Segoe UI", sans-serif;
  border-radius: 8px;
}
.emf *, .emf *::before, .emf *::after { box-sizing: border-box; }
.emf-kicker {
  display: block;
  font-family: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  font-size: 0.72rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--emf-brass);
}
.emf h1 {
  margin: 6px 0 8px;
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, serif;
  font-size: 1.85rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1.05;
}
.emf p { margin: 0 0 12px; max-width: 36em; line-height: 1.45; }
.emf-head p { color: #c5d5ce; }
.emf-stage {
  display: grid;
  grid-template-columns: 56px minmax(0, 220px);
  gap: 14px;
  align-items: end;
  margin: 8px 0 12px;
}
.emf-strip {
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  gap: 6px;
  min-height: 168px;
}
.emf-cell {
  display: flex;
  align-items: flex-end;
  height: 36px;
  padding: 4px;
  background: #102226;
  border: 1px solid rgba(198, 161, 91, 0.45);
}
.emf-cell-bars, .emf-bars { display: flex; align-items: flex-end; gap: 3px; width: 100%; }
.emf-cell-bars i, .emf-bars i {
  display: block;
  flex: 1;
  background: var(--emf-glass-ink);
  min-height: 4px;
}
.emf-chassis {
  position: relative;
  padding: 12px 12px 16px;
  background: linear-gradient(160deg, #e4d2a8 0%, var(--emf-brass) 42%, var(--emf-brass-deep) 100%);
  border-radius: 28px;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.25);
}
.emf-chassis[data-platform="android"] { border-radius: 14px; }
.emf-glass {
  position: relative;
  min-height: 280px;
  padding: 28px 14px 16px;
  background: var(--emf-glass);
  color: var(--emf-glass-ink);
  border-radius: 20px;
  overflow: hidden;
}
.emf-chassis[data-platform="android"] .emf-glass { border-radius: 8px; }
.emf-notch {
  position: absolute;
  top: 8px;
  left: 50%;
  width: 72px;
  height: 12px;
  margin-left: -36px;
  background: #1c2422;
  border-radius: 0 0 8px 8px;
}
.emf-punch {
  position: absolute;
  top: 10px;
  right: 14px;
  width: 10px;
  height: 10px;
  background: #1c2422;
  border-radius: 50%;
}
.emf-screen-name {
  display: block;
  margin-top: 8px;
  font-family: "Iowan Old Style", Palatino, serif;
  font-size: 1.15rem;
}
.emf-bars { height: 120px; margin-top: 28px; }
.emf-bars i { background: #1e6b5a; }
.emf-local {
  display: block;
  margin-top: 16px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.72rem;
  letter-spacing: 0.04em;
}
.emf-lamp {
  position: absolute;
  right: 16px;
  bottom: 8px;
  width: 8px;
  height: 8px;
  background: var(--emf-lamp);
  border-radius: 50%;
  box-shadow: 0 0 8px var(--emf-lamp);
}
.emf-index {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.8rem;
  color: var(--emf-brass);
}
.emf-switch { display: flex; gap: 8px; margin-bottom: 14px; }
.emf-switch button, .emf-upload button, .emf-upload input {
  font: inherit;
  color: inherit;
}
.emf-switch button {
  min-width: 88px;
  padding: 8px 12px;
  background: transparent;
  border: 1px solid rgba(198, 161, 91, 0.7);
  color: var(--emf-ink);
  cursor: pointer;
}
.emf-switch button[aria-checked="true"] {
  background: var(--emf-brass);
  color: #1b1408;
  border-color: var(--emf-brass);
}
.emf-upload { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 12px; }
.emf-upload label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; }
.emf-upload input {
  width: 180px;
  padding: 8px 10px;
  background: #102226;
  border: 1px solid rgba(198, 161, 91, 0.55);
  border-radius: 0;
}
.emf-upload button {
  padding: 8px 12px;
  background: var(--emf-lamp);
  color: #1b1408;
  border: 0;
  cursor: pointer;
  font-weight: 600;
}
.emf button:focus-visible, .emf input:focus-visible {
  outline: 2px solid var(--emf-lamp);
  outline-offset: 2px;
}
.emf [role="alert"] { color: #ffd0c6; }
.emf-honest { color: #c5d5ce; }
@media (max-width: 560px) {
  .emf { padding: 16px; }
  .emf-stage { grid-template-columns: 1fr; }
  .emf-strip { flex-direction: row; min-height: 0; }
  .emf-cell { width: 36px; height: 48px; }
  .emf-chassis { width: min(220px, 100%); }
}
@media (prefers-reduced-motion: reduce) {
  .emf-lamp { box-shadow: none; }
  .emf-bars i, .emf-cell-bars i { transition: none; }
}
`;
