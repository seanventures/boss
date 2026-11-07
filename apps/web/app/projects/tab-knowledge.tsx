"use client";

/**
 * REPO & KNOWLEDGE (screen 06, contract §4): each repo with its access strip (Read · Branch ·
 * Open PR · Merge: you · Staging) and a live Re-check; Helm's dossier (stack chips, copyable
 * commands, the newest build's tests as a ring, the code map, hotspots as heat bars); a snippet
 * of the project's knowledge graph centred on its hottest entity, labels laid out so none
 * overlap; docs & decisions; and what agents learned on the job, with the run that taught it
 * and how often later runs reused it. No dossier yet: "Build dossier now" (the onboarding pass,
 * idempotent) with its progress. Prose lives in the drawers (`dossier`, `doc:<key>`).
 */
import React, { useMemo, useState } from "react";
import {
  ArrowRight, ArrowSquareOut, ArrowsClockwise, Atom, BookOpen, Brain, CaretRight, Check, Copy, Cube, Database, Eye, FileCode, FileText, Files,
  Gavel, GitBranch, GitPullRequest, GithubLogo, Graph, IdentificationBadge, Lightning, Lock, Package, Plus, RocketLaunch, Scales, Signpost,
  Sparkle, UploadSimple, Wrench, type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  PX_PATHS, isPxDemo, parseKnowledge, pxHref, pxWrite, startOnboarding, usePxRead, writePxUrl,
  type GraphNode, type Knowledge, type KnowledgeRepo, type StepState,
} from "../../lib/project-experience";
import { FX_KNOWLEDGE } from "../../lib/project-experience-fixtures";
import { UploadDocument, UploadPreview } from "../knowledge/kg-upload";
import type { KgUploadPreview } from "../../lib/kg-view";
import { Drawer, Empty, Face, Flow, Ico, PxLink, ReadGate, Ring, Said, Tag, openDrawer } from "./kit";
import { whenLocal } from "../../lib/local-time";
import type { PxCtx } from "./project-page";

const useKnowledge = (projectId: string, fast = false) =>
  usePxRead<Knowledge>(PX_PATHS.knowledge(projectId), parseKnowledge, { everyMs: fast ? 5_000 : 60_000, demo: () => parseKnowledge(FX_KNOWLEDGE) });

export function KnowledgeTab({ ctx }: { ctx: PxCtx }) {
  const [fast, setFast] = useState(false);
  const read = useKnowledge(ctx.projectId, fast);
  const building = read.data?.build?.state === "running";
  if (fast !== building) setFast(building);
  const graphHref = `?area=knowledge&project_id=${encodeURIComponent(ctx.projectId)}`;
  return (
    <div className="px-stack" data-testid="px-knowledge">
      <ReadGate read={read} what="Repo & knowledge" missing={
        <>
          <Repos ctx={ctx} repos={ctx.overview.project.repos.map((r) => ({ ...r, binding_id: "", checked: { ...r.access }, last_push: "", via: "", deploys_to: "" }))} />
          <div className="px-card"><Empty icon={Graph} tone="violet" title="The dossier didn't load"
                 line="Helm's read of the code (stack, commands, hotspots) shows here once BOSS answers. The Knowledge Graph holds what's mapped.">
            <a className="px-btn" href={pxHref(graphHref)} data-testid="px-kn-open-graph">Open Knowledge Graph</a></Empty></div>
        </>
      }>
        {(k) => (
          <>
            <Repos ctx={ctx} repos={k.repos.length ? k.repos : []} onChecked={read.refresh} />
            <div className="px-split px-kn-split">
              <div className="px-stack">
                <Dossier ctx={ctx} k={k} onStarted={read.refresh} />
                <Docs k={k} />
              </div>
              <div className="px-stack">
                <GraphCard ctx={ctx} k={k} fallbackHref={graphHref} onStarted={read.refresh} />
                <Learned k={k} />
              </div>
            </div>
          </>
        )}
      </ReadGate>
    </div>
  );
}

// ── repositories ─────────────────────────────────────────────────────────────────────────────

const accessState = (v: boolean | null): StepState => (v === true ? "done" : v === false ? "failed" : "todo");
const accessWord = (v: boolean | null, what: string) => (v === true ? `${what}: checked, OK` : v === false ? `${what}: the last check failed` : `${what}: not checked yet`);

function Repos({ ctx, repos, onChecked }: { ctx: PxCtx; repos: KnowledgeRepo[]; onChecked?: () => void }) {
  const [busy, setBusy] = useState("");
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const recheck = async (r: KnowledgeRepo) => {
    setBusy(r.full_name); setSaid({ text: "" });
    const w = await pxWrite("/v1/setup/checks", { project_id: ctx.projectId, target: r.binding_id ? `repository:${r.binding_id}` : "repository",
                                                  idempotency_key: `px-recheck-${r.binding_id || r.full_name}-${Date.now()}` }, "Re-check");
    setBusy("");
    setSaid(w.ok ? { text: w.demo ? "Sample mode — nothing was sent." : `Checked ${r.full_name} again.`, tone: "green" } : { text: w.error ?? "Not checked.", tone: "red" });
    if (w.ok && !w.demo) onChecked?.();
  };
  return (
    <section className="px-card" aria-labelledby="px-kn-repos" data-testid="px-kn-repos">
      <div className="px-card-h"><Ico icon={GithubLogo} c="teal" /><h2 id="px-kn-repos">Repositories</h2><span className="px-nbadge">{repos.length}</span>
        <a className="px-more" href={pxHref("?area=setup&focus=repository&open=add")} data-testid="px-kn-add-repo"><Plus size={11} /> Add repo</a></div>
      {repos.length ? repos.map((r) => (
        <div key={r.full_name} className="px-kn-repo" data-testid={`px-kn-repo-${r.full_name}`}>
          <div style={{ minWidth: 0 }}>
            <a className="px-kn-repo-name px-mono" href={r.external_url || `https://github.com/${r.full_name}`} target="_blank" rel="noreferrer noopener">
              {r.full_name} <ArrowSquareOut size={12} aria-label="opens in a new tab" /></a>
            <div className="px-kn-repo-meta">
              <span><GitBranch size={10} aria-hidden /> {r.branch}</span>
              {r.last_push ? <span>last push {r.last_push.replace(/ ago$/, "")}</span> : null}
              {r.via ? <span>via {r.via}</span> : null}
            </div>
          </div>
          <div className="px-kn-access" title="Access checks — BOSS re-checks before every run">
            <span className="px-lbl px-hide-phone">Access</span>
            <Flow testid={`px-kn-access-${r.full_name}`} nodes={[
              { label: "Read", state: accessState(r.checked.read), icon: <Eye size={12} aria-label={accessWord(r.checked.read, "Read")} /> },
              { label: "Branch", state: accessState(r.checked.write), icon: <GitBranch size={12} aria-label={accessWord(r.checked.write, "Branch")} /> },
              { label: "Open PR", state: accessState(r.checked.pr), icon: <GitPullRequest size={12} aria-label={accessWord(r.checked.pr, "Open PR")} /> },
              { label: "Merge: you", type: "you", icon: <Lock size={11} aria-label="By design a person merges" /> },
              ...(r.deploys_to ? [{ label: r.deploys_to, state: "done", icon: <RocketLaunch size={12} aria-hidden /> }] : []),
            ]} />
          </div>
          <GatedButton permission="setup:check" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-kn-recheck-${r.full_name}`}
                       title="Run the access checks again now" disabled={busy === r.full_name} onClick={() => void recheck(r)}>
            <ArrowsClockwise size={12} aria-hidden className={busy === r.full_name ? "px-spin" : undefined} /> {busy === r.full_name ? "Checking…" : "Re-check"}</GatedButton>
        </div>
      )) : (
        <Empty icon={GithubLogo} tone="teal" title="No repo yet" line="Add the repo this project builds in; Helm reads it and gets it ready for work.">
          <a className="px-btn" data-kind="primary" href={pxHref("?area=setup&focus=repository&open=add")}>Add a repo</a>
        </Empty>
      )}
      <Said text={said.text} tone={said.tone} />
    </section>
  );
}

// ── dossier ──────────────────────────────────────────────────────────────────────────────────

/** A stack fact's icon (phosphor has no brand marks: the kind of thing it is). */
export function stackIcon(name: string): Icon {
  const n = name.toLowerCase();
  if (/postgres|mysql|sqlite|mongo|redis|database/.test(n)) return Database;
  if (/docker|compose|kubernetes|helm/.test(n)) return Cube;
  if (/react|vue|svelte|angular/.test(n)) return Atom;
  if (/fastapi|flask|express|spring|django|rails|next/.test(n)) return Lightning;
  if (/vite|webpack|npm|pnpm|yarn|maven|gradle|cargo|make/.test(n)) return Package;
  return FileCode;
}
const CMD_WORDS: Record<string, string> = { install: "Install", test: "Test", build: "Build", run: "Run", lint: "Lint" };
const MAP_COLORS = ["var(--accent)", "var(--teal)", "var(--green)", "var(--amber)", "var(--faint)"];

function CopyCode({ label, value }: { label: string; value: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="px-kn-cmd" title={`Copy: ${value}`} aria-label={`Copy the ${label} command`} data-copied={done ? "1" : undefined}
            onClick={() => { void navigator.clipboard?.writeText(value); setDone(true); window.setTimeout(() => setDone(false), 1500); }}>
      <code>{value}</code>{done ? <Check size={11} aria-hidden /> : <Copy size={11} aria-hidden />}
    </button>
  );
}

function BuildDossier({ ctx, k, onStarted, title, line }: { ctx: PxCtx; k: Knowledge; onStarted: () => void; title: string; line: string }) {
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const b = k.build;
  const running = b?.state === "running";
  const start = async () => {
    const r = await startOnboarding(ctx.projectId);
    setSaid(r.ok ? { text: r.demo ? "Sample mode — nothing was sent." : "Helm is reading the repository.", tone: "green" } : { text: r.error ?? "Not started.", tone: "red" });
    if (r.ok && !r.demo) onStarted();
  };
  return (
    <div className="px-kn-build" data-testid="px-kn-build">
      <Empty icon={running ? Sparkle : IdentificationBadge} tone="teal" title={running ? "Helm is reading the repository" : title}
             line={running ? (b?.progress ? `Mapping the code · ${b.progress}` : "Mapping the code, the commands and the tests.") : line}>
        {running ? null : (
          <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-btn" data-kind="primary" data-testid="px-kn-build-now"
                       title="Helm reads the repository once: stack, commands, tests, hotspots (safe to press again)" onClick={() => void start()}>
            <Sparkle size={13} aria-hidden /> {b?.label || "Build dossier now"}</GatedButton>
        )}
      </Empty>
      {running && b?.nodes.length ? <Flow nodes={b.nodes.map((n) => ({ label: n.sub ? `${n.label} · ${n.sub}` : n.label, state: n.state }))} testid="px-kn-build-progress" /> : null}
      <Said text={said.text} tone={said.tone} />
    </div>
  );
}

function Dossier({ ctx, k, onStarted }: { ctx: PxCtx; k: Knowledge; onStarted: () => void }) {
  const d = k.dossier;
  if (!d) {
    return (
      <section className="px-card" aria-labelledby="px-kn-dossier" data-testid="px-kn-dossier-empty">
        <div className="px-card-h"><Ico icon={IdentificationBadge} c="teal" /><h2 id="px-kn-dossier">Dossier</h2></div>
        <BuildDossier ctx={ctx} k={k} onStarted={onStarted} title="Helm builds this during onboarding"
                      line="One read of the repository: stack, commands, tests and hotspots. About two minutes." />
      </section>
    );
  }
  const maxHeat = Math.max(1, ...d.hotspots.map((h) => h.heat ?? 0));
  const total = d.code_map.files || d.code_map.parts.reduce((x, p) => x + p.files, 0) || 1;
  const pct = d.tests && d.tests.total ? Math.round((100 * d.tests.passed) / d.tests.total) : 0;
  return (
    <section className="px-card" aria-labelledby="px-kn-dossier" data-testid="px-kn-dossier">
      <div className="px-card-h"><Ico icon={IdentificationBadge} c="teal" /><h2 id="px-kn-dossier">Dossier</h2>
        <small className="px-faint">by {d.by}{d.built_ago ? ` · re-read ${d.built_ago}` : d.built_at ? ` · re-read ${whenLocal(d.built_at)}` : ""}</small>
        <a className="px-more" href="#" data-testid="px-kn-full-dossier" onClick={(e) => { e.preventDefault(); writePxUrl({ drawer: "dossier" }); }}>Full dossier</a></div>
      {d.stack.length ? (
        <div className="px-row px-wrap" style={{ marginBottom: 12, gap: 6 }} data-testid="px-kn-stack">
          {d.stack.map((s) => { const I = stackIcon(s); return <span key={s} className="px-tag px-kn-chip"><I size={11} aria-hidden />{s}</span>; })}
        </div>
      ) : null}
      <div className="px-kn-dgrid">
        <div>
          <span className="px-lbl">Commands</span>
          {Object.keys(d.commands).length ? (
            <dl className="px-kn-cmds" data-testid="px-kn-commands">
              {Object.entries(d.commands).map(([k2, v]) => (
                <React.Fragment key={k2}><dt>{CMD_WORDS[k2] ?? k2[0].toUpperCase() + k2.slice(1)}</dt><dd><CopyCode label={k2} value={v} /></dd></React.Fragment>
              ))}
            </dl>
          ) : <p className="px-faint" style={{ fontSize: ".6rem" }}>The repo declares no commands; the first build finds them.</p>}
        </div>
        <div>
          <span className="px-lbl">Tests</span>
          {d.tests ? (
            <div className="px-row" style={{ marginTop: 6 }} data-testid="px-kn-tests">
              <Ring pct={pct} tone={d.tests.failing ? "teal" : "green"} size="l" label={`${d.tests.passed} of ${d.tests.total} tests pass`}>{pct}%</Ring>
              <div><b className="px-mono">{d.tests.passed} / {d.tests.total}</b>
                {d.tests.failing ? (
                  <div className="px-faint" style={{ fontSize: ".58rem" }}>{d.tests.failing} failing{" "}
                    <a href="#" data-testid="px-kn-tests-work" onClick={(e) => { e.preventDefault(); ctx.tab("work"); }}>→ on Work</a></div>
                ) : <div className="px-faint" style={{ fontSize: ".58rem" }}><Check size={10} aria-hidden /> all pass · {d.tests.from}</div>}
              </div>
            </div>
          ) : d.test_files !== null ? (
            <div className="px-row" style={{ marginTop: 6 }} data-testid="px-kn-test-files">
              <b className="px-mono">{d.test_files}</b><span className="px-faint" style={{ fontSize: ".6rem" }}>test file{d.test_files === 1 ? "" : "s"} · the first build runs them</span>
            </div>
          ) : null}
          {d.code_map.parts.length ? (
            <div style={{ marginTop: 12 }} data-testid="px-kn-codemap">
              <span className="px-lbl">Code map</span>
              <div className="px-kn-map" role="img" aria-label={d.code_map.parts.map((p) => `${p.dir} ${Math.round((100 * p.files) / total)}%`).join(", ")}>
                {d.code_map.parts.map((p, i) => (
                  <span key={p.dir} style={{ width: `${(100 * p.files) / total}%`, background: MAP_COLORS[i] ?? MAP_COLORS[4] }}
                        title={`${p.dir} · ${p.files} files (${Math.round((100 * p.files) / total)}%)`} />
                ))}
              </div>
              <small className="px-faint" style={{ fontSize: ".54rem" }}>{d.code_map.parts.map((p) => p.dir).join(" · ")} · {total} files</small>
            </div>
          ) : null}
        </div>
      </div>
      {d.hotspots.length ? (
        <div style={{ marginTop: 14 }} data-testid="px-kn-hotspots">
          <span className="px-lbl">Hotspots</span>
          {d.hotspots.map((h) => (
            <div key={h.path} className="px-hb" title={`${h.path} — ${h.why}`}>
              <code className="px-mono px-ell" style={{ fontSize: ".6rem" }}>{h.path}</code>
              {h.heat !== null ? (
                <><span className="px-hb-bar" aria-hidden><span style={{ width: `${(100 * h.heat) / maxHeat}%` }} /></span>
                  <span className="px-mono px-faint">{h.heat}×</span></>
              ) : (
                <><span className="px-faint px-ell" style={{ fontSize: ".56rem" }}>{h.why}</span>
                  {h.has_tests === false ? <Tag tone="amber">no tests</Tag> : <span />}</>
              )}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

// ── docs & decisions ─────────────────────────────────────────────────────────────────────────

const DOC_ICON: Record<string, { icon: Icon; c: "blue" | "violet" | "teal" | "amber" | "green" }> = {
  prd: { icon: FileText, c: "blue" }, adr: { icon: Signpost, c: "violet" }, runbook: { icon: Wrench, c: "teal" },
  readme: { icon: BookOpen, c: "green" }, decision: { icon: Gavel, c: "amber" },
};
/** A doc's drawer key (`doc:<key>`): a PRD by its meeting, a repo doc by its path, a decision by its id. */
export function docKey(d: { kind: string; href: string; path?: string }): string {
  const q = new URLSearchParams(d.href.includes("?") ? d.href.slice(d.href.indexOf("?") + 1) : "");
  if (d.kind === "prd" && q.get("meeting")) return `prd:${q.get("meeting")}`;
  if (d.kind === "decision" && q.get("decision")) return `decision:${q.get("decision")}`;
  if (d.kind === "readme") return "readme";
  return d.path || d.href;
}
const allDocs = (k: Knowledge) => [
  ...k.docs.map((d) => ({ ...d, key: docKey(d) })),
  ...k.decisions.map((d) => ({ label: d.label, kind: "decision", href: d.href, sub: d.sub || whenLocal(d.at), path: "", external_url: "", key: d.href.includes("decision=") ? docKey({ kind: "decision", href: d.href }) : `policy:${d.label}` })),
];

function Docs({ k }: { k: Knowledge }) {
  const rows = allDocs(k);
  return (
    <section className="px-card" aria-labelledby="px-kn-docs" data-testid="px-kn-docs">
      <div className="px-card-h"><Ico icon={Files} c="blue" /><h2 id="px-kn-docs">Docs &amp; decisions</h2>
        <a className="px-more" href="#" data-testid="px-kn-add-doc" onClick={(e) => { e.preventDefault(); openDrawer("doc", "new"); }}><UploadSimple size={11} /> Add doc</a></div>
      {rows.length ? rows.map((d, i) => {
        const ic = DOC_ICON[d.kind] ?? DOC_ICON.prd;
        return (
          <button key={`${d.key}-${i}`} type="button" className="px-li px-kn-doc" data-testid={`px-kn-doc-${i}`} onClick={() => openDrawer("doc", d.key)}>
            <Ico icon={ic.icon} c={ic.c} />
            <span style={{ minWidth: 0 }}><b className="px-ell" style={{ display: "block" }}>{d.label}</b><small>{d.sub}</small></span>
            <CaretRight size={13} className="px-faint" aria-hidden />
          </button>
        );
      }) : (
        <Empty icon={Files} tone="blue" title="No docs yet" line="PRDs, ADRs and decisions land here as the crew writes them. Add one to teach the crew.">
          <button type="button" className="px-btn" data-size="s" onClick={() => openDrawer("doc", "new")}><UploadSimple size={12} /> Add doc</button>
        </Empty>
      )}
    </section>
  );
}

// ── the knowledge graph ──────────────────────────────────────────────────────────────────────

export type Box = { x: number; y: number; w: number; h: number };
export type Laid = { pos: Map<string, { x: number; y: number; r: number }>; labels: Map<string, { x: number; y: number; anchor: "start" | "middle" | "end"; box: Box; inside: boolean }> };
const FONT = 10;
const textWidth = (t: string, bold = false) => t.length * FONT * (bold ? 0.62 : 0.56) + 2;
export const overlaps = (a: Box, b: Box) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
export const circleBox = (p: { x: number; y: number; r: number }): Box => ({ x: p.x - p.r, y: p.y - p.r, w: 2 * p.r, h: 2 * p.r });

/** Lay the snippet out: the hub in the centre, its neighbours on an ellipse, and each label on
 *  the side of its node that collides with nothing (no other label, no node, inside the frame).
 *  The SVG scales as one, so a layout without overlaps has none at any width. */
export function layoutGraph(nodes: { id: string; label: string }[], W = 420, H = 240): Laid {
  const pos = new Map<string, { x: number; y: number; r: number }>();
  const labels: Laid["labels"] = new Map();
  if (!nodes.length) return { pos, labels };
  const [hub, ...rest] = nodes;
  pos.set(hub.id, { x: W / 2, y: H / 2, r: 24 });
  const rx = W * 0.34, ry = H * 0.3;
  rest.forEach((n, i) => {
    const a = (i / rest.length) * Math.PI * 2 - Math.PI / 2 + (rest.length % 2 ? 0 : Math.PI / rest.length);
    pos.set(n.id, { x: W / 2 + Math.cos(a) * rx, y: H / 2 + Math.sin(a) * ry, r: 13 });
  });
  const taken: Box[] = [...pos.values()].map(circleBox);
  const inFrame = (b: Box) => b.x >= 1 && b.y >= 1 && b.x + b.w <= W - 1 && b.y + b.h <= H - 1;
  const place = (n: { id: string; label: string }, isHub: boolean) => {
    const p = pos.get(n.id)!;
    const w = textWidth(n.label, isHub), h = FONT + 3;
    if (isHub && w <= 2 * p.r - 4) {
      labels.set(n.id, { x: p.x, y: p.y + FONT / 2 - 1, anchor: "middle", box: { x: p.x - w / 2, y: p.y - h / 2, w, h }, inside: true });
      return;
    }
    const dx = p.x - W / 2, dy = p.y - H / 2;
    const below = { x: p.x - w / 2, y: p.y + p.r + 3, anchor: "middle" as const };
    const above = { x: p.x - w / 2, y: p.y - p.r - 3 - h, anchor: "middle" as const };
    const right = { x: p.x + p.r + 4, y: p.y - h / 2, anchor: "start" as const };
    const left = { x: p.x - p.r - 4 - w, y: p.y - h / 2, anchor: "end" as const };
    const order = Math.abs(dy) * 1.6 >= Math.abs(dx) ? (dy >= 0 ? [below, right, left, above] : [above, right, left, below])
      : (dx >= 0 ? [right, below, above, left] : [left, below, above, right]);
    // Each candidate, also nudged to stay in the frame and clear of the others; the first that collides with nothing wins.
    type Cand = { x: number; y: number; anchor: "start" | "middle" | "end" };
    let best: { c: Cand; box: Box; score: number } | null = null;
    for (const c of order as Cand[]) {
      for (const shift of [0, 12, -12, 24, -24, 40, -40]) {
        const box = { x: c.x + (c.anchor === "middle" ? shift : 0), y: c.y + (c.anchor === "middle" ? 0 : shift), w, h };
        if (box.x < 1) box.x = 1;
        if (box.x + w > W - 1) box.x = W - 1 - w;
        const hits = taken.filter((t) => overlaps(t, box)).length + (inFrame(box) ? 0 : 5);
        if (!best || hits < best.score) best = { c, box, score: hits };
        if (hits === 0) break;
      }
      if (best && best.score === 0) break;
    }
    const b = best!;
    const tx = b.c.anchor === "middle" ? b.box.x + w / 2 : b.c.anchor === "start" ? b.box.x : b.box.x + w;
    labels.set(n.id, { x: tx, y: b.box.y + h - 3, anchor: b.c.anchor, box: b.box, inside: false });
    taken.push(b.box);
  };
  place(hub, true);
  rest.forEach((n) => place(n, false));
  return { pos, labels };
}

const GROUP_STYLE: Record<string, { stroke: string; fill: string; word: string }> = {
  hub: { stroke: "var(--accent)", fill: "color-mix(in srgb, var(--accent) 22%, var(--surface))", word: "the hottest entity" },
  rule: { stroke: "var(--px-violet, #8b7cf6)", fill: "var(--surface)", word: "business rule" },
  learned: { stroke: "var(--amber)", fill: "var(--surface)", word: "learned on the job" },
  code: { stroke: "var(--green)", fill: "var(--surface)", word: "code" },
  data: { stroke: "var(--teal)", fill: "var(--surface)", word: "data" },
  entity: { stroke: "var(--teal)", fill: "var(--surface)", word: "entity" },
};

function GraphCard({ ctx, k, fallbackHref, onStarted }: { ctx: PxCtx; k: Knowledge; fallbackHref: string; onStarted: () => void }) {
  const g = k.graph;
  const href = g.href ? `${g.href}${g.href.includes("project_id") ? "" : `&project_id=${encodeURIComponent(ctx.projectId)}`}` : fallbackHref;
  const nodes: GraphNode[] = useMemo(() => {
    const hub = g.nodes.find((n) => n.id === g.hub) ?? g.nodes[0];
    return hub ? [hub, ...g.nodes.filter((n) => n.id !== hub.id)].slice(0, 8) : [];
  }, [g.nodes, g.hub]);
  const W = 420, H = 240;
  const laid = useMemo(() => layoutGraph(nodes, W, H), [nodes]);
  return (
    <section className="px-card" aria-labelledby="px-kn-graph" data-testid="px-kn-graph">
      <div className="px-card-h"><Ico icon={Graph} c="violet" /><h2 id="px-kn-graph">Knowledge graph</h2>
        <a className="px-more" href={pxHref(href)} data-testid="px-kn-open-graph">Open graph <ArrowRight size={11} /></a></div>
      {nodes.length ? (
        <svg className="px-kg" viewBox={`0 0 ${W} ${H}`} role="img" data-testid="px-kn-graph-svg"
             aria-label={`Knowledge graph centred on ${nodes[0].label}: ${nodes.slice(1).map((n) => n.label).join(", ")}`}>
          {g.edges.map((e, i) => {
            const a = laid.pos.get(e.from), b = laid.pos.get(e.to);
            if (!a || !b) return null;
            return <line key={i} className={`e${e.label ? " e-rule" : ""}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y}><title>{e.kind.replace(/_/g, " ")}</title></line>;
          })}
          {nodes.map((n, i) => {
            const p = laid.pos.get(n.id)!; const l = laid.labels.get(n.id)!;
            const st = GROUP_STYLE[i === 0 ? "hub" : n.group] ?? GROUP_STYLE.entity;
            return (
              <g key={n.id} data-testid={`px-kn-node-${i}`} data-group={i === 0 ? "hub" : n.group}>
                <title>{`${n.detail || n.label} — ${st.word}`}</title>
                <circle cx={p.x} cy={p.y} r={p.r} fill={st.fill} stroke={st.stroke} strokeWidth={2} />
                <text x={l.x} y={l.y} textAnchor={l.anchor} style={i === 0 ? { fontWeight: 700 } : undefined}>{n.label}</text>
              </g>
            );
          })}
          {g.edges.filter((e) => e.label).slice(0, 2).map((e, i) => {
            const a = laid.pos.get(e.from), b = laid.pos.get(e.to);
            if (!a || !b) return null;
            const x = (a.x + b.x) / 2, y = (a.y + b.y) / 2, w = textWidth(e.label);
            // The first spot beside the edge's middle that clears every label and node.
            const spot = [[6, -2], [-6 - w, -2], [6, 12], [-6 - w, 12], [-w / 2, -8], [-w / 2, 16]]
              .map(([dx, dy]) => ({ x: x + dx, y: y + dy - 10, w, h: 12 }))
              .find((box) => box.x >= 1 && box.x + w <= W - 1 && box.y >= 1 && box.y + 12 <= H - 1
                && ![...laid.labels.values()].some((l) => overlaps(l.box, box)) && ![...laid.pos.values()].some((p) => overlaps(circleBox(p), box)));
            return spot ? <text key={`el-${i}`} x={spot.x} y={spot.y + 10} className="px-kg-el">{e.label}</text> : null;
          })}
        </svg>
      ) : (
        <Empty icon={Graph} tone="violet" title="Not indexed yet" line="Helm maps the code into the graph: its modules, endpoints and tables.">
          <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid="px-kn-index"
                       onClick={() => void startOnboarding(ctx.projectId).then((r) => { if (r.ok && !r.demo) onStarted(); })}>
            <ArrowsClockwise size={12} aria-hidden /> Index</GatedButton>
        </Empty>
      )}
      <div className="px-row px-wrap" style={{ marginTop: 10, gap: 6 }} data-testid="px-kn-graph-counts">
        <span className="px-tag"><Cube size={11} aria-hidden /> {g.counts.nodes} nodes</span>
        {g.counts.rules !== null ? <Tag tone="violet"><Scales size={11} aria-hidden /> {g.counts.rules} rules</Tag> : null}
        {g.counts.learned !== null ? <Tag tone="blue"><Brain size={11} aria-hidden /> {g.counts.learned} learned</Tag> : null}
      </div>
    </section>
  );
}

// ── learned on the job ───────────────────────────────────────────────────────────────────────

function Learned({ k }: { k: Knowledge }) {
  return (
    <section className="px-card" aria-labelledby="px-kn-learned" data-testid="px-kn-learned">
      <div className="px-card-h"><Ico icon={Brain} c="green" /><h2 id="px-kn-learned">Learned on the job</h2></div>
      {k.learned.length ? k.learned.map((l) => (
        <a key={l.id || l.label} className="px-li px-kn-learned-row" href={pxHref(l.href)} data-testid={`px-kn-learned-${l.id || l.label}`} title={l.detail}>
          <Face agent={l.agent ?? { name: "Helm", role_key: "engineering_captain" }} size={24} />
          <span style={{ minWidth: 0 }}><b className="px-ell" style={{ display: "block" }}>{l.label}</b><small>{l.sub}</small></span>
          <Tag tone={l.state === "used" ? "green" : "idle"}>{l.state === "used" ? "used" : "new"}</Tag>
        </a>
      )) : (
        <Empty icon={Brain} tone="green" title="Nothing learned yet"
               line="When an agent hits a snag and finds the way through, the lesson lands here, and later runs reuse it." />
      )}
    </section>
  );
}

// ── drawers: the full dossier, a doc, Add doc ────────────────────────────────────────────────

/** `drawer=dossier` and `drawer=doc:<key>` (contract §10): prose lives here, not on the tab. */
export function KnowledgeDrawer({ ctx, drawer, onClose }: { ctx: PxCtx; drawer: { kind: string; id: string }; onClose: () => void }) {
  const read = useKnowledge(ctx.projectId);
  const k = read.data;
  if (drawer.kind === "doc" && drawer.id === "new") return <AddDocDrawer ctx={ctx} onClose={onClose} />;
  if (!k) return <Drawer title="Repo & knowledge" onClose={onClose} testid="px-drawer-knowledge"><p className="px-faint">Loading…</p></Drawer>;
  if (drawer.kind === "dossier") {
    const d = k.dossier;
    return (
      <Drawer title="Dossier" icon={<Ico icon={IdentificationBadge} c="teal" />} onClose={onClose} testid="px-drawer-dossier" wide
              sub={d ? <span>by {d.by}{d.built_ago ? ` · re-read ${d.built_ago}` : ""}</span> : null}>
        {d ? (
          <div className="px-stack">
            <div className="px-row px-wrap" style={{ gap: 6 }}>{d.stack.map((s) => { const I = stackIcon(s); return <span key={s} className="px-tag"><I size={11} aria-hidden />{s}</span>; })}</div>
            {Object.keys(d.commands).length ? (
              <dl className="px-kn-cmds">{Object.entries(d.commands).map(([k2, v]) => <React.Fragment key={k2}><dt>{CMD_WORDS[k2] ?? k2}</dt><dd><CopyCode label={k2} value={v} /></dd></React.Fragment>)}</dl>
            ) : null}
            {d.hotspots.length ? <div><span className="px-lbl">Hotspots</span>{d.hotspots.map((h) => <p key={h.path} style={{ margin: "6px 0", fontSize: ".62rem" }}><code className="px-code">{h.path}</code> — {h.why}</p>)}</div> : null}
            {d.code_map.parts.length ? <p className="px-faint" style={{ fontSize: ".62rem" }}>{d.code_map.parts.map((p) => `${p.dir} ${p.files} files`).join(" · ")} · {d.code_map.files} files in all</p> : null}
            <PxLink href={`?area=knowledge&project_id=${encodeURIComponent(ctx.projectId)}`}>Open the Knowledge Graph</PxLink>
          </div>
        ) : <BuildDossier ctx={ctx} k={k} onStarted={read.refresh} title="No dossier yet" line="Helm reads the repository once: stack, commands, tests, hotspots." />}
      </Drawer>
    );
  }
  const doc = allDocs(k).find((x) => x.key === drawer.id);
  if (!doc) {
    return <Drawer title="Document" onClose={onClose} testid="px-drawer-doc"><Empty icon={FileText} title="Not found" line="This document isn't on the project any more." /></Drawer>;
  }
  const ic = DOC_ICON[doc.kind] ?? DOC_ICON.prd;
  const where = doc.external_url || doc.href;
  return (
    <Drawer title={doc.label} icon={<Ico icon={ic.icon} c={ic.c} />} onClose={onClose} testid="px-drawer-doc" sub={<span>{doc.sub}</span>}
            footer={where ? <PxLink href={where} className="px-btn" testid="px-drawer-doc-open">{doc.key.startsWith("policy:") ? "Open the policy" : doc.kind === "decision" ? "Open in Approvals" : doc.kind === "prd" ? "Open the PRD" : "Open"}</PxLink> : null}>
      <Tag>{doc.kind === "prd" ? "PRD" : doc.kind === "adr" ? "ADR" : doc.kind === "decision" ? "Decision" : doc.kind === "readme" ? "README" : "Runbook"}</Tag>
      {doc.path ? <p style={{ fontSize: ".64rem", whiteSpace: "pre-wrap", marginTop: 10 }} data-testid="px-drawer-doc-body">{doc.path}</p> : null}
    </Drawer>
  );
}

function AddDocDrawer({ ctx, onClose }: { ctx: PxCtx; onClose: () => void }) {
  const w = useMemo(() => ({ projectId: ctx.projectId, actor: ctx.actor }), [ctx.projectId, ctx.actor]);
  const [preview, setPreview] = useState<KgUploadPreview | null>(null);
  const [done, setDone] = useState(false);
  return (
    <Drawer title="Add a document" icon={<Ico icon={UploadSimple} c="blue" />} onClose={onClose} testid="px-drawer-add-doc" wide
            sub={<span>It joins this project&apos;s Knowledge Graph; nothing is added until you press Add.</span>}>
      {isPxDemo() ? <p className="px-faint">Sample mode — uploads are off.</p>
        : done ? <Empty icon={Check} tone="green" title="Added" line="The document is in the Knowledge Graph. It shows under Docs once read." />
        : preview ? <UploadPreview w={w} preview={preview} onDone={(added) => { setPreview(null); setDone(Boolean(added)); }} />
        : <UploadDocument w={w} disabled={false} disabledWhy="" onStaged={setPreview} />}
    </Drawer>
  );
}
