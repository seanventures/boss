"use client";

/**
 * The rail's PROJECTS section (BOSSAPP-914 screen 1): Projects, then each project (a dot coloured
 * by its state — amber needs you, teal working, green ok) for one-click switching, then
 * "+ Add project". The open project is highlighted; it replaces the top bar's project menu as
 * the way to move between projects (the menu stays for New project and archiving).
 */
import React from "react";
import { CaretRight, Folders, Plus } from "@phosphor-icons/react";

import { PX_PATHS, parseSummary, projectHref, usePxRead, type ProjectsSummary } from "../../lib/project-experience";
import { FX_SUMMARY } from "../../lib/project-experience-fixtures";
import { openAddProject } from "./kit";

export function ProjectsNav({ items, active, activeProjectId, onGo, open, onToggle }: {
  items: { id: string; name: string }[];
  /** The shell's active area. */
  active: string;
  activeProjectId: string | null;
  onGo: () => void;
  open: boolean;
  onToggle: () => void;
}) {
  const summary = usePxRead<ProjectsSummary>(PX_PATHS.summary(), parseSummary, { everyMs: 30_000, demo: () => parseSummary(FX_SUMMARY) });
  const cards = summary.data?.items;
  const list = cards?.length ? cards.map((c) => ({ id: c.id, name: c.name, dot: c.needs_you ? "you" : c.running || c.stage === "working" ? "now" : c.stage === "onboarding" ? "now" : "done" }))
    : items.map((p) => ({ id: p.id, name: p.name, dot: "" }));
  const onProjects = active === "projects";
  const onProject = active === "project";
  return (
    <div className="bx-nav-section" data-open={open || undefined} data-testid="nav-projects">
      <span style={{ display: "flex", alignItems: "center" }}>
        <button type="button" className={`bx-nav-item${onProjects ? " active" : ""}`} aria-current={onProjects ? "page" : undefined}
                data-area-nav="projects" data-label="Projects" title="Every project: its work, repo and deploys" onClick={onGo} style={{ flex: 1 }}>
          <Folders size={17} weight={onProjects || onProject ? "fill" : "regular"} aria-hidden />
          <span>Projects</span>
        </button>
        {list.length ? (
          <button type="button" className="bx-nav-caret-btn" aria-expanded={open} aria-label={open ? "Hide the project list" : "Show the project list"}
                  onClick={onToggle} data-testid="nav-projects-toggle"
                  style={{ background: "transparent", border: 0, color: "var(--faint)", cursor: "pointer", padding: "4px 6px" }}>
            <CaretRight size={12} weight="bold" style={{ transform: open ? "rotate(90deg)" : undefined, transition: "transform .15s" }} aria-hidden />
          </button>
        ) : null}
      </span>
      {open || onProject ? (
        <div className="bx-nav-children" data-testid="nav-projects-list">
          {(open ? list : list.filter((p) => p.id === activeProjectId)).map((p) => {
            const here = onProject && p.id === activeProjectId;
            return (
              <a key={p.id} className={`bx-nav-item bx-nav-child${here ? " active" : ""}`} aria-current={here ? "page" : undefined}
                 href={projectHref(p.id)} data-console-load data-testid={`nav-project-${p.id}`} title={p.name}>
                <span aria-hidden style={{ width: 7, height: 7, borderRadius: 2, flex: "none",
                  background: p.dot === "you" ? "var(--amber)" : p.dot === "now" ? "var(--teal)" : p.dot === "done" ? "var(--green)" : "var(--quiet)" }} />
                <span>{p.name}</span>
              </a>
            );
          })}
          {open ? (
            <button type="button" className="bx-nav-item bx-nav-child" data-testid="nav-add-project" onClick={() => openAddProject("import")}>
              <Plus size={13} aria-hidden /><span>Add project</span>
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
