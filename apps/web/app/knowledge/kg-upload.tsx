"use client";

/**
 * KNOWLEDGE GRAPH › DOCUMENTS › UPLOAD (rulebook source G3). "Upload a document (PDF)" reads the
 * file on the server, which proposes its sections — one per heading and per rule card, each with
 * its page — and shows them here as a PREVIEW. Nothing is in the Knowledge Graph until the person
 * presses "Add the document": they may first drop a section, merge one with the next, or split
 * one at a line (operator decision 2026-11-01). A preview an agent proposed waits here too.
 *
 * After it is added, "Extract rules" starts the rulebook_extraction workflow: the Rulebook Analyst
 * proposes each rule the document states, for review in Rules review.
 *
 * One door for every read and write: lib/kg-view.ts. Writes pre-grey on knowledge:manage.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  DOCUMENT_MANAGE, SCAN_START, confirmUpload, discardUpload, editUpload, fileBase64, getUploadSection,
  kgRulesHref, kgRunHref, stageUpload,
  type KgUploadEdit, type KgUploadPreview, type KgUploadSection,
} from "../../lib/kg-view";
import { post } from "../workflows/delivery-api";

type Who = { projectId: string; actor: string };

const KIND_WORDS: Record<KgUploadSection["kind"], string> = {
  title_page: "Title page", contents: "Contents", heading: "Heading", rule_card: "Rule card", custom: "Section",
};

export function pagesWords(page: number | null | undefined, end: number | null | undefined): string {
  if (!page) return "";
  return end && end !== page ? `pp. ${page}–${end}` : `p. ${page}`;
}

function countsWords(p: KgUploadPreview): string {
  const parts = [`${p.counts.sections} sections`];
  if (p.counts.rule_cards) parts.push(`${p.counts.rule_cards} rule cards`);
  if (p.counts.headings) parts.push(`${p.counts.headings} headings`);
  if (p.counts.other) parts.push(`${p.counts.other} other`);
  return `${parts[0]}: ${parts.slice(1).join(", ")}`;
}

const NO_WORKFLOW_RE = /no delivery workflow/i;

/** Start rulebook_extraction on a document (installing it from the catalogue when the project
 *  predates it), and say where the run and the proposals are. */
export function ExtractRules({ w, documentId, title, disabledWhy, archived, orgId }: {
  w: Who; documentId: string; title: string; disabledWhy?: string | null; archived?: boolean; orgId?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string; run?: string | null } | null>(null);

  const start = useCallback(async (installed = false): Promise<void> => {
    setBusy(true); setMsg(null);
    const r = await post("start", {
      project_id: w.projectId, workflow_key: "rulebook_extraction", actor: w.actor,
      input: { document_id: documentId, title: `Extract the rules of ${title}` }, prefer_project_copy: true,
    });
    if (!r.ok && !installed && NO_WORKFLOW_RE.test(String(r.error ?? ""))) {
      const done = await post("catalogue/install", { project_id: w.projectId });
      if (done.ok) return start(true);
    }
    setBusy(false);
    if (!r.ok) { setMsg({ ok: false, text: r.error ? String(r.error) : "The extraction did not start." }); return; }
    const data = (r.data ?? {}) as { run?: { id?: string }; id?: string; run_id?: string };
    const run = data.run?.id ?? data.run_id ?? data.id ?? null;
    setMsg({ ok: true, text: "The Rulebook Analyst is reading the document. Each rule it finds waits in Rules review.", run });
  }, [w, documentId, title]);

  const blocked = !!disabledWhy;
  return (
    <span className="kg-doc-check">
      <GatedButton permission={SCAN_START} className="ag-btn ag-btn-primary" data-testid="kg-extract-rules"
                   disabled={archived || busy || blocked}
                   title={disabledWhy || "Have the Rulebook Analyst propose each rule this document states, for your review"}
                   onClick={() => void start()}>{busy ? "Starting…" : "Extract rules"}</GatedButton>
      {blocked ? <span className="kg-quiet" data-testid="kg-extract-blocked">{disabledWhy}</span> : null}
      {msg ? (
        <span className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-extract-msg">
          {msg.text}
          {msg.ok && msg.run ? <> <a className="inf-link" href={kgRunHref({ workflow_run_id: msg.run }, orgId) ?? undefined}>Open the run</a></> : null}
          {msg.ok ? <> · <a className="inf-link" href={kgRulesHref({}, orgId)}>Rules review</a></> : null}
        </span>
      ) : null}
    </span>
  );
}

function SectionRow({ w, preview, section, last, busy, onEdit }: {
  w: Who; preview: KgUploadPreview; section: KgUploadSection; last: boolean; busy: boolean;
  onEdit: (edit: KgUploadEdit) => void;
}) {
  const [lines, setLines] = useState<string[] | null>(null);
  const [splitting, setSplitting] = useState(false);
  const openSplit = async () => {
    if (splitting) { setSplitting(false); return; }
    setSplitting(true);
    if (!lines) {
      const r = await getUploadSection(w, preview.upload_id, section.key);
      setLines(r.ok ? r.data.lines : []);
    }
  };
  return (
    <li className="kg-up-row" data-testid="kg-upload-section" data-kind={section.kind}>
      <div className="kg-up-row-head">
        <span className="kg-chip kg-up-kind" data-kind={section.kind}>{KIND_WORDS[section.kind]}</span>
        <span className="kg-up-title" data-user-text>{section.title}</span>
        <span className="kg-quiet kg-up-page">{pagesWords(section.page, section.page_end)}</span>
        <span className="kg-up-actions">
          <button type="button" className="inf-link" data-testid="kg-upload-merge" disabled={busy || last}
                  title={last ? "The last section has nothing after it" : "Join this section with the next one"}
                  onClick={() => onEdit({ op: "merge", key: section.key })}>Merge with next</button>
          <button type="button" className="inf-link" data-testid="kg-upload-split" disabled={busy || section.line_count < 2}
                  aria-expanded={splitting} title="Choose a line to start a new section at"
                  onClick={() => void openSplit()}>Split…</button>
          <button type="button" className="inf-link" data-tone="reject" data-testid="kg-upload-drop" disabled={busy}
                  title="Leave this section out of the document" onClick={() => onEdit({ op: "drop", key: section.key })}>Drop</button>
        </span>
      </div>
      {section.first_lines.length ? (
        <p className="kg-up-first" data-user-text>{section.first_lines.join(" ")}</p>
      ) : null}
      {splitting ? (
        <ol className="kg-up-lines" data-testid="kg-upload-lines" aria-label={`Lines of ${section.title}`}>
          {lines === null ? <li className="kg-quiet">Reading the section…</li> : lines.map((line, i) => (
            <li key={i}>
              {i > 0 ? (
                <button type="button" className="inf-link" data-testid="kg-upload-split-here" disabled={busy}
                        onClick={() => { setSplitting(false); setLines(null); onEdit({ op: "split", key: section.key, line: i }); }}>
                  Split here
                </button>
              ) : <span className="kg-quiet">first line</span>}
              <span className="kg-up-line" data-user-text>{line}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </li>
  );
}

/** A pending upload: its proposed sections, editable, and the confirmation. */
export function UploadPreview({ w, preview: initial, archived, onDone }: {
  w: Who; preview: KgUploadPreview; archived?: boolean;
  onDone: (added: { document_id: string; title: string; rule_cards: number; sections: number } | null) => void;
}) {
  const [preview, setPreview] = useState(initial);
  const [title, setTitle] = useState(initial.title);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setPreview(initial); setTitle(initial.title); }, [initial]);

  const edit = async (change: KgUploadEdit) => {
    setBusy("edit"); setErr(null);
    const r = await editUpload(w, preview.upload_id, [change]);
    setBusy(null);
    if (!r.ok) { setErr(r.error); return; }
    setPreview(r.data);
  };
  const confirm = async () => {
    setBusy("confirm"); setErr(null);
    const r = await confirmUpload(w, preview.upload_id, title.trim() && title.trim() !== preview.title ? title.trim() : undefined);
    setBusy(null);
    if (!r.ok) { setErr(r.error); return; }
    onDone({ document_id: r.data.document_id, title: r.data.title, rule_cards: r.data.rule_cards, sections: r.data.sections });
  };
  const discard = async () => {
    setBusy("discard"); setErr(null);
    const r = await discardUpload(w, preview.upload_id);
    setBusy(null);
    if (!r.ok) { setErr(r.error); return; }
    onDone(null);
  };

  return (
    <section className="kg-up" data-testid="kg-upload-preview" aria-label={`Preview of ${preview.title}`}>
      <div className="kg-up-head">
        <span className="kg-eyebrow">Check the sections before they are added</span>
        <label className="kg-field kg-up-name"><span>Title</span>
          <input className="input" value={title} disabled={archived || busy !== null} data-testid="kg-upload-title"
                 onChange={(e) => setTitle(e.target.value)} /></label>
        <p className="kg-quiet" data-testid="kg-upload-counts">
          <span data-user-text>{preview.filename}</span> · {preview.pages} {preview.pages === 1 ? "page" : "pages"} · {countsWords(preview)}
        </p>
        <p className="kg-quiet" data-testid="kg-upload-note">
          {preview.proposed_by_kind === "agent" ? "An agent proposed this split. " : ""}
          Nothing is in the Knowledge Graph yet. Each section becomes a clause with its exact text and page; drop, merge or split any that are wrong, then add the document.
        </p>
      </div>
      <ol className="kg-up-list" data-testid="kg-upload-sections">
        {preview.sections.map((s, i) => (
          <SectionRow key={`${s.key}:${s.title}:${s.line_count}`} w={w} preview={preview} section={s}
                      last={i === preview.sections.length - 1} busy={busy !== null || !!archived} onEdit={(e) => void edit(e)} />
        ))}
      </ol>
      {err ? <p className="kg-inline-err" role="alert" data-testid="kg-upload-err">{err}</p> : null}
      <div className="kg-panel-actions">
        <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn ag-btn-primary" data-testid="kg-upload-confirm"
                     disabled={archived || busy !== null || !title.trim()}
                     title={`Add “${title.trim() || preview.title}” with ${preview.counts.sections} sections`}
                     onClick={() => void confirm()}>{busy === "confirm" ? "Adding…" : `Add the document (${preview.counts.sections} sections)`}</GatedButton>
        <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn" data-testid="kg-upload-discard" disabled={archived || busy !== null}
                     title="Leave this file out: nothing is added" onClick={() => void discard()}>Discard</GatedButton>
      </div>
    </section>
  );
}

/** "Upload a document (PDF)": pick a file, the server proposes sections, the preview opens. */
export function UploadDocument({ w, disabled, disabledWhy, onStaged }: {
  w: Who; disabled: boolean; disabledWhy: string; onStaged: (preview: KgUploadPreview) => void;
}) {
  const input = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const chosen = async (file: File | null | undefined) => {
    if (!file) return;
    setErr(null);
    if (file.size > 20 * 1024 * 1024) { setErr(`“${file.name}” is larger than 20 MB.`); return; }
    setBusy(true);
    const r = await stageUpload(w, { filename: file.name, content_base64: await fileBase64(file) });
    setBusy(false);
    if (input.current) input.current.value = "";
    if (!r.ok) { setErr(r.error); return; }
    onStaged(r.data);
  };

  return (
    <div className="kg-doc-row" data-testid="kg-upload">
      <span className="kg-doc-pack-label">Upload</span>
      <span className="kg-doc-pack-value">A PDF or text file: its sections are proposed for you to check</span>
      <input ref={input} type="file" accept="application/pdf,.pdf,text/plain,.txt" hidden data-testid="kg-upload-file"
             onChange={(e) => void chosen(e.target.files?.[0])} />
      <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn" data-testid="kg-upload-open" disabled={disabled || busy}
                   title={disabled ? disabledWhy : "Choose a PDF or text file to read"}
                   onClick={() => input.current?.click()}>{busy ? "Reading…" : "Upload a document (PDF)"}</GatedButton>
      {disabled ? <span className="kg-quiet">{disabledWhy}</span> : null}
      {err ? <span className="kg-inline-err" role="alert" data-testid="kg-upload-open-err">{err}</span> : null}
    </div>
  );
}
