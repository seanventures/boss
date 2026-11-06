"use client";

/**
 * ⌘K, EXTENDED WITH PROJECT RECORDS (BOSSAPP-914 screen 11, contract §9): the shell's palette asks
 * GET /v1/jump?q=&project_id= as you type and lists runs, PRs, builds, deploys and agents of the
 * open project, grouped; plus the other projects, "Ask <captain> about …" and "New work in this
 * project". Enter takes the first item (the shell's rule); an external item (a PR) opens a tab.
 * On an engine without §9, projects and the two actions still answer.
 */
import React, { useEffect, useState } from "react";
import {
  ArrowSquareOut, ChatCircleDots, Code, FileText, Folders, GitPullRequest, Globe, Plus, Robot, FlowArrow, type Icon,
} from "@phosphor-icons/react";

import { FX_JUMP } from "../../lib/project-experience-fixtures";
import { PX_PATHS, isMissingStatus, isPxDemo, navigateTo, parseJump, projectHref, pxHref, type JumpItem } from "../../lib/project-experience";
import { askCaptain } from "./project-page";

const KIND_ICON: Record<string, Icon> = { pr: GitPullRequest, run: FlowArrow, build: Code, deploy: Globe, agent: Robot, project: Folders, doc: FileText };
const GROUPS: { label: string; kinds: string[] }[] = [
  { label: "Work", kinds: ["pr", "run", "build"] }, { label: "Deploys & agents", kinds: ["deploy", "agent"] }, { label: "Projects & docs", kinds: ["project", "doc"] },
];

export function JumpResults({ query, projectId, projects, onDone }: {
  query: string; projectId: string; projects: { id: string; name: string }[]; onDone: () => void;
}) {
  const q = query.trim();
  const [items, setItems] = useState<JumpItem[]>([]);
  useEffect(() => {
    if (!q) { setItems([]); return undefined; }
    if (isPxDemo()) { setItems(parseJump(FX_JUMP).filter((i) => match(i.label, q) || match(i.sub, q))); return undefined; }
    const ctl = new AbortController();
    const t = window.setTimeout(() => {
      fetch(PX_PATHS.jump(q, projectId), { cache: "no-store", signal: ctl.signal })
        .then(async (r) => (r.ok ? parseJump(await r.json()) : isMissingStatus(r.status) ? [] : []))
        .then(setItems).catch(() => { /* aborted or offline: the local answers still show */ });
    }, 140);
    return () => { window.clearTimeout(t); ctl.abort(); };
  }, [q, projectId]);
  if (!q) return null;
  const local: JumpItem[] = projects.filter((p) => match(p.name, q) && !items.some((i) => i.kind === "project" && i.label === p.name))
    .slice(0, 5).map((p) => ({ label: p.name, kind: "project", href: projectHref(p.id), sub: "project", external: false }));
  const all = [...items, ...local];
  const go = (it: JumpItem) => {
    onDone();
    if (it.external) window.open(it.href, "_blank", "noopener,noreferrer");
    else navigateTo(pxHref(it.href));
  };
  return (
    <div data-testid="palette-jump">
      {GROUPS.map((g) => {
        const rows = all.filter((i) => g.kinds.includes(i.kind));
        if (!rows.length) return null;
        return (
          <div key={g.label} className="px-jump-g" role="group" aria-label={g.label}>
            <span className="px-lbl" style={{ fontFamily: "var(--mono)", fontSize: ".54rem", letterSpacing: ".12em", textTransform: "uppercase", color: "var(--faint)", padding: "2px 8px" }}>{g.label}</span>
            {rows.map((it, i) => {
              const I = KIND_ICON[it.kind] ?? FileText;
              return (
                <button key={`${it.kind}-${it.href}-${i}`} type="button" className="bx-palette-item" data-testid={`palette-jump-${it.kind}`}
                        onClick={() => go(it)} title={it.external ? "Opens in a new tab" : undefined}>
                  <I size={17} aria-hidden />
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <span style={{ fontWeight: 600 }}>{it.label}</span>
                    {it.sub ? <span style={{ display: "block", fontSize: "0.78rem", color: it.sub === "needs you" ? "var(--amber)" : "var(--muted)" }}>{it.sub}</span> : null}
                  </span>
                  {it.external ? <ArrowSquareOut size={13} aria-label="opens in a new tab" /> : null}
                </button>
              );
            })}
          </div>
        );
      })}
      <div className="px-jump-g" role="group" aria-label="Do">
        <span style={{ fontFamily: "var(--mono)", fontSize: ".54rem", letterSpacing: ".12em", textTransform: "uppercase", color: "var(--faint)", padding: "2px 8px" }}>Do</span>
        <button type="button" className="bx-palette-item" data-testid="palette-jump-ask" onClick={() => { onDone(); askCaptain(projectId, null, q); }}>
          <ChatCircleDots size={17} aria-hidden /><span><span style={{ fontWeight: 600 }}>Ask about &ldquo;{q}&rdquo;</span>
            <span style={{ display: "block", fontSize: "0.78rem", color: "var(--muted)" }}>this project&apos;s captain</span></span>
        </button>
        <button type="button" className="bx-palette-item" data-testid="palette-jump-new-work"
                onClick={() => { onDone(); navigateTo(projectHref(projectId, { tab: "work", compose: "1" })); }}>
          <Plus size={17} aria-hidden /><span><span style={{ fontWeight: 600 }}>New work in this project</span></span>
        </button>
      </div>
    </div>
  );
}

function match(text: string, q: string): boolean {
  const t = text.toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => t.includes(w.replace(/^#/, "")));
}
