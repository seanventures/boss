"use client";

/**
 * REPO & KNOWLEDGE (screen 6, contract §4): repos with their access as a strip, Helm's dossier
 * (stack, commands, tests, hotspots), a snippet of the project's knowledge graph, docs and
 * decisions, and what agents learned on the job.
 */
import React from "react";
import {
  ArrowRight, ArrowSquareOut, Atom, Brain, Check, Copy, FileText, GitBranch, GithubLogo, Graph, Notebook, Plus, Lock,
} from "@phosphor-icons/react";

import { PX_PATHS, parseKnowledge, pxHref, usePxRead, type Knowledge, type Repo } from "../../lib/project-experience";
import { FX_KNOWLEDGE } from "../../lib/project-experience-fixtures";
import { Empty, Face, Flow, Ico, PxLink, ReadGate, Ring, Tag } from "./kit";
import { whenLocal } from "../../lib/local-time";
import type { PxCtx } from "./project-page";

export function KnowledgeTab({ ctx }: { ctx: PxCtx }) {
  const read = usePxRead<Knowledge>(PX_PATHS.knowledge(ctx.projectId), parseKnowledge, { everyMs: 60_000, demo: () => parseKnowledge(FX_KNOWLEDGE) });
  const graphHref = `?area=knowledge&project_id=${encodeURIComponent(ctx.projectId)}`;
  return (
    <div className="px-stack" data-testid="px-knowledge">
      <ReadGate read={read} what="Repo & knowledge" missing={
        <>
          <Repos ctx={ctx} repos={ctx.overview.project.repos} />
          <div className="px-card"><Empty icon={Graph} tone="violet" title="The dossier arrives with the next BOSS update"
                 line="Helm's read of the code (stack, commands, hotspots) shows here. The Knowledge Graph already holds what's mapped.">
            <a className="px-btn" href={pxHref(graphHref)} data-testid="px-kn-open-graph">Open Knowledge Graph</a></Empty></div>
        </>
      }>
        {(k) => (
          <>
            <Repos ctx={ctx} repos={k.repos.length ? k.repos : ctx.overview.project.repos} />
            <div className="px-split">
              <Dossier k={k} />
              <GraphCard k={k} fallbackHref={graphHref} />
            </div>
            <div className="px-split">
              <section className="px-card" aria-labelledby="px-kn-docs" data-testid="px-kn-docs">
                <div className="px-card-h"><Ico icon={Notebook} c="blue" /><h2 id="px-kn-docs">Docs &amp; decisions</h2>
                  <a className="px-more" href={pxHref("?area=meetings")}><Plus size={11} /> Add doc</a></div>
                {[...k.docs.map((d) => ({ ...d, icon: <FileText size={16} aria-hidden /> })),
                  ...k.decisions.map((d) => ({ label: d.label, sub: `Decision · ${whenLocal(d.at)}`, href: d.href, kind: "decision", icon: <Atom size={16} aria-hidden /> }))].map((d, i) => (
                  <div key={`${d.label}-${i}`} className="px-li">
                    <span className="px-ico" data-c={d.kind === "decision" ? "amber" : "blue"} style={{ width: 26, height: 26 }}>{d.icon}</span>
                    <span><b>{d.href ? <PxLink href={d.href} className="">{d.label}</PxLink> : d.label}</b><small>{d.sub}</small></span>
                    <span />
                  </div>
                ))}
                {!k.docs.length && !k.decisions.length ? <p className="px-faint">PRDs, ADRs and decisions land here as the crew writes them.</p> : null}
              </section>
              <section className="px-card" aria-labelledby="px-kn-learned" data-testid="px-kn-learned">
                <div className="px-card-h"><Ico icon={Brain} c="green" /><h2 id="px-kn-learned">Learned on the job</h2></div>
                {k.learned.length ? k.learned.map((l, i) => (
                  <div key={`${l.label}-${i}`} className="px-li">
                    <Face agent={{ name: "Vera", role_key: "qa" }} size={22} />
                    <span><b>{l.label}</b><small>{l.sub}</small></span>
                    <Tag tone={l.state === "used" ? "green" : undefined}>{l.state}</Tag>
                  </div>
                )) : <p className="px-faint">What agents learn while working (and reuse later) shows here.</p>}
              </section>
            </div>
          </>
        )}
      </ReadGate>
    </div>
  );
}

function Repos({ ctx, repos }: { ctx: PxCtx; repos: Repo[] }) {
  return (
    <section className="px-card" aria-labelledby="px-kn-repos" data-testid="px-kn-repos">
      <div className="px-card-h"><Ico icon={GithubLogo} c="teal" /><h2 id="px-kn-repos">Repositories</h2><span className="px-nbadge">{repos.length}</span>
        <a className="px-more" href={pxHref("?area=setup&focus=repository&open=add")} data-testid="px-kn-add-repo"><Plus size={11} /> Add repo</a></div>
      {repos.length ? repos.map((r) => (
        <div key={r.full_name} className="px-row px-wrap" style={{ padding: "6px 0" }}>
          <div style={{ minWidth: 0, flex: "1 1 220px" }}>
            <a className="px-mono" style={{ fontWeight: 700, fontSize: ".7rem" }} href={r.external_url || `https://github.com/${r.full_name}`} target="_blank" rel="noreferrer noopener">
              {r.full_name} <ArrowSquareOut size={11} aria-label="opens in a new tab" /></a>
            <div className="px-faint" style={{ fontSize: ".56rem" }}><GitBranch size={10} aria-hidden /> {r.branch}</div>
          </div>
          <span className="px-lbl px-hide-phone">Access</span>
          <Flow nodes={[
            { label: "Read", state: r.access.read ? "done" : "failed" }, { label: "Branch", state: r.access.write ? "done" : "failed" },
            { label: "Open PR", state: r.access.pr ? "done" : "failed" },
            { label: "Merge: you", type: "waiting", icon: <Lock size={11} aria-hidden /> },
          ]} />
          <a className="px-btn" data-size="s" href={pxHref("?area=setup&focus=repository")} title="Check the repo's access again in Repo and models">Re-check</a>
        </div>
      )) : (
        <Empty icon={GithubLogo} tone="teal" title="No repo yet" line="Add the repo this project builds in; Helm reads it and gets it ready for work.">
          <a className="px-btn" data-kind="primary" href={pxHref("?area=setup&focus=repository&open=add")}>Add a repo</a>
        </Empty>
      )}
      <span hidden>{ctx.projectId}</span>
    </section>
  );
}

function Dossier({ k }: { k: Knowledge }) {
  const d = k.dossier;
  if (!d) return <section className="px-card"><Empty icon={Notebook} title="No dossier yet" line="Helm writes one when it onboards the project." /></section>;
  const maxHeat = Math.max(1, ...d.hotspots.map((h) => h.heat));
  return (
    <section className="px-card" aria-labelledby="px-kn-dossier" data-testid="px-kn-dossier">
      <div className="px-card-h"><Ico icon={Notebook} c="blue" /><h2 id="px-kn-dossier">Dossier</h2>
        <small className="px-faint">by Helm{d.built_at ? ` · re-read ${whenLocal(d.built_at)}` : ""}</small>
        {d.href ? <PxLink href={d.href} className="px-more">Full dossier</PxLink> : null}</div>
      <div className="px-row px-wrap" style={{ marginBottom: 12 }}>{d.stack.map((s) => <span key={s} className="px-tag">{s}</span>)}</div>
      <div className="px-grid px-g2">
        <div>
          <span className="px-lbl">Commands</span>
          <dl className="px-kv" style={{ marginTop: 6 }}>
            {Object.entries(d.commands).map(([k2, v]) => (
              <React.Fragment key={k2}><dt>{k2[0].toUpperCase() + k2.slice(1)}</dt>
                <dd><code className="px-code">{v}</code> <button type="button" className="px-icon-btn" style={{ width: 20, height: 20 }} aria-label={`Copy ${k2} command`}
                        onClick={() => void navigator.clipboard?.writeText(v)}><Copy size={11} /></button></dd></React.Fragment>
            ))}
          </dl>
        </div>
        {d.tests ? (
          <div>
            <span className="px-lbl">Tests</span>
            <div className="px-row" style={{ marginTop: 6 }}>
              <Ring pct={d.tests.total ? (100 * d.tests.passed) / d.tests.total : 0} tone={d.tests.passed === d.tests.total ? "green" : "teal"} size="l"
                    label={`${d.tests.passed} of ${d.tests.total} tests pass`}>{d.tests.total ? Math.round((100 * d.tests.passed) / d.tests.total) : 0}%</Ring>
              <div><b className="px-mono">{d.tests.passed} / {d.tests.total}</b>
                {d.tests.total > d.tests.passed ? <div className="px-faint" style={{ fontSize: ".58rem" }}>{d.tests.total - d.tests.passed} failing <ArrowRight size={10} /> on Work</div>
                  : <div className="px-faint" style={{ fontSize: ".58rem" }}><Check size={10} /> all pass</div>}</div>
            </div>
          </div>
        ) : null}
      </div>
      {d.hotspots.length ? (
        <div style={{ marginTop: 12 }}>
          <span className="px-lbl">Hotspots</span>
          {d.hotspots.map((h) => (
            <div key={h.path} className="px-hb" title={h.why}>
              <code className="px-mono px-ell" style={{ fontSize: ".6rem" }}>{h.path}</code>
              <span className="px-hb-bar" aria-hidden><span style={{ width: `${(100 * h.heat) / maxHeat}%` }} /></span>
              <span className="px-mono px-faint">{h.heat}×</span>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function GraphCard({ k, fallbackHref }: { k: Knowledge; fallbackHref: string }) {
  const g = k.graph;
  const href = g.href || fallbackHref;
  const nodes = g.nodes.slice(0, 8);
  const W = 420, H = 230;
  const pos = new Map(nodes.map((n, i) => {
    if (i === 0) return [n.id, { x: W / 2, y: H / 2 }];
    const a = ((i - 1) / Math.max(1, nodes.length - 1)) * Math.PI * 2 - Math.PI / 2;
    return [n.id, { x: W / 2 + Math.cos(a) * 150, y: H / 2 + Math.sin(a) * 78 }];
  }));
  const color = (kind: string) => (kind === "rule" ? "#8b7cf6" : kind === "code" ? "var(--green)" : "var(--teal)");
  return (
    <section className="px-card" aria-labelledby="px-kn-graph" data-testid="px-kn-graph">
      <div className="px-card-h"><Ico icon={Graph} c="violet" /><h2 id="px-kn-graph">Knowledge graph</h2>
        <a className="px-more" href={pxHref(href)} data-testid="px-kn-open-graph">Open graph <ArrowRight size={11} /></a></div>
      {nodes.length ? (
        <svg className="px-kg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Knowledge graph: ${nodes.map((n) => n.label).join(", ")}`}>
          {g.edges.map((e, i) => {
            const a = pos.get(e.from), b = pos.get(e.to);
            if (!a || !b) return null;
            return <g key={i}><line className={`e${e.label ? " e-rule" : ""}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} />
              {e.label ? <text x={(a.x + b.x) / 2 + 6} y={(a.y + b.y) / 2} style={{ fill: "#8b7cf6", fontSize: 10 }}>{e.label}</text> : null}</g>;
          })}
          {nodes.map((n, i) => {
            const p = pos.get(n.id)!;
            return <g key={n.id}><circle cx={p.x} cy={p.y} r={i === 0 ? 24 : 15} fill="var(--surface)" stroke={color(n.kind)} strokeWidth={2} />
              <text x={p.x} y={i === 0 ? p.y + 4 : p.y + 30} textAnchor="middle" style={i === 0 ? { fontWeight: 700 } : undefined}>{n.label}</text></g>;
          })}
        </svg>
      ) : <Empty icon={Graph} tone="violet" title="Nothing mapped yet" line="The graph fills in as Helm maps the code and the crew learns." />}
      <div className="px-row px-wrap" style={{ marginTop: 10 }}>
        <span className="px-tag">{g.counts.nodes} nodes</span><Tag tone="violet">{g.counts.rules} rules</Tag><Tag tone="blue">{g.counts.learned} learned</Tag>
      </div>
    </section>
  );
}
