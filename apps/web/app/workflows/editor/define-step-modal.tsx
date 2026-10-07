"use client";

import { useGate } from "../../../components/gated";
import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "@phosphor-icons/react";

import { apiWrite } from "../../../lib/api-write";
import type { CustomStepDef } from "../../../lib/workflow-editor-types";
import type { Bindable, CustomStep } from "../../delivery-workflows-ui";
import { DefineStepForm } from "../define-step-form";

type Loaded = { bindable: Bindable; riskFloor: number };

async function loadBindable(projectId: string): Promise<Loaded> {
  const q = encodeURIComponent(projectId);
  const [mine, caps] = await Promise.all([
    fetch(`/api/delivery-workflows?view=custom_steps&project_id=${q}`, { cache: "no-store" }).then((r) => r.json()),
    fetch(`/api/delivery-workflows?view=capabilities&project_id=${q}`, { cache: "no-store" }).then((r) => r.json()),
  ]);
  if (mine?.error) throw new Error(String(mine.error));
  return {
    bindable: (mine?.bindable as Bindable) ?? { skills: [], mcp_servers: [] },
    riskFloor: Number(caps?.manual_approval_risk_floor ?? 2),
  };
}

/**
 * `+ New step` and the palette's pencil, in place: the step form in a dialog over the editor,
 * so defining a step never means leaving the workflow being drawn.
 *
 * The SAME form the Library's Steps view uses (`DefineStepForm`), so a step defined here and one
 * defined there meet the same rules. On success it closes with the step, which the editor
 * inserts at the selection.
 */
export function DefineStepModal({ projectId, edit, open, onClose, actor }: {
  projectId: string;
  edit?: CustomStepDef;
  open: boolean;
  onClose(created?: CustomStepDef): void;
  /** The signed-in member the definition is attributed to. */
  actor?: string;
}) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement;
    let live = true;
    setFailed("");
    loadBindable(projectId).then((l) => { if (live) setLoaded(l); },
                                 (e) => { if (live) setFailed(String((e as Error)?.message ?? e)); });
    return () => {
      live = false;
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, [open, projectId]);

  const defineGate = useGate("workflow:create");
  if (!open || typeof document === "undefined") return null;

  const define = async (fields: Record<string, unknown>): Promise<{ ok: boolean; error?: string }> => {
    if (!defineGate.allowed) return { ok: false, error: defineGate.reason };
    setBusy(String(fields.label ?? "step"));
    try {
      const r = edit
        ? await apiWrite("/api/delivery-workflows", {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...fields, step_key: edit.step_key, ...(actor ? { actor } : {}) }),
          })
        : await apiWrite("/api/delivery-workflows", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "define_step", ...fields, ...(actor ? { actor } : {}) }),
          });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) return { ok: false, error: String(body?.error ?? `API returned ${r.status}`) };
      onClose(body as CustomStepDef);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String((e as Error)?.message ?? e) };
    } finally {
      setBusy(null);
    }
  };

  return createPortal(
    <div className="wf-modal-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={dialog} className="wf-modal" role="dialog" aria-modal="true"
           aria-label={edit ? "Edit step" : "New step"} data-testid="wf-define-modal"
           onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } }}>
        <header className="wf-modal-head">
          <h2>{edit ? "Edit step" : "New step"}</h2>
          <button type="button" className="wf-icon-btn" aria-label="Close" onClick={() => onClose()}>
            <X size={16} weight="bold" />
          </button>
        </header>
        {failed ? <div className="wf-field-error" role="alert">{failed}</div> : null}
        {!loaded && !failed ? <div className="wf-faint">Loading…</div> : null}
        {loaded ? (
          <DefineStepForm bindable={loaded.bindable} riskFloor={loaded.riskFloor} busy={busy}
                          projectId={projectId} key={edit?.step_key ?? "new"}
                          editing={(edit as unknown as CustomStep | undefined) ?? null}
                          onCancel={() => onClose()} onDefine={define} />
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
