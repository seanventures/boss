"use client";

/**
 * KNOWLEDGE GRAPH › WIKI (BOSSAPP-891): the project's readable pages, kept current by people and
 * agents. The list on the left (the knowledge index first); the page on the right with its links,
 * the pages linking to it, its history and any change an agent proposed. A person edits and
 * saves at once; an agent's change waits as a decision in Approvals and is never written from
 * here. "Refresh from the graph" renders the graph's current facts into the kg-* pages (a page a
 * person changed gets the refresh as a proposal instead).
 *
 * One door for every read and write: lib/wiki.ts. Writes are pre-greyed on knowledge:manage.
 *
 * NOTHING TYPED IS LOST SILENTLY (BOSSAPP-1037). The new-page editor used to print "Saved as
 * <slug>" the moment a title existed — before any PUT — so a person read it as saved and walked
 * away. The slug line now says "Will be saved as <slug>" until Save succeeds, and only a real
 * save says "Saved". While the editor holds unsaved text, closing the tab asks (beforeunload) and
 * every in-app move that would drop it (another page, New page, Cancel) asks first — the one
 * guard, lib/unsaved-guard.ts. A save puts the page in the list at once from the PUT's answer,
 * then re-reads the list; a list read that answers late can never paint over a newer one.
 */
import React, { useCallback, useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { kgWhen } from "../../lib/kg-view";
import { useUnsavedGuard } from "../../lib/unsaved-guard";
import {
  WIKI_MANAGE, getWikiPage, getWikiPages, getWikiRevisions, maintainWiki, restoreWikiPage, saveWikiPage, slugOf,
  type WikiPage, type WikiPageRow, type WikiRevision,
} from "../../lib/wiki";
import { Markdown } from "../markdown";
import { ConfirmSheet } from "../projects/confirm-sheet";
import { Who } from "../who";
// The console's confirm sheet and its px- classes (loaded by the project pages; the wiki lives
// in Knowledge, so it brings them itself).
import "../projects/px.css";

type Caller = { projectId: string; actor: string };

const NEW_PAGE = { title: "", body: "" };
/** Who wrote a revision: the graph by name, a person or agent through <Who> (BOSSAPP-1040). */
function By({ rev, face = false }: { rev: Pick<WikiRevision, "author_kind" | "author_id">; face?: boolean }) {
  if (rev.author_kind === "system") return <>the graph</>;
  const id = (rev.author_id || "").trim() || rev.author_kind;
  const party = rev.author_kind === "agent" && !/^(agent:|agt_)/i.test(id) ? `agent:${id}` : id;
  return <Who id={party} face={face} />;
}

/** [[slug]] and [[slug|label]] become links that open the page here. */
function linked(body: string): string {
  return body.replace(/\[\[([a-z0-9][a-z0-9-]{0,79})(?:\|([^\]]*))?\]\]/g,
    (_m, slug: string, label?: string) => `[${label || slug}](/?area=knowledge&kg_tab=wiki&wiki=${slug})`);
}

/**
 * What the editor holds, as words: is anything unsaved, which slug a Save writes, and the line
 * shown under the title. Never "Saved" before the PUT answered (BOSSAPP-1037).
 */
export function wikiDraft(o: { slug: string | null; initial: { title: string; body: string };
                               title: string; body: string }): {
  dirty: boolean; target: string; slugLine: string | null;
} {
  const target = o.slug ?? slugOf(o.title);
  const dirty = o.title !== o.initial.title || o.body !== o.initial.body;
  return { dirty, target, slugLine: !o.slug && target ? `Will be saved as ${target}` : null };
}

function Editor({ w, slug, initial, onSaved, onCancel, onDirty }: {
  w: Caller; slug: string | null; initial: { title: string; body: string };
  onSaved: (slug: string, row: WikiPageRow | null) => void; onCancel: () => void;
  onDirty: (dirty: boolean) => void;
}) {
  const [title, setTitle] = useState(initial.title);
  const [body, setBody] = useState(initial.body);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { dirty, target, slugLine } = wikiDraft({ slug, initial, title, body });
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => () => onDirty(false), [onDirty]);
  const save = async () => {
    if (!title.trim() || !target) { setError("A page needs a title."); return; }
    setBusy(true); setError(null);
    const r = await saveWikiPage(w, target, title, body);
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    const rev = r.data?.revision;
    onDirty(false);
    onSaved(target, rev ? {
      slug: target, title: rev.title || title, revision_no: rev.revision_no,
      author_kind: (rev.author_kind as WikiPageRow["author_kind"]) || "person", author_id: rev.author_id,
      updated_at: rev.created_at, maintained_by: null, pending: 0, chars: body.length,
    } : null);
  };
  return (
    <div className="kg-doc-form" data-testid="wiki-editor">
      <input className="input" aria-label="Title" data-testid="wiki-title" value={title}
             onChange={(e) => setTitle(e.target.value)} placeholder="Page title" />
      {slugLine ? <span className="kg-quiet" data-testid="wiki-slug">{slugLine}</span> : null}
      {dirty ? <span className="kg-quiet" data-testid="wiki-unsaved">Not saved yet</span> : null}
      <textarea className="input" aria-label="Page" data-testid="wiki-body" rows={14} value={body}
                onChange={(e) => setBody(e.target.value)} placeholder="Markdown. Link a page as [[slug]]." />
      {error ? <div className="kg-banner" data-tone="red" role="alert" data-testid="wiki-error">{error}</div> : null}
      <span className="kg-doc-pick">
        <GatedButton permission={WIKI_MANAGE} projectId={w.projectId} className="ag-btn" data-testid="wiki-save"
                     disabled={busy} onClick={() => void save()}>{busy ? "Saving…" : "Save"}</GatedButton>
        <button type="button" className="inf-link" data-testid="wiki-cancel" onClick={onCancel}>Cancel</button>
      </span>
    </div>
  );
}

function History({ w, slug, onRestored }: { w: Caller; slug: string; onRestored: () => void }) {
  const [revs, setRevs] = useState<WikiRevision[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** The revision open read-only; the history API already carries each revision's body. */
  const [viewing, setViewing] = useState<WikiRevision | null>(null);
  /** The revision Restore asks about: the console's confirm sheet, never a bare write. */
  const [restoring, setRestoring] = useState<WikiRevision | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    void getWikiRevisions(w, slug, ctl.signal).then((r) => {
      if (r.ok) setRevs(r.data.revisions); else if (r.status !== -1) setError(r.error);
    });
    return () => ctl.abort();
  }, [w, slug]);
  if (error) return <p className="kg-quiet" role="alert">{error}</p>;
  if (!revs) return <p className="kg-quiet">Loading history…</p>;
  if (viewing) {
    return (
      <div data-testid="wiki-revision-view">
        <p className="kg-quiet">
          Revision {viewing.revision_no} by <By rev={viewing} />
          {viewing.created_at ? `, ${kgWhen(viewing.created_at)}` : ""} — read-only.{" "}
          <button type="button" className="inf-link" data-testid="wiki-revision-back"
                  onClick={() => setViewing(null)}>Back to history</button>
        </p>
        <div data-testid="wiki-revision-body" data-user-text><Markdown text={linked(viewing.body)} /></div>
      </div>
    );
  }
  return (
    <>
      <ul className="kg-doc-clause-list" data-testid="wiki-history">
        {revs.map((rev) => (
          <li key={rev.id} className="kg-doc-clause" data-testid="wiki-revision">
            <span className="kg-doc-clause-section">v{rev.revision_no}</span>
            <span className="kg-doc-clause-text">
              <By rev={rev} face />{rev.source_note ? ` — ${rev.source_note}` : ""}
            </span>
            {rev.created_at ? <span className="kg-quiet">{kgWhen(rev.created_at)}</span> : null}
            <button type="button" className="inf-link" data-testid="wiki-revision-open"
                    onClick={() => setViewing(rev)}>View</button>
            {rev.current ? <span className="kg-quiet">current</span> : (
              <GatedButton permission={WIKI_MANAGE} projectId={w.projectId} className="inf-link" data-testid="wiki-restore"
                           onClick={() => setRestoring(rev)}>
                Restore
              </GatedButton>
            )}
          </li>
        ))}
      </ul>
      {restoring ? (
        <ConfirmSheet title={`Restore v${restoring.revision_no}?`} press="Restore" testid="wiki-restore-confirm"
                      lede="The page goes back to this revision; the restore is recorded as a new revision."
                      rows={[{ label: "Page", value: slug, mono: true },
                             { label: "Revision", value: `v${restoring.revision_no}` },
                             { label: "By", value: <By rev={restoring} /> }]}
                      onConfirm={async () => {
                        const r = await restoreWikiPage(w, slug, restoring.revision_no);
                        if (!r.ok) return r.error;
                        onRestored();
                        return null;
                      }}
                      onClose={() => setRestoring(null)} />
      ) : null}
    </>
  );
}

export function WikiTab({ projectId, actor, archived = false }: { projectId: string; actor: string; archived?: boolean }) {
  const w = React.useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [pages, setPages] = useState<{ data: WikiPageRow[] | null; error: string | null }>({ data: null, error: null });
  const [open, setOpen] = useState<string | null>(null);
  const [page, setPage] = useState<{ data: WikiPage | null; error: string | null }>({ data: null, error: null });
  const [mode, setMode] = useState<"read" | "edit" | "new" | "history">("read");
  const [note, setNote] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const leave = useUnsavedGuard(dirty);
  /** Leave the editor (or stay, if the person keeps their unsaved text). */
  const away = (go: () => void) => leave(() => { setDirty(false); go(); });

  // Only the newest list read may paint: an older one answering late (a retry while the API
  // warmed up, a slow first read) must never put back a list from before a save.
  const listSeq = React.useRef(0);
  const loadPages = useCallback(async () => {
    const seq = ++listSeq.current;
    const r = await getWikiPages(w);
    if (seq !== listSeq.current) return;
    if (r.ok) {
      setPages({ data: r.data.pages, error: null });
      setOpen((cur) => cur ?? r.data.pages[0]?.slug ?? null);
    } else if (r.status !== -1) setPages({ data: null, error: r.error });
  }, [w]);
  const loadPage = useCallback(async (slug: string) => {
    const r = await getWikiPage(w, slug);
    setPage(r.ok ? { data: r.data, error: null } : { data: null, error: r.error });
  }, [w]);

  useEffect(() => {
    void loadPages();
    if (typeof window !== "undefined") {
      const linkedSlug = new URLSearchParams(window.location.search).get("wiki");
      if (linkedSlug) setOpen(linkedSlug);
    }
  }, [loadPages]);
  useEffect(() => { if (open) void loadPage(open); }, [open, loadPage]);

  /** A real save: the page is in the list now (from the PUT's answer), then the list is re-read. */
  const saved = (slug: string, row: WikiPageRow | null, opened: boolean) => {
    if (row) {
      listSeq.current += 1; // a read already in flight predates this save
      setPages((cur) => ({ error: null, data: [...(cur.data ?? []).filter((p) => p.slug !== slug), row]
        .sort((a, b) => (a.slug === "knowledge" ? -1 : b.slug === "knowledge" ? 1 : a.title.toLowerCase().localeCompare(b.title.toLowerCase()))) }));
    }
    setDirty(false);
    setMode("read");
    if (opened) setOpen(slug);
    setNote(`Saved “${row?.title ?? slug}”.`);
    void loadPages();
    void loadPage(slug);
  };

  const refresh = async () => {
    setRefreshing(true); setNote(null);
    const r = await maintainWiki(w);
    setRefreshing(false);
    if (!r.ok) { setNote(r.error); return; }
    const { written, proposed } = r.data;
    setNote(`${written.length} page(s) updated from the graph`
      + (proposed.length ? `; ${proposed.length} proposed for your approval (edited by a person)` : "") + ".");
    void loadPages();
    if (open) void loadPage(open);
  };

  return (
    <div className="kg-wiki" data-testid="kg-wiki">
      <aside data-testid="wiki-list">
        <span className="kg-doc-pick">
          <GatedButton permission={WIKI_MANAGE} projectId={projectId} className="ag-btn" data-testid="wiki-new"
                       disabled={archived} onClick={() => away(() => setMode("new"))}>New page</GatedButton>
          <GatedButton permission={WIKI_MANAGE} projectId={projectId} className="inf-link" data-testid="wiki-refresh"
                       disabled={archived || refreshing} title="Render the graph's current facts into the kg-* pages"
                       onClick={() => void refresh()}>{refreshing ? "Refreshing…" : "Refresh from the graph"}</GatedButton>
        </span>
        {note ? <p className="kg-quiet" data-testid="wiki-note">{note}</p> : null}
        {pages.error ? <p className="kg-quiet" role="alert">{pages.error}</p> : null}
        {pages.data && pages.data.length === 0 ? (
          <p className="kg-quiet" data-testid="wiki-empty">No pages yet. Refresh from the graph, or write the first one.</p>
        ) : null}
        <ul className="kg-doc-clause-list">
          {(pages.data ?? []).map((p) => (
            <li key={p.slug}>
              <button type="button" className="inf-link" data-testid="wiki-page-link" aria-current={p.slug === open}
                      onClick={() => away(() => { setOpen(p.slug); setMode("read"); })}>{p.title}</button>
              {p.pending ? <span className="kg-quiet" data-testid="wiki-pending-count"> · {p.pending} waiting</span> : null}
            </li>
          ))}
        </ul>
      </aside>
      <section data-testid="wiki-page">
        {mode === "new" ? (
          <Editor w={w} slug={null} initial={NEW_PAGE} onCancel={() => away(() => setMode("read"))} onDirty={setDirty}
                  onSaved={(slug, row) => saved(slug, row, true)} />
        ) : page.error ? <p className="kg-quiet" role="alert">{page.error}</p>
          : !page.data ? <p className="kg-quiet">{open ? "Loading…" : "Pick a page."}</p>
          : mode === "edit" ? (
            <Editor w={w} slug={page.data.slug} initial={{ title: page.data.title, body: page.data.body }}
                    onCancel={() => away(() => setMode("read"))} onDirty={setDirty}
                    onSaved={(slug, row) => saved(slug, row, false)} />
          ) : (
            <article>
              <div className="kg-doc-head">
                <span className="kg-doc-title" data-testid="wiki-page-title">{page.data.title}</span>
                <span className="kg-quiet" data-testid="wiki-byline">
                  v{page.data.revision.revision_no} by <By rev={page.data.revision} />
                  {page.data.revision.created_at ? `, ${kgWhen(page.data.revision.created_at)}` : ""}
                </span>
                <GatedButton permission={WIKI_MANAGE} projectId={projectId} className="inf-link" data-testid="wiki-edit"
                             disabled={archived} onClick={() => setMode("edit")}>Edit</GatedButton>
                <button type="button" className="inf-link" data-testid="wiki-history-toggle"
                        onClick={() => setMode(mode === "history" ? "read" : "history")}>
                  {mode === "history" ? "Hide history" : "History"}
                </button>
              </div>
              {page.data.pending.length ? (
                <div className="kg-banner" data-tone="amber" data-testid="wiki-pending">
                  <span className="kg-banner-text">
                    {page.data.pending.length} proposed change(s) wait for approval in Approvals; nothing changes here until then.
                  </span>
                </div>
              ) : null}
              {mode === "history" ? (
                <History w={w} slug={page.data.slug}
                         onRestored={() => { setMode("read"); void loadPages(); void loadPage(page.data!.slug); }} />
              ) : (
                <div data-testid="wiki-body-view" data-user-text><Markdown text={linked(page.data.body)} /></div>
              )}
              {page.data.backlinks.length ? (
                <p className="kg-quiet" data-testid="wiki-backlinks">
                  Linked from:{" "}
                  {page.data.backlinks.map((b, i) => (
                    <React.Fragment key={b.slug}>
                      {i ? ", " : ""}
                      <button type="button" className="inf-link" onClick={() => { setOpen(b.slug); setMode("read"); }}>{b.title}</button>
                    </React.Fragment>
                  ))}
                </p>
              ) : null}
            </article>
          )}
      </section>
    </div>
  );
}
