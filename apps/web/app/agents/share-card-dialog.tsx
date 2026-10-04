"use client";

/**
 * Share card (spec §3.12): the agent's passport as a 1200×630 PNG, to download or paste.
 *
 * The image is drawn by app/api/agent-card/[role] (next/og, behind auth). `Include stats` adds
 * the vitals and is off by default. `Copy card JSON` copies the A2A Agent Card the API serves —
 * the machine-readable twin of the picture.
 */
import React, { useEffect, useState } from "react";
import { Copy, DownloadSimple, Image as ImageIcon, X } from "@phosphor-icons/react";

import { getAgentCard, toast } from "../../lib/agent-world";

export function cardUrl(projectId: string, roleKey: string, stats: boolean, version: number): string {
  return `/api/agent-card/${encodeURIComponent(roleKey)}?project_id=${encodeURIComponent(projectId)}`
    + `&stats=${stats ? 1 : 0}&v=${version}`;
}

export function ShareCardDialog({ projectId, roleKey, name, version, onClose }: {
  projectId: string; roleKey: string; name: string; version: number; onClose: () => void;
}) {
  const [stats, setStats] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [broken, setBroken] = useState(false);
  const src = cardUrl(projectId, roleKey, stats, version);
  useEffect(() => { setLoaded(false); setBroken(false); }, [src]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const copyImage = async () => {
    try {
      const blob = await (await fetch(src, { cache: "no-store" })).blob();
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      toast("success", "Image copied");
    } catch { toast("error", "Clipboard unavailable"); }
  };
  const copyJson = async () => {
    const r = await getAgentCard(projectId, roleKey);
    if (!r.ok) { toast("error", r.error); return; }
    try { await navigator.clipboard.writeText(JSON.stringify(r.data, null, 2)); toast("success", "Copied card JSON"); }
    catch { toast("error", "Clipboard unavailable"); }
  };

  return (
    <div className="ag-share-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="ag-share" role="dialog" aria-modal="true" aria-label={`Share card: ${name}`}
               data-testid="ag-share-dialog">
        <header className="ag-share-head">
          <span>Share card</span>
          <label className="ag-check" title="Adds trust, runs and clean rate">
            <input type="checkbox" checked={stats} data-testid="ag-share-stats" onChange={(e) => setStats(e.target.checked)} />
            Include stats
          </label>
          <button type="button" className="ag-icon-btn" aria-label="Close" onClick={onClose}><X size={14} aria-hidden /></button>
        </header>
        <div className="ag-share-preview" data-loaded={loaded || undefined}>
          {broken ? <span className="ag-bad" role="alert">Card unavailable</span> : (
            <img src={src} alt={`${name} share card`} width={1200} height={630} data-testid="ag-share-img"
                 onLoad={() => setLoaded(true)} onError={() => setBroken(true)} />
          )}
        </div>
        <footer className="ag-share-actions">
          <a className="ag-btn ag-btn-primary" href={src} download={`${roleKey}-card.png`} data-testid="ag-share-download">
            <DownloadSimple size={13} aria-hidden /> Download PNG
          </a>
          <button type="button" className="ag-btn" onClick={() => void copyImage()}>
            <ImageIcon size={13} aria-hidden /> Copy image
          </button>
          <button type="button" className="ag-btn" onClick={() => void copyJson()}>
            <Copy size={13} aria-hidden /> Copy card JSON
          </button>
        </footer>
      </section>
    </div>
  );
}
