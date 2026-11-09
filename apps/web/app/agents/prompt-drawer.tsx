"use client";

/**
 * `See its prompt` (agent-loadout spec §3.3): exactly what the model is given, without the task,
 * by layer, Rules, Identity, Remit, Instructions, Voice, Memory, for Work, Chat or Panel.
 * Read-only, with Copy. It is how an operator checks that an upload landed, and where.
 */
import { X } from "@phosphor-icons/react";
import React, { useEffect, useState } from "react";

import { instructionsApi, MODES, type InstructionMode, type PromptBlock } from "../../lib/agent-instructions";
import { showToast } from "../client-forms";
import { SectionLabel } from "../section-label";
import { DrawerShell } from "../skills/overlay";

const LABEL: Record<InstructionMode, string> = { work: "Work", chat: "Chat", panel: "Panel" };

export function PromptDrawer({ projectId, actor, roleKey, onClose }: {
  projectId: string; actor: string; roleKey: string; onClose: () => void;
}) {
  const [mode, setMode] = useState<InstructionMode>("work");
  const [blocks, setBlocks] = useState<PromptBlock[] | null>(null);
  const [failed, setFailed] = useState("");
  useEffect(() => {
    let live = true;
    setBlocks(null);
    void instructionsApi.prompt(projectId, actor, roleKey, mode).then((r) => {
      if (!live) return;
      if (r.ok) { setBlocks(r.data); setFailed(""); } else setFailed(r.error);
    });
    return () => { live = false; };
  }, [projectId, actor, roleKey, mode]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText((blocks ?? []).map((b) => b.text).join("\n\n"));
      showToast("success", "Copied");
    } catch { showToast("error", "Copy failed"); }
  };
  return (
    <DrawerShell label="See its prompt" testId="prompt-drawer" onClose={onClose} float over>
      <div className="kit-drawer-head">
        <h3 className="kit-drawer-title">See its prompt</h3>
        <button type="button" className="ag-icon-btn" aria-label="Close" data-tip="Close" onClick={onClose}><X size={14} aria-hidden /></button>
        <span className="kit-filters ld-drawer-tools" role="group" aria-label="Mode">
          {MODES.map((m) => (
            <button key={m} type="button" className="kit-chip kit-chip-btn" aria-pressed={mode === m}
                    data-testid={`prompt-mode-${m}`} onClick={() => setMode(m)}>{LABEL[m]}</button>
          ))}
          <button type="button" className="kit-btn kit-btn-sm ld-copy" disabled={!blocks?.length} data-testid="prompt-copy"
                  onClick={() => void copy()}>Copy</button>
        </span>
      </div>
      <div className="kit-drawer-body ld-prompt" data-testid="prompt-blocks">
        {failed ? <span className="ld-error">{failed}</span> : null}
        {!blocks && !failed ? <span className="kit-faint">Reading…</span> : null}
        {(blocks ?? []).map((b) => (
          <div key={b.layer} className="ld-prompt-block" data-layer={b.layer} data-testid={`prompt-${b.layer}`}>
            <SectionLabel as="h4">{b.label}</SectionLabel>
            <pre data-user-text>{b.text}</pre>
          </div>
        ))}
      </div>
    </DrawerShell>
  );
}
