"use client";

/**
 * Loadout › Instructions (agent-loadout spec §3.3): the operator's written way of working for
 * this agent — a review checklist, house rules — uploaded as a .md/.txt or pasted. Always in its
 * prompt, in the modes it is for; it shapes how it works, never what it may do. Each change is a
 * new agent version (`Added · v8 · applies to new work`).
 *
 * The rows, the budget meter, `+ Add ▾`, each row's 👁 preview and `⋯` menu, the `Not used` pill
 * (a document today's check refuses is left out of the prompt) and the read-only state (no
 * agent-editor rights: rows and preview only).
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Eye, DotsThree, FileText, Plus, X } from "@phosphor-icons/react";

import {
  ago, firstLine, instructionsApi, isFull, LIMITS, meterLine, MODE_LABEL, MODES, readTextFile,
  sizeLabel, type InstructionItem, type InstructionList, type InstructionMode,
} from "../../lib/agent-instructions";
import { showToast } from "../client-forms";
import { Markdown } from "../markdown";
import { SectionLabel } from "../section-label";
import { DrawerShell, ModalShell } from "../skills/overlay";
import { InstructionSheet, SHEET_TIPS, type SheetInput } from "./instruction-sheet";
import { Pop } from "./loadout-pop";

export const INSTRUCTION_TIPS = {
  label: "Always in its prompt. Shapes how it works, never what it may do.",
  meter: "Sent with every call. Over 4k, make it a skill.",
  full: "At the 4k token budget. Remove one or make it a skill.",
  since: "The agent version that added this",
  readOnly: "Needs agent-editor rights",
};

/** `Work only`, `Work, Chat` — shown only when a document is not in every mode. */
export function modesLabel(modes: InstructionMode[]): string | null {
  if (modes.length === MODES.length) return null;
  return modes.length === 1 ? `${MODE_LABEL[modes[0]]} only` : modes.map((m) => MODE_LABEL[m]).join(", ");
}

/**
 * Markdown as text only: no raw HTML (the renderer builds React nodes, never markup), links
 * only to http(s) and opened `noopener noreferrer`, and images NOT loaded — a remote image in a
 * playbook is a tracking pixel — but shown as `[image: alt]`.
 */
export function safeMarkdownText(body: string): string {
  return body
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, (_m, alt: string) => `[image: ${alt || "no alt"}]`)
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label: string, href: string) => (/^https?:\/\//i.test(href) ? m : label));
}

export function InstructionsRow({ projectId, roleKey, actor, onVersion, onMakeSkill, reloadKey, onAccess }: {
  projectId: string; roleKey: string; actor: string;
  /** The agent's new version after a change, for the certificate. */
  onVersion: (version: number) => void;
  onMakeSkill: (text: string, fileName: string) => void;
  /** Bumped by the parent to re-read (after a skill-from-file, say). */
  reloadKey?: number;
  /** Told whether this viewer may edit (the API's `can_edit`), for the rows above. */
  onAccess?: (canEdit: boolean) => void;
}) {
  const [list, setList] = useState<InstructionList | null>(null);
  const [failed, setFailed] = useState("");
  const [sheet, setSheet] = useState<SheetInput | null>(null);
  const [preview, setPreview] = useState<{ item: InstructionItem; edit: boolean } | null>(null);
  const [removing, setRemoving] = useState<InstructionItem | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [now, setNow] = useState(0);
  const upload = useRef<HTMLInputElement>(null);
  const replaceTarget = useRef<InstructionItem | null>(null);

  const load = useCallback(async () => {
    const r = await instructionsApi.list(projectId, roleKey, actor);
    if (r.ok) { setList(r.data); setFailed(""); onAccess?.(r.data.canEdit); } else setFailed(r.error);
    setNow(Date.now());
  }, [projectId, roleKey, actor]); // eslint-disable-line react-hooks/exhaustive-deps -- onAccess is a setter
  useEffect(() => { void load(); }, [load, reloadKey]);

  const changed = (version: number, verb: string) => {
    showToast("success", `${verb} · v${version} · applies to new work`);
    onVersion(version);
    void load();
  };

  const pickFile = (target: InstructionItem | null) => {
    replaceTarget.current = target;
    upload.current?.click();
  };
  const onFile = async (file: File | undefined) => {
    if (!file) return;
    const read = await readTextFile(file);
    if (upload.current) upload.current.value = "";
    if (!read.ok) { showToast("error", read.error); return; }
    const target = replaceTarget.current;
    setSheet(target ? { kind: "replace", item: target, fileName: read.name, text: read.text }
      : { kind: "upload", fileName: read.name, text: read.text });
  };

  const patch = async (item: InstructionItem, changes: { modes?: InstructionMode[]; position?: number }) => {
    setBusy(item.id);
    const r = await instructionsApi.update(projectId, actor, roleKey, item.id, changes);
    setBusy(null);
    if (!r.ok) { showToast("error", r.error); return; }
    changed(r.data.version, "Saved");
  };
  const remove = async (item: InstructionItem) => {
    setBusy(item.id);
    const r = await instructionsApi.remove(projectId, actor, roleKey, item.id);
    setBusy(null);
    setRemoving(null);
    setPreview(null);
    if (!r.ok) { showToast("error", r.error); return; }
    changed(r.data.version, "Removed");
  };

  const canEdit = !!list?.canEdit;
  const full = isFull(list);
  const items = list?.items ?? [];

  const addMenu = (
    <Pop label="+ Add" testId="instructions-add" disabled={full}
         tip={full ? INSTRUCTION_TIPS.full : undefined} trigger={<><Plus size={12} /> Add ▾</>}>
      {(close) => (<>
        <button type="button" role="menuitem" data-tip={SHEET_TIPS.upload} data-testid="instructions-upload"
                onClick={() => { close(); pickFile(null); }}>Upload file</button>
        <button type="button" role="menuitem" data-tip={SHEET_TIPS.paste} data-testid="instructions-paste"
                onClick={() => { close(); setSheet({ kind: "paste" }); }}>Paste text</button>
      </>)}
    </Pop>
  );

  return (
    <div className="ld-row ld-row-ins" data-testid="loadout-instructions">
      <SectionLabel tip={INSTRUCTION_TIPS.label} as="h4" testid="loadout-label-instructions">Instructions</SectionLabel>
      <div className="ld-ins">
        <div className="ld-ins-head">
          <span className="ld-meter" data-tip={INSTRUCTION_TIPS.meter} data-testid="instructions-meter">
            {list ? meterLine(list.budget) : meterLine(null)}
          </span>
          {canEdit && items.length ? addMenu : null}
        </div>
        {failed ? (
          <button type="button" className="kit-link" data-testid="instructions-retry" onClick={() => void load()}>
            Can&apos;t reach BOSS API · Retry
          </button>
        ) : null}
        {!list && !failed ? (
          <>{[0, 1].map((i) => <span key={i} className="kit-skeleton ld-skel-row" aria-hidden="true" />)}</>
        ) : null}
        {list && !items.length ? (
          <span className="ld-empty" data-testid="instructions-empty">
            <span className="kit-faint">No instructions</span>
            {canEdit ? (<>
              <button type="button" className="kit-btn kit-btn-sm" data-tip={SHEET_TIPS.upload} data-testid="instructions-empty-upload"
                      onClick={() => pickFile(null)}>Upload file</button>
              <button type="button" className="kit-btn kit-btn-sm" data-tip={SHEET_TIPS.paste} data-testid="instructions-empty-paste"
                      onClick={() => setSheet({ kind: "paste" })}>Paste text</button>
            </>) : null}
          </span>
        ) : null}
        {items.map((item, index) => {
          const only = modesLabel(item.modes);
          const workOnly = item.modes.length === 1 && item.modes[0] === "work";
          return (
            <div key={item.id} className="ld-ins-row" data-busy={busy === item.id || undefined}
                 data-testid={`instruction-${item.id}`} data-name={item.name}>
              <FileText size={15} aria-hidden="true" />
              <span className="ld-ins-name" data-tip={firstLine(item.preview) || item.name}>{item.name}</span>
              <span className="ld-ins-meta">
                <span data-tip={`≈ ${item.tokensEst.toLocaleString("en-US")} tokens`}>{sizeLabel(item.sizeChars)}</span>
                {only ? <span className="kit-chip" data-testid="instruction-modes">{only}</span> : null}
                {!item.usable ? (
                  <span className="kit-chip" data-tone="amber" data-testid="instruction-not-used"
                        data-tip={`Fails the check now: ${item.unusableReason ?? "it may not be used"}. Edit or remove it.`}>Not used</span>
                ) : null}
                <span className="kit-faint" data-tip={`Updated by ${item.updatedBy}${item.updatedAt ? ` · ${item.updatedAt.slice(0, 16).replace("T", " ")}` : ""}`}>
                  {ago(item.updatedAt, now)}
                </span>
              </span>
              <span className="ld-ins-acts">
                <button type="button" className="ld-icon" aria-label="Preview" data-tip="Preview" data-testid={`instruction-preview-${item.id}`}
                        onClick={() => setPreview({ item, edit: false })}><Eye size={15} /></button>
                {canEdit ? (
                  <Pop label={`More for ${item.name}`} testId={`instruction-more-${item.id}`} className="ld-icon" align="right"
                       trigger={<DotsThree size={15} weight="bold" />}>
                    {(close) => (<>
                      <button type="button" role="menuitem" onClick={() => { close(); pickFile(item); }}>Replace file</button>
                      <button type="button" role="menuitem" onClick={() => { close(); setPreview({ item, edit: true }); }}>Edit</button>
                      <button type="button" role="menuitemcheckbox" aria-checked={workOnly}
                              data-testid={`instruction-work-only-${item.id}`}
                              onClick={() => { close(); void patch(item, { modes: workOnly ? [...MODES] : ["work"] }); }}>
                        {workOnly ? "✓ " : ""}Only for work
                      </button>
                      {index > 0 ? <button type="button" role="menuitem" onClick={() => { close(); void patch(item, { position: index - 1 }); }}>Move up</button> : null}
                      {index < items.length - 1 ? <button type="button" role="menuitem" onClick={() => { close(); void patch(item, { position: index + 1 }); }}>Move down</button> : null}
                      <button type="button" role="menuitem" data-testid={`instruction-remove-${item.id}`}
                              onClick={() => { close(); setRemoving(item); }}>Remove</button>
                    </>)}
                  </Pop>
                ) : null}
              </span>
            </div>
          );
        })}
      </div>
      <input ref={upload} type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" hidden
             data-testid="instructions-file" onChange={(e) => void onFile(e.target.files?.[0])} />
      {sheet ? (
        <InstructionSheet projectId={projectId} actor={actor} roleKey={roleKey} input={sheet}
                          taken={items.map((i) => i.name.toLowerCase())}
                          onClose={() => setSheet(null)}
                          onSaved={(saved, replaced) => { setSheet(null); changed(saved.version, replaced ? "Saved" : "Added"); }}
                          onMakeSkill={(text, fileName) => { setSheet(null); onMakeSkill(text, fileName); }} />
      ) : null}
      {preview ? (
        <PreviewDrawer projectId={projectId} actor={actor} roleKey={roleKey} item={preview.item} startEditing={preview.edit}
                       canEdit={canEdit} onClose={() => setPreview(null)}
                       onSaved={(v) => { setPreview(null); changed(v, "Saved"); }}
                       onRemove={() => setRemoving(preview.item)} />
      ) : null}
      {removing ? (
        <ModalShell label={`Remove ${removing.name}?`} testId="instruction-remove-confirm" onClose={() => setRemoving(null)} over>
          <div className="kit-modal-head"><h3>Remove {removing.name}?</h3></div>
          <div className="kit-modal-foot">
            <button type="button" className="kit-btn" onClick={() => setRemoving(null)}>Cancel</button>
            <button type="button" className="kit-btn kit-btn-danger" disabled={busy === removing.id} data-testid="instruction-remove-yes"
                    onClick={() => void remove(removing)}>Remove</button>
          </div>
        </ModalShell>
      ) : null}
    </div>
  );
}

function PreviewDrawer({ projectId, actor, roleKey, item, startEditing, canEdit, onClose, onSaved, onRemove }: {
  projectId: string; actor: string; roleKey: string; item: InstructionItem; startEditing: boolean; canEdit: boolean;
  onClose: () => void; onSaved: (version: number) => void; onRemove: () => void;
}) {
  const [body, setBody] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(startEditing && canEdit);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<{ error: string; tip?: string } | null>(null);
  useEffect(() => {
    void instructionsApi.body(projectId, roleKey, item.id).then((r) => {
      const text = r.ok ? r.data : "";
      setBody(text);
      setDraft(text);
      if (!r.ok) setError({ error: r.error });
    });
  }, [projectId, roleKey, item.id]);
  const save = async () => {
    setSaving(true);
    const r = await instructionsApi.update(projectId, actor, roleKey, item.id, { body: draft });
    setSaving(false);
    if (!r.ok) { setError({ error: r.error, tip: r.tip }); return; }
    onSaved(r.data.version);
  };
  return (
    <DrawerShell label={item.name} testId="instruction-drawer" onClose={onClose} float over>
      <div className="kit-drawer-head">
        <h3 className="kit-drawer-title">{item.name}</h3>
        <button type="button" className="ag-icon-btn" aria-label="Close" data-tip="Close" onClick={onClose}><X size={14} aria-hidden /></button>
        <span className="ld-drawer-facts">
          <span>{sizeLabel(item.sizeChars)} · ≈ {item.tokensEst.toLocaleString("en-US")} tokens</span>
          {item.sinceVersion ? <span className="kit-chip" data-tip={INSTRUCTION_TIPS.since}>Since v{item.sinceVersion}</span> : null}
        </span>
      </div>
      <div className="kit-drawer-body ld-preview" data-testid="instruction-body">
        {body === null ? <span className="kit-faint">Reading…</span> : editing ? (
          <>
            <textarea className="ld-text ld-mono" rows={18} value={draft} data-autofocus data-testid="instruction-edit"
                      onChange={(e) => { setDraft(e.target.value); setError(null); }} />
            <span className="kit-faint" data-testid="instruction-counter">
              {draft.length.toLocaleString("en-US")} / {LIMITS.docMaxChars.toLocaleString("en-US")}
            </span>
          </>
        ) : (
          <div data-user-text><Markdown text={safeMarkdownText(body)} className="ld-md" /></div>
        )}
        {error ? <span className="ld-error" role="alert" data-tip={error.tip || undefined}>{error.error}</span> : null}
      </div>
      {canEdit ? (
        <div className="kit-modal-foot ld-drawer-foot">
          {editing ? (<>
            <button type="button" className="kit-btn" onClick={() => { setEditing(false); setDraft(body ?? ""); setError(null); }}>Cancel</button>
            <button type="button" className="kit-btn kit-btn-primary" data-testid="instruction-save"
                    disabled={saving || draft === body || !draft.trim() || draft.length > LIMITS.docMaxChars}
                    onClick={() => void save()}>{saving ? "Saving…" : "Save"}</button>
          </>) : (<>
            <button type="button" className="kit-btn kit-btn-danger" onClick={onRemove}>Remove</button>
            <button type="button" className="kit-btn" data-testid="instruction-edit-open" onClick={() => setEditing(true)}>Edit</button>
          </>)}
        </div>
      ) : null}
    </DrawerShell>
  );
}
