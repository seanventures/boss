"use client";

import React, { useEffect, useState, type RefObject } from "react";

import { Popover } from "../inspector/popover";

export type CopyChoice = { mode: "update" } | { mode: "new"; name: string };

/**
 * Saving a standard: the one question it needs.
 *
 *  - No copy yet: the copy's name, prefilled `<name> (mine)`.
 *  - A copy exists: `Update “<copy>”` (the default) or `New copy`. Defaulting to the copy you
 *    already have is what stops a second Save making a second "(copy)" definition.
 */
export function SaveCopyPopover({ anchor, open, onClose, standardName, copy, busy, nameError, onSave }: {
  anchor: RefObject<HTMLElement | null>;
  open: boolean;
  onClose(): void;
  standardName: string;
  copy: { name: string } | null | undefined;
  busy: boolean;
  nameError: string;
  onSave(choice: CopyChoice): void;
}) {
  const [mode, setMode] = useState<"update" | "new">(copy ? "update" : "new");
  const [name, setName] = useState(`${standardName} (mine)`);
  useEffect(() => {
    if (!open) return;
    setMode(copy ? "update" : "new");
    setName(`${standardName} (mine)`);
  }, [open, copy, standardName]);

  const submit = () => {
    if (busy) return;
    if (mode === "update") onSave({ mode: "update" });
    else if (name.trim()) onSave({ mode: "new", name: name.trim() });
  };

  return (
    <Popover anchor={anchor} open={open} onClose={onClose} label="Save as my copy" width={300}
             testId="wf-save-copy">
      <form className="wf-pop-form" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {copy ? (
          <div role="radiogroup" aria-label="Save as" className="wf-radios">
            <label className="wf-check">
              <input type="radio" name="wf-copy" checked={mode === "update"} data-testid="wf-copy-update"
                     onChange={() => setMode("update")} />
              <span>Update “{copy.name}”</span>
            </label>
            <label className="wf-check">
              <input type="radio" name="wf-copy" checked={mode === "new"} data-testid="wf-copy-new"
                     onChange={() => setMode("new")} />
              <span>New copy</span>
            </label>
          </div>
        ) : null}
        {mode === "new" ? (
          <label className="wf-field">
            <span className="wf-label">Name</span>
            <input className="wf-input" autoFocus value={name} maxLength={120}
                   aria-invalid={nameError ? true : undefined} data-testid="wf-copy-name"
                   onFocus={(e) => e.currentTarget.select()}
                   onChange={(e) => setName(e.target.value)} />
            {nameError ? <span className="wf-field-error" role="alert">{nameError}</span> : null}
          </label>
        ) : null}
        <div className="wf-pop-actions">
          <button type="submit" className="wf-btn wf-btn-primary" data-testid="wf-copy-save"
                  disabled={busy || (mode === "new" && !name.trim())}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </Popover>
  );
}
