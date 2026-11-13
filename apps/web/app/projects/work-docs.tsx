"use client";

/**
 * DOCUMENTS ON A WORK ITEM (BOSSAPP-874), inside the Tree view's item panel. A row per document
 * (its kind, revision and open comments); one opens in place: the text to edit, Save (from the
 * revision it was opened at — a stale save says so and offers the other writer's text), select a
 * passage → Comment, the threads with where their passage went, and the revisions with a diff
 * against the one before and Restore.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { ChatCircleText, FileText, Plus } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  ANCHOR_WORDS, commentOn, conflictOf, createDoc, readDiff, readDoc, readDocs, replyTo, resolveThread,
  restoreDoc, saveDoc, type Doc, type DocDiff, type DocRevision,
} from "../../lib/documents";

const KIND_WORDS: Record<string, string> = { prd: "PRD", plan: "Plan", minutes: "Minutes", architecture: "Architecture", note: "Note" };

export function ItemDocuments({ projectId, workItemId, demo }: { projectId: string; workItemId: string; demo: boolean }) {
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [said, setSaid] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await readDocs(projectId, workItemId);
    setDocs(r.ok && r.data ? r.data : []);
  }, [projectId, workItemId]);
  useEffect(() => { void load(); }, [load]);
  const add = async () => {
    if (!title.trim()) return;
    const r = await createDoc(projectId, workItemId, title.trim(), "plan");
    setSaid(r.ok ? null : r.error);
    if (r.ok) { setTitle(""); await load(); setOpen(String((r.body as { document?: { id?: string } } | null)?.document?.id ?? "") || null); }
  };
  return (
    <details className="wt-policy" data-testid="wt-docs" open={Boolean(docs?.length)}>
      <summary><FileText size={12} aria-hidden /> Documents {docs ? `· ${docs.length}` : ""}</summary>
      {(docs ?? []).map((d) => (
        <div key={d.id} data-testid={`wt-doc-${d.id}`}>
          <button type="button" className="wt-open" onClick={() => setOpen(open === d.id ? null : d.id)}>
            <span className="wt-key">{KIND_WORDS[d.kind] ?? d.kind}</span>
            <span className="wt-title">{d.title}</span>
            <span className="wt-faint">rev {d.current_revision_no}{d.open_threads ? ` · ${d.open_threads} open comment${d.open_threads > 1 ? "s" : ""}` : ""}</span>
          </button>
          {open === d.id ? <DocEditor projectId={projectId} id={d.id} demo={demo} onChanged={() => void load()} /> : null}
        </div>
      ))}
      <div className="wt-line">
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="New document title"
               aria-label="New document title" data-testid="wt-doc-title" disabled={demo} />
        <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo || !title.trim()}
                     onClick={() => void add()} data-testid="wt-doc-add">
          <Plus size={12} aria-hidden /> New document
        </GatedButton>
      </div>
      {said ? <p className="wt-said" role="alert">{said}</p> : null}
    </details>
  );
}

export function DocEditor({ projectId, id, demo, onChanged }: { projectId: string; id: string; demo: boolean; onChanged: () => void }) {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [text, setText] = useState("");
  const [base, setBase] = useState<string | null>(null);
  const [theirs, setTheirs] = useState<DocRevision | null>(null);
  const [sel, setSel] = useState<{ start: number; end: number } | null>(null);
  const [comment, setComment] = useState("");
  const [reply, setReply] = useState<Record<string, string>>({});
  const [diff, setDiff] = useState<DocDiff | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement | null>(null);
  const load = useCallback(async (keepText = false) => {
    const r = await readDoc(projectId, id);
    if (r.ok && r.data) {
      setDoc(r.data);
      setBase(r.data.current_revision_id);
      if (!keepText) setText(r.data.current?.body ?? "");
    }
  }, [projectId, id]);
  useEffect(() => { void load(); }, [load]);
  if (!doc) return <div className="wt-panel" data-testid="wt-doc-loading">Loading…</div>;
  const pristine = text === (doc.current?.body ?? "");
  const save = async () => {
    const r = await saveDoc(projectId, id, text, base);
    if (r.ok) { setTheirs(null); setSaid("Saved."); await load(); onChanged(); return; }
    const c = conflictOf(r.refusal);
    setTheirs(c.revision);
    setSaid(c.locked ? r.error : c.revision ? `Changed since you opened it (now revision ${c.revision.revision_no}). Your text is kept; compare and save again.` : r.error);
  };
  const takeTheirs = () => {
    if (!theirs) return;
    setText(theirs.body ?? ""); setBase(theirs.id); setTheirs(null); setSaid(null); void load(true);
  };
  const keepMine = () => { if (theirs) { setBase(theirs.id); setTheirs(null); setSaid("Save again to put your text on top."); } };
  const pick = () => {
    const el = area.current;
    if (el && el.selectionEnd > el.selectionStart) setSel({ start: el.selectionStart, end: el.selectionEnd });
    else setSel(null);
  };
  const addComment = async () => {
    if (!sel || !comment.trim() || !doc.current_revision_id) return;
    const r = await commentOn(projectId, id, doc.current_revision_id, sel.start, sel.end, comment.trim());
    setSaid(r.ok ? null : r.error);
    if (r.ok) { setComment(""); setSel(null); await load(true); onChanged(); }
  };
  const showDiff = async (no: number) => {
    const r = await readDiff(projectId, id, Math.max(1, no - 1), no);
    setDiff(r.ok && r.data ? r.data : null);
  };
  const threads = doc.threads ?? [];
  return (
    <div className="wt-panel" data-testid="wt-doc-editor">
      {doc.locked_by ? <p className="wt-faint" data-testid="wt-doc-locked">{doc.locked_by} is editing.</p> : null}
      <textarea ref={area} className="textarea wt-json" rows={10} value={text} onChange={(e) => setText(e.target.value)}
                onSelect={pick} onMouseUp={pick} onKeyUp={pick} aria-label="Document text" data-testid="wt-doc-text" disabled={demo} />
      <div className="wt-line">
        <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo || pristine}
                     onClick={() => void save()} data-testid="wt-doc-save">Save</GatedButton>
        <span className="wt-faint">Revision {doc.current_revision_no}</span>
        {sel && pristine ? (
          <>
            <input className="input" value={comment} onChange={(e) => setComment(e.target.value)} aria-label="Comment on the selection"
                   placeholder={`Comment on “${text.slice(sel.start, sel.end).slice(0, 40)}”`} data-testid="wt-doc-comment" />
            <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo || !comment.trim()}
                         onClick={() => void addComment()} data-testid="wt-doc-comment-add">
              <ChatCircleText size={12} aria-hidden /> Comment
            </GatedButton>
          </>
        ) : <span className="wt-faint">{pristine ? "Select a passage to comment on it." : "Save before commenting."}</span>}
      </div>
      {theirs ? (
        <div className="wt-feedback" role="alert" data-testid="wt-doc-conflict">
          <p>Revision {theirs.revision_no} by {theirs.author_id || theirs.author_kind}:</p>
          <pre className="wt-pre">{(theirs.body ?? "").slice(0, 2000)}</pre>
          <div className="wt-line">
            <button type="button" className="px-btn" onClick={takeTheirs} data-testid="wt-doc-take-theirs">Use theirs</button>
            <button type="button" className="px-btn" onClick={keepMine} data-testid="wt-doc-keep-mine">Keep mine</button>
          </div>
        </div>
      ) : null}
      {threads.length ? (
        <ol className="wt-timeline" aria-label="Comment threads" data-testid="wt-doc-threads">
          {threads.map((t) => (
            <li key={t.id} data-e={t.status === "resolved" ? "completed" : t.anchor_state === "orphaned" ? "rejected" : "raised"} data-testid={`wt-thread-${t.id}`}>
              <span className="wt-ev" title={ANCHOR_WORDS[t.anchor_state]}>{t.status === "resolved" ? "Resolved" : ANCHOR_WORDS[t.anchor_state]}</span>
              <span className="wt-comment">“{t.quote.slice(0, 80)}”</span>
              {t.comments.map((c) => <span key={c.id}><b>{c.author}</b> {c.body}</span>)}
              {t.status === "open" ? (
                <span className="wt-line">
                  <input className="input" value={reply[t.id] ?? ""} onChange={(e) => setReply({ ...reply, [t.id]: e.target.value })}
                         aria-label="Reply" placeholder="Reply" disabled={demo} />
                  <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo || !(reply[t.id] ?? "").trim()}
                               onClick={() => void replyTo(projectId, id, t.id, reply[t.id]).then((r) => { setSaid(r.ok ? null : r.error); setReply({ ...reply, [t.id]: "" }); return load(true); })}>
                    Reply
                  </GatedButton>
                  <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo}
                               onClick={() => void resolveThread(projectId, id, t.id, true).then(() => load(true))} data-testid={`wt-thread-resolve-${t.id}`}>
                    Resolve
                  </GatedButton>
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}
      <details>
        <summary>Revisions · {doc.revisions?.length ?? 0}</summary>
        <ol className="wt-timeline" data-testid="wt-doc-revisions">
          {(doc.revisions ?? []).map((v) => (
            <li key={v.id}>
              <span className="wt-ev">Rev {v.revision_no}</span>
              <span className="wt-faint">{v.author_kind === "agent" ? "agent " : ""}{v.author_id}{v.note ? ` · ${v.note}` : ""}</span>
              {v.revision_no > 1 ? <button type="button" className="px-btn" onClick={() => void showDiff(v.revision_no)} data-testid={`wt-doc-diff-${v.revision_no}`}>Changes</button> : null}
              {v.revision_no !== doc.current_revision_no ? (
                <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo}
                             onClick={() => void restoreDoc(projectId, id, v.revision_no, base).then((r) => { setSaid(r.ok ? `Restored revision ${v.revision_no}.` : r.error); return load(); })}>
                  Restore
                </GatedButton>
              ) : null}
            </li>
          ))}
        </ol>
        {diff ? (
          <pre className="wt-pre" data-testid="wt-doc-diff" aria-label={`Changes from revision ${diff.from} to ${diff.to}`}>
            {diff.lines.filter((l) => !l.startsWith("---") && !l.startsWith("+++")).map((l, i) => (
              <span key={i} data-d={l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : undefined}>{l}{"\n"}</span>
            ))}
          </pre>
        ) : null}
      </details>
      {said ? <p className="wt-said" role="status" data-testid="wt-doc-said">{said}</p> : null}
    </div>
  );
}
