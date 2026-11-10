"use client";

/**
 * THE FILE TREE (BOSSAPP-954, wireframe 6 §1; the operator's Code-tab notes): a real folder tree,
 * the same in Files changed and Code.
 *
 *   rows        folders before files, collapsible, an icon by file type, NAMES only (never full
 *               paths); a chain of single-child folders is one row ("local_x/helpdesk")
 *   open        the folders of the open file and of changed files start expanded
 *   changed     +/− and a dot for open comments
 *   width       the pane (review-dock.tsx) is resized by dragging its edge, remembered per person;
 *               a long name is cut in the MIDDLE so its start and extension both show
 *   go to file  fuzzy on the full path; results are the name and a faint folder; ↑ ↓ ↵
 *   breadcrumb  above the file: each folder segment opens that folder in the tree
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  CaretDown, CaretRight, File, FileC, FileCode, FileCpp, FileCSharp, FileCss, FileCsv, FileHtml, FileImage, FileJs,
  FileJsx, FileMd, FilePy, FileRs, FileSql, FileSvg, FileTs, FileTsx, FileTxt, FileVue, Folder, FolderOpen, MagnifyingGlass,
} from "@phosphor-icons/react";

import {
  ancestorsOf, buildTree, crumbs, defaultExpanded, fuzzyFiles, middleEllipsis, type TreeFile, type TreeNode,
} from "../../../../lib/change-review";

const LETTER: Record<string, string> = { added: "A", removed: "D", deleted: "D", renamed: "R", modified: "M",
                                         A: "A", D: "D", R: "R", M: "M" };

type Icon = typeof FileCode;
const BY_EXT: Record<string, Icon> = {
  ts: FileTs, tsx: FileTsx, js: FileJs, mjs: FileJs, cjs: FileJs, jsx: FileJsx, py: FilePy, css: FileCss, scss: FileCss,
  html: FileHtml, htm: FileHtml, md: FileMd, mdx: FileMd, sql: FileSql, svg: FileSvg, png: FileImage, jpg: FileImage,
  jpeg: FileImage, gif: FileImage, webp: FileImage, ico: FileImage, csv: FileCsv, txt: FileTxt, vue: FileVue, rs: FileRs,
  c: FileC, h: FileC, cpp: FileCpp, cs: FileCSharp,
};
export function iconFor(name: string): Icon {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return BY_EXT[ext] ?? (/^(dockerfile|makefile|\..+rc|.*\.(json|ya?ml|toml|sh|go|java|kt|rb|php|swift|xml))$/i.test(name) ? FileCode : File);
}

/** The letters a row has room for at this pane width and depth (≈7px a letter). */
const budget = (width: number, depth: number, stats: boolean) =>
  Math.max(8, Math.floor((width - 44 - depth * 12 - (stats ? 64 : 22)) / 7));

export function FileTree({ files, current, onPick, testid = "gr-tree-file", stats = false, reveal = null, width = 260 }: {
  files: TreeFile[]; current: string | null; onPick: (path: string) => void; testid?: string;
  /** Show +/− (Files changed); the Code tab shows the status letter. */
  stats?: boolean;
  /** A folder (or file) to open the tree at: its folders expand and the row scrolls into view. */
  reveal?: { path: string; n: number } | null;
  width?: number;
}) {
  const nodes = useMemo(() => buildTree(files), [files]);
  const [open, setOpen] = useState<Set<string>>(() => defaultExpanded(nodes, current));
  const keyOfFiles = files.map((f) => f.path).join("\n");
  useEffect(() => { setOpen((was) => new Set([...was, ...defaultExpanded(nodes, current)])); }, [keyOfFiles]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!current) return;
    setOpen((was) => new Set([...was, ...ancestorsOf(nodes, current)]));
  }, [current, nodes]);
  const ref = useRef<HTMLUListElement>(null);
  useEffect(() => {
    if (!reveal) return;
    setOpen((was) => new Set([...was, ...ancestorsOf(nodes, reveal.path), reveal.path]));
    window.setTimeout(() => {
      const el = ref.current?.querySelector(`[data-path="${CSS.escape(reveal.path)}"]`);
      if (el) { (el as HTMLElement).scrollIntoView({ block: "nearest" }); (el as HTMLElement).focus(); }
    }, 30);
  }, [reveal]); // eslint-disable-line react-hooks/exhaustive-deps
  const toggle = (p: string) => setOpen((s) => { const n = new Set(s); if (n.has(p)) n.delete(p); else n.add(p); return n; });
  return <ul className="gr-tree-l" role="tree" data-testid="gr-tree" ref={ref}>{nodes.map((n) =>
    <Node key={`${n.kind}:${n.path}`} node={n} depth={0} current={current} onPick={onPick} testid={testid} stats={stats}
          open={open} toggle={toggle} width={width} />)}</ul>;
}

function Node({ node, depth, current, onPick, testid, stats, open, toggle, width }: {
  node: TreeNode; depth: number; current: string | null; onPick: (p: string) => void; testid: string; stats: boolean;
  open: Set<string>; toggle: (p: string) => void; width: number;
}) {
  const pad = { paddingLeft: `${0.5 + depth * 0.75}rem` };
  const fit = budget(width, depth, stats);
  if (node.kind === "dir") {
    const isOpen = open.has(node.path);
    const Icon = isOpen ? FolderOpen : Folder;
    return (
      <li role="treeitem" aria-expanded={isOpen}>
        <button type="button" className="gr-tree-row" data-k="dir" style={pad} onClick={() => toggle(node.path)}
                data-testid="gr-tree-dir" data-path={node.path} title={node.path}>
          {isOpen ? <CaretDown size={11} aria-hidden /> : <CaretRight size={11} aria-hidden />}
          <Icon size={13} aria-hidden /><span className="gr-name">{middleEllipsis(node.name, fit)}</span>
        </button>
        {isOpen ? <ul role="group">{node.children.map((c) =>
          <Node key={`${c.kind}:${c.path}`} node={c} depth={depth + 1} current={current} onPick={onPick} testid={testid}
                stats={stats} open={open} toggle={toggle} width={width} />)}</ul> : null}
      </li>
    );
  }
  const st = node.status ? LETTER[node.status] ?? node.status.slice(0, 1).toUpperCase() : "";
  const Icon = iconFor(node.name);
  return (
    <li role="treeitem" aria-selected={node.path === current}>
      <button type="button" className="gr-tree-row" data-k="file" data-changed={node.changed || undefined} style={pad}
              aria-current={node.path === current ? "true" : undefined} title={node.path}
              onClick={() => onPick(node.path)} data-testid={testid} data-path={node.path}>
        <span className="gr-caret-gap" aria-hidden /><Icon size={13} aria-hidden />
        <span className="gr-name" data-testid="gr-tree-name">{middleEllipsis(node.name, fit)}</span>
        {node.open ? <span className="gr-dot" title={`${node.open} open comment${node.open === 1 ? "" : "s"}`}
                           data-testid="gr-comment-dot">●<i>{node.open}</i></span>
          : node.comments ? <span className="lr-count" title={`${node.comments} comment thread${node.comments === 1 ? "" : "s"}`}>{node.comments}</span> : null}
        <span className="cs-spacer" />
        {stats && node.changed ? <span className="gr-pm"><span className="cs-add">+{node.additions}</span>
          {node.deletions ? <span className="cs-del">−{node.deletions}</span> : null}</span> : null}
        {st ? <span className="lr-st" data-s={st}>{st}</span> : null}
      </button>
    </li>
  );
}

// ── go to file ───────────────────────────────────────────────────────────────────────────────

export function GoToFile({ paths, onPick, placeholder = "Go to file", testid = "lr-goto" }: {
  paths: string[]; onPick: (path: string) => void; placeholder?: string; testid?: string;
}) {
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const hits = useMemo(() => (q.trim() ? fuzzyFiles(paths, q) : []), [paths, q]);
  useEffect(() => { setAt(0); }, [q]);
  const choose = (p: string) => { onPick(p); setQ(""); };
  return (
    <div className="gr-goto">
      <label className="lr-goto"><MagnifyingGlass size={13} aria-hidden />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={placeholder} aria-label={placeholder} data-testid={testid}
               role="combobox" aria-expanded={hits.length > 0} aria-controls="gr-goto-list" aria-autocomplete="list"
               aria-activedescendant={hits[at] ? `gr-goto-${at}` : undefined}
               onKeyDown={(e) => {
                 if (e.key === "ArrowDown") { e.preventDefault(); setAt((i) => Math.min(hits.length - 1, i + 1)); }
                 else if (e.key === "ArrowUp") { e.preventDefault(); setAt((i) => Math.max(0, i - 1)); }
                 else if (e.key === "Enter" && hits[at]) { e.preventDefault(); choose(hits[at].path); }
                 else if (e.key === "Escape") setQ("");
               }} /></label>
      {hits.length ? (
        <ul className="gr-goto-l" id="gr-goto-list" role="listbox" data-testid="gr-goto-results">
          {hits.map((h, i) => (
            <li key={h.path} id={`gr-goto-${i}`} role="option" aria-selected={i === at}>
              <button type="button" onMouseEnter={() => setAt(i)} onClick={() => choose(h.path)} title={h.path} data-testid="gr-goto-hit">
                <b className="gr-name">{h.name}</b>{h.folder ? <span className="cs-faint gr-name">{h.folder}</span> : null}</button>
            </li>))}
        </ul>) : q.trim() ? <p className="cs-empty gr-goto-none">No file matches.</p> : null}
    </div>
  );
}

// ── the breadcrumb above a file ──────────────────────────────────────────────────────────────

export function Breadcrumb({ path, onFolder }: { path: string; onFolder: (folder: string) => void }) {
  return (
    <nav className="gr-crumb" aria-label="Path" data-testid="gr-breadcrumb">
      {crumbs(path).map((c, i) => (
        <React.Fragment key={c.path}>
          {i ? <span className="cs-faint" aria-hidden>/</span> : null}
          {c.file ? <b className="cs-mono" data-testid="lr-file-path-name">{c.name}</b>
            : <button type="button" className="lr-link" onClick={() => onFolder(c.path)} data-testid="gr-crumb">{c.name}</button>}
        </React.Fragment>))}
    </nav>
  );
}
