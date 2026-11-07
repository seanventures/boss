"use client";

/**
 * ONBOARDING IN PROGRESS (BOSSAPP-914 screen 10, contract §8): the project page while Helm onboards.
 *
 *   Discover → Ready for work → Propose backlog → Work it → Deploy        (the stage rail)
 *   Now: <agent> is <doing what>                                        (one line, live, Watch)
 *   Discovered (radial map) · Ready for work (six checks, the agent on each) · Backlog proposed
 *
 * The agent does every check it can; the ones only a person can do are amber with their one
 * action, and the same items sit in the project's Needs-you bar. "Accept backlog" puts the ranked
 * items on the Roadmap's Next lane (POST …/onboarding/backlog/accept) — nothing starts a build until
 * a person starts an item; "Edit" reorders, drops and renames them first. Both unlock when Ready
 * for work is complete, as the wireframe says.
 */
import React, { useMemo, useState } from "react";
import {
  ArrowDown, ArrowUp, ArrowsClockwise, Check, CheckCircle, GithubLogo, GitPullRequest, ListChecks, ListNumbers, MagnifyingGlass, Note,
  PencilSimple, Rocket, Sparkle, Terminal, XCircle,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { notifyChanged } from "../changed";
import { acceptBacklog, projectHref, pxHref, startOnboarding, type NextItem, type Node, type Onboarding } from "../../lib/project-experience";
import { Face, Ico, STATE_WORD } from "./kit";

const STAGE_ICON: Record<string, React.ReactNode> = {
  discover: <MagnifyingGlass size={16} weight="bold" />, ready: <ListChecks size={16} weight="bold" />, backlog: <ListNumbers size={16} weight="bold" />,
  first_work: <ArrowsClockwise size={16} weight="bold" />, deploy: <Rocket size={16} weight="bold" />,
};
/** The crew member on each check (the engine's role key → the kit's face and name). */
const CREW: Record<string, { name: string; role_key: string }> = {
  engineering_captain: { name: "Helm", role_key: "engineering_captain" }, qa: { name: "Vera", role_key: "qa" },
  builder: { name: "Forge", role_key: "builder" }, sre: { name: "Beacon", role_key: "sre" },
};
const crewOf = (key: string | undefined) => CREW[key ?? ""] ?? CREW.engineering_captain;

/** Helm in the middle, what it found around it: each a node that turns green when found. */
export function DiscoverDiagram({ nodes }: { nodes: Node[] }) {
  const n = Math.max(nodes.length, 1);
  const W = 420, H = 210, cx = W / 2, cy = H / 2;
  const pts = nodes.map((_, i) => {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / n + (n % 2 ? 0 : Math.PI / n);
    return { x: cx + Math.cos(a) * 160, y: cy + Math.sin(a) * 74 };
  });
  return (
    <div style={{ position: "relative" }} data-testid="px-discover">
      <svg className="px-radial" viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={`Discovered: ${nodes.map((x) => `${x.label} ${STATE_WORD[x.state]}`).join(", ")}`}>
        {pts.map((p, i) => (
          <line key={`e${i}`} x1={cx} y1={cy} x2={p.x} y2={p.y} stroke={nodes[i].state === "done" ? "var(--green)" : "var(--teal)"}
                strokeWidth={1.6} strokeDasharray={nodes[i].state === "done" ? undefined : "5 4"} opacity={0.8} />
        ))}
        {pts.map((p, i) => (
          <g key={`n${i}`}>
            <circle cx={p.x} cy={p.y} r={11} fill="var(--surface)" stroke={nodes[i].state === "done" ? "var(--green)" : nodes[i].state === "failed" ? "var(--red)" : "var(--teal)"} strokeWidth={2} />
            {nodes[i].state === "done" ? <path d={`M${p.x - 4} ${p.y} l3 3 l5 -6`} stroke="var(--green)" strokeWidth={2} fill="none" /> : null}
            <text x={p.x} y={p.y + 25} textAnchor="middle">{nodes[i].label}</text>
            {nodes[i].sub ? <text x={p.x} y={p.y + 38} textAnchor="middle" style={{ fill: "var(--faint)", fontSize: 10 }}>{nodes[i].sub}</text> : null}
          </g>
        ))}
        <circle cx={cx} cy={cy} r={24} fill="color-mix(in srgb, var(--accent) 18%, var(--surface))" stroke="var(--accent)" strokeWidth={2} />
      </svg>
      <span style={{ position: "absolute", left: "50%", top: "50%", transform: "translate(-50%, -50%)" }} aria-hidden>
        <Face agent={{ name: "Helm", role_key: "engineering_captain" }} size={36} state="working" />
      </span>
    </div>
  );
}

/** A candidate's source in words and an icon: "CI", "issue #12", "TODO ×7", "PR #7". */
export function candidateSource(b: NextItem): { icon: React.ReactNode; words: string } {
  const [kind, id] = (b.ref || "").split(":");
  if (kind === "ci" || b.source === "ci") return { icon: <XCircle size={11} aria-hidden />, words: "CI" };
  if (kind === "issue") return { icon: <GithubLogo size={11} aria-hidden />, words: `issue #${id}` };
  if (kind === "pr") return { icon: <GitPullRequest size={11} aria-hidden />, words: `PR #${id}` };
  const todos = /(\d+)\s+TODO/i.exec(b.detail);
  if (kind === "todo" || todos) return { icon: <Note size={11} aria-hidden />, words: todos ? `TODO ×${todos[1]}` : "TODOs" };
  return { icon: <MagnifyingGlass size={11} aria-hidden />, words: kind === "osv" ? "security scan" : "code scan" };
}

/** Elapsed onboarding time in words ("6 min"). */
export const elapsedWords = (s: number) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`);

type SaidT = { text: string; ok: boolean; link?: boolean } | null;

export function OnboardingView({ projectId, ob }: { projectId: string; ob: Onboarding }) {
  const [said, setSaid] = useState<SaidT>(null);
  const rows = ob.ready.length ? ob.ready : ob.checklist.map((c) => ({ ...c, agent: "engineering_captain" }));
  const ready = rows.filter((c) => c.state === "done" || c.state === "skipped").length;
  const unlocked = rows.length > 0 && ready === rows.length;
  const restart = async () => {
    const r = await startOnboarding(projectId);
    setSaid({ ok: r.ok, text: r.ok ? (r.demo ? "Sample mode — nothing was sent." : ob.state === "not_started" ? "Onboarding started." : "Onboarding restarted.") : (r.error ?? "It didn't start.") });
    if (r.ok && !r.demo) notifyChanged("project.onboarding");
  };
  const nowAgent = useMemo(() => {
    const doing = rows.find((c) => c.state === "now" && c.owner !== "you");
    const named = Object.values(CREW).find((c) => ob.now.startsWith(`${c.name} `));
    return named ?? crewOf(doing?.agent);
  }, [rows, ob.now]);
  return (
    <div className="px-stack" data-testid="px-onboarding">
      <section className="px-card">
        <div className="px-ob" role="list" aria-label="Onboarding stages" data-testid="px-ob-stages">
          {ob.stages.map((st) => (
            <div key={st.key} className="px-ob-s" data-s={st.state} role="listitem" aria-label={`${st.label}: ${STATE_WORD[st.state]}`}>
              <span className="px-jr-shape" aria-hidden>{st.state === "done" ? <Check size={16} weight="bold" /> : STAGE_ICON[st.key] ?? <Sparkle size={16} />}</span>
              <span className="px-ob-cap">{st.label}</span>
              <span className="px-ob-meta">{st.sub}</span>
            </div>
          ))}
        </div>
        {ob.now ? (
          <div className="px-now" style={{ marginTop: 14 }} data-testid="px-ob-now" aria-live="polite">
            <Face agent={nowAgent} size={22} state="working" />
            <b>Now</b><span>{ob.now}</span>
            <span className="px-spacer" />
            <a href={pxHref("?area=agent_ops")} data-testid="px-ob-watch" title="The agent's live log"><Terminal size={12} /> Watch</a>
          </div>
        ) : null}
        {ob.state === "failed" || ob.state === "not_started" ? (
          <div className="px-row px-wrap" style={{ marginTop: 10 }}>
            {ob.state === "failed" ? <span className="px-tag" data-tone="red">Onboarding stopped</span> : <span className="px-tag">Not started</span>}
            {ob.error ? <small className="px-faint">{ob.error}</small> : null}
            <GatedButton permission="workflow_run:start" projectId={projectId} className="px-btn" data-kind="primary" data-size="s" data-testid="px-ob-restart" onClick={() => void restart()}>
              <ArrowsClockwise size={12} /> {ob.state === "failed" ? "Try again" : "Start onboarding"}</GatedButton>
          </div>
        ) : null}
      </section>
      <div className="px-grid px-g3 px-ob-cards">
        <section className="px-card" aria-labelledby="px-ob-d">
          <div className="px-card-h"><Ico icon={MagnifyingGlass} c="green" /><h2 id="px-ob-d">Discovered</h2>
            {ob.stages.find((x) => x.key === "discover")?.state === "done" ? <span className="px-tag" data-tone="green">done</span>
              : ob.state === "not_started" ? <span className="px-tag">not started</span> : <span className="px-tag" data-tone="blue">live</span>}</div>
          {/* While it reads, the live nodes (Stack found · Mapping code · Tests run · Ready check). */}
          <DiscoverDiagram nodes={ob.discover.nodes.length ? ob.discover.nodes : ob.discover.live ?? []} />
          <a href={projectHref(projectId, { tab: "knowledge" })} data-testid="px-ob-dossier">Open dossier →</a>
        </section>
        <section className="px-card px-ob-ready" aria-labelledby="px-ob-r">
          <div className="px-card-h"><Ico icon={ListChecks} c="teal" /><h2 id="px-ob-r">Ready for work</h2>
            <span className="px-tag" data-tone="blue">{ready}/{rows.length}</span></div>
          <div data-testid="px-ob-checklist">
            {!rows.length ? <p className="px-faint" style={{ margin: 0 }}>Helm checks repo access, the build, the tests, the workflow, where it deploys and the secrets it needs — and asks only for what it can&apos;t do.</p> : null}
            {rows.map((c) => {
              const you = c.owner === "you" && c.state !== "done";
              return (
                <div key={c.key} className="px-ck-i" data-s={you ? "you" : c.state} data-testid={`px-ob-item-${c.key}`}>
                  <span className="px-ck-b" aria-hidden>{c.state === "done" || c.state === "skipped" ? <Check size={11} weight="bold" /> : you ? "!" : c.state === "failed" ? "✕" : ""}</span>
                  <span className="px-ck-t">{c.label}<small>{c.sub || c.detail}</small><span className="px-sr">{you ? "needs you" : STATE_WORD[c.state]}</span></span>
                  {you && c.action ? <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(c.action.href)} data-testid={`px-ob-act-${c.key}`}>{c.action.label}</a>
                    : <Face agent={crewOf(c.agent)} size={20} state={c.state === "now" ? "working" : "idle"} />}
                </div>
              );
            })}
          </div>
        </section>
        <BacklogCard projectId={projectId} ob={ob} unlocked={unlocked} total={rows.length} onSaid={setSaid} />
      </div>
      {said ? (
        <div className="px-toast" data-ok={said.ok || undefined} role="status" data-testid="px-ob-said">
          {said.ok ? <CheckCircle size={14} weight="fill" aria-hidden /> : null}<span>{said.text}</span>
          {said.link ? <a href={projectHref(projectId, { tab: "roadmap" })} data-testid="px-ob-said-roadmap">Open the Roadmap →</a> : null}
        </div>
      ) : null}
    </div>
  );
}

/** Backlog proposed: the ranked candidates, "+N more", Accept backlog and its editor. */
function BacklogCard({ projectId, ob, unlocked, total, onSaid }: {
  projectId: string; ob: Onboarding; unlocked: boolean; total: number; onSaid: (s: SaidT) => void;
}) {
  const fresh = ob.backlog.filter((b) => !b.accepted);
  const [more, setMore] = useState(false);
  const [editing, setEditing] = useState(false);
  const [order, setOrder] = useState<string[]>([]);
  const [dropped, setDropped] = useState<Record<string, boolean>>({});
  const [names, setNames] = useState<Record<string, string>>({});
  const byId = Object.fromEntries(ob.backlog.map((b) => [b.id, b]));
  const extra = Math.max(0, Math.max(ob.backlog_total, ob.backlog.length) - 4);
  const shown = more ? ob.backlog : ob.backlog.slice(0, 4);
  const lockTip = `Unlocks when Ready is ${total}/${total}`;
  const accept = async (ids: string[], titles: Record<string, string> = {}) => {
    if (!ids.length) { onSaid({ ok: false, text: "Pick at least one item to accept." }); return; }
    const r = await acceptBacklog(projectId, ids, titles);
    if (r.ok) {
      const n = r.demo ? ids.length : (Array.isArray(r.body?.accepted) ? (r.body?.accepted as unknown[]).length : ids.length);
      onSaid({ ok: true, link: !r.demo, text: r.demo ? "Sample mode — nothing was sent." : `${n} item${n === 1 ? "" : "s"} on the Roadmap's Next lane. Nothing has started.` });
      setEditing(false);
      if (!r.demo) notifyChanged("project.roadmap");
    } else onSaid({ ok: false, text: r.error ?? "It wasn't accepted." });
  };
  const moveBy = (id: string, d: number) => setOrder((o) => {
    const i = o.indexOf(id), j = i + d;
    if (i < 0 || j < 0 || j >= o.length) return o;
    const next = [...o]; [next[i], next[j]] = [next[j], next[i]]; return next;
  });
  return (
    <section className="px-card" aria-labelledby="px-ob-b" data-testid="px-ob-backlog">
      <div className="px-card-h"><Ico icon={ListNumbers} c="violet" /><h2 id="px-ob-b">Backlog proposed</h2>
        <span title="Ranked by Marshal, Chief of Staff" data-testid="px-ob-marshal"><Face agent={{ name: "Marshal", role_key: "chief_of_staff" }} size={20} /></span>
        <small className="px-faint">by Marshal</small></div>
      {editing ? (
        <div data-testid="px-ob-editor">
          <small className="px-faint" style={{ display: "block", marginBottom: 6 }}>Untick to drop, use the arrows to reorder, edit a name. Nothing starts.</small>
          {order.map((id, i) => {
            const b = byId[id];
            if (!b) return null;
            return (
              <div key={id} className="px-ob-ed" data-off={dropped[id] || undefined} data-testid={`px-ob-ed-${i}`}>
                <input type="checkbox" checked={!dropped[id]} aria-label={`Keep ${b.label}`} onChange={(e) => setDropped((d) => ({ ...d, [id]: !e.target.checked }))} />
                <input className="px-input" value={names[id] ?? b.label} aria-label={`Name of item ${i + 1}`} onChange={(e) => setNames((x) => ({ ...x, [id]: e.target.value }))} />
                <button type="button" className="px-icon-btn" aria-label={`Move ${b.label} up`} disabled={i === 0} onClick={() => moveBy(id, -1)}><ArrowUp size={12} /></button>
                <button type="button" className="px-icon-btn" aria-label={`Move ${b.label} down`} disabled={i === order.length - 1} onClick={() => moveBy(id, 1)}><ArrowDown size={12} /></button>
              </div>
            );
          })}
          <div className="px-row" style={{ marginTop: 10 }}>
            <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" data-kind="primary" data-size="s" data-testid="px-ob-editor-accept"
                         onClick={() => void accept(order.filter((id) => !dropped[id]),
                                                    Object.fromEntries(Object.entries(names).filter(([id, v]) => v.trim() && v !== byId[id]?.label)))}>
              <Check size={12} /> Accept {order.filter((id) => !dropped[id]).length}</GatedButton>
            <button type="button" className="px-btn" data-kind="ghost" data-size="s" onClick={() => setEditing(false)} data-testid="px-ob-editor-cancel">Cancel</button>
          </div>
        </div>
      ) : (
        <>
          {ob.backlog.length ? shown.map((b, i) => {
            const kind = b.kind === "bug" || /bug|ci/i.test(b.source) ? "bug" : "feature";
            const src = candidateSource(b);
            return (
              <div key={b.id || i} className="px-li" data-testid={`px-ob-backlog-${i}`}>
                <span className="px-rank">{i + 1}</span>
                <span style={{ minWidth: 0 }}><b className="px-ell" style={{ display: "block" }}>{b.label}</b>
                  <small className="px-row" style={{ gap: 4 }}>{src.icon}{b.external_url ? <a href={b.external_url} target="_blank" rel="noreferrer noopener">{src.words}</a> : src.words}</small></span>
                {b.accepted ? <span className="px-tag" data-tone="green" title="Already on the Roadmap">on Roadmap</span>
                  : <span className="px-tag" data-tone={kind === "bug" ? "red" : undefined}>{kind}</span>}
              </div>
            );
          }) : <p className="px-faint">Marshal ranks open issues, failing CI and TODOs once Helm has read the repo.</p>}
          {extra > 0 ? (
            <button type="button" className="px-li px-ob-more" onClick={() => setMore((m) => !m)} aria-expanded={more} data-testid="px-ob-more">
              <span className="px-rank">…</span><b>{more ? "Show the top 4" : `+${extra} more`}</b><span />
            </button>
          ) : null}
          {ob.backlog.length ? (
            <div className="px-row" style={{ marginTop: 12 }}>
              <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" data-kind="primary" data-testid="px-ob-accept"
                           disabled={!unlocked || !fresh.length} title={!unlocked ? lockTip : fresh.length ? "Put these on the Roadmap's Next lane (nothing starts)" : "All on the Roadmap"}
                           onClick={() => void accept(fresh.map((b) => b.id))}><Check size={13} /> Accept backlog</GatedButton>
              <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" data-testid="px-ob-edit" disabled={!unlocked || !fresh.length}
                           title={!unlocked ? lockTip : "Reorder, drop or rename before accepting"}
                           onClick={() => { setOrder(fresh.map((b) => b.id)); setDropped({}); setNames({}); setEditing(true); }}><PencilSimple size={13} /> Edit</GatedButton>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
