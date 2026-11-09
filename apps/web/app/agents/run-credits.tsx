"use client";

/**
 * Run Credits: the agents that made this run, in the order they worked (spec §3.6, §11.1).
 *
 * `Made by`, a face and a role-in-run line each (from the server's `credits`), the totals, and
 * the person accountable. Download it as a PNG, copy the image, or copy the text. It opens by
 * itself once, when a run you are watching finishes; never again for that run in this tab.
 */
import { Copy, DownloadSimple, Image as ImageIcon, X } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useRef, useState } from "react";

import type { AvatarVariant, Chain } from "../../lib/agent-world-types";
import { creditWho, INCLUDED_TIP, minutesLabel, spendLabel } from "./chain-model";
import { AiBadge, nameOf, RunFace } from "./run-face";
import { useIdentityVersion } from "../../lib/use-agent-identities";

export type CreditRow = { roleKey: string | null; variant: AvatarVariant; name: string; role: string; line: string };

/** Who made it, in chain order. People are credited too: "You" only when it was the viewer. */
export function creditRows(chain: Pick<Chain, "credits" | "acts"> & Partial<Pick<Chain, "accountable" | "people">>, viewer: string): CreditRow[] {
  const rows = chain.credits.length
    ? chain.credits
    : [...new Set(chain.acts.map((a) => a.roleKey).filter((k): k is string => !!k))].map((roleKey) => ({ roleKey, line: "" }));
  return rows.map((c) => {
    const who = creditWho(c, chain.accountable ?? "", viewer, chain.people);
    // By name when BOSS knows it; never the raw principal id on a credit someone will share.
    const n = nameOf(who.roleKey, who.variant, undefined, who.name);
    return { roleKey: c.roleKey, variant: who.variant, name: n.name, role: n.role, line: c.line };
  });
}

/** The credits as plain text, for a PR description or a chat. */
export function creditsText(chain: Chain, viewer: string, title: string): string {
  const rows = creditRows(chain, viewer);
  const agents = rows.filter((r) => r.roleKey).length;
  return [
    `${title}, made by ${agents} ${agents === 1 ? "agent" : "agents"}`,
    ...rows.map((r) => `• ${r.name}${r.role !== r.name ? ` (${r.role})` : ""}${r.line ? `, ${r.line}` : ""}`),
    `${chain.totals.steps} steps, ${minutesLabel(chain.totals.minutes)}, ${spendLabel(chain.totals.cost, chain.totals.included)}`,
    `Accountable: ${chain.accountable === viewer ? viewer || "you" : chain.people?.[chain.accountable] || chain.accountable}`,
    "Built with BOSS, open source",
  ].join("\n");
}

/** Where the PNG comes from: the route that draws it, behind the console's own session. */
export function creditsPngHref(runId: string, projectId: string, title = ""): string {
  const q = new URLSearchParams({ project_id: projectId, ...(title ? { title: title.slice(0, 80) } : {}) });
  return `/api/run-credits/${encodeURIComponent(runId)}?${q.toString()}`;
}

async function creditsPng(runId: string, projectId: string, title = ""): Promise<Blob | null> {
  try {
    const r = await fetch(creditsPngHref(runId, projectId, title), { cache: "no-store" });
    if (!r.ok) return null;
    const blob = await r.blob();
    return blob.size ? blob : null;
  } catch {
    return null;
  }
}

/** Save a PNG as a file the browser downloads. */
export async function downloadCredits(runId: string, projectId: string, title = ""): Promise<boolean> {
  const blob = await creditsPng(runId, projectId, title);
  if (!blob) return false;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `boss-credits-${runId}.png`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return true;
}

const SEEN = "boss.credits.shown.";

function shownBefore(runId: string): boolean {
  try { return window.sessionStorage.getItem(SEEN + runId) === "1"; } catch { return false; }
}
function markShown(runId: string): void {
  try { window.sessionStorage.setItem(SEEN + runId, "1"); } catch { /* private mode: it may show again */ }
}

export function CreditsDialog({ chain, runId, projectId, viewer, title, onClose }: {
  chain: Chain; runId: string; projectId: string; viewer: string; title: string; onClose(): void;
}) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const [said, setSaid] = useState("");
  const [busy, setBusy] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const rows = creditRows(chain, viewer);
  const agents = rows.filter((r) => r.roleKey).length;
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  const act = useCallback(async (what: "png" | "image" | "text") => {
    setBusy(true);
    setSaid("");
    try {
      if (what === "png") {
        setSaid(await downloadCredits(runId, projectId, title) ? "Downloaded" : "Image unavailable");
      } else if (what === "image") {
        const blob = await creditsPng(runId, projectId, title);
        const Item = (globalThis as { ClipboardItem?: new (d: Record<string, Blob>) => unknown }).ClipboardItem;
        if (blob && Item && navigator.clipboard?.write) {
          await navigator.clipboard.write([new Item({ "image/png": blob }) as ClipboardItem]);
          setSaid("Image copied");
        } else {
          setSaid("Copy not supported");
        }
      } else {
        await navigator.clipboard.writeText(creditsText(chain, viewer, title));
        setSaid("Copied");
      }
    } catch {
      setSaid("Copy blocked");
    } finally {
      setBusy(false);
    }
  }, [chain, viewer, title, runId, projectId]);
  return (
    <div className="ag-credits-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="ag-credits" role="dialog" aria-modal="true" aria-label="Run credits" data-testid="ag-credits">
        <header className="ag-credits-head">
          <span className="ag-credits-title">
            <span className="area-name">Made by</span>
            <strong data-user-text>{title}</strong>
          </span>
          <button type="button" ref={closeRef} className="ag-credits-close" onClick={onClose}
                  aria-label="Close" data-tip="Close"><X size={14} weight="bold" aria-hidden /></button>
        </header>
        <ol className="ag-credits-cast" data-testid="ag-credits-cast">
          {rows.map((r, i) => (
            <li key={`${r.roleKey ?? "person"}-${i}`} className="ag-credits-row" data-role-key={r.roleKey ?? ""}
                style={{ ["--ag-i" as string]: i }}>
              <RunFace roleKey={r.roleKey} variant={r.variant} size={48} state="done" />
              <span className="ag-credits-who">
                <span className="ag-credits-name">
                  <strong>{r.name}</strong>
                  {r.roleKey ? <AiBadge /> : null}
                </span>
                {r.role !== r.name ? <span className="ag-credits-role">{r.role}</span> : null}
                {r.line ? <span className="ag-credits-line" data-tip={r.line}>{r.line}</span> : null}
              </span>
            </li>
          ))}
        </ol>
        <dl className="ag-credits-totals">
          <div><dt>Agents</dt><dd data-testid="ag-credits-agents">{agents}</dd></div>
          <div><dt>Steps</dt><dd>{chain.totals.steps}</dd></div>
          <div><dt>Time</dt><dd>{minutesLabel(chain.totals.minutes)}</dd></div>
          <div><dt>Cost</dt><dd data-testid="ag-credits-cost" data-tip={chain.totals.included ? INCLUDED_TIP : undefined}>{spendLabel(chain.totals.cost, chain.totals.included)}</dd></div>
          <div><dt>Accountable</dt><dd data-testid="ag-credits-accountable">
            {chain.accountable && chain.accountable === viewer ? "You" : chain.people?.[chain.accountable] || chain.accountable || "—"}
          </dd></div>
        </dl>
        <footer className="ag-credits-actions">
          <button type="button" className="cap-btn primary" disabled={busy} data-testid="ag-credits-png"
                  onClick={() => void act("png")}>
            <DownloadSimple size={14} weight="bold" aria-hidden /> Download PNG
          </button>
          <button type="button" className="cap-btn" disabled={busy} data-testid="ag-credits-copy-image"
                  onClick={() => void act("image")}>
            <ImageIcon size={14} weight="bold" aria-hidden /> Copy image
          </button>
          <button type="button" className="cap-btn" disabled={busy} data-testid="ag-credits-copy-text"
                  onClick={() => void act("text")}>
            <Copy size={14} weight="bold" aria-hidden /> Copy text
          </button>
          <span className="ag-credits-said" role="status" data-testid="ag-credits-said">{said}</span>
        </footer>
      </div>
    </div>
  );
}

/**
 * The run header's `Credits` button. Hidden until the run has signed work to credit. Opens by
 * itself once when a run you are watching goes from live to finished.
 */
export function CreditsButton({ chain, runId, projectId, viewer, title, live, autoOpen = true, className }: {
  chain: Chain | null; runId: string; projectId: string; viewer: string; title: string; live: boolean;
  /** Open by itself at the finish (the run view). The Factory cockpit never pops it. */
  autoOpen?: boolean;
  /** Its look where a tag would read as disabled (the cockpit header: a real button). */
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  // Seen live in this view. The chain's last read can land after the run's status flips, so the
  // finish is "was live, is not, and has work to credit" whenever the last of those arrives.
  const sawLive = useRef(live);
  const hasWork = !!chain && chain.acts.some((a) => a.roleKey);
  useEffect(() => {
    if (live) { sawLive.current = true; return; }
    if (autoOpen && sawLive.current && hasWork) {
      sawLive.current = false;
      if (!shownBefore(runId)) { markShown(runId); setOpen(true); }
    }
  }, [live, hasWork, runId, autoOpen]);
  const close = useCallback(() => setOpen(false), []);
  if (!chain || !hasWork) return null;
  return (
    <>
      <button type="button" className={className ?? "wf-tag ag-credits-btn"} data-testid="ag-credits-open"
              data-tip="Who made this run" aria-haspopup="dialog" aria-expanded={open}
              onClick={() => { markShown(runId); setOpen(true); }}>
        Credits
      </button>
      {open ? (
        <CreditsDialog chain={chain} runId={runId} projectId={projectId} viewer={viewer} title={title} onClose={close} />
      ) : null}
    </>
  );
}
