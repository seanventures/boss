"use client";

import React, { useState } from "react";

/**
 * A one-line command with a Copy button.
 *
 * The SSH form is longer than the sign-in card is wide. Scrolling a code box to read a command
 * you only ever paste is busywork, so the command is copied whole; it still selects in one
 * click where the clipboard is unavailable (plain http to a non-localhost address).
 *
 * `label` says what is copied ("Copy command": a command, never mistaken for a link to open).
 * Never hand it a credential: the command is drawn as text and read aloud in the button's name.
 */
export function CopyCommand({ command, label = "Copy" }: { command: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="idn-cmd">
      <pre data-copy="data"><code>{command}</code></pre>
      <button type="button" className="idn-btn sm" aria-label={`${label}: ${command}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(command);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          } catch { /* no clipboard here; the text still selects in one click */ }
        }}>
        {copied ? "Copied" : label}
      </button>
    </div>
  );
}
