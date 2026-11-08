"use client";

/**
 * DELETE A PROJECT, in the console (services/projects_delete.py).
 *
 * One dialog, opened from Project › Settings › Danger zone and from the project menu's "Delete
 * project…". It reads what goes (GET /v1/projects/{id}/delete) and shows it as a compact list,
 * then what stays, the export (or that there is none), the name typed back, and the step-up the
 * API asks for. The Delete button stays disabled with its reason until the name matches (and,
 * when a production app is live, until "Also take down production" is ticked).
 *
 * After the delete the console lands on Projects with "Deleted <name>": a full page load, so the
 * project is gone from the sidebar, ⌘K, Approvals and the bell at once.
 */
import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Archive, DotsThree, GearSix, Key, Trash, WarningCircle } from "@phosphor-icons/react";

import { useDetailsDismiss } from "../lib/details-dismiss";

import { GatedButton } from "../components/gated";
import { useGuardedCall } from "../components/identity/identity-provider";
import { IdentityError, refusalSentence } from "../lib/identity/api";
import { notifyChanged } from "./changed";
import { toastAfterLoad } from "./client-forms";
import { ArchiveDialog } from "./project-archive";
import { deleteProject, readDelete, type DeletePreview } from "./project-delete-actions";
import styles from "./project-delete.module.css";

const DELETE_TIP = "Delete this project and everything BOSS holds for it. The GitHub repository stays";

/** "12 workflow runs" — the label's own word, lower-cased after a number. */
export function countLine(count: number, label: string): string {
  return `${count.toLocaleString("en-GB")} ${label.charAt(0).toLowerCase()}${label.slice(1)}`;
}

/** Where the console lands after a delete: Projects, in the same organization, no project. */
export function projectsHref(search = typeof window === "undefined" ? "" : window.location.search): string {
  const cur = new URLSearchParams(search);
  const next = new URLSearchParams();
  const org = cur.get("org");
  if (org) next.set("org", org);
  next.set("area", "projects");
  return `/?${next.toString()}`;
}

/** Why the Delete button is off, or "" when it may be pressed. */
export function deleteBlocked(preview: DeletePreview | null, typed: string, alsoProd: boolean): string {
  if (!preview) return "Reading what this project holds…";
  if (preview.refused) return preview.refused;
  if (!typed.trim()) return `Type ${preview.name} to delete it.`;
  if (typed.trim() !== preview.name.trim()) return `The name doesn't match. Type ${preview.name} exactly.`;
  if (preview.production_apps.length && !alsoProd) return "Retire its production first, or tick Also take down production.";
  return "";
}

export function DeleteProjectDialog({ projectId, name, onClose }: {
  projectId: string;
  name: string;
  onClose: () => void;
}) {
  const [preview, setPreview] = useState<DeletePreview | null>(null);
  const [state, setState] = useState<"reading" | "ready" | "failed" | "working">("reading");
  const [error, setError] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [alsoProd, setAlsoProd] = useState(false);
  const guarded = useGuardedCall();
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const titleId = useId();
  const bodyId = useId();
  const fieldId = useId();
  const reasonId = useId();

  const load = useCallback(async () => {
    setState("reading");
    setError(null);
    try {
      setPreview(await readDelete(projectId));
      setState("ready");
    } catch (e) {
      setError(refusalSentence(e));
      setState("failed");
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { cancelRef.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && state !== "working") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, state]);

  const blocked = state === "reading" ? "Reading what this project holds…" : deleteBlocked(preview, typed, alsoProd);
  const shownName = preview?.name ?? name;

  const confirm = async () => {
    if (blocked || state !== "ready") return;
    setState("working");
    setError(null);
    try {
      await guarded(`Delete ${shownName}`, () => deleteProject(projectId, typed.trim(), alsoProd));
    } catch (e) {
      // REFUSED IN WORDS, AND THE DIALOG STAYS: a cancelled check, the home project, a live
      // production app, a server that would not let go of an app. Nothing was deleted.
      setError(e instanceof IdentityError && e.code === "step_up_cancelled"
        ? "Not deleted: you cancelled the check. Press Delete project again to confirm it's you."
        : refusalSentence(e));
      setState("ready");
      return;
    }
    toastAfterLoad("success", `Deleted ${shownName}`);
    notifyChanged("projects");
    window.location.assign(projectsHref());
  };

  const apps = preview ? [...preview.staging_apps, ...preview.production_apps] : [];
  return (
    <OnTop>
    <div className={styles.backdrop} data-testid="delete-dialog-backdrop"
         onPointerDown={(event) => { if (event.target === event.currentTarget && state !== "working") onClose(); }}>
      <div className={styles.dialog} role="alertdialog" aria-modal="true" aria-labelledby={titleId}
           aria-describedby={bodyId} data-testid="delete-dialog" data-state={state}>
        <h2 id={titleId} className={styles.title}>
          <Trash size={16} weight="duotone" aria-hidden /> Delete {shownName}?
        </h2>
        <div id={bodyId} className={styles.body}>
          <p className={styles.lede}>This removes the project and everything BOSS holds for it. It can&apos;t be undone.</p>
          {state === "reading" ? (
            <div className={styles.skeleton} data-testid="delete-dialog-reading" aria-busy="true">
              <span /><span /><span />
            </div>
          ) : null}
          {state === "failed" ? (
            <div className={styles.error} role="alert" data-testid="delete-dialog-failed">
              <p>Could not read what this project holds — {error}</p>
              <button type="button" className={styles.secondary} onClick={() => void load()}>Retry</button>
            </div>
          ) : null}
          {preview && (state === "ready" || state === "working") ? (
            <>
              {preview.refused ? (
                <p className={styles.refused} role="alert" data-testid="delete-home">{preview.refused}</p>
              ) : null}
              <section aria-label="What goes" data-testid="delete-goes">
                <h3 className={styles.heading}>What goes</h3>
                {preview.counts.length || apps.length || preview.graph.nodes ? (
                  <ul className={styles.counts}>
                    {preview.counts.map((c) => (
                      <li key={c.key} data-testid={`delete-count-${c.key}`}>
                        <b>{c.count.toLocaleString("en-GB")}</b> <span>{c.label.toLowerCase()}</span>
                      </li>
                    ))}
                  </ul>
                ) : <p className={styles.muted} data-testid="delete-empty">Nothing yet: the project holds no work or records.</p>}
                {apps.length ? (
                  <ul className={styles.apps} data-testid="delete-apps">
                    {apps.map((a) => (
                      <li key={a.target_id} data-prod={a.production ? "" : undefined}>
                        {a.app_name} <span className={styles.muted}>· {a.environment}{a.host ? ` on ${a.host}` : ""}</span>
                        {a.production ? <span className={styles.prodTag}>Production</span> : null}
                        <span className={styles.muted}> — taken off the server</span>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </section>
              <section aria-label="What stays" data-testid="delete-keeps">
                <h3 className={styles.heading}>What stays</h3>
                <ul className={styles.keeps}>
                  {preview.keeps.map((line) => <li key={line}>{line}</li>)}
                </ul>
              </section>
              <p className={styles.export} data-testid="delete-export">
                {preview.export.available && preview.export.href
                  ? <a href={preview.export.href} download>Download an export first</a>
                  : preview.export.note}
              </p>
              {preview.production_apps.length && !preview.refused ? (
                <label className={styles.prod} data-testid="delete-prod">
                  <input type="checkbox" checked={alsoProd} onChange={(e) => setAlsoProd(e.target.checked)}
                         data-testid="delete-also-prod" disabled={state === "working"} />
                  <span>
                    <WarningCircle size={14} weight="fill" aria-hidden /> {preview.production_apps.map((a) => a.app_name).join(", ")} is
                    live in production. Retire its production first, or tick to <b>also take down production</b>.
                  </span>
                </label>
              ) : null}
              {!preview.refused ? (
                <div className={styles.field}>
                  <label htmlFor={fieldId}>Type <b className={styles.mono}>{preview.name}</b> to confirm</label>
                  <input id={fieldId} value={typed} autoComplete="off" spellCheck={false}
                         onChange={(e) => setTyped(e.target.value)} disabled={state === "working"}
                         aria-describedby={reasonId} data-testid="delete-name"
                         onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void confirm(); } }} />
                  <p className={styles.stepup} data-testid="delete-stepup">
                    <Key size={13} aria-hidden /> Then confirm it&apos;s you with your passkey, if BOSS asks.
                  </p>
                </div>
              ) : null}
            </>
          ) : null}
          {error && state === "ready" ? (
            <p className={styles.refused} role="alert" data-testid="delete-dialog-refused">{error}</p>
          ) : null}
        </div>
        <div className={styles.actions}>
          <span id={reasonId} className={styles.reason} data-testid="delete-reason" aria-live="polite">
            {blocked && state !== "failed" && !preview?.refused ? blocked : ""}
          </span>
          <button ref={cancelRef} type="button" className={styles.secondary} onClick={onClose}
                  disabled={state === "working"} data-testid="delete-cancel">Cancel</button>
          <GatedButton permission="project:delete" className={styles.danger}
                       title={blocked || DELETE_TIP}
                       disabled={!!blocked || state !== "ready"} data-testid="delete-confirm"
                       data-blocked={blocked || undefined}
                       onClick={() => void confirm()}>
            {state === "working" ? "Deleting…" : "Delete project"}
          </GatedButton>
        </div>
      </div>
    </div>
    </OnTop>
  );
}

/** A dialog drawn at the top of the page (document.body), above the top bar and any stacking
 *  context of the page it was opened from (Settings' cards, the header). */
function OnTop({ children }: { children: React.ReactNode }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  return mounted ? createPortal(children, document.body) : null;
}

/** THE PROJECT'S ⋯ MENU, in its page header: Settings, Archive project…, Delete project…. */
export function ProjectMoreMenu({ projectId, name, home, onSettings }: {
  projectId: string;
  name: string;
  home: boolean;
  onSettings: () => void;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  const { onToggle, close } = useDetailsDismiss(ref);
  const [deleting, setDeleting] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const homeTip = "The home project can't be archived or deleted: BOSS's own agent and the worker's schedules live there";
  return (
    <>
      <details ref={ref} className={styles.more} data-testid="px-head-more" onToggle={onToggle}>
        <summary className="px-btn" aria-label="More for this project" title="More: settings, archive or delete this project">
          <DotsThree size={16} weight="bold" aria-hidden />
        </summary>
        <div className={styles.moreMenu} role="menu">
          <button type="button" role="menuitem" data-testid="px-more-settings" onClick={() => { close(); onSettings(); }}>
            <GearSix size={14} aria-hidden /> Settings</button>
          {home ? (
            <>
              <button type="button" role="menuitem" aria-disabled="true" title={homeTip} data-testid="px-more-archive"
                      onClick={(e) => e.preventDefault()}><Archive size={14} aria-hidden /> Archive project…<span className="sr-only">{homeTip}</span></button>
              <button type="button" role="menuitem" aria-disabled="true" title={homeTip} data-testid="px-more-delete" data-danger=""
                      onClick={(e) => e.preventDefault()}><Trash size={14} aria-hidden /> Delete project…<span className="sr-only">{homeTip}</span></button>
            </>
          ) : (
            <>
              <GatedButton permission="project:delete" role="menuitem" data-testid="px-more-archive"
                           title="Hide this project and pause its schedules, monitors and indexing. Nothing is deleted"
                           onClick={() => { close(); setArchiving(true); }}><Archive size={14} aria-hidden /> Archive project…</GatedButton>
              <GatedButton permission="project:delete" role="menuitem" data-testid="px-more-delete" data-danger=""
                           title={DELETE_TIP} onClick={() => { close(); setDeleting(true); }}><Trash size={14} aria-hidden /> Delete project…</GatedButton>
            </>
          )}
        </div>
      </details>
      {deleting ? <DeleteProjectDialog projectId={projectId} name={name} onClose={() => setDeleting(false)} /> : null}
      {archiving ? (
        <OnTop><ArchiveDialog projectId={projectId} name={name} onClose={() => setArchiving(false)}
                       onArchived={() => window.location.assign(projectsHref())} /></OnTop>
      ) : null}
    </>
  );
}

/** Project › Settings' foot: Archive and Delete, quiet until pressed. */
export function DangerZone({ projectId, name, home }: {
  projectId: string;
  name: string;
  home: boolean;
}) {
  const [deleting, setDeleting] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const onArchive = () => setArchiving(true);
  const homeTip = "The home project can't be archived or deleted: BOSS's own agent and the worker's schedules live there";
  return (
    <section className={styles.zone} aria-labelledby="px-danger-zone" data-testid="px-set-danger">
      <h2 id="px-danger-zone" className={styles.zoneTitle}>Danger zone</h2>
      <div className={styles.zoneRow}>
        <div>
          <b>Archive project</b>
          <p className={styles.muted}>Hide it and pause its schedules, monitors and indexing. Nothing is deleted; restore it any time.</p>
        </div>
        {home ? (
          <button type="button" className={styles.secondary} aria-disabled="true" title={homeTip}
                  data-testid="px-danger-archive" onClick={(e) => e.preventDefault()}>Archive…<span className="sr-only">{homeTip}</span></button>
        ) : (
          <GatedButton permission="project:delete" className={styles.secondary} data-testid="px-danger-archive"
                       title="Hide this project and pause its schedules, monitors and indexing. Nothing is deleted"
                       onClick={onArchive}>Archive…</GatedButton>
        )}
      </div>
      <div className={styles.zoneRow}>
        <div>
          <b>Delete project</b>
          <p className={styles.muted}>Remove it and everything BOSS holds for it: runs, builds, decisions, apps on servers, its knowledge graph. The GitHub repository stays.</p>
        </div>
        {home ? (
          <button type="button" className={styles.dangerQuiet} aria-disabled="true" title={homeTip}
                  data-testid="px-danger-delete" onClick={(e) => e.preventDefault()}>Delete project…<span className="sr-only">{homeTip}</span></button>
        ) : (
          <GatedButton permission="project:delete" className={styles.dangerQuiet} data-testid="px-danger-delete"
                       title={DELETE_TIP} onClick={() => setDeleting(true)}>Delete project…</GatedButton>
        )}
      </div>
      {deleting ? <DeleteProjectDialog projectId={projectId} name={name} onClose={() => setDeleting(false)} /> : null}
      {archiving ? (
        <OnTop><ArchiveDialog projectId={projectId} name={name} onClose={() => setArchiving(false)}
                       onArchived={() => window.location.assign(projectsHref())} /></OnTop>
      ) : null}
    </section>
  );
}
