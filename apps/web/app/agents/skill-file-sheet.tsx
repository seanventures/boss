"use client";

/**
 * `New skill from file` (agent-loadout spec §4.6): a SKILL.md-style playbook becomes a `guide`
 * skill in the Library and is given to this agent in the same step. Unlike instructions, a guide
 * is not in every prompt: the agent always sees `What it does` and reads the body only when it
 * calls it. It can't act — the risk is always `Reads`.
 */
import { GatedButton } from "../../components/gated";
import { X } from "@phosphor-icons/react";
import React, { useState } from "react";

import { frontmatter, instructionsApi, type Refused } from "../../lib/agent-instructions";
import { ModalShell } from "../skills/overlay";

export const SKILL_FILE_TIPS = {
  does: "Shown to the agent; decides when it reads it",
  reads: "A guide only explains; it can't act",
};

/** The label a SKILL.md gives itself: its frontmatter name, else the file's own name. */
export function skillLabelOf(fileName: string, text: string): string {
  const named = frontmatter(text)?.name;
  const stem = fileName.replace(/^.*\//, "").replace(/\.(md|markdown|txt)$/i, "");
  const raw = (named || (stem.toLowerCase() === "skill" ? "" : stem)).replace(/[-_]+/g, " ").trim();
  return raw ? raw[0].toUpperCase() + raw.slice(1) : "";
}

export function SkillFileSheet({ projectId, actor, roleKey, agentName, fileName, text, onClose, onCreated }: {
  projectId: string; actor: string; roleKey: string; agentName: string; fileName: string; text: string;
  onClose: () => void; onCreated: (label: string, version: number | null) => void;
}) {
  const [label, setLabel] = useState(() => skillLabelOf(fileName, text));
  const [summary, setSummary] = useState(() => frontmatter(text)?.description ?? "");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<Refused | null>(null);
  const body = text.replace(/^﻿?---[ \t]*\n[\s\S]*?\n---[ \t]*(?:\n|$)/, "");
  const lines = body.split("\n");

  const create = async () => {
    setBusy(true);
    const r = await instructionsApi.skillFromFile(projectId, actor, {
      filename: fileName, text, label: label.trim(), summary: summary.trim(), grantTo: roleKey });
    setBusy(false);
    if (!r.ok) { setRefused(r); return; }
    onCreated(r.data.label || label, r.data.version);
  };

  return (
    <ModalShell label="New skill from file" testId="skill-file-sheet" onClose={onClose} wide over>
      <div className="kit-modal-head">
        <h3>New skill from file</h3>
        <button type="button" className="ag-icon-btn" aria-label="Close" data-tip="Close" onClick={onClose}><X size={14} aria-hidden /></button>
      </div>
      <label className="kit-field">
        <span className="kit-field-label">Name</span>
        <input value={label} maxLength={120} data-autofocus data-testid="skill-file-name"
               onChange={(e) => { setLabel(e.target.value); setRefused(null); }} />
      </label>
      <label className="kit-field">
        <span className="kit-field-label" data-tip={SKILL_FILE_TIPS.does}>What it does</span>
        <input value={summary} maxLength={400} data-testid="skill-file-summary"
               onChange={(e) => { setSummary(e.target.value); setRefused(null); }} />
      </label>
      <div className="kit-field">
        <span className="kit-field-label">Text</span>
        <pre className="ld-file-preview" data-user-text data-testid="skill-file-preview">{lines.slice(0, 14).join("\n")}</pre>
      </div>
      <span className="ld-sheet-facts">
        <span className="kit-chip" data-tip={SKILL_FILE_TIPS.reads} data-testid="skill-file-risk">Reads</span>
        <span className="kit-faint">{fileName}</span>
      </span>
      {refused ? (
        <span className="ld-error" role="alert" data-tip={refused.tip || undefined} data-testid="skill-file-error">{refused.error}</span>
      ) : null}
      <div className="kit-modal-foot">
        <button type="button" className="kit-btn" onClick={onClose}>Cancel</button>
        <GatedButton permission="skill:manage" type="button" className="kit-btn kit-btn-primary" data-testid="skill-file-create"
                disabled={busy || !label.trim() || !summary.trim() || !body.trim()}
                data-tip={`Creates it in the Library and gives it to ${agentName}`}
                onClick={() => void create()}>{busy ? "Creating…" : "Create & give"}</GatedButton>
      </div>
    </ModalShell>
  );
}
