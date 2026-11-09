"use client";

/**
 * `Add instructions` (agent-loadout spec §3.3): one sheet for `Upload file` and `Paste text`,
 * and for `Replace file` on an existing document.
 *
 * Every change of name, text or modes runs a DRY RUN (debounced, not a press): the API lints
 * and measures it and stores nothing, so the estimate, the warnings and any refusal are the
 * server's own, `Add` stays disabled until one passes. A refusal is one red line of ≤ 6 words
 * with the full reason (the line, the phrase) in its tooltip; a secret is never echoed back.
 */
import { GatedButton } from "../../components/gated";
import { X } from "@phosphor-icons/react";
import React, { useEffect, useMemo, useRef, useState } from "react";

import {
  instructionsApi, kTokens, looksLikeSkill, LIMITS, MODE_LABEL, MODES, tokensOf,
  type DryRun, type InstructionItem, type InstructionMode, type Refused, type Saved,
} from "../../lib/agent-instructions";
import { ModalShell } from "../skills/overlay";

export const SHEET_TIPS = {
  usedFor: "Where it is added to the prompt",
  makeSkill: "Reusable by any agent; read only when needed",
  upload: ".md or .txt, up to 16k characters. No secrets.",
  paste: "Write or paste a checklist or house rules",
};

export type SheetInput =
  | { kind: "upload"; fileName: string; text: string }
  | { kind: "paste" }
  | { kind: "replace"; item: InstructionItem; fileName: string; text: string };

const PREVIEW_LINES = 14;

export function InstructionSheet({ projectId, actor, roleKey, input, taken, onClose, onSaved, onMakeSkill }: {
  projectId: string; actor: string; roleKey: string; input: SheetInput;
  /** Names already used by this agent (lower-cased), so a clash is said before a request. */
  taken: string[];
  onClose: () => void;
  onSaved: (saved: Saved, replaced: boolean) => void;
  onMakeSkill: (text: string, fileName: string) => void;
}) {
  const replacing = input.kind === "replace" ? input.item : null;
  const [name, setName] = useState(input.kind === "upload" ? input.fileName : replacing ? replacing.name : "Instructions");
  const [text, setText] = useState(input.kind === "paste" ? "" : input.text);
  const [modes, setModes] = useState<InstructionMode[]>(replacing ? replacing.modes : [...MODES]);
  const [showAll, setShowAll] = useState(false);
  const [check, setCheck] = useState<DryRun | null>(null);
  const [refused, setRefused] = useState<Refused | null>(null);
  const [checking, setChecking] = useState(false);
  const [adding, setAdding] = useState(false);
  const seq = useRef(0);

  const clash = !replacing && taken.includes(name.trim().toLowerCase());
  const empty = !text.trim() || !name.trim() || modes.length === 0;

  useEffect(() => {
    if (empty || clash) { setCheck(null); setRefused(null); return; }
    const mine = ++seq.current;
    setChecking(true);
    const t = setTimeout(async () => {
      const r = replacing
        ? await instructionsApi.dryRunUpdate(projectId, actor, roleKey, replacing.id, { body: text })
        : await instructionsApi.dryRun(projectId, actor, roleKey, { name: name.trim(), body: text, modes, source: input.kind === "upload" ? "file" : "pasted" });
      if (mine !== seq.current) return;
      setChecking(false);
      if (r.ok) { setCheck(r.data); setRefused(null); } else { setCheck(null); setRefused(r); }
    }, 350);
    return () => clearTimeout(t);
  }, [name, text, modes, empty, clash, replacing, projectId, actor, roleKey, input.kind]);

  const skillish = useMemo(() => looksLikeSkill(text), [text]);
  const after = check ? Math.max(...MODES.map((m) => check.budgetAfter[m].usedChars)) : null;
  const limitTokens = kTokens(tokensOf(LIMITS.budgetChars));

  const add = async () => {
    if (!check || adding) return;
    setAdding(true);
    const r = replacing
      ? await instructionsApi.update(projectId, actor, roleKey, replacing.id, { body: text })
      : await instructionsApi.add(projectId, actor, roleKey, { name: name.trim(), body: text, modes, source: input.kind === "upload" ? "file" : "pasted" });
    setAdding(false);
    if (!r.ok) { setRefused(r); setCheck(null); return; }
    onSaved(r.data, !!replacing);
  };

  const lines = text.split("\n");
  const fileName = input.kind === "paste" ? `${name.trim() || "instructions"}.md` : input.fileName;
  const error = clash ? { error: "Name taken", tip: "Another of its instructions has this name" } as Pick<Refused, "error" | "tip"> : refused;

  return (
    <ModalShell label={replacing ? "Replace file" : "Add instructions"} testId="instruction-sheet" onClose={onClose} wide over>
      <div className="kit-modal-head">
        <h3>{replacing ? "Replace file" : "Add instructions"}</h3>
        <button type="button" className="ag-icon-btn" aria-label="Close" data-tip="Close" onClick={onClose}><X size={14} aria-hidden /></button>
      </div>
      <label className="kit-field">
        <span className="kit-field-label">Name</span>
        <input value={name} maxLength={60} disabled={!!replacing} data-testid="instruction-name"
               onChange={(e) => setName(e.target.value)} />
      </label>
      <div className="kit-field">
        <span className="kit-field-label">Text</span>
        {input.kind === "paste" ? (
          <textarea className="ld-text" rows={10} value={text} placeholder="- Always list untested branches first" data-autofocus
                    data-testid="instruction-text" onChange={(e) => setText(e.target.value)} />
        ) : (
          <>
            <pre className="ld-file-preview" data-user-text data-testid="instruction-file-preview">
              {(showAll ? lines : lines.slice(0, PREVIEW_LINES)).join("\n")}
            </pre>
            {lines.length > PREVIEW_LINES && !showAll ? (
              <button type="button" className="kit-link" data-testid="instruction-show-all" onClick={() => setShowAll(true)}>Show all</button>
            ) : null}
          </>
        )}
      </div>
      {!replacing ? (
        <div className="kit-field">
          <span className="kit-field-label" data-tip={SHEET_TIPS.usedFor}>Used for</span>
          <span className="kit-filters" role="group" aria-label="Used for">
            {MODES.map((m) => (
              <button key={m} type="button" className="kit-chip kit-chip-btn" aria-pressed={modes.includes(m)}
                      data-testid={`instruction-mode-${m}`}
                      onClick={() => setModes((cur) => (cur.includes(m) ? cur.filter((x) => x !== m) : MODES.filter((x) => x === m || cur.includes(x))))}>
                {MODE_LABEL[m]}
              </button>
            ))}
          </span>
        </div>
      ) : null}
      <div className="ld-sheet-facts" aria-live="polite">
        {check ? (
          <span data-testid="instruction-estimate">≈ {check.tokensEst.toLocaleString("en-US")} tokens · {kTokens(tokensOf(after ?? 0))} / {limitTokens} after</span>
        ) : checking && !empty ? <span className="kit-faint">Checking…</span> : null}
        {check?.warnings.map((w) => (
          <span key={w} className="kit-chip" data-tone="amber" data-testid="instruction-warning"
                data-tip={w.startsWith("Names ") ? `It holds no skill or tool called ${w.slice(6, w.indexOf(" ·"))}; it can't use it` : w}>
            {w.startsWith("Mentions a real person") ? "Mentions a real person" : w}
          </span>
        ))}
        {skillish ? (
          <span className="kit-chip" data-tone="accent" data-testid="instruction-looks-like-skill">Looks like a skill</span>
        ) : null}
        {skillish ? (
          <button type="button" className="kit-link" data-tip={SHEET_TIPS.makeSkill} data-testid="instruction-make-skill"
                  onClick={() => onMakeSkill(text, fileName)}>Make it a skill</button>
        ) : null}
      </div>
      {error ? (
        <span className="ld-error" role="alert" data-tip={error.tip || undefined} data-testid="instruction-error">{error.error}</span>
      ) : null}
      <div className="kit-modal-foot ld-foot">
        {!skillish && !replacing ? (
          <button type="button" className="kit-link ld-quiet" data-tip={SHEET_TIPS.makeSkill} data-testid="instruction-make-skill-instead"
                  disabled={!text.trim()} onClick={() => onMakeSkill(text, fileName)}>Make it a skill instead</button>
        ) : <span />}
        <span className="ld-foot-actions">
          <button type="button" className="kit-btn" onClick={onClose}>Cancel</button>
          <GatedButton permission="agent:update" type="button" className="kit-btn kit-btn-primary" disabled={!check || adding || !!error}
                  data-testid="instruction-add" onClick={() => void add()}>
            {adding ? (replacing ? "Saving…" : "Adding…") : replacing ? "Save" : "Add"}
          </GatedButton>
        </span>
      </div>
    </ModalShell>
  );
}
