"use client";

/**
 * Small pieces every Infra tab shares: durations as the rail says them (`1m 52s`), a short
 * sha, the copy button with its `Copied` toast, and a menu that closes on Esc or an outside
 * press. Tokens only; no inline style.
 */
import React, { useEffect, useRef, useState } from "react";

import { showToast } from "../client-forms";

export { ago, localTime, useNow, span } from "../agents/live-util";

/** `0m 41s`, `1m 52s`, `1h 03m`, a deploy's own length. */
export function dur(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

/** Copy text; the toast says `Copied` (§5.1). */
export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard?.writeText(text);
    showToast("success", "Copied");
  } catch {
    showToast("error", "Couldn't copy");
  }
}

export function CopyButton({ text, label = "Copy", testId, tip }: { text: string; label?: string; testId?: string; tip?: string }) {
  return (
    <button type="button" className="ag-btn" data-testid={testId} title={tip} onClick={() => void copyText(text)}>
      {label}
    </button>
  );
}

/** A `▾` menu: a button that opens a list; Esc and an outside press close it. */
export function Menu({ label, testId, items, tip, primary }: {
  label: React.ReactNode; testId: string; tip?: string; primary?: boolean;
  items: { key: string; label: string; testId: string; tip?: string; onPick: () => void; hidden?: boolean }[];
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const shown = items.filter((i) => !i.hidden);
  return (
    <span className="inf-pop" ref={box}>
      <button type="button" className={primary ? "ag-btn ag-btn-primary" : "ag-btn"} aria-haspopup="menu" aria-expanded={open}
              data-testid={testId} title={tip} onClick={() => setOpen((v) => !v)}>
        {label}
      </button>
      {open ? (
        <span className="inf-menu" role="menu">
          {shown.map((i) => (
            <button key={i.key} type="button" role="menuitem" data-testid={i.testId} title={i.tip}
                    onClick={() => { setOpen(false); i.onPick(); }}>
              {i.label}
            </button>
          ))}
        </span>
      ) : null}
    </span>
  );
}

/** An inline error line with the API's sentence; a long one keeps its words in the tooltip. */
/**
 * AN ERROR IN WORDS. The API's refusals are written for whoever calls it, so they carry the
 * ids and source files a developer needs ("ChangeSet chg_49e7… is a host deployment: approve
 * its decision dec_… (services/governed_host_deploy.py)"). A person reads none of that: ids,
 * paths and exception class names go, the sentence stays.
 */
export function inWords(error: string): string {
  return error
    .replace(/\s*\((?:[\w.-]+\/)+[\w.-]+\.(?:py|ts|tsx|js)\)/g, "")
    .replace(/(?:[\w.-]+\/)+[\w.-]+\.(?:py|ts|tsx|js)\b/g, "")
    .replace(/^(?:[A-Z]\w*(?:Error|Exception)):\s*/, "")
    .replace(/\bChangeSet\s+[a-z]+_[0-9a-f]{6,}\b/gi, "This change")
    .replace(/\b(?:decision|run|deployment|job|target|host)\s+[a-z]+_[0-9a-f]{6,}\b/gi, (m) => m.split(/\s+/)[0])
    .replace(/\s*\b[a-z]{2,8}_[0-9a-f]{8,}\b/gi, "")
    .replace(/\s{2,}/g, " ")
    .replace(/\s+([.,:;])/g, "$1")
    .trim();
}

export function ErrorLine({ error, testId }: { error: string | null; testId?: string }) {
  if (!error) return null;
  const said = inWords(error);
  return <span className="inf-error" role="alert" data-testid={testId} title={said} data-copy="data">{said}</span>;
}
