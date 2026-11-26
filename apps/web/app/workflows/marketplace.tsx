"use client";

/**
 * THE SHARED LIBRARY (design PR #287, BOSSAPP-1296): one list of workflows to reuse, BOSS's and
 * your team's, each labelled, with tags and search. The engine is workflow_marketplace.
 *
 *     "Library is one list, labelled BOSS or Your team, with tag filters. Add to project ...
 *      Share to Library works from a project's workflow."
 *
 * A row opens its card: the steps, what it includes, its inputs, how many projects use it, and
 * the full brief behind a toggle. Two actions:
 *
 *   - Add to project: pick any project of the org you may add to. The workflow lands in that
 *     project's own workflows. A BOSS item follows Library updates until someone changes it
 *     there; a team item's new versions always wait for that project's Review update.
 *   - Edit a copy: add it here (if it is not yet) and open it in the editor. Saving changes the
 *     project's copy only; a later Library version is then offered, never forced.
 *
 * A workflow this project shared says so and can be unshared (installed copies stay).
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, Books, Check, MagnifyingGlass, PencilSimple, Plus, ShareNetwork, Trash,
} from "@phosphor-icons/react";

import { useCan } from "../../components/identity/identity-provider";
import { notifyChanged } from "../changed";
import { fetchLibraryShelf, installPack, unpublishPack } from "../../lib/workflow-api";
import { couldNot, reasonOf } from "../../lib/action-result";
import type { LibraryProject, MarketPack } from "../../lib/workflow-editor-types";

const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** "BOSS" or "Your team": who it comes from. */
export const sourceLabel = (p: MarketPack) => (p.source === "boss" ? "BOSS" : "Your team");

/** "BOSS · v1.0 · 3 workflows" — who, which version, how many. */
export function packMeta(p: MarketPack): string {
  return `${sourceLabel(p)} · v${p.version} · ${count(p.workflows.length, "workflow")}`;
}

/** The one-line description under a row: summary · steps · projects using it. */
export function rowLine(p: MarketPack): string {
  const steps = p.workflows.reduce((n, w) => n + (w.step_count ?? w.steps.length), 0);
  const used = p.used_count ?? (p.installed_in ?? []).length;
  return [p.summary || p.workflows[0]?.description || "", steps ? count(steps, "step") : "",
          count(used, "project")].filter(Boolean).join(" · ");
}

/** How a project's copy takes new versions: BOSS's by itself until changed; a team's always
 *  waits for that project's Review update (workflow_marketplace.publish offers, never pushes). */
export function updateHint(p: MarketPack): string {
  return p.source === "boss" ? "Follows Library updates until you change it."
    : "New versions wait for your review.";
}

/** Does the item match the search words (name, summary, tags, its workflows' names)? */
export function shelfMatches(p: MarketPack, query: string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = [p.name, p.summary, p.publisher, ...(p.tags ?? []),
               ...p.workflows.flatMap((w) => [w.name, w.description])].join(" ").toLowerCase();
  return words.every((w) => hay.includes(w));
}

/** Where the shelf drops to one column (library.css, `.wf-shelf`). */
export const SHELF_ONE_COLUMN = "(max-width: 860px)";

export function SharedLibrary({ projectId, onBack, onOpen }: {
  projectId: string;
  onBack: () => void;
  /** Open a workflow of THIS project in the editor (Edit a copy). */
  onOpen?: (key: string) => void;
}) {
  const [packs, setPacks] = useState<MarketPack[] | null>(null);
  const [projects, setProjects] = useState<LibraryProject[]>([]);
  const [failed, setFailed] = useState("");
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const installGate = useCan("workflow:install");
  // Unshare needs both: the project's workflow:publish and the org's workflow:share.
  const publishGate = useCan("workflow:publish");
  const shareGate = useCan("workflow:share");
  const unshareBlocked = (publishGate.allowed ? null : publishGate.reason)
    ?? (shareGate.allowed ? null : shareGate.reason);

  const load = useCallback(async () => {
    try {
      const shelf = await fetchLibraryShelf(projectId);
      setPacks(shelf.items);
      setProjects(shelf.projects);
      setFailed("");
    } catch (e) {
      setFailed(couldNot("load the Library", reasonOf(e)));
    }
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const tags = useMemo(() => {
    const seen: string[] = [];
    for (const p of packs ?? []) for (const t of p.tags ?? []) if (!seen.includes(t)) seen.push(t);
    return seen;
  }, [packs]);
  const shown = useMemo(() => (packs ?? []).filter(
    (p) => (!tag || (p.tags ?? []).includes(tag)) && shelfMatches(p, query)), [packs, tag, query]);
  const current = shown.find((p) => p.id === picked) ?? shown[0] ?? null;
  // OPENS AT THE TOP: search and tags first, never under the sticky header. The page arrives
  // wherever the Library list was scrolled to.
  useEffect(() => { window.scrollTo?.({ top: 0 }); }, []);

  return (
    <section className="wf-market" data-testid="wf-market" aria-label="Shared library">
      <div className="wf-market-bar">
        <button type="button" className="wf-btn wf-btn-ghost" data-testid="wf-market-back" onClick={onBack}>
          <ArrowLeft size={14} aria-hidden /> Workflows
        </button>
        <h3 className="wf-market-title"><Books size={16} aria-hidden /> Shared library</h3>
        <span className="wf-lib-spacer" />
        <label className="wf-lib-search">
          <MagnifyingGlass size={14} aria-hidden />
          <input type="search" value={query} placeholder="Search workflows" aria-label="Search workflows"
                 data-testid="wf-shelf-search" onChange={(e) => setQuery(e.target.value)} />
        </label>
      </div>

      {tags.length ? (
        <div className="wf-shelf-tags" role="group" aria-label="Tags" data-testid="wf-shelf-tags">
          {[null, ...tags].map((t) => (
            <button key={t ?? "all"} type="button" className="wf-shelf-tag" aria-pressed={tag === t}
                    data-testid={`wf-shelf-tag-${t ?? "all"}`} onClick={() => setTag(t)}>
              {t ?? "All"}
            </button>
          ))}
        </div>
      ) : null}

      {failed ? (
        <p className="wf-lib-error" role="alert" data-testid="wf-market-error">
          <span data-user-text>{failed}</span>
          <button type="button" className="wf-link" onClick={() => void load()}>Retry</button>
        </p>
      ) : null}

      {packs === null && !failed ? (
        <p className="wf-lib-muted" data-testid="wf-market-loading">Loading…</p>
      ) : shown.length ? (
        <div className="wf-shelf" data-testid="wf-market-grid">
          <div className="wf-shelf-list" role="listbox" aria-label="Workflows">
            {shown.map((p) => (
              <ShelfRow key={p.id} pack={p} selected={current?.id === p.id} here={projectId}
                        onPick={() => setPicked(p.id)} />
            ))}
          </div>
          {current ? (
            <ShelfCard key={current.id} pack={current} projectId={projectId} projects={projects}
                       reveal={picked === current.id}
                       installBlocked={installGate.allowed ? null : installGate.reason}
                       shareBlocked={unshareBlocked}
                       onOpen={onOpen} onChanged={load} />
          ) : null}
        </div>
      ) : packs ? (
        <p className="wf-lib-muted" data-testid="wf-market-empty">
          {query || tag ? "Nothing matches." : "Nothing shared yet. Share one of yours from its ⋯ menu."}
        </p>
      ) : null}
    </section>
  );
}

function ShelfRow({ pack: p, selected, here, onPick }: {
  pack: MarketPack; selected: boolean; here: string; onPick: () => void;
}) {
  const added = p.installed || (p.installed_in ?? []).includes(here);
  return (
    <div className="wf-shelf-row" role="option" tabIndex={0} aria-selected={selected}
         data-testid={`wf-pack-${p.id}`} data-installed={added || undefined}
         onClick={onPick}
         onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(); } }}>
      <span className="wf-shelf-name" data-user-text>{p.name}</span>
      <span className="wf-shelf-meta">
        {added ? (
          <span className="wf-pack-ok" data-testid={`wf-pack-installed-${p.id}`}>
            <Check size={12} weight="bold" aria-hidden /> Added
          </span>
        ) : null}
        <span className="wf-shelf-src" data-source={p.source} data-testid={`wf-pack-src-${p.id}`}>
          {sourceLabel(p)}
        </span>
      </span>
      <span className="wf-shelf-line" data-user-text data-testid={`wf-pack-line-${p.id}`}>{rowLine(p)}</span>
    </div>
  );
}

function ShelfCard({ pack: p, projectId, projects, installBlocked, shareBlocked, onOpen, onChanged, reveal = false }: {
  pack: MarketPack; projectId: string; projects: LibraryProject[];
  /** Picked by the person (not the first card shown by default): bring it into view. */
  reveal?: boolean;
  installBlocked: string | null; shareBlocked: string | null;
  onOpen?: (key: string) => void; onChanged: () => Promise<void>;
}) {
  const ref = useRef<HTMLElement | null>(null);
  // PICKED ON A PHONE (one column): bring the card into view. Only a pick: the default card
  // scrolling itself into view on arrival is what landed the page 126-577px down, the search
  // and tags under the sticky header. And only one column: beside the list it is already there.
  useEffect(() => {
    if (!reveal || !window.matchMedia?.(SHELF_ONE_COLUMN).matches) return;
    ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [p.id, reveal]);
  const choices = projects.length ? projects : [{ id: projectId, name: "This project" }];
  const [target, setTarget] = useState(choices.some((c) => c.id === projectId) ? projectId : choices[0].id);
  const [busy, setBusy] = useState<"add" | "edit" | "unshare" | null>(null);
  const [said, setSaid] = useState<{ text: string; tone?: "error"; open?: string } | null>(null);
  const [asking, setAsking] = useState(false);
  const first = p.workflows[0] ?? null;
  const steps = p.workflows.flatMap((w) => w.steps);
  const inputs = [...new Set(p.workflows.flatMap((w) => w.inputs ?? []))];
  const brief = p.workflows.map((w) => w.objective || w.description).filter(Boolean).join("\n\n");
  const includes = [
    ...p.needs.map((n) => `${n.label}${n.ok ? "" : ` (${n.note ?? "not set up"})`}`),
    ...p.agents.map((a) => a.name),
  ];
  const addedTo = (id: string) => (id === projectId && p.installed) || (p.installed_in ?? []).includes(id);
  const targetName = choices.find((c) => c.id === target)?.name ?? target;
  // THE PICKED PROJECT DECIDES (not the one the page is on): the server lists only projects where
  // the person holds workflow:install. An older API sends no list: this project's gate decides.
  const mayAdd = (id: string): string | null => projects.length
    ? (projects.some((c) => c.id === id) ? null : "You cannot add workflows to that project.")
    : installBlocked;
  const addBlocked = mayAdd(target);
  const editBlocked = mayAdd(projectId);

  const add = async () => {
    setBusy("add");
    setSaid(null);
    try {
      const done = await installPack(target, p.id);
      notifyChanged("marketplace");
      setSaid({ text: `Added to ${targetName}.`, open: target === projectId ? done.keys[0] : undefined });
      await onChanged();
    } catch (e) {
      setSaid({ text: String((e as Error)?.message ?? e), tone: "error" });
    } finally {
      setBusy(null);
    }
  };

  const editCopy = async () => {
    if (!first) return;
    setBusy("edit");
    setSaid(null);
    try {
      let key = first.key;
      if (!p.installed && !p.published_here) {
        const done = await installPack(projectId, p.id);
        notifyChanged("marketplace");
        key = done.keys[0] ?? key;
      }
      onOpen?.(key);
    } catch (e) {
      setSaid({ text: String((e as Error)?.message ?? e), tone: "error" });
    } finally {
      setBusy(null);
    }
  };

  const unshare = async () => {
    setBusy("unshare");
    try {
      await unpublishPack(projectId, p.id);
      notifyChanged("marketplace");
      await onChanged();
    } catch (e) {
      setSaid({ text: String((e as Error)?.message ?? e), tone: "error" });
    } finally {
      setBusy(null);
    }
  };

  return (
    <aside className="wf-shelf-card" ref={ref} aria-label={p.name} data-testid={`wf-shelf-card-${p.id}`}>
      <h4 className="wf-pack-name" data-user-text>{p.name}</h4>
      <div className="wf-pack-meta" data-testid={`wf-pack-meta-${p.id}`}
           title={p.source === "org" ? `Shared by ${p.publisher}` : undefined}>
        <span className="wf-shelf-src" data-source={p.source}>{sourceLabel(p)}</span>{" "}
        v{p.version}{p.source === "org" && p.publisher ? ` · by ${p.publisher}` : ""}
        {p.update_available ? (
          <span className="wf-tag" data-kind="modified" data-testid={`wf-pack-update-${p.id}`}
                title="You changed it here. Review the update on it in your workflows.">
            Update v{p.update_available}
          </span>
        ) : null}
        {p.waiting.length ? (
          <span className="wf-tag" data-kind="modified" data-testid={`wf-pack-waiting-${p.id}`}
                title={p.waiting[0]}>Waiting</span>
        ) : null}
      </div>
      {steps.length ? (
        <ol className="wf-shelf-steps" aria-label="Steps" data-testid={`wf-pack-steps-${p.id}`} data-user-text>
          {steps.map((s, i) => <li key={`${s}-${i}`}><span>{s}</span></li>)}
        </ol>
      ) : null}
      <dl className="wf-market-kv">
        {p.workflows.length > 1 ? (
          <><dt>Workflows</dt><dd data-user-text>{p.workflows.map((w) => w.name).join(" · ")}</dd></>
        ) : null}
        {includes.length ? (
          <><dt>Includes</dt><dd data-testid="wf-market-needs" data-user-text>{includes.join(", ")}</dd></>
        ) : null}
        {inputs.length ? (
          <><dt>Input</dt><dd data-testid={`wf-pack-inputs-${p.id}`} data-user-text>{inputs.join(", ")}</dd></>
        ) : null}
        <dt>Used</dt><dd>{count(p.used_count ?? (p.installed_in ?? []).length, "project")}</dd>
        {(p.tags ?? []).length ? <><dt>Tags</dt><dd>{(p.tags ?? []).join(", ")}</dd></> : null}
      </dl>
      {brief ? (
        <details className="wf-shelf-brief">
          <summary>Full brief</summary>
          <p data-user-text>{brief}</p>
        </details>
      ) : null}

      {p.published_here ? (
        <div className="wf-market-actions">
          <span className="wf-pack-ok" data-testid={`wf-pack-published-${p.id}`}>
            <ShareNetwork size={13} aria-hidden /> Shared from this project
          </span>
          {asking ? (
            <span className="wf-pack-ask" role="alertdialog" aria-label="Unshare?">
              Unshare?
              <button type="button" className="wf-btn wf-btn-danger wf-btn-sm" autoFocus
                      data-testid={`wf-pack-unpublish-go-${p.id}`} disabled={busy !== null}
                      title="Copies already added stay" onClick={() => { setAsking(false); void unshare(); }}>
                <Trash size={13} aria-hidden /> Unshare
              </button>
              <button type="button" className="wf-btn wf-btn-ghost wf-btn-sm" onClick={() => setAsking(false)}>
                Cancel
              </button>
            </span>
          ) : (
            <button type="button" className="wf-btn wf-btn-ghost wf-btn-sm" data-testid={`wf-pack-unpublish-${p.id}`}
                    disabled={busy !== null || Boolean(shareBlocked)} title={shareBlocked ?? "Copies already added stay"}
                    onClick={() => setAsking(true)}>
              <Trash size={13} aria-hidden /> {busy === "unshare" ? "Unsharing…" : "Unshare"}
            </button>
          )}
        </div>
      ) : (
        <>
          <div className="wf-shelf-add">
            <select className="wf-shelf-project" aria-label="Project" value={target}
                    data-testid={`wf-pack-project-${p.id}`} onChange={(e) => { setTarget(e.target.value); setSaid(null); }}>
              {choices.map((c) => (
                <option key={c.id} value={c.id}>{c.name}{addedTo(c.id) ? " ✓" : ""}</option>
              ))}
            </select>
            <button type="button" className="wf-btn wf-btn-primary" data-testid={`wf-pack-install-${p.id}`}
                    disabled={busy !== null || Boolean(addBlocked) || addedTo(target)}
                    title={addBlocked ?? (addedTo(target) ? `Already in ${targetName}` : undefined)}
                    onClick={() => void add()}>
              {addedTo(target) ? <Check size={14} weight="bold" aria-hidden /> : <Plus size={14} aria-hidden />}
              {busy === "add" ? "Adding…" : addedTo(target) ? "Added" : "Add to project"}
            </button>
            {onOpen && first ? (
              <button type="button" className="wf-btn" data-testid={`wf-pack-edit-${p.id}`}
                      disabled={busy !== null || (!p.installed && Boolean(editBlocked))}
                      title={!p.installed && editBlocked ? editBlocked
                        : "Open this project's copy in the editor. Your changes stay in this project."}
                      onClick={() => void editCopy()}>
                <PencilSimple size={14} aria-hidden /> {busy === "edit" ? "Opening…" : "Edit a copy"}
              </button>
            ) : null}
          </div>
          <p className="wf-shelf-hint" data-testid={`wf-pack-hint-${p.id}`}>{updateHint(p)}</p>
        </>
      )}
      {said ? (
        <p className="wf-pack-said" role={said.tone === "error" ? "alert" : "status"} data-tone={said.tone}
           data-testid={`wf-pack-said-${p.id}`}>
          {said.text}
          {said.open && onOpen ? (
            <> <button type="button" className="wf-link" onClick={() => onOpen(said.open!)}>Open</button></>
          ) : null}
        </p>
      ) : null}
    </aside>
  );
}
