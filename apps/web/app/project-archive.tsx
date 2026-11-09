"use client";

/**
 * ARCHIVE A PROJECT, in the console (BOSSAPP-768 S5; KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.8, §5.9).
 *
 *   ArchiveDialog    the confirm the project menu's "Archive project…" opens: it NAMES WHAT STOPS
 *                    (read from the API, counted for this project) and what stays, then archives.
 *   ArchivedProjects the project menu's "Show archived" view: each archived project, when and by
 *                    whom, with Restore.
 *   ArchivedNotice   the read-only banner the header strip shows while the open project is
 *                    archived, with Restore.
 *
 * Every state is designed: reading, failed (the API's sentence + Retry), refused (the API says why,
 * the dialog stays open), working, empty. Restore and Archive mirror `project:delete`; the API
 * decides, and a stale second factor opens "Confirm it's you" (lib/api-write.ts).
 */
import React, { useCallback, useEffect, useId, useRef, useState } from "react";

import { GatedButton } from "../components/gated";
import { showToast } from "./client-forms";
import {
  archiveProject, listArchivedProjects, readArchive, restoreProject,
  type ArchivedProject, type ArchiveState,
} from "./project-archive-actions";
import styles from "./project-archive.module.css";

/** "Archived 3 Oct 2026 by ceo", a date a person reads, not an ISO string. */
export function archivedSentence(at: string | null | undefined, by?: string | null): string {
  if (!at) return "Archived";
  const when = new Date(at);
  const day = Number.isNaN(when.getTime()) ? at
    : when.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  return `Archived ${day}${by ? ` by ${by}` : ""}`;
}

const ARCHIVE_TIP = "Hide this project and pause its schedules, monitors and indexing. Nothing is deleted";
const RESTORE_TIP = "List it again and resume its schedules, monitors and indexing";

export function ArchiveDialog({ projectId, name, onClose, onArchived }: {
  projectId: string;
  name: string;
  onClose: () => void;
  onArchived: (state: ArchiveState) => void;
}) {
  const [preview, setPreview] = useState<ArchiveState | null>(null);
  const [state, setState] = useState<"reading" | "ready" | "failed" | "working">("reading");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const titleId = useId();
  const bodyId = useId();

  const load = useCallback(async () => {
    setState("reading");
    setError(null);
    const got = await readArchive(projectId);
    if (got.ok) { setPreview(got.data); setState("ready"); }
    else { setError(got.error); setState("failed"); }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { cancelRef.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && state !== "working") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, state]);

  const confirm = async () => {
    setState("working");
    setError(null);
    const done = await archiveProject(projectId, note);
    if (done.ok) {
      showToast("success", `${name} is archived`);
      onArchived(done.data);
      return;
    }
    // REFUSED IN WORDS, AND THE DIALOG STAYS: the home project, the org's last open one, a
    // missing permission. Closing on a refusal would leave the operator guessing what happened.
    setError(done.error);
    setState("ready");
  };

  return (
    <div className={styles.backdrop} data-testid="archive-dialog-backdrop"
         onPointerDown={(event) => { if (event.target === event.currentTarget && state !== "working") onClose(); }}>
      <div className={styles.dialog} role="alertdialog" aria-modal="true" aria-labelledby={titleId}
           aria-describedby={bodyId} data-testid="archive-dialog" data-state={state}>
        <h2 id={titleId} className={styles.title}>Archive {name}?</h2>
        <div id={bodyId} className={styles.body}>
          {state === "reading" ? (
            <div className={styles.skeleton} data-testid="archive-dialog-reading" aria-busy="true">
              <span /><span /><span />
              <p className={styles.muted}>Reading what this project runs…</p>
            </div>
          ) : null}
          {state === "failed" ? (
            <div className={styles.error} role="alert" data-testid="archive-dialog-failed">
              <p>Could not read what this project runs, {error}</p>
              <button type="button" className={styles.secondary} onClick={() => void load()}>Retry</button>
            </div>
          ) : null}
          {preview && (state === "ready" || state === "working") ? (
            <>
              <section aria-label="What stops" data-testid="archive-stops">
                <h3 className={styles.heading}>What stops</h3>
                <ul className={styles.stops}>
                  {(preview.stops ?? []).map((line) => <li key={line}>{line}</li>)}
                </ul>
              </section>
              <section aria-label="What stays" data-testid="archive-keeps">
                <h3 className={styles.heading}>What stays</h3>
                <ul className={styles.keeps}>
                  {(preview.keeps ?? []).map((line) => <li key={line}>{line}</li>)}
                </ul>
              </section>
              <label className={styles.note}>
                Why (optional)
                <input value={note} maxLength={500} onChange={(event) => setNote(event.target.value)}
                       placeholder="e.g. case study finished" data-testid="archive-note" />
              </label>
            </>
          ) : null}
          {error && state === "ready" ? (
            <p className={styles.refused} role="alert" data-testid="archive-dialog-refused">{error}</p>
          ) : null}
        </div>
        <div className={styles.actions}>
          <button ref={cancelRef} type="button" className={styles.secondary} onClick={onClose}
                  disabled={state === "working"} data-testid="archive-cancel">Cancel</button>
          <GatedButton permission="project:delete" className={styles.danger} title={ARCHIVE_TIP}
                       disabled={state !== "ready"} data-testid="archive-confirm"
                       onClick={() => void confirm()}>
            {state === "working" ? "Archiving…" : "Archive project"}
          </GatedButton>
        </div>
      </div>
    </div>
  );
}

function RestoreButton({ project, onRestored }: { project: { id: string; name: string }; onRestored: (id: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restore = async () => {
    setBusy(true);
    setError(null);
    const done = await restoreProject(project.id);
    setBusy(false);
    if (done.ok) { showToast("success", `${project.name} is restored`); onRestored(project.id); }
    else setError(done.error);
  };
  return (
    <span className={styles.restoreWrap}>
      <GatedButton permission="project:delete" className={styles.restore} title={RESTORE_TIP}
                   disabled={busy} data-testid={`restore-${project.id}`} onClick={() => void restore()}>
        {busy ? "Restoring…" : "Restore"}
      </GatedButton>
      {error ? <span className={styles.refused} role="alert">{error}</span> : null}
    </span>
  );
}

/** The project menu's "Show archived" view. */
export function ArchivedProjects({ orgId, onRestored }: { orgId?: string; onRestored: (id: string) => void }) {
  const [items, setItems] = useState<ArchivedProject[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    const got = await listArchivedProjects(orgId);
    if (got.ok) setItems(got.data);
    else { setItems(null); setError(got.error); }
  }, [orgId]);
  useEffect(() => { void load(); }, [load]);

  if (error) {
    return (
      <div className={styles.error} role="alert" data-testid="archived-failed">
        <p>Could not read archived projects, {error}</p>
        <button type="button" className={styles.secondary} onClick={() => void load()}>Retry</button>
      </div>
    );
  }
  if (items === null) return <p className={styles.muted} data-testid="archived-reading">Reading archived projects…</p>;
  if (items.length === 0) return <p className={styles.muted} data-testid="archived-empty">No archived projects.</p>;
  return (
    <ul className={styles.archivedList} aria-label="Archived projects" data-testid="archived-list">
      {items.map((p) => (
        <li key={p.id} data-testid={`archived-${p.id}`}>
          <span className={styles.archivedName}>
            {p.name}
            <span className={styles.badge}>Archived</span>
          </span>
          <span className={styles.muted}>{archivedSentence(p.archived_at, p.archived_by)}</span>
          <RestoreButton project={p} onRestored={onRestored} />
        </li>
      ))}
    </ul>
  );
}

/** Whether the open project is archived: read once per project (not on the strip's poll). */
export function useArchived(projectId: string | undefined): ArchiveState | null {
  const [state, setState] = useState<ArchiveState | null>(null);
  useEffect(() => {
    let live = true;
    setState(null);
    if (!projectId) return;
    void readArchive(projectId).then((got) => { if (live && got.ok) setState(got.data); });
    return () => { live = false; };
  }, [projectId]);
  return state?.archived ? state : null;
}

/** The read-only banner while the open project is archived (§5.8 "archived project"). */
export function ArchivedNotice({ state, onRestored }: { state: ArchiveState; onRestored: (id: string) => void }) {
  return (
    <div className={styles.notice} role="status" data-testid="archived-notice">
      <span className={styles.badge}>Archived</span>
      <span>
        {state.name} takes no new work: its schedules, monitors and indexing are paused.
        {" "}<span className={styles.muted}>{archivedSentence(state.archived_at, state.archived_by)}.</span>
      </span>
      <RestoreButton project={state} onRestored={onRestored} />
    </div>
  );
}
