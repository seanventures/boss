"use client";

/**
 * Add skill (docs/design/SKILLS_AND_MCP_HOME.md): the four ways a skill arrives, on one screen.
 * Write one opens the skill drawer, which then gives it to agents (Agents) and tests it (Tests).
 * Import takes a file, YAML or a repo. Describe asks BOSS to draft it. Built in lists the skills
 * BOSS ships.
 */
import React from "react";
import { DownloadSimple, Package, PencilSimple, Sparkle, X } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import type { Permission } from "../../lib/identity/permissions.gen";
import { ModalShell } from "./overlay";

export type AddSkillWay = "write" | "import" | "describe" | "built_in";

const WAYS: { way: AddSkillWay; label: string; sub: string; icon: React.ReactNode; permission?: Permission }[] = [
  { way: "write", label: "Write one", sub: "Name it, say how it runs", icon: <PencilSimple size={18} weight="duotone" />, permission: "skill:manage" },
  { way: "import", label: "Import", sub: "A file, YAML or repo", icon: <DownloadSimple size={18} weight="duotone" />, permission: "skill:manage" },
  { way: "describe", label: "Describe", sub: "BOSS drafts it with you", icon: <Sparkle size={18} weight="duotone" /> },
  { way: "built_in", label: "Built in", sub: "Turn on one BOSS ships", icon: <Package size={18} weight="duotone" /> },
];

export function AddSkillSheet({ onClose, onPick }: { onClose: () => void; onPick: (way: AddSkillWay) => void }) {
  return (
    <ModalShell label="Add skill" testId="kit-add-skill" onClose={onClose}>
      <header className="kit-modal-head">
        <h3>Add skill</h3>
        <button type="button" className="kit-icon-btn" aria-label="Close" data-tip="Close" onClick={onClose}><X size={16} /></button>
      </header>
      <div className="kit-ways" role="list">
        {WAYS.map((w) => {
          const body = (
            <>
              <span className="kit-way-icon kit-icon" aria-hidden="true">{w.icon}</span>
              <span className="kit-way-text">
                <span className="kit-way-label">{w.label}</span>
                <span className="kit-faint">{w.sub}</span>
              </span>
            </>
          );
          return w.permission ? (
            <GatedButton key={w.way} permission={w.permission} type="button" role="listitem" className="kit-way"
                         data-testid={`kit-add-skill-${w.way}`} onClick={() => onPick(w.way)}>{body}</GatedButton>
          ) : (
            <button key={w.way} type="button" role="listitem" className="kit-way" data-testid={`kit-add-skill-${w.way}`}
                    onClick={() => onPick(w.way)}>{body}</button>
          );
        })}
      </div>
    </ModalShell>
  );
}
