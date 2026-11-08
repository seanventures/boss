"use client";

/**
 * ⌘K, EXTENDED WITH PROJECT RECORDS (BOSSAPP-914 screen 11, contract §9): the shell's palette asks
 * GET /v1/jump?q=&project_id= as you type and lists, grouped like the wireframe:
 *
 *   WORK               PR #3 · Visual refresh finish  [needs you]   ↵ drawer · ⌘↵ GitHub
 *                      Run · …  (Workflows) · Build · …  (Code Tasks)
 *   DEPLOYS & AGENTS   Staging · 203.0.113.20:8004  (open ↗) · Ask Helm about "pr 3"  (captain)
 *   DO                 New work in this project  N · Switch project…  P
 *
 * The open project is the scope (a tag in the input row; ✕ searches every project). ↑/↓ move the
 * selection across the whole list in the order it is drawn, ↵ opens the selected row, ⌘↵ opens
 * a PR on GitHub, Esc closes (the shell). With no query the project's newest things show.
 * On an engine without §9, projects and the actions still answer.
 */
import { LOCAL } from "../../lib/local-time";
import { changeHref } from "../../lib/repo-links";
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  CircleNotch, Code, FileText, FlowArrow, Folders, GitPullRequest, Globe, Plus, Robot, X, type Icon,
} from "@phosphor-icons/react";

import { FX_JUMP } from "../../lib/project-experience-fixtures";
import { PX_PATHS, isPxDemo, navigateTo, projectHref, pxHref } from "../../lib/project-experience";
import { AgentAvatar } from "../agent-avatar";
import { askCaptain } from "./project-page";
import "./jump.css";

export type JumpRow = {
  label: string; kind: string; href: string; sub: string; hint: string; needs_you: boolean;
  external_url: string; role_key: string; project_id: string;
  /** When it happened (a dated deploy row): said as the viewer's own day after the label. */
  at?: string;
};
const s = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));

/** GET /v1/jump's items (contract §9 + screen 11's `hint`, `needs_you`, `external_url`). */
export function parseJumpRows(v: unknown): JumpRow[] {
  const items = (v && typeof v === "object" && Array.isArray((v as { items?: unknown }).items)) ? (v as { items: unknown[] }).items : [];
  return items.map((z) => {
    const y = (z && typeof z === "object" ? z : {}) as Record<string, unknown>;
    return { label: s(y.label), kind: s(y.kind), href: s(y.href), sub: s(y.sub), hint: s(y.hint), needs_you: y.needs_you === true,
             external_url: changeHref(s(y.external_url)) ?? "", role_key: s(y.role_key), project_id: s(y.project_id), at: s(y.at) || undefined };
  }).filter((j) => j.label && (j.href || j.external_url));
}

const KIND: Record<string, { icon: Icon; tone: string }> = {
  pr: { icon: GitPullRequest, tone: "amber" }, run: { icon: FlowArrow, tone: "teal" }, build: { icon: Code, tone: "blue" },
  deploy: { icon: Globe, tone: "green" }, agent: { icon: Robot, tone: "violet" }, project: { icon: Folders, tone: "blue" },
  doc: { icon: FileText, tone: "blue" },
};
const GROUPS: { label: string; kinds: string[] }[] = [
  { label: "Work", kinds: ["pr", "run", "build"] }, { label: "Deploys & agents", kinds: ["deploy", "agent"] },
  { label: "Projects & docs", kinds: ["project", "doc"] },
];

/** Every row of the open palette, in the order it is drawn on screen. */
function paletteRows(root: HTMLElement | null): HTMLButtonElement[] {
  const list = root?.closest(".bx-palette")?.querySelector(".bx-palette-list");
  if (!list) return [];
  return [...list.querySelectorAll<HTMLButtonElement>(".bx-palette-item")]
    .filter((b) => !b.closest("[hidden]") && !b.disabled)
    // Drawn order (the records sit above the areas by CSS `order`), DOM order on a tie.
    .map((b, i) => ({ b, i, top: b.getBoundingClientRect().top }))
    .sort((x, y) => x.top - y.top || x.i - y.i).map((x) => x.b);
}

export function JumpResults({ query, projectId, projects, onDone }: {
  query: string; projectId: string; projects: { id: string; name: string }[]; onDone: () => void;
}) {
  const q = query.trim();
  const here = useRef<HTMLDivElement>(null);
  const [scoped, setScoped] = useState(true);
  const [items, setItems] = useState<JumpRow[]>([]);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [sel, setSel] = useState(-1);
  const [palette, setPalette] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => { setPalette(here.current?.closest<HTMLElement>(".bx-palette") ?? null); }, []);
  const scope = scoped ? projectId : "";
  const projectName = projects.find((p) => p.id === projectId)?.name ?? "";

  useEffect(() => {
    if (isPxDemo()) {
      setItems(parseJumpRows(FX_JUMP).filter((i) => !q || match(i.label, q) || match(i.sub, q)));
      return undefined;
    }
    const ctl = new AbortController();
    setState("loading");
    const t = window.setTimeout(() => {
      fetch(PX_PATHS.jump(q, scope || undefined), { cache: "no-store", signal: ctl.signal })
        .then(async (r) => {
          if (!r.ok) { setItems([]); setState(r.status === 404 ? "idle" : "error"); return; }
          setItems(parseJumpRows(await r.json())); setState("idle");
        })
        .catch(() => { if (!ctl.signal.aborted) setState("error"); });
    }, q ? 140 : 0);
    return () => { window.clearTimeout(t); ctl.abort(); };
  }, [q, scope]);

  // The selection follows the list: the first record row when a query finds one.
  useEffect(() => { setSel(-1); }, [q, scope]);
  useLayoutEffect(() => {
    const rows = paletteRows(here.current);
    // The first drawn row is chosen until ↑/↓ moves it (↵ takes it), as the wireframe shows.
    rows.forEach((b, i) => b.classList.toggle("active", i === (sel >= 0 ? sel : 0)));
    if (sel >= 0) rows[sel]?.scrollIntoView?.({ block: "nearest" });
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const rows = paletteRows(here.current);
      if (!rows.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setSel((cur) => (e.key === "ArrowDown" ? Math.min(rows.length - 1, cur + 1) : Math.max(0, cur - 1)));
        return;
      }
      if (e.key === "Enter") {
        const target = rows[sel >= 0 ? sel : 0];
        if (!target) return;
        e.preventDefault(); e.stopPropagation();
        const ext = target.getAttribute("data-external");
        if ((e.metaKey || e.ctrlKey) && ext) { onDone(); window.open(ext, "_blank", "noopener,noreferrer"); return; }
        target.click();
        return;
      }
      // The Do row keys, once the person is choosing from the list (never while typing a query).
      if (sel >= 0 && !e.metaKey && !e.ctrlKey && !e.altKey && (e.key === "n" || e.key === "N" || e.key === "p" || e.key === "P")) {
        const id = e.key.toLowerCase() === "n" ? "palette-jump-new-work" : "palette-jump-switch";
        const b = here.current?.querySelector<HTMLButtonElement>(`[data-testid="${id}"]`);
        if (b) { e.preventDefault(); e.stopPropagation(); b.click(); }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [sel, onDone]);

  const captain = items.find((i) => i.kind === "captain") ?? null;
  const local: JumpRow[] = projects.filter((p) => q && match(p.name, q) && !items.some((i) => i.kind === "project" && i.label === p.name))
    .slice(0, 5).map((p) => ({ label: p.name, kind: "project", href: projectHref(p.id), sub: "project", hint: "Project", needs_you: false,
                               external_url: "", role_key: "", project_id: p.id }));
  const all = [...items.filter((i) => i.kind !== "captain"), ...local];
  const go = (it: JumpRow, external = false) => {
    onDone();
    const href = external && it.external_url ? it.external_url : it.href || it.external_url;
    if (/^https?:\/\//.test(href)) window.open(href, "_blank", "noopener,noreferrer");
    else if (href.startsWith("/changes/")) window.location.assign(href);   // a local change set: BOSS's own view
    else navigateTo(pxHref(href));
  };
  const [switching, setSwitching] = useState(false);
  const askLabel = captain ? `Ask ${captain.label}${q ? ` about “${q}”` : "…"}` : `Ask about “${q}”`;
  const nothing = q && state !== "loading" && !all.length;

  return (
    <div ref={here} className="pxj" data-testid="palette-jump" data-has={all.length || undefined}>
      {palette && projectId ? createPortal(
        <span className="pxj-in" data-testid="palette-jump-inrow">
          {state === "loading" ? <CircleNotch size={14} className="pxj-spin" aria-label="Searching" /> : null}
          {scoped && projectName ? (
            <span className="pxj-scope" data-testid="palette-jump-scope" title="Searching this project">
              <Folders size={12} aria-hidden />{projectName}
              <button type="button" aria-label="Search every project" title="Search every project" data-testid="palette-jump-unscope"
                      onClick={() => {
                        setScoped(false);
                        // 11-02 the typing goes on: focus returns to the query, never to the page.
                        palette?.querySelector<HTMLInputElement>("input")?.focus();
                      }}><X size={10} weight="bold" /></button>
            </span>
          ) : null}
          <kbd className="pxj-kbd">esc</kbd>
        </span>, palette) : null}
      {state === "error" ? <div className="pxj-note" data-testid="palette-jump-error">Search unavailable</div> : null}
      {GROUPS.map((g) => {
        const rows = all.filter((i) => g.kinds.includes(i.kind));
        const withCaptain = g.label === "Deploys & agents" && captain;
        if (!rows.length && !withCaptain) return null;
        return (
          <div key={g.label} className="pxj-g" role="group" aria-label={g.label}>
            <span className="pxj-lbl">{g.label}</span>
            {rows.map((it, i) => {
              const k = KIND[it.kind] ?? KIND.doc;
              const I = k.icon;
              return (
                <button key={`${it.kind}-${it.href}-${i}`} type="button" className="bx-palette-item pxj-i" data-testid={`palette-jump-${it.kind}`}
                        data-external={it.kind === "pr" ? it.external_url || undefined : undefined}
                        onClick={(e) => go(it, e.metaKey || e.ctrlKey)} title={it.sub && it.sub !== it.hint ? it.sub : undefined}>
                  <span className="pxj-ico" data-c={it.needs_you ? "amber" : k.tone} aria-hidden><I size={14} /></span>
                  <span className="pxj-t">{it.at && Number.isFinite(Date.parse(it.at)) ? `${it.label} · ${LOCAL.day(Date.parse(it.at))}` : it.label}</span>
                  {it.needs_you ? <span className="pxj-tag" data-testid="palette-jump-needs">needs you</span> : null}
                  <span className="pxj-k">{it.hint || it.sub}</span>
                </button>
              );
            })}
            {withCaptain ? (
              <button type="button" className="bx-palette-item pxj-i" data-testid="palette-jump-ask"
                      onClick={() => { onDone(); askCaptain(projectId, { name: captain.label, role_key: captain.role_key }, q); }}>
                <AgentAvatar agent={captain.role_key || "engineering_captain"} size={22} aiBadge={false} />
                <span className="pxj-t">{askLabel}</span><span className="pxj-k">captain</span>
              </button>
            ) : null}
          </div>
        );
      })}
      {nothing ? (
        <button type="button" className="bx-palette-item pxj-i pxj-empty" data-testid="palette-jump-empty"
                onClick={() => { onDone(); askCaptain(projectId, captain ? { name: captain.label, role_key: captain.role_key } : null, q); }}>
          <span className="pxj-ico" aria-hidden><Robot size={14} /></span><span className="pxj-t">No matches · Ask {captain?.label ?? "Helm"}</span>
        </button>
      ) : null}
      <div className="pxj-g pxj-do" role="group" aria-label="Do">
        <span className="pxj-lbl">Do</span>
        {!captain && q ? (
          <button type="button" className="bx-palette-item pxj-i" data-testid="palette-jump-ask" onClick={() => { onDone(); askCaptain(projectId, null, q); }}>
            <span className="pxj-ico" aria-hidden><Robot size={14} /></span><span className="pxj-t">{askLabel}</span><span className="pxj-k">captain</span>
          </button>
        ) : null}
        <button type="button" className="bx-palette-item pxj-i" data-testid="palette-jump-new-work"
                onClick={() => { onDone(); navigateTo(projectHref(projectId, { tab: "work", compose: "1" })); }}>
          <span className="pxj-ico" aria-hidden><Plus size={14} /></span><span className="pxj-t">New work in this project</span><kbd className="pxj-key">N</kbd>
        </button>
        <button type="button" className="bx-palette-item pxj-i" data-testid="palette-jump-switch" aria-expanded={switching}
                onClick={() => setSwitching((v) => !v)}>
          <span className="pxj-ico" aria-hidden><Folders size={14} /></span><span className="pxj-t">Switch project…</span><kbd className="pxj-key">P</kbd>
        </button>
        {switching ? projects.filter((p) => p.id !== projectId).map((p) => (
          <button key={p.id} type="button" className="bx-palette-item pxj-i pxj-sub" data-testid={`palette-jump-to-${p.id}`}
                  onClick={() => { onDone(); navigateTo(projectHref(p.id)); }}>
            <span className="pxj-ico" aria-hidden><Folders size={14} /></span><span className="pxj-t">{p.name}</span><span className="pxj-k">Project</span>
          </button>
        )) : null}
      </div>
    </div>
  );
}

function match(text: string, q: string): boolean {
  const t = text.toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => t.includes(w.replace(/^#/, "")));
}
