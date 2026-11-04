"use client";

/**
 * NEW PROJECT, AS A SHEET (BOSSAPP-768 round 2). The form lived inside the project menu's narrow
 * dropdown: the type's text was cut ("…existing cod"), it scrolled sideways, its buttons were the
 * browser's grey ones, and cancelling the step-up passkey closed the menu and lost the typed name.
 *
 * Now a centred sheet in the console's look, outside the menu: nothing a passkey prompt does can
 * close it, and what was typed is the sheet's own state (never a form the browser resets), kept
 * through a cancelled or refused step-up until Create succeeds or the person presses Cancel.
 * The same action (POST /v1/projects via createProjectAction) and permission (project:create).
 */
import React, { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { ProjectType } from "@boss/api-client";

import { GatedButton } from "../components/gated";
import { callAction } from "../lib/call-action";
import { showToast } from "./client-forms";
import { createProjectAction } from "./project-actions";

export type NewProjectDraft = { name: string; description: string; type: ProjectType };
export const EMPTY_DRAFT: NewProjectDraft = { name: "", description: "", type: "app" };

export const PROJECT_TYPES: { key: ProjectType; label: string; line: string }[] = [
  { key: "app", label: "Build an app",
    line: "Agents plan, build, test and ship an app through gated pull requests." },
  { key: "knowledge_graph", label: "Knowledge Graph for an existing codebase",
    line: "Import a repository, map its code, and have agents find its business rules and learn as they work." },
];

export function NewProjectSheet({ draft, onDraft, onClose, onCreated, onBack }: {
  /** Held by the caller, so closing the menu around it (a passkey prompt) never loses it. */
  draft: NewProjectDraft;
  onDraft: (next: NewProjectDraft) => void;
  onClose: () => void;
  onCreated: (projectId: string, type: ProjectType) => void;
  /** Back to New project's three doors (BOSSAPP-810), when this form was opened from them. */
  onBack?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleId = useId();
  const nameRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => { nameRef.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  const kg = draft.type === "knowledge_graph";
  const submit = async () => {
    if (!draft.name.trim()) { setError("Give the project a name, then press Create."); return; }
    setBusy(true); setError(null);
    const form = new FormData();
    form.set("name", draft.name);
    form.set("description", draft.description);
    form.set("project_type", draft.type);
    try {
      const r = await callAction(createProjectAction, undefined, "project:create")(form);
      const id = r.ok ? r.data?.projectId : null;
      if (r.ok && typeof id === "string") {
        showToast("success", r.message ?? `Project ${draft.name} created`);
        onCreated(id, (r.data?.projectType as ProjectType) ?? draft.type);
        return;
      }
      // A refusal, or a step-up the person cancelled: said here, and everything typed stays.
      setError(r.ok ? "The project was not created." : (r.error ?? "The project was not created."));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const sheet = (
    <div className="bx-np-backdrop" data-testid="new-project-backdrop"
         onPointerDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <form className="bx-np-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId}
            data-testid="new-project-form" data-permission="project:create" noValidate
            onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <h2 id={titleId} className="bx-np-title">New project</h2>
        <p className="bx-np-sub">A project of its own in this organization: its agents, skills, budget, settings and work.</p>
        <fieldset className="bx-project-type" data-testid="new-project-type">
          <legend>What is it for?</legend>
          {PROJECT_TYPES.map((t) => (
            <label key={t.key} className="bx-project-type-option" data-checked={draft.type === t.key || undefined}
                   data-testid={`new-project-type-${t.key}`}>
              <input type="radio" name="project_type" value={t.key} checked={draft.type === t.key}
                     onChange={() => onDraft({ ...draft, type: t.key })} />
              <span className="bx-project-type-text"><b>{t.label}</b><small>{t.line}</small></span>
            </label>
          ))}
        </fieldset>
        <label className="bx-np-field">
          <span>Name</span>
          <input ref={nameRef} name="name" className="input" required maxLength={120} value={draft.name}
                 placeholder={kg ? "e.g. Fineract knowledge graph" : "e.g. HelpDesk AI"}
                 onChange={(e) => onDraft({ ...draft, name: e.target.value })} />
        </label>
        <label className="bx-np-field">
          <span>Description <em>optional</em></span>
          <input name="description" className="input" maxLength={2000} value={draft.description}
                 onChange={(e) => onDraft({ ...draft, description: e.target.value })} />
        </label>
        {kg ? <p className="bx-np-next" data-testid="new-project-next">Next: import the code, choose the agents who work on it, and run a first scan for business rules.</p> : null}
        {error ? <p className="bx-np-error" role="alert" data-testid="new-project-error">{error}</p> : null}
        <div className="bx-np-actions">
          {onBack ? (
            <button type="button" className="ag-btn spj-back-btn" data-testid="new-project-back" disabled={busy}
                    onClick={onBack}>← Back</button>
          ) : null}
          <button type="button" className="ag-btn" data-testid="new-project-cancel" disabled={busy} onClick={onClose}>Cancel</button>
          <GatedButton permission="project:create" type="submit" className="ag-btn ag-btn-primary" data-testid="new-project-create"
                       disabled={busy}
                       title={kg ? "Create the project and open its setup: import the code, assign the team, a first scan" : "Create the project and open it"}>
            {busy ? "Creating…" : kg ? "Create and set up" : "Create"}
          </GatedButton>
        </div>
      </form>
    </div>
  );
  return typeof document === "undefined" ? sheet : createPortal(sheet, document.body);
}
