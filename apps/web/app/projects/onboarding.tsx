"use client";

/**
 * ONBOARDING (BOSSAPP-914 screen 10 with screen 00b's live parts, contract §8): the project page
 * while Helm onboards, and its own page afterwards (`&tab=onboarding`: Projects / <project> /
 * Onboarding). Live while the run moves: the record is read every 3 s and the timer ticks.
 *
 *   Discover → Ready for work → Propose backlog → Work it → Deploy            (the stage strip)
 *   Now: <agent> is <doing what>                                     · Watch   (one live line)
 *   Workspace  GitHub ✓ … · Model ✓ …   (read-only, §15)                        Read again
 *   Helm is reading your repo: Helm → Stack found · Mapping code · Tests run → Ready check
 *   Discovered (what it found) · Ready for work (six checks, the agent on each) · Backlog proposed
 *   Only you can (the amber items) · First work, suggested (≈ min · ≈ $ · Start when ready)
 *
 * The agent does every check it can; only what it cannot do is amber, with its one action, and
 * the same items are the project's Needs-you list. "Accept backlog" puts the ranked items on the
 * Roadmap's Next lane (the proposals already ARE the Roadmap's recommendations) — nothing starts a
 * build until a person starts an item; "Edit" reorders, drops and renames them first. Both unlock
 * when Ready for work is complete, as the wireframe says.
 */
import React, { useEffect, useMemo, useState } from "react";
import {
  ArrowDown, ArrowUp, ArrowsClockwise, Brain, Check, CheckCircle, Compass, GithubLogo, GitPullRequest, HandWaving, ListChecks, ListNumbers,
  MagnifyingGlass, Note, PencilSimple, Play, Rocket, Sparkle, Terminal, Warning, XCircle,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { notifyChanged } from "../changed";
import {
  acceptBacklog, firstWorkAccept, navigateTo, projectHref, pxHref, runAccept, startOnboarding,
  type NextItem, type Node, type Onboarding, type OnboardingItem,
} from "../../lib/project-experience";
import { Face, Ico, STATE_WORD } from "./kit";
import "./onboarding.css";

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
const MARSHAL = { name: "Marshal", role_key: "chief_of_staff" };

/** Elapsed onboarding time in words ("6 min"). */
export const elapsedWords = (s: number) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`);
/** The live diagram's timer ("1m 52s"). */
export const clockWords = (s: number) => (s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
/** "re-read 38s ago". */
export const agoWords = (s: number) => (s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86_400 ? `${Math.floor(s / 3600)}h ago` : `${Math.floor(s / 86_400)}d ago`);

/** Real elapsed seconds while the run moves: the server's count, then the viewer's clock from the
 *  run's start (a tick a second); a finished run's own count, still. */
export function useElapsed(ob: Pick<Onboarding, "state" | "elapsed_s" | "started_at"> | null): number {
  const [now, setNow] = useState(() => Date.now());
  const running = ob?.state === "running";
  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  if (!ob) return 0;
  const started = ob.started_at ? Date.parse(ob.started_at) : NaN;
  if (running && Number.isFinite(started)) return Math.max(ob.elapsed_s, Math.floor((now - started) / 1000));
  return ob.elapsed_s;
}

// ── 00b: "Helm is reading your repo" ─────────────────────────────────────────────────────────

const LIVE_Y = [56, 126, 196];
/** Helm, the three things it does while it reads (Stack found · Mapping code · Tests run), then
 *  the Ready check: each edge follows its target's state (done: solid green; now: dashed teal;
 *  todo: dashed grey), labels at most four words, the sub is the real value (118 / 214 files). */
export function LiveDiagram({ nodes }: { nodes: Node[] }) {
  const steps = nodes.filter((x) => x.key !== "ready").slice(0, 3);
  const ready = nodes.find((x) => x.key === "ready") ?? { key: "ready", label: "Ready check", state: "todo" as const, sub: "", href: "" };
  const tone = (st: string) => (st === "done" ? "var(--green)" : st === "now" ? "var(--teal)" : st === "failed" ? "var(--red)" : st === "waiting" ? "var(--amber)" : "var(--border-strong)");
  const mark = (st: string) => (st === "done" ? "✓ " : st === "now" ? "○ " : st === "failed" ? "✕ " : "");
  return (
    <div className="px-live" data-testid="px-ob-live">
      <svg viewBox="0 0 640 250" role="img" preserveAspectRatio="xMidYMid meet"
           aria-label={`Helm: ${[...steps, ready].map((x) => `${x.label} ${STATE_WORD[x.state]}${x.sub ? ` (${x.sub})` : ""}`).join(", ")}`}>
        {steps.map((st, i) => (
          <path key={`e${st.key}`} className="px-live-edge" data-s={st.state} d={`M120 126 C 200 126, 220 ${LIVE_Y[i]}, 300 ${LIVE_Y[i]}`}
                fill="none" stroke={tone(st.state)} strokeWidth={2} strokeDasharray={st.state === "done" ? undefined : "6 5"} />
        ))}
        {steps.map((st, i) => (
          <path key={`r${st.key}`} d={`M420 ${LIVE_Y[i]} C 470 ${LIVE_Y[i]}, 480 126, 520 126`} fill="none" stroke="var(--border-strong)" strokeWidth={1.4} strokeDasharray="4 5" />
        ))}
        <circle cx={78} cy={126} r={44} className="px-live-helm" />
        <text x={78} y={194} textAnchor="middle" className="px-live-cap">Helm</text>
        {steps.map((st, i) => (
          <g key={st.key} data-testid={`px-ob-live-${st.key}`} data-s={st.state}>
            <rect x={300} y={LIVE_Y[i] - 19} width={120} height={38} rx={10} className="px-live-node" data-s={st.state} stroke={tone(st.state)} />
            <text x={360} y={LIVE_Y[i] + 4} textAnchor="middle" className="px-live-label">{mark(st.state)}{st.label}</text>
            {st.sub ? <text x={360} y={LIVE_Y[i] + 33} textAnchor="middle" className="px-live-sub">{st.sub}</text> : null}
          </g>
        ))}
        <g data-testid="px-ob-live-ready" data-s={ready.state}>
          <rect x={520} y={107} width={112} height={38} rx={19} className="px-live-ready" data-s={ready.state} />
          <text x={576} y={130} textAnchor="middle" className="px-live-label" data-faint={ready.state !== "done" || undefined}>{mark(ready.state)}{ready.label}</text>
        </g>
      </svg>
      <span className="px-live-face" aria-hidden><Face agent={CREW.engineering_captain} size={56} state="working" /></span>
      {/* A phone draws the same flow top to bottom, at a size that can be read (390 px). */}
      <ol className="px-live-list" aria-hidden>
        <li data-s="done"><Face agent={CREW.engineering_captain} size={28} state="working" /><b>Helm</b></li>
        {[...steps, ready].map((st) => (
          <li key={st.key} data-s={st.state}><span className="px-live-dot" aria-hidden>{mark(st.state).trim() || "·"}</span>
            <b>{st.label}</b>{st.sub ? <small className="px-mono">{st.sub}</small> : null}</li>
        ))}
      </ol>
    </div>
  );
}

// ── 10: "Discovered" ───────────────────────────────────────────────────────────────────────────

/** Helm in the middle, what it found around it: each a node that turns green when found. */
export function DiscoverDiagram({ nodes }: { nodes: Node[] }) {
  const n = Math.max(nodes.length, 1);
  const W = 300, H = 200, cx = 150, cy = 95;
  // The wireframe's own five places (files · stack · tests · hot spots · docs); any other count
  // goes round the circle.
  const FIVE = [{ x: 60, y: 35 }, { x: 240, y: 35 }, { x: 45, y: 130 }, { x: 255, y: 130 }, { x: 150, y: 170 }];
  const pts = nodes.length === 5 ? FIVE : nodes.map((_, i) => {
    const a = -Math.PI / 2 - Math.PI / 4 + (i * 2 * Math.PI) / n;
    return { x: cx + Math.cos(a) * 112, y: cy + Math.sin(a) * 68 };
  });
  // A label sits under its node; the bottom node's sits to its right (as drawn).
  const labelAt = (p: { x: number; y: number }) => (p.y > 160 ? { x: p.x + 20, y: p.y + 4, anchor: "start" as const } : { x: p.x, y: p.y + 29, anchor: "middle" as const });
  return (
    <div style={{ position: "relative" }} data-testid="px-discover">
      <svg className="px-radial" viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={`Discovered: ${nodes.map((x) => `${x.label} ${STATE_WORD[x.state]}`).join(", ")}`}>
        {pts.map((p, i) => (
          <line key={`e${i}`} x1={cx} y1={cy} x2={p.x} y2={p.y} className="px-live-edge" data-s={nodes[i].state}
                stroke={nodes[i].state === "done" ? "var(--green)" : nodes[i].state === "skipped" ? "var(--border-strong)" : "var(--teal)"}
                strokeWidth={1.5} strokeDasharray={nodes[i].state === "done" ? undefined : "5 4"} opacity={0.85} />
        ))}
        {pts.map((p, i) => {
          const st = nodes[i].state;
          const c = st === "done" ? "var(--green)" : st === "failed" ? "var(--red)" : st === "skipped" ? "var(--border-strong)" : "var(--teal)";
          return (
            <g key={`n${i}`} data-testid={`px-discover-${nodes[i].key || i}`}>
              <circle cx={p.x} cy={p.y} r={p.y > 160 ? 12 : 15} fill={st === "done" ? "var(--green-soft)" : "var(--surface)"} stroke={c} strokeWidth={2} />
              {st === "done" ? <path d={`M${p.x - 4.5} ${p.y} l3 3.2 l6 -6.5`} stroke="var(--green)" strokeWidth={2} fill="none" /> : null}
              {(() => { const l = labelAt(p); return <text x={l.x} y={l.y} textAnchor={l.anchor}>{nodes[i].label}</text>; })()}
            </g>
          );
        })}
        <circle cx={cx} cy={cy} r={24} fill="color-mix(in srgb, var(--accent) 18%, var(--surface))" stroke="var(--accent)" strokeWidth={2} />
      </svg>
      <span style={{ position: "absolute", left: "50%", top: `${(cy / H) * 100}%`, transform: "translate(-50%, -50%)" }} aria-hidden>
        <Face agent={CREW.engineering_captain} size={34} state="working" />
      </span>
    </div>
  );
}

/** A candidate's source in words and an icon: "CI", "issue #12", "TODO ×7", "PR #7", "tests". */
export function candidateSource(b: NextItem): { icon: React.ReactNode; words: string } {
  const [kind, id] = (b.ref || "").split(":");
  if (kind === "ci" || b.source === "ci") return { icon: <XCircle size={11} aria-hidden />, words: "CI" };
  if (kind === "tests" || b.source === "tests") return { icon: <XCircle size={11} aria-hidden />, words: "tests" };
  if (kind === "issue") return { icon: <GithubLogo size={11} aria-hidden />, words: `issue #${id}` };
  if (kind === "pr") return { icon: <GitPullRequest size={11} aria-hidden />, words: `PR #${id}` };
  if (kind === "alert") return { icon: <Warning size={11} aria-hidden />, words: "alert" };
  if (kind === "deferred" || kind === "build") return { icon: <ArrowsClockwise size={11} aria-hidden />, words: "BOSS build" };
  const todos = /(\d+)\s+TODO/i.exec(b.detail);
  if (kind === "todo" || todos) return { icon: <Note size={11} aria-hidden />, words: todos ? `TODO ×${todos[1]}` : "TODOs" };
  return { icon: <MagnifyingGlass size={11} aria-hidden />, words: kind === "osv" ? "security scan" : kind === "rec" ? "roadmap" : "code scan" };
}

type SaidT = { text: string; ok: boolean; link?: boolean } | null;

/** The workspace's GitHub and model (§15): read-only, each a link to its own Setup page. */
function WorkspaceRow({ ob }: { ob: Onboarding }) {
  const repo = ob.checklist.find((c) => c.key === "repo_access");
  const model = ob.checklist.find((c) => c.key === "models");
  const gh = repo?.state === "done"; const md = model?.state === "done";
  return (
    <div className="px-ob-ws" data-testid="px-ob-workspace">
      <span className="px-lbl">Workspace</span>
      <a className="px-chip" data-ok={gh || undefined} href={pxHref("?area=setup&focus=repository")} data-testid="px-ob-ws-github" title="GitHub accounts live in Setup › Repo and models">
        <GithubLogo size={11} aria-hidden /> GitHub {gh ? "✓" : "—"} <span className="px-faint">{repo?.sub || "checking"}</span></a>
      <a className="px-chip" data-ok={md || undefined} href={pxHref("?area=setup&focus=models")} data-testid="px-ob-ws-model" title="Models live in Setup › Repo and models">
        <Brain size={11} aria-hidden /> Model {md ? "✓" : "—"} <span className="px-faint">{model?.sub || "checking"}</span></a>
    </div>
  );
}

export function OnboardingView({ projectId, ob, onRefresh }: { projectId: string; ob: Onboarding; onRefresh?: () => void }) {
  const [said, setSaid] = useState<SaidT>(null);
  const elapsed = useElapsed(ob);
  const rows = ob.ready.length ? ob.ready : ob.checklist.map((c) => ({ ...c, agent: "engineering_captain" }));
  const ready = rows.filter((c) => c.state === "done" || c.state === "skipped").length;
  const unlocked = rows.length > 0 && ready === rows.length;
  const running = ob.state === "running";
  const restart = async () => {
    const r = await startOnboarding(projectId);
    setSaid({ ok: r.ok, text: r.ok ? (r.demo ? "Sample mode — nothing was sent." : ob.state === "not_started" ? "Onboarding started." : "Helm is reading the repository again.") : (r.error ?? "It didn't start.") });
    if (r.ok && !r.demo) { notifyChanged("project.onboarding"); onRefresh?.(); }
  };
  const nowAgent = useMemo(() => {
    const doing = rows.find((c) => c.state === "now" && c.owner !== "you");
    const named = Object.values(CREW).find((c) => ob.now.startsWith(`${c.name} `));
    return named ?? crewOf(doing?.agent);
  }, [rows, ob.now]);
  const discoverDone = ob.stages.find((x) => x.key === "discover")?.state === "done";
  const live = ob.discover.live ?? [];
  return (
    <div className="px-stack px-ob-view" data-testid="px-onboarding" data-state={ob.state}>
      <section className="px-card" aria-label="Onboarding stages">
        <div className="px-ob" role="list" aria-label="Onboarding stages" data-testid="px-ob-stages">
          {ob.stages.map((st) => (
            <div key={st.key} className="px-ob-s" data-s={st.state} role="listitem" aria-label={`${st.label}: ${STATE_WORD[st.state]}`} data-testid={`px-ob-stage-${st.key}`}>
              <span className="px-jr-shape" aria-hidden>{st.state === "done" ? <Check size={16} weight="bold" /> : st.state === "failed" ? <XCircle size={16} weight="bold" /> : STAGE_ICON[st.key] ?? <Sparkle size={16} />}</span>
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
        <div className="px-ob-foot">
          <WorkspaceRow ob={ob} />
          <span className="px-spacer" />
          {ob.state === "failed" ? <span className="px-tag" data-tone="red">Onboarding stopped</span> : null}
          {!running && ob.read_ago_s != null ? (
            <small className="px-faint px-mono" data-testid="px-ob-read-ago" title={ob.trigger === "manual" ? "Read again on request" : "Read when the project was added"}>
              {(ob.runs ?? 1) > 1 ? "re-read" : "read"} {agoWords(ob.read_ago_s)}</small>
          ) : null}
          {!running ? (
            <GatedButton permission="workflow_run:start" projectId={projectId} className="px-btn" data-kind={ob.state === "failed" || ob.state === "not_started" ? "primary" : "ghost"} data-size="s"
                         data-testid="px-ob-restart" onClick={() => void restart()} title="Helm reads the repository again; nothing you accepted changes">
              <ArrowsClockwise size={12} /> {ob.state === "failed" ? "Try again" : ob.state === "not_started" ? "Start onboarding" : "Read again"}</GatedButton>
          ) : null}
        </div>
        {ob.error ? <p className="px-ob-error" role="alert" data-testid="px-ob-error"><XCircle size={13} aria-hidden /> {ob.error}</p> : null}
      </section>

      {ob.state !== "not_started" ? (
        <section className="px-card px-ob-livecard" aria-labelledby="px-ob-l" data-testid="px-ob-livecard">
          <div className="px-card-h"><Ico icon={Compass} c="teal" /><h2 id="px-ob-l">{running ? "Helm is reading your repo" : "Helm read your repo"}</h2>
            {running ? <span className="px-tag" data-tone="blue"><span className="px-dot" data-s="now" aria-hidden /> live</span>
              : ob.state === "failed" ? <span className="px-tag" data-tone="red">stopped</span> : <span className="px-tag" data-tone="green">done</span>}
            <span className="px-more px-mono px-faint" data-testid="px-ob-clock" title="Time since onboarding started">{clockWords(elapsed)}</span></div>
          <LiveDiagram nodes={live} />
        </section>
      ) : null}

      <div className="px-grid px-g3 px-ob-cards">
        <section className="px-card" aria-labelledby="px-ob-d">
          <div className="px-card-h"><Ico icon={MagnifyingGlass} c="green" /><h2 id="px-ob-d">Discovered</h2>
            {discoverDone ? <span className="px-tag" data-tone="green">done</span>
              : ob.state === "not_started" ? <span className="px-tag">not started</span> : <span className="px-tag" data-tone="blue">live</span>}</div>
          {ob.discover.nodes.length ? <DiscoverDiagram nodes={ob.discover.nodes} />
            : <p className="px-faint" style={{ margin: 0 }}>Files, stack, tests, hot spots and docs appear here as Helm finds them.</p>}
          <a href={projectHref(projectId, { tab: "knowledge" })} data-testid="px-ob-dossier">Open dossier →</a>
        </section>
        <section className="px-card px-ob-ready" aria-labelledby="px-ob-r">
          <div className="px-card-h"><Ico icon={ListChecks} c="teal" /><h2 id="px-ob-r">Ready for work</h2>
            <span className="px-tag" data-tone="blue" data-testid="px-ob-ready-count">{ready}/{rows.length}</span>
</div>
          <div data-testid="px-ob-checklist">
            {!rows.length ? <p className="px-faint" style={{ margin: 0 }}>Helm checks repo access, the build, the tests, the workflow, where it deploys and the secrets it needs — and asks only for what it can&apos;t do.</p> : null}
            {rows.map((c) => {
              const you = c.owner === "you" && c.state !== "done";
              return (
                <div key={c.key} className="px-ck-i" data-s={you ? "you" : c.state} data-testid={`px-ob-item-${c.key}`} title={c.detail}>
                  <span className="px-ck-b" aria-hidden>{c.state === "done" || c.state === "skipped" ? <Check size={11} weight="bold" /> : you ? "!" : c.state === "failed" ? "✕" : ""}</span>
                  <span className="px-ck-t">{c.label}<small>{c.sub || c.detail}</small><span className="px-sr">{you ? "needs you" : STATE_WORD[c.state]}. {c.detail}</span></span>
                  {you && c.action ? <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(c.action.href)} data-testid={`px-ob-act-${c.key}`}>{c.action.label}</a>
                    : <span title={`${crewOf(c.agent).name} ${c.state === "now" ? "is on it" : "did this"}`}><Face agent={crewOf(c.agent)} size={20} state={c.state === "now" ? "working" : "idle"} /></span>}
                </div>
              );
            })}
          </div>
        </section>
        <BacklogCard projectId={projectId} ob={ob} unlocked={unlocked} total={rows.length} onSaid={setSaid} />
      </div>

      <div className="px-grid px-g2 px-ob-cards">
        <OnlyYouCard ob={ob} />
        {/* One primary per view (X-78): Accept backlog until the backlog is on the Roadmap, then Start. */}
        <FirstWorkCard projectId={projectId} ob={ob} readyDone={unlocked} onSaid={setSaid} primary={!ob.backlog.some((b) => !b.accepted)} />
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

/** "Only you can" (00b-21..23): the amber items, each with its one action. */
export function OnlyYouCard({ ob, extra }: { ob: Onboarding | null; extra?: React.ReactNode }) {
  const items: OnboardingItem[] = ob?.only_you ?? [];
  return (
    <section className="px-card" aria-labelledby="px-ob-you" data-testid="px-ob-only-you">
      <div className="px-card-h"><Ico icon={HandWaving} c="amber" /><h2 id="px-ob-you">Only you can</h2></div>
      {extra}
      {items.map((it) => (
        <div key={it.key} className="px-ny" data-testid={`px-ob-you-${it.key}`}>
          <span className="px-ny-ico" aria-hidden>!</span>
          <div style={{ flex: 1, minWidth: 0 }}><div className="px-ny-t">{it.label}{it.sub ? ` · ${it.sub}` : ""}</div><small className="px-faint">{it.detail}</small></div>
          {it.action ? <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(it.action.href)}>{it.action.label}</a> : null}
        </div>
      ))}
      {!items.length && !extra ? <p className="px-faint" style={{ margin: "0 0 6px" }} data-testid="px-ob-you-none">Nothing — Helm asks only for what it can&apos;t do itself.</p> : null}
      <small className="px-faint">Everything else Helm does itself.</small>
    </section>
  );
}

/** "First work, suggested" (00b-24..28): Marshal's pick, why, its estimate from this project's
 *  history, Start when ready (enabled once Ready is done) and Other ideas. */
export function FirstWorkCard({ projectId, ob, readyDone, onSaid, primary = true }: { projectId: string; ob: Onboarding | null; readyDone: boolean; onSaid?: (s: SaidT) => void; primary?: boolean }) {
  const fw = ob?.first_work ?? null;
  const [busy, setBusy] = useState(false);
  const start = async () => {
    if (!fw) return;
    setBusy(true);
    const r = await runAccept(projectId, firstWorkAccept(projectId, fw), "Start");
    setBusy(false);
    if (r.ok) {
      onSaid?.({ ok: true, text: r.demo ? "Sample mode — nothing was sent." : "Started: a build opens a pull request on your repo." });
      if (!r.demo) { notifyChanged("project.work"); navigateTo(projectHref(projectId, { tab: "work" })); }
    } else onSaid?.({ ok: false, text: r.error ?? "It didn't start." });
  };
  const sourceWord = fw ? (fw.source === "ci" ? "CI" : fw.source === "tests" ? "Tests" : fw.source === "bug" ? "Bug" : fw.source === "ticket" ? "Issue" : fw.source === "alert" ? "Alert" : fw.source === "boss" ? "BOSS build" : "Roadmap") : "";
  const red = fw ? fw.kind === "bug" || fw.source === "ci" || fw.source === "tests" : false;
  const basis = fw?.estimate_basis === "history" && fw.estimate_runs ? `from ${fw.estimate_runs} past run${fw.estimate_runs === 1 ? "" : "s"} here` : "the workflow's typical time";
  return (
    <section className="px-card px-ob-first" aria-labelledby="px-ob-first" data-testid="px-ob-first">
      <div className="px-card-h"><Ico icon={Sparkle} c="violet" /><h2 id="px-ob-first">First work, suggested</h2>
        <span className="px-more" title="Ranked by Marshal" data-testid="px-ob-first-marshal"><Face agent={MARSHAL} size={20} /></span></div>
      {fw ? (
        <>
          <b className="px-ob-first-t" data-testid="px-ob-first-label">{fw.label}</b>
          {fw.why_first ? <p className="px-faint px-ob-why" data-testid="px-ob-first-why">{fw.first_by === "marshal" ? "Marshal: " : ""}{fw.why_first}{fw.why ? ` · ${fw.why}` : ""}</p> : null}
          <div className="px-row px-wrap" style={{ margin: "6px 0 10px" }}>
            {sourceWord ? <span className="px-tag" data-tone={red ? "red" : undefined}>{red ? <XCircle size={11} aria-hidden /> : null}{sourceWord}</span> : null}
            {fw.estimate_min !== null ? <span className="px-tag" data-tone="cost" title={`≈ minutes to the pull request: ${basis}`} data-testid="px-ob-first-min">≈ {fw.estimate_min} min</span> : null}
            {fw.estimate_usd !== null ? <span className="px-tag" data-tone="cost" title="≈ what it costs: the median of this project's past runs" data-testid="px-ob-first-usd">≈ ${fw.estimate_usd.toFixed(2)}</span> : null}
          </div>
          <div className="px-row px-wrap">
            <GatedButton permission="workflow_run:start" projectId={projectId} className="px-btn" data-kind={primary ? "primary" : undefined} data-testid="px-ob-first-start"
                         disabled={!readyDone || busy} title={!readyDone ? "Starts when Ready is done" : "Start it now: a build opens a PR on your repo"}
                         onClick={() => void start()}><Play size={12} weight="fill" /> Start when ready</GatedButton>
            {fw.others_href ? <a className="px-btn" data-kind="ghost" href={pxHref(fw.others_href)} data-testid="px-ob-first-others">Other ideas</a> : null}
          </div>
        </>
      ) : <p className="px-faint">Marshal suggests one as soon as Helm has read the repo.</p>}
    </section>
  );
}

/** Backlog proposed: the ranked candidates with their why, "+N more", Accept backlog and its editor. */
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
        <span title="Ranked by Marshal, Chief of Staff" data-testid="px-ob-marshal"><Face agent={MARSHAL} size={20} /></span>
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
            const kind = b.kind === "bug" || /bug|ci|tests/i.test(b.source) ? "bug" : b.kind === "chore" ? "chore" : "feature";
            const src = candidateSource(b);
            return (
              <div key={b.id || i} className="px-ck-i px-ob-bl" data-testid={`px-ob-backlog-${i}`}>
                <span className="px-rank">{i + 1}</span>
                <span className="px-ck-t" style={{ minWidth: 0 }} title={b.detail}><span className="px-ell" style={{ display: "block" }}>{b.label}</span>
                  <small className="px-row" style={{ gap: 4 }}>{src.icon}{b.external_url ? <a href={b.external_url} target="_blank" rel="noreferrer noopener">{src.words} ↗</a> : src.words}
                    {b.why ? <span className="px-faint" data-testid={`px-ob-backlog-why-${i}`}>· {b.why}</span> : null}</small></span>
                {b.accepted ? <span className="px-tag" data-tone="green" title="On the Roadmap's Next lane">on Roadmap</span>
                  : <span className="px-tag" data-tone={kind === "bug" ? "red" : undefined}>{kind}</span>}
              </div>
            );
          }) : <p className="px-faint">{ob.state === "running" ? "Marshal ranks failing tests, open issues and PRs, alerts and what Helm finds — drafting." : "Nothing to propose: no failing tests, open issues or findings."}</p>}
          {extra > 0 ? (
            <button type="button" className="px-ck-i px-ob-more" onClick={() => setMore((m) => !m)} aria-expanded={more} data-testid="px-ob-more">
              <span className="px-rank" style={{ opacity: 0.5 }}>…</span><span className="px-ck-t px-faint">{more ? "Show the top 4" : `+${extra} more`}</span><span />
            </button>
          ) : null}
          {ob.backlog.length ? (
            <div className="px-row" style={{ marginTop: 12, gap: 6 }}>
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
