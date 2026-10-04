"use client";

/**
 * Waive… (factory spec §4.2.1): open the PR past the checks a build failed. One checkbox per
 * failing check, a check that can never be waived shown disabled with its reason, a required
 * `Reason` (recorded), then `Waive and open PR`. The merge still needs the operator.
 */
import { SpinnerGap } from "@phosphor-icons/react";
import React, { useEffect, useId, useRef, useState } from "react";

import type { FailingGate } from "../../lib/agents-live-board";
import { callAction } from "../../lib/call-action";
import { notifyChanged } from "../changed";
import { showToast } from "../client-forms";
import { waiveChecksAction } from "./floor-actions";

export type WaiveSheetProps = {
  runId: string;
  app: string;
  failing: FailingGate[];
  projectId: string;
  actor: string;
  onClose: () => void;
  onDone?: () => void;
};

export function WaiveSheet({ runId, app, failing, projectId, actor, onClose, onDone }: WaiveSheetProps) {
  const titleId = useId();
  const box = useRef<HTMLFormElement>(null);
  const waivable = failing.filter((g) => g.waivable);
  const [picked, setPicked] = useState<Set<string>>(() => new Set(waivable.map((g) => g.name)));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    box.current?.querySelector<HTMLElement>("input:not([disabled]), textarea")?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const ready = picked.size > 0 && reason.trim().length > 0 && !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    form.set("run_id", runId);
    form.set("project_id", projectId);
    form.set("actor", actor);
    form.set("reason", reason.trim());
    for (const g of picked) form.append("gate", g);
    try {
      const r = await callAction(waiveChecksAction)(form);
      if (r.ok) {
        showToast("success", `${picked.size} check${picked.size === 1 ? "" : "s"} waived · opening PR`);
        notifyChanged("decisions");
        onDone?.();
        onClose();
      } else setError(r.error ?? "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fx-sheet-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <form ref={box} className="fx-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId}
            data-testid="fx-waive-sheet" onSubmit={(e) => void submit(e)}>
        <header className="fx-sheet-head">
          <h2 id={titleId}>Waive checks</h2>
          <span className="fx-sheet-sub ag-trunc" data-user-text title={app}>{app}</span>
          <button type="button" className="ag-act-close" aria-label="Close" onClick={onClose}>×</button>
        </header>
        <fieldset className="fx-waive-gates">
          <legend className="ag-sr">Failing checks</legend>
          {failing.map((g) => (
            <label key={g.name} className="fx-waive-gate" data-disabled={!g.waivable || undefined}
                   title={!g.waivable ? (g.reason || "Can't be waived") : undefined}>
              <input type="checkbox" name="gate" value={g.name} disabled={!g.waivable}
                     checked={g.waivable && picked.has(g.name)} data-testid={`fx-waive-${g.name}`}
                     onChange={(e) => setPicked((p) => {
                       const n = new Set(p);
                       if (e.target.checked) n.add(g.name); else n.delete(g.name);
                       return n;
                     })} />
              <span>{g.label}</span>
            </label>
          ))}
        </fieldset>
        <label className="fx-field">
          <span>Reason</span>
          <textarea name="reason" required rows={2} value={reason} data-testid="fx-waive-reason"
                    onChange={(e) => setReason(e.target.value)} />
        </label>
        {error ? <span className="ag-att-error" role="alert" data-user-text>{error}</span> : null}
        <div className="fx-sheet-actions">
          <button type="submit" className="ag-btn ag-btn-primary" disabled={!ready} aria-busy={busy || undefined}
                  title="Open the PR anyway. The merge still needs you." data-testid="fx-waive-submit">
            {busy ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}
            Waive and open PR
          </button>
          <button type="button" className="ag-link" onClick={onClose}>Cancel</button>
        </div>
      </form>
    </div>
  );
}
