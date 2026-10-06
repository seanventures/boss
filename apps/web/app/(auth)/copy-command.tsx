"use client";

import React, { useState } from "react";

/**
 * A one-line command with a Copy button.
 *
 * The SSH form is longer than the sign-in card is wide. Scrolling a code box to read a command
 * you only ever paste is busywork, so the command is copied whole; it still selects in one
 * click where the clipboard is unavailable (plain http to a non-localhost address).
 */
export function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="idn-cmd">
      <pre data-copy="data"><code>{command}</code></pre>
      <button type="button" className="idn-btn sm" aria-label={`Copy: ${command}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(command);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          } catch { /* no clipboard here; the text still selects in one click */ }
        }}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
