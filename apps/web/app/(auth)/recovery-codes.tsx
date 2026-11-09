"use client";

/**
 * RECOVERY CODES, SHOWN ONCE (IDENTITY.md §4.7, §9.7): ten codes, each single-use. Download,
 * print or copy them; Continue stays locked until the person says they stored them. The codes
 * live only in this component's memory and are gone when it unmounts, BOSS keeps hashes only.
 */
import React, { useState } from "react";
import { Copy, DownloadSimple, Printer, ShieldCheck } from "@phosphor-icons/react";

export function RecoveryCodesView({ codes, host, onContinue, continueLabel = "Continue" }: {
  codes: string[]; host: string; onContinue: () => void; continueLabel?: string;
}) {
  const [stored, setStored] = useState(false);
  const [copied, setCopied] = useState(false);
  const text = `BOSS recovery codes for ${host}\nEach works once.\n\n${codes.join("\n")}\n`;

  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "boss-recovery-codes.txt";
    a.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <div data-testid="recovery-codes">
      <ol className="idn-codes" aria-label="Recovery codes">
        {codes.map((c) => <li key={c} data-copy="data">{c}</li>)}
      </ol>
      <div className="idn-code-acts">
        <button type="button" className="idn-btn sm" onClick={download}><DownloadSimple size={14} aria-hidden /> Download</button>
        <button type="button" className="idn-btn sm" onClick={() => window.print()}><Printer size={14} aria-hidden /> Print</button>
        <button type="button" className="idn-btn sm" onClick={async () => {
          try { await navigator.clipboard.writeText(codes.join("\n")); setCopied(true); } catch { /* select the list instead */ }
        }}><Copy size={14} aria-hidden /> {copied ? "Copied" : "Copy"}</button>
      </div>
      <label className="idn-check idn-stored">
        <input type="checkbox" checked={stored} onChange={(e) => setStored(e.target.checked)} data-testid="codes-stored" />
        I've stored them somewhere safe
      </label>
      <button type="button" className="idn-big p" disabled={!stored} aria-disabled={!stored || undefined} onClick={onContinue} data-testid="codes-continue">
        <ShieldCheck size={18} aria-hidden /> {continueLabel}
      </button>
      <p className="idn-fine" data-copy="prose">BOSS can't show these again. Each code works once; below three left, you'll make new ones.</p>
    </div>
  );
}
