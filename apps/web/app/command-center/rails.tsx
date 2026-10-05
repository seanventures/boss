"use client";

/**
 * Two shelves (cc v2), each ONE horizontally scrolling row — snap, a fade at the right edge and
 * a › button — never a wrapping grid:
 *
 *   START FROM A TEMPLATE: the composer's sixteen IDEAS with their cover art. A card opens the
 *   template's own page (`?template=<slug>`, the page the composer already has); "All 16 ↗"
 *   opens the composer's gallery. Nothing is drawn twice.
 *
 *   BUILT BY YOUR TEAM: the Factory's apps (/v1/factory/apps). Cover art is the template's when
 *   the app is one, else a monogram; the chip is the Factory's state in two or three words; the
 *   builders' faces, builds and cost underneath. A card opens that app's build in the Factory.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { IDEAS, IdeaCover, ideaFor, openTemplate, type Idea } from "../ask-boss-hero";
import { appNeed, shelfOrder, shelfState, shelfWord, type AppNeed, type CcApp, type ShelfState } from "../../lib/command-center-apps";
import type { AttentionItem } from "../../lib/agents-live-board";
import { AgentAvatar } from "../agent-avatar";
import { nameOf } from "../agents/run-face";
import { addFeaturePatch } from "../../lib/compose-links";
import { Go, dollars } from "./hero";

/** One scrolling row. The › button scrolls a card's width; it hides at the end. */
export function Rail({ label, children, testId, resetKey }: {
  label: string; children: React.ReactNode; testId: string;
  /** When this changes (a filter), the row goes back to its start. */
  resetKey?: string;
}) {
  const track = useRef<HTMLDivElement>(null);
  const [end, setEnd] = useState(false);
  const [start, setStart] = useState(true);
  const read = useCallback(() => {
    const t = track.current;
    if (!t) return;
    setStart(t.scrollLeft <= 4);
    setEnd(t.scrollLeft + t.clientWidth >= t.scrollWidth - 4);
  }, []);
  useEffect(() => {
    read();
    const t = track.current;
    if (!t) return undefined;
    t.addEventListener("scroll", read, { passive: true });
    window.addEventListener("resize", read);
    return () => { t.removeEventListener("scroll", read); window.removeEventListener("resize", read); };
  }, [read, children]);
  useEffect(() => {
    const t = track.current;
    if (t) { t.scrollLeft = 0; read(); }
  }, [resetKey, read]);
  const nudge = (dir: 1 | -1) => {
    const t = track.current;
    if (!t) return;
    const card = t.firstElementChild as HTMLElement | null;
    const step = (card?.offsetWidth ?? 260) + 14;
    t.scrollBy({ left: dir * step, behavior: "smooth" });
  };
  return (
    <div className="cc-rail" data-end={end ? "true" : undefined} data-start={start ? "true" : undefined}>
      <div className="cc-rail-track" ref={track} role="group" aria-label={label} data-testid={testId}>
        {children}
      </div>
      {!start ? (
        <button type="button" className="cc-rail-btn prev" aria-label={`Back: ${label}`} onClick={() => nudge(-1)}>‹</button>
      ) : null}
      {!end ? (
        <button type="button" className="cc-rail-btn" aria-label={`More: ${label}`} data-testid={`${testId}-next`}
                onClick={() => nudge(1)}>›</button>
      ) : null}
    </div>
  );
}

function TemplateCard({ idea, scope }: { idea: Idea; scope: string }) {
  return (
    <button type="button" className="cc-tpl" data-testid="cc-tpl"
            title={`${idea.title} — open the brief. Nothing builds until you press Build.`}
            onClick={() => openTemplate(idea.title)}>
      <span className="cc-tpl-art" aria-hidden><IdeaCover idea={idea} scope={scope} /></span>
      <span className="cc-tpl-body" data-user-text>
        <span className="cc-tpl-tag">{idea.tag}</span>
        <b>{idea.title}</b>
        <span className="cc-tpl-who">For {ideaFor(idea)}</span>
      </span>
    </button>
  );
}

const GALLERY_PARAM = "templates";

/** ALL THE TEMPLATES ON THEIR OWN PAGE (independent test: "All 16" opened a cramped popover
 *  under the top bar). A page with a URL (`?templates=all`), Back and Escape close it, and a
 *  card opens that template's own page on top. */
function Gallery({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector(".tpl-page:not(.cc-gallery)")) onClose();
    };
    window.addEventListener("keydown", escape);
    const before = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", escape); document.body.style.overflow = before; };
  }, [onClose]);
  return createPortal(
    <div className="tpl-page cc-gallery" role="dialog" aria-modal="true" aria-label="Templates" data-testid="cc-gallery">
      <header className="tpl-page-head">
        <button type="button" className="tpl-back" onClick={onClose} data-testid="cc-gallery-back">← Back</button>
        <span className="tpl-crumb">Templates · {IDEAS.length}</span>
      </header>
      <div className="tpl-page-inner">
        <h1 className="cc-gallery-h">Start from a template</h1>
        <div className="cc-gallery-grid">
          {IDEAS.map((idea) => <TemplateCard key={idea.title} idea={idea} scope="gal" />)}
        </div>
      </div>
    </div>, document.body);
}

export function TemplatesShelf() {
  const [all, setAll] = useState(false);
  useEffect(() => {
    const sync = () => {
      try { setAll(new URL(window.location.href).searchParams.get(GALLERY_PARAM) === "all"); } catch { /* none */ }
    };
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const open = () => {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set(GALLERY_PARAM, "all");
      window.history.pushState(null, "", url.toString());
    } catch { /* none */ }
    setAll(true);
  };
  const close = useCallback(() => {
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete(GALLERY_PARAM);
      window.history.replaceState(null, "", url.toString());
    } catch { /* none */ }
    setAll(false);
  }, []);
  return (
    <section className="cc-shelf" aria-labelledby="cc-tpl-h" data-testid="cc-templates">
      <div className="cc-sec-h">
        <h2 id="cc-tpl-h">Start from a template</h2>
        <span className="cc-sub">Each one is a full brief your team can build today</span>
        <span className="cc-spacer" />
        <button type="button" className="cc-link" data-testid="cc-templates-all"
                title="All the templates on one page" onClick={open}>All {IDEAS.length} ↗</button>
      </div>
      <Rail label="Templates" testId="cc-tpl-rail">
        {IDEAS.map((idea: Idea) => <TemplateCard key={idea.title} idea={idea} scope="cc" />)}
      </Rail>
      {all ? <Gallery onClose={close} /> : null}
    </section>
  );
}

const norm = (s: string) => s.toLowerCase().replace(/[^a-z]/g, "");

/** The template an app was built from, matched by name ("Subscription-Audit Tool" is
 *  "Subscription audit"), else null. */
export function templateFor(app: string): Idea | null {
  const n = norm(app);
  if (!n) return null;
  for (const idea of IDEAS) {
    const k = norm(idea.title);
    if (k && (n.includes(k) || k.includes(n) || k.slice(0, 10) === n.slice(0, 10))) return idea;
  }
  return null;
}

/** Two letters from the app's name, on a colour field of its own. */
function Monogram({ name }: { name: string }) {
  let h = 0;
  for (const c of name) h = (h + c.charCodeAt(0)) % 100000;
  const hue = (h * 37) % 360;
  const ini = (name.match(/[A-Za-z]+/g) ?? ["?"]).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  return <span className="cc-mono" style={{ ["--h" as string]: hue }}><span>{ini}</span></span>;
}

export type ShelfFilter = "all" | "pr" | "needs" | "stopped";

export function filterApps(apps: CcApp[], f: ShelfFilter, needOf: (a: CcApp) => AppNeed | null = (a) => appNeed(a)): CcApp[] {
  if (f === "all") return apps;
  if (f === "stopped") return apps.filter((a) => ["stopped", "failed"].includes(shelfState(a, needOf(a))));
  return apps.filter((a) => shelfState(a, needOf(a)) === f);
}

export function AppsShelf({ apps, attention = [], orgId }: {
  apps: CcApp[] | null; attention?: AttentionItem[]; orgId?: string | null;
}) {
  const [filter, setFilter] = useState<ShelfFilter>("all");
  if (apps && apps.length === 0) return null;
  const needOf = (a: CcApp) => appNeed(a, attention);
  const list = apps ? filterApps(shelfOrder(apps, needOf), filter, needOf) : [];
  const n = (f: ShelfFilter) => (apps ? filterApps(apps, f, needOf).length : 0);
  const chips: [ShelfFilter, string][] = [["all", "All"], ["pr", "PR open"], ["needs", "Needs you"], ["stopped", "Stopped"]];
  return (
    <section className="cc-shelf" aria-labelledby="cc-apps-h" data-testid="cc-apps">
      <div className="cc-sec-h">
        <h2 id="cc-apps-h">Built by your team</h2>
        <div className="cc-filters" role="group" aria-label="Show">
          {chips.filter(([f]) => f === "all" || n(f) > 0).map(([f, word]) => (
            <button key={f} type="button" className="cc-f" aria-pressed={filter === f} data-testid={`cc-apps-f-${f}`}
                    onClick={() => setFilter(f)}>{word} {n(f)}</button>
          ))}
        </div>
        <span className="cc-spacer" />
        <Go className="cc-link" patch={{ area: "code_tasks", view: "apps", build: null, role: null }} orgId={orgId}
            testId="cc-factory-link">Factory ↗</Go>
      </div>
      <Rail label="Apps" testId="cc-apps-rail" resetKey={filter}>
        {list.map((a) => {
          const need = needOf(a);
          const state: ShelfState = shelfState(a, need);
          // The card opens what waits on you, else the latest build — the chip's own rule.
          const target = need?.runId ?? a.openRunId;
          const idea = templateFor(a.app);
          const builders = a.builders.slice(0, 5);
          return (
            <div key={a.key} className="cc-app" data-state={state}>
              <Go className="cc-app-open" patch={{ area: "code_tasks", view: "apps", build: target, role: null }}
                  orgId={orgId} testId="cc-app" title={`${a.app} · ${a.word}. Opens it in the Factory.`}>
                <span className="cc-app-art" data-state={state}>
                  {idea ? <IdeaCover idea={idea} scope={`app-${a.key}`} /> : <Monogram name={a.app} />}
                  <span className="cc-app-state" data-state={state}>{shelfWord(a, need)}</span>
                </span>
                <span className="cc-app-body">
                  <b data-user-text>{a.app}</b>
                  <span className="cc-app-meta">
                    <span className="cc-minis" aria-label={builders.map((k) => nameOf(k).name).join(", ")}>
                      {builders.map((k) => <span key={k} className="cc-mini"><AgentAvatar agent={k} size={22} aiBadge={false} /></span>)}
                    </span>
                    <span>{a.builds} build{a.builds === 1 ? "" : "s"} · {a.costUsd > 0 ? dollars(a.costUsd) : a.included ? "Included" : "$0"}</span>
                  </span>
                </span>
              </Go>
              {/* ADDING TO AN APP is the main job: its own door on every card (hover on a
                  desktop, always shown on touch), into the composer with the app chosen. */}
              <Go className="cc-app-add" patch={addFeaturePatch(a.key)} orgId={orgId} testId="cc-app-add"
                  title={`Add a feature to ${a.app}`} ariaLabel={`Add a feature to ${a.app}`}>
                ＋ Add a feature
              </Go>
            </div>
          );
        })}
      </Rail>
    </section>
  );
}
