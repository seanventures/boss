"use client";

/**
 * THE WORK PANEL: ONE PIECE OF WORK'S LANDING PAGE (operator-approved wireframe, 2026-11-19;
 * ~/Downloads/work-panel-spec.md). The work row and the drawer's "Open" land here:
 *
 *   1  header       title, kind, state in one sentence, started / waiting / elapsed / cost /
 *                   attempts, ONE primary action (workNextStep) and the change's preview
 *   2  who did what Spec → Build → Review & tests → Merge → Staging → Production, each with its
 *                   agent, status and time, and the inline "BOSS is fixing…" / "Your move"
 *   3  asked / discussed / built   the person's words + must-haves with verdicts, the panel's
 *                   decisions, the one-sentence summary of what was built
 *   4  rail         the change's preview, where it runs, attempts on one line, the cost sparkline
 *      comments     the review's comments, deduped, with "Let BOSS fix these"
 *   5  Ask Helm     the captain dock, three questions per state, full screen
 *
 * The drawer itself is a short preview of this page (header, flow, primary action) with "Open".
 * The build page's tool-call log, outputs and gates stay there ("Full timeline & live log").
 */
import React, { useMemo, useState } from "react";
import {
  ArrowRight, ArrowsOut, Bug, ChatCircleDots, ChatsCircle, Code, CurrencyDollar, DotsThree, FlowArrow, GitMerge, Graph, Hammer,
  Gear, Lightbulb, LinkSimple, MagnifyingGlass, Monitor, PaperPlaneRight, Stack, Terminal, Ticket, Wrench,
} from "@phosphor-icons/react";

import { changeRef, linkTarget } from "../../lib/repo-links";
import { mergeWaitOf, workNextStep, type NextStep } from "../../lib/work-next-step";
import {
  attemptsLine, connectorState, costPoints, durationWords, flowNodes, helmSuggestions, isCodeWork, secondsBetween, stateSentence, type FlowNode,
} from "../../lib/work-panel";
import {
  KIND_WORDS, ATTEMPT_WORDS, pxHref, pxWrite, writePxUrl, type Agent, type Deployments, type OutcomeEntry, type WorkItem, type WorkOutcome, type WorkStory,
} from "../../lib/project-experience";
import { localWith } from "../../lib/local-time";
import { GatedButton } from "../../components/gated";
import { ChangeLink } from "../change-link";
import { PrdRecordView } from "../prd-record";
import { Face, Said } from "./kit";
import { WorkPreviewButton } from "./work-preview";
import type { PxCtx } from "./project-page";
import "./work-panel.css";

const WORK_WORD: Record<string, { word: string; s: string; tone: string }> = {
  needs_you: { word: "Needs you", s: "wait", tone: "amber" }, running: { word: "Running", s: "live", tone: "mint" },
  stalled: { word: "Stalled", s: "wait", tone: "amber" }, done: { word: "Done", s: "done", tone: "green" },
  failed: { word: "Failed", s: "failed", tone: "red" }, planned: { word: "Planned", s: "todo", tone: "" },
};
const KIND_ICON: Record<string, typeof Lightbulb> = { feature: Lightbulb, bug: Bug, fix: Wrench, ticket: Ticket, chore: Wrench, incident: MagnifyingGlass, setup: Stack, job: Gear };
const FLOW_WORD: Record<string, string> = { done: "done", live: "BOSS working", wait: "waiting on you", todo: "next", failed: "failed", skipped: "skipped" };
const BADGE: Record<string, string> = { done: "✓", live: "↻", wait: "!", failed: "✕" };

const hhmm = (iso: string) => localWith(iso, { hour: "numeric", minute: "2-digit" });
const money = (usd: number) => `$${usd.toFixed(2)}`;

/** The crew a station falls back to: the row's faces, the captain and its squad. */
function crewOf(ctx: PxCtx, w: WorkItem): Agent[] {
  const cap = ctx.overview.captain;
  return [...w.agents, ...(cap ? [cap as unknown as Agent, ...cap.squad] : [])];
}

// ── the one primary action ─────────────────────────────────────────────────────────────────

/** ONE primary action: workNextStep's words only. BOSS working → "Watch live" (no amber);
 *  BOSS still fixing the change → "Watch the fix" first, Merge second. */
export function PrimaryAction({ ctx, w, next, testid }: { ctx: PxCtx; w: WorkItem; next: NextStep | null; testid: string }) {
  const mp = mergeWaitOf(w);
  const l = w.links;
  if (!next) {
    if (!l.log || (w.state !== "running" && w.state !== "stalled")) return null;
    return <a className="wp-btn" data-kind="mint" href={pxHref(l.log)} data-testid={`${testid}-watch`}><Terminal size={14} aria-hidden /> Watch live</a>;
  }
  const kind = next.secondary ? undefined : "primary";
  const mergeBtn = next.kind === "merge" && mp ? (l.decision
    ? <a className="wp-btn" data-kind={kind} href={pxHref(next.href)} data-testid={`${testid}-merge`}><GitMerge size={14} aria-hidden /> {next.label}</a>
    : <a className="wp-btn" data-kind={kind} href={next.href || undefined} {...linkTarget(mp.external_url)} data-testid={`${testid}-merge`}><GitMerge size={14} aria-hidden /> {next.label}</a>)
    : null;
  return (
    <>
      {next.wait?.href ? <a className="wp-btn" data-kind="mint" href={pxHref(next.wait.href)} data-testid={`${testid}-wait`}><Terminal size={14} aria-hidden /> {next.wait.label}</a> : null}
      {mergeBtn ?? (l.decision || l.workflow_run
        ? <a className="wp-btn" data-kind="primary" href={pxHref(l.decision || l.workflow_run)} data-testid={`${testid}-decide`}>{next.label}</a> : null)}
    </>
  );
}

// ── 1 header ───────────────────────────────────────────────────────────────────────────────

function Header({ ctx, w, story, next, short, testid }: { ctx: PxCtx; w: WorkItem; story: WorkStory | null; next: NextStep | null; short?: boolean; testid: string }) {
  const st = WORK_WORD[w.state] ?? WORK_WORD.planned;
  const said = stateSentence(w, next);
  const Icon = KIND_ICON[w.kind] ?? Lightbulb;
  const mp = mergeWaitOf(w);
  const mergeAt = w.attempts.find((t) => t.id === mp?.build_id);
  const waiting = w.state === "needs_you" ? durationWords(secondsBetween(mergeAt?.updated_at || w.updated_at || w.created_at)) : "";
  const elapsed = story?.minutes ? durationWords(story.minutes * 60) : durationWords(secondsBetween(w.created_at, w.state === "running" ? null : w.updated_at));
  const paid = (w.cost_usd ?? 0) > 0;
  const cost = paid ? money(w.cost_usd ?? 0) : w.list_usd ? `≈ ${money(w.list_usd)}` : "";
  return (
    <section className="wp-card wp-hdr" aria-label="Summary" data-testid={`${testid}-header`}>
      <div>
        <div className="wp-ttl">
          <div className="wp-kind" aria-hidden><Icon size={22} weight="duotone" /></div>
          <div style={{ minWidth: 0 }}>
            {short ? null : <h1 className="wp-h1" data-testid={`${testid}-title`}>{w.label}</h1>}
            <div className="wp-meta">
              <span className="wp-pill">{KIND_WORDS[w.kind] ?? w.kind}</span>
              {w.workflow || story?.number || w.run_number ? (
                <span className="wp-pill" data-testid={`${testid}-number`}>{[w.workflow, story?.number || w.run_number ? `run #${story?.number || w.run_number}` : ""].filter(Boolean).join(" · ")}</span>
              ) : null}
              <span className="wp-pill" data-tone={st.tone} data-testid={`${testid}-state`}><span className="wp-dot" data-s={st.s} aria-hidden />{st.word}</span>
              {w.agents.length ? (
                <span className="wp-stack" role="img" aria-label={`Agents: ${w.agents.map((a) => a.name).join(", ")}`}>
                  {w.agents.slice(0, 5).map((a, i) => <Face key={`${a.name}-${i}`} agent={a} size={24} />)}
                </span>
              ) : null}
            </div>
          </div>
        </div>
        <p className="wp-state" data-testid={`${testid}-sentence`}><b data-tone={said.tone}>{said.lead}</b>{said.rest ? ` ${said.rest}` : ""}</p>
        {short ? null : (
          <div className="wp-kpis" data-testid={`${testid}-kpis`}>
            {w.created_at ? <div className="wp-kpi">Started<b>{hhmm(w.created_at)}</b></div> : null}
            {waiting ? <div className="wp-kpi">Waiting on you<b>{waiting}</b></div> : null}
            {elapsed ? <div className="wp-kpi">Elapsed<b>{elapsed}</b></div> : null}
            {cost ? <div className="wp-kpi" title={paid ? "Spent" : "List price of the calls your plan includes"}>Cost<b>{cost}</b></div> : null}
            {w.attempt_count ? <div className="wp-kpi">Attempts<b>{w.attempt_count}</b></div> : null}
          </div>
        )}
      </div>
      <div className="wp-act">
        <div className="wp-act-row">
          {w.preview_app && (w.state === "needs_you" || w.state === "done") ? (
            <WorkPreviewButton app={w.preview_app} projectId={ctx.projectId} actor={ctx.actor} testid={`${testid}-preview`} className="wp-btn" />
          ) : null}
          <PrimaryAction ctx={ctx} w={w} next={next} testid={testid} />
        </div>
        {next?.kind === "merge" && (next.hint || mp) ? (
          <span className="wp-hint" data-testid={`${testid}-hint`}>
            {next.hint}{next.hint && mp ? " · " : ""}
            {mp ? <ChangeLink url={mp.external_url} number={mp.number} className="" icon={false} title="see the change" testid={`${testid}-see-change`} /> : null}
          </span>
        ) : null}
      </div>
    </section>
  );
}

// ── 2 who did what ─────────────────────────────────────────────────────────────────────────

export function WhoDidWhat({ nodes, logHref, logLabel = "Full timeline & live log", vertical, testid }: {
  nodes: FlowNode[]; logHref: string; logLabel?: string; vertical?: boolean; testid: string;
}) {
  return (
    <section className="wp-card wp-flow" aria-label="Who did what" data-vertical={vertical || undefined} data-testid={`${testid}-flow`}>
      <h2 className="wp-sec-h">Who did what <span className="wp-ln" /></h2>
      <ol className="wp-lane" style={{ "--wp-n": Math.max(nodes.length, 1) } as React.CSSProperties}>
        {nodes.map((n, i) => (
          <li key={n.key} className="wp-node" data-s={n.state} data-testid={`${testid}-node-${n.key}`} title={n.title || undefined}
              aria-label={`${n.name}: ${FLOW_WORD[n.state]}${n.sub ? `, ${n.sub}` : ""}`}>
            <div className="wp-ring">
              {n.you ? <Face agent={{ name: "You" }} size={34} /> : n.agent ? <Face agent={n.agent} size={34} state={n.state === "live" ? "working" : "idle"} /> : <span />}
              {BADGE[n.state] ? <span className="wp-badge" aria-hidden>{BADGE[n.state]}</span> : null}
            </div>
            <div className="wp-nm">{n.name}</div>
            <div className="wp-st">{n.sub || FLOW_WORD[n.state]}</div>
            {n.who ? <div className="wp-who">{n.who}</div> : null}
            {n.note ? (
              <div className="wp-note" data-tone={n.note.tone} data-testid={`${testid}-note-${n.key}`}>
                <div className="wp-note-t">{n.note.tone === "boss" ? <span className="wp-dot" data-s="live" aria-hidden /> : null}{n.note.title}</div>
                {n.note.text}{n.note.action ? <> · <a href={pxHref(n.note.action.href)}>{n.note.action.label}</a></> : null}
              </div>
            ) : null}
            {i < nodes.length - 1 ? <span className="wp-conn" data-s={connectorState(nodes, i)} aria-hidden /> : null}
          </li>
        ))}
      </ol>
      <div className="wp-flow-foot">
        <span className="wp-legend" aria-hidden>
          <span><span className="wp-dot" data-s="done" />done</span><span><span className="wp-dot" data-s="live" />BOSS working</span>
          <span><span className="wp-dot" data-s="wait" />you</span><span><span className="wp-dot" />next</span>
        </span>
        {logHref ? <a href={pxHref(logHref)} data-testid={`${testid}-log`}>{logLabel} <ArrowRight size={12} aria-hidden /></a> : null}
      </div>
    </section>
  );
}

// ── 3 asked / discussed / built ────────────────────────────────────────────────────────────

const MUST_MARK: Record<string, string> = { met: "✓", partial: "½", not_met: "✕", unchecked: "·" };

function Story({ w, story, testid }: { w: WorkItem; story: WorkStory | null; testid: string }) {
  const built = story?.explained?.built ?? null;
  const said = story?.explained?.discussed ?? null;
  const prd = story?.prd ?? null;
  const [prdOpen, setPrdOpen] = useState(false);
  const musts = built?.mustHaves ?? [];
  const counts = { met: musts.filter((m) => m.verdict === "met").length, partial: musts.filter((m) => m.verdict === "partial").length };
  const seats = (prd?.meeting?.positions ?? []).map((p) => ({ name: p.agent || p.seat, role_key: p.seat, avatar: "" } as Agent));
  const dissent = prd?.meeting?.dissent.length ?? 0;
  const files = built?.groups.flatMap((g) => g.files) ?? [];
  const testFiles = built?.groups.find((g) => g.purpose === "Tests")?.files ?? [];
  const tests = built?.tests;
  return (
    <section className="wp-card wp-story" aria-label="What was asked, discussed and built" data-testid={`${testid}-story`}>
      <div data-testid={`${testid}-asked`}>
        <h3 className="wp-k"><span className="wp-n">1</span>Asked</h3>
        <p className="wp-quote">“{w.detail || w.label}”</p>
        {musts.length ? (
          <>
            <h4 className="wp-k" style={{ marginTop: 14 }}>Must-haves <span className="wp-r" data-testid={`${testid}-must-count`}>{counts.met} met · {counts.partial} partly</span></h4>
            <div className="wp-meter" role="img" aria-label={`${counts.met} of ${musts.length} must-haves met, ${counts.partial} partly`}>
              {musts.map((m) => <i key={m.n} data-v={m.verdict} />)}
            </div>
            <ul className="wp-must" data-testid={`${testid}-musts`}>
              {musts.map((m) => (
                <li key={m.n} title={m.evidence ?? undefined}><span className="wp-must-ic" data-v={m.verdict} aria-label={m.verdict.replace("_", " ")}>{MUST_MARK[m.verdict]}</span>{m.text}</li>
              ))}
            </ul>
          </>
        ) : null}
        {prd?.markdown ? (
          <>
            <button type="button" className="wp-more" aria-expanded={prdOpen} onClick={() => setPrdOpen((v) => !v)} data-testid={`${testid}-prd`}>
              Read the PRD · {(prd.markdown.length / 1000).toFixed(1)}k chars <ArrowRight size={12} aria-hidden /></button>
            {prdOpen ? <div style={{ marginTop: 8 }}><PrdRecordView prd={prd} open link={(h) => pxHref(h)} testid={`${testid}-prd-doc`} /></div> : null}
          </>
        ) : null}
      </div>
      <div data-testid={`${testid}-discussed`}>
        <h3 className="wp-k"><span className="wp-n">2</span>Discussed</h3>
        {said ? (
          <>
            {seats.length || said.seats ? (
              <div className="wp-row" style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                <span className="wp-stack">{seats.slice(0, 6).map((a, i) => <Face key={`${a.role_key}-${i}`} agent={a} size={24} />)}</span>
                <span className="wp-muted" style={{ fontSize: 11.5 }}>{seats.length || said.seats} agents{dissent ? ` · ${dissent} dissent` : ""}</span>
              </div>
            ) : null}
            {said.decisions.length ? <ul className="wp-tight">{said.decisions.slice(0, 3).map((d) => <li key={d}>{d}</li>)}</ul> : null}
            {said.openQuestions.length ? <div className="wp-leftq" data-testid={`${testid}-left`}><b>Left for you:</b> {said.openQuestions.join(" · ")}</div> : null}
            {said.href ? <a className="wp-more" href={pxHref(said.href)}>Open meeting <ArrowRight size={12} aria-hidden /></a> : null}
          </>
        ) : <p className="wp-p wp-dim">No panel for this work — it went straight to the build.</p>}
      </div>
      <div data-testid={`${testid}-built`}>
        <h3 className="wp-k"><span className="wp-n">3</span>Built</h3>
        {built ? (
          <>
            {built.summary ? <p className="wp-p" data-testid={`${testid}-summary`}>{built.summary}</p> : null}
            {built.groups.length ? (
              <div className="wp-files">{built.groups.map((g) => <span key={g.purpose} className="wp-fchip" title={g.files.join("\n")}>{g.purpose} <b>{g.count}</b></span>)}</div>
            ) : null}
            <ul className="wp-tight" style={{ marginTop: 10 }}>
              {tests && (tests.passed !== null || testFiles.length) ? (
                <li><span><b>Tests:</b> {[testFiles.map((f) => f.split("/").pop()?.replace(/\.(test|spec)\.[a-z]+$/, "")).join(", "),
                  tests.passed === true ? (tests.count ? `${tests.count}/${tests.count} pass` : "pass") : tests.passed === false ? "failing" : ""].filter(Boolean).join(" · ")}</span></li>
              ) : null}
              {built.review.rounds ? <li><span><b>Review:</b> {built.review.rounds} round{built.review.rounds === 1 ? "" : "s"}{built.review.raised ? `, ${built.review.raised} finding${built.review.raised === 1 ? "" : "s"}` : ""}</span></li> : null}
              {built.followUps.length ? <li><span><b>Follow-ups:</b> {built.followUps.length}{mergeWaitOf(w)?.fixing ? " → BOSS is on them" : ""}</span></li> : null}
            </ul>
            {files.length ? (
              <details>
                <summary className="wp-more">{built.filesChanged || files.length} files changed ▾</summary>
                <div className="wp-files">{files.map((f) => <span key={f} className="wp-fchip">{f}</span>)}</div>
              </details>
            ) : null}
          </>
        ) : <p className="wp-p wp-dim">Nothing built yet.</p>}
      </div>
    </section>
  );
}

// ── 3' what a workflow run found or produced (work that changes no app) ──────────────────

const ENTRY_MARK: Record<string, { s: string; mark: string; word: string }> = {
  approved: { s: "done", mark: "✓", word: "approved" }, done: { s: "done", mark: "✓", word: "done" },
  proposed: { s: "live", mark: "→", word: "proposed" }, running: { s: "live", mark: "↻", word: "running" },
  waiting: { s: "wait", mark: "!", word: "waiting on you" }, failed: { s: "failed", mark: "✕", word: "failed" },
  rejected: { s: "failed", mark: "✕", word: "declined" }, declined: { s: "failed", mark: "✕", word: "declined" },
};

function Entry({ e, testid }: { e: OutcomeEntry; testid: string }) {
  const m = ENTRY_MARK[e.state] ?? { s: "done", mark: "·", word: e.state };
  return (
    <li className="wp-oc-e" data-s={m.s} data-testid={testid}>
      <span className="wp-oc-mark" aria-label={m.word}>{m.mark}</span>
      <div style={{ minWidth: 0 }}>
        <div className="wp-oc-step">{e.step}{e.person ? <span className="wp-pill" data-tone={m.s === "failed" ? "red" : m.s === "wait" ? "amber" : "green"}>{m.word}</span> : null}</div>
        {e.person ? <div className="wp-oc-who">{[e.who ? `${e.who}` : "", e.at ? hhmm(e.at) : ""].filter(Boolean).join(" · ") || "a person decides"}</div>
          : <p className="wp-p">{e.text}</p>}
      </div>
    </li>
  );
}

/** ASKED · FOUND · REMEDIATION (an investigation) or ASKED · PRODUCED (a job), from the run's
 *  own step outputs. A section with nothing on record is left out, never drawn empty. */
export function Outcome({ w, oc, testid }: { w: WorkItem; oc: WorkOutcome; testid: string }) {
  const ask = oc.asked;
  const asked = ask.text || w.title || w.label;
  const f = oc.found;
  let n = 0;
  const num = () => <span className="wp-n">{++n}</span>;
  return (
    <section className="wp-card wp-oc" aria-label={oc.shape === "investigation" ? "What was asked, found and done" : "What was asked and produced"} data-testid={`${testid}-outcome`} data-shape={oc.shape}>
      <div data-testid={`${testid}-asked`}>
        <h3 className="wp-k">{num()}Asked</h3>
        <p className="wp-quote">“{asked}”</p>
        {ask.trigger ? (
          <div className="wp-oc-trig" data-testid={`${testid}-trigger`}>
            <span className="wp-oc-step">{ask.trigger_step || "What triggered it"}</span>
            <p className="wp-p">{ask.trigger}</p>
          </div>
        ) : null}
        {ask.facts.length ? <div className="wp-files">{ask.facts.map((x) => <span key={x.label} className="wp-fchip">{x.label} <b>{x.value}</b></span>)}</div> : null}
      </div>
      {f ? (
        <div data-testid={`${testid}-found`}>
          <h3 className="wp-k">{num()}Found
            <span className="wp-r">{[f.step, f.confidence !== null ? `confidence ${f.confidence.toFixed(2)}` : ""].filter(Boolean).join(" · ")}</span></h3>
          <p className="wp-oc-head" data-testid={`${testid}-cause`}>{f.text}</p>
          {f.rationale ? <p className="wp-p wp-muted" style={{ marginTop: 8 }}>{f.rationale}</p> : null}
          {f.refs.length || f.cited ? (
            <div className="wp-files" data-testid={`${testid}-refs`}>
              {f.refs.map((r) => <span key={r} className="wp-fchip">{r}</span>)}
              {f.cited ? <span className="wp-fchip">facts cited <b>{f.cited}</b></span> : null}
            </div>
          ) : null}
          {oc.evidence.length ? (
            <details className="wp-oc-more" data-testid={`${testid}-evidence`}>
              <summary className="wp-more">Evidence · {oc.evidence.length} step{oc.evidence.length === 1 ? "" : "s"} ▾</summary>
              <ul className="wp-tight" style={{ marginTop: 8 }}>{oc.evidence.map((e) => <li key={e.step}><span><b>{e.step}:</b> {e.text}</span></li>)}</ul>
            </details>
          ) : null}
          {f.others.length ? (
            <details className="wp-oc-more">
              <summary className="wp-more">Also considered · {f.others.length} ▾</summary>
              <ul className="wp-tight" style={{ marginTop: 8 }}>{f.others.map((o) => <li key={o.text}><span>{o.text}{o.confidence !== null ? ` (${o.confidence.toFixed(2)})` : ""}</span></li>)}</ul>
            </details>
          ) : null}
        </div>
      ) : null}
      {oc.produced.length || oc.counts.length ? (
        <div data-testid={`${testid}-produced`}>
          <h3 className="wp-k">{num()}Produced</h3>
          {oc.counts.length ? (
            <div className="wp-oc-counts" data-testid={`${testid}-counts`}>
              {oc.counts.map((c) => <div key={c.label} className="wp-kpi">{c.label}<b>{c.value.toLocaleString()}</b></div>)}
            </div>
          ) : null}
          <ul className="wp-tight" style={{ marginTop: oc.counts.length ? 12 : 0 }}>
            {oc.produced.map((p) => <li key={p.step}><span><b>{p.step}:</b> {p.text}</span></li>)}
          </ul>
        </div>
      ) : null}
      {oc.remediation.length ? (
        <div data-testid={`${testid}-remediation`}>
          <h3 className="wp-k">{num()}{oc.shape === "investigation" ? "Remediation" : "Decisions"}</h3>
          <ol className="wp-oc-list">{oc.remediation.map((e, i) => <Entry key={`${e.step}-${i}`} e={e} testid={`${testid}-rem`} />)}</ol>
        </div>
      ) : null}
    </section>
  );
}

/** Where its outputs are: the links its steps recorded and the run itself. */
function OutputsCard({ oc, w, testid }: { oc: WorkOutcome; w: WorkItem; testid: string }) {
  const run = oc.run_href || w.links.workflow_run;
  if (!oc.links.length && !run) return null;
  return (
    <section className="wp-card wp-box" aria-label="Outputs" data-testid={`${testid}-outputs`}>
      <h2 className="wp-box-h"><LinkSimple size={16} aria-hidden />Outputs</h2>
      <ul className="wp-oc-links">
        {oc.links.map((l) => <li key={l.href}><a href={pxHref(l.href)} data-testid={`${testid}-out-link`}>{l.label} <ArrowRight size={12} aria-hidden /></a></li>)}
        {run ? <li><a href={pxHref(run)} data-testid={`${testid}-out-run`}>The run, step by step, with every output <ArrowRight size={12} aria-hidden /></a></li> : null}
      </ul>
    </section>
  );
}

// ── review comments ────────────────────────────────────────────────────────────────────────

function Comments({ ctx, w, story, testid }: { ctx: PxCtx; w: WorkItem; story: WorkStory | null; testid: string }) {
  const rc = story?.review_comments;
  const items = rc?.items ?? [];
  const [off, setOff] = useState<Record<string, boolean>>({});
  const [said, setSaid] = useState("");
  const [busy, setBusy] = useState(false);
  if (!items.length) return null;
  const fix = mergeWaitOf(w)?.fixing ?? null;
  const target = mergeWaitOf(w)?.build_id || story?.explained?.built?.buildId || "";
  const picked = items.filter((c) => !off[c.key]);
  const row = (c: (typeof items)[number]) => (
    <li key={c.key} className="wp-cmt" data-testid={`${testid}-comment`}>
      {fix ? null : <input type="checkbox" checked={!off[c.key]} onChange={() => setOff((o) => ({ ...o, [c.key]: !o[c.key] }))} aria-label={`Include: ${c.text}`} />}
      <span className="wp-sev" data-s={c.severity}>{c.severity}</span>
      <div className="wp-cmt-body">{c.text}
        <div className="wp-cmt-src">{[c.severity === "gate" ? `gate ${c.source}` : c.source, c.path ? `${c.path}${c.line ? `:${c.line}` : ""}` : "",
          c.merged > 1 ? `${c.merged} findings merged` : ""].filter(Boolean).join(" · ")}</div>
      </div>
    </li>
  );
  const letFix = async () => {
    if (!target || !picked.length) return;
    setBusy(true);
    const r = await pxWrite(`/v1/code-tasks/${encodeURIComponent(target)}/follow-up`, {
      title: `Review comments — ${w.label}`, actor: ctx.actor,
      instructions: `Fix these review comments on the change, most severe first:\n${picked.map((c) => `- [${c.severity}] ${c.path ? `${c.path}${c.line ? `:${c.line}` : ""} — ` : ""}${c.text}`).join("\n")}`,
    }, "Let BOSS fix these");
    setBusy(false);
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "BOSS started a follow-up for these.") : (r.error ?? "It didn't start."));
    if (r.ok && !r.demo) ctx.refreshWork();
  };
  return (
    <section className="wp-card wp-box" aria-label="Review comments" data-testid={`${testid}-comments`}>
      <h2 className="wp-box-h"><ChatsCircle size={16} aria-hidden />Review comments
        <span className="wp-pill">{items.length}{rc && rc.raw > items.length ? ` · deduped from ${rc.raw}` : ""}</span></h2>
      <ul className="wp-cmts">{items.slice(0, 3).map(row)}</ul>
      {items.length > 3 ? (
        <details>
          <summary className="wp-more">{items.length - 3} more ▾</summary>
          <ul className="wp-cmts">{items.slice(3).map(row)}</ul>
        </details>
      ) : null}
      <div className="wp-box-foot">
        {fix ? (
          <>
            <span className="wp-muted" style={{ fontSize: 12, display: "inline-flex", gap: 6, alignItems: "center" }}><span className="wp-dot" data-s="live" aria-hidden />{fix.label} is already fixing these</span>
            {w.links.log ? <a className="wp-btn" data-kind="mint" data-size="s" href={pxHref(w.links.log)} data-testid={`${testid}-watch-fix`}>Watch the fix</a> : null}
          </>
        ) : (
          <>
            <span className="wp-muted" style={{ fontSize: 12 }}>{picked.length} of {items.length} picked<Said text={said} /></span>
            <GatedButton permission="code_task:steer" projectId={ctx.projectId} className="wp-btn" data-kind="mint" data-size="s" disabled={busy || !picked.length || !target}
                         data-testid={`${testid}-let-fix`} onClick={() => void letFix()}>{busy ? "Starting…" : "Let BOSS fix these"}</GatedButton>
          </>
        )}
      </div>
    </section>
  );
}

// ── 4 rail ─────────────────────────────────────────────────────────────────────────────────

function PreviewCard({ ctx, w, testid }: { ctx: PxCtx; w: WorkItem; testid: string }) {
  const app = w.preview_app;
  const mp = mergeWaitOf(w);
  const ref = mp ? changeRef(mp.external_url, mp.number, true) : "the change";
  return (
    <section className="wp-card wp-box" aria-label="Preview" data-testid={`${testid}-preview-card`}>
      <h2 className="wp-box-h"><Monitor size={16} aria-hidden />Preview of {ref}</h2>
      <div className="wp-thumb">
        <svg viewBox="0 0 320 200" aria-hidden focusable="false">
          <rect width="320" height="200" fill="var(--v2-surface)" />
          <rect x="16" y="44" width="180" height="12" rx="3" fill="var(--v2-border-strong)" />
          <rect x="16" y="64" width="120" height="8" rx="3" fill="var(--v2-border)" />
          <rect x="16" y="90" width="288" height="94" rx="6" fill="var(--v2-panel)" />
          <path d="M16 160 C80 130 120 175 190 145 S280 120 304 135 V184 H16z" fill="var(--v2-edge)" />
          <g fill="var(--v2-mint)"><circle cx="90" cy="125" r="4" /><circle cx="170" cy="150" r="4" /><circle cx="240" cy="115" r="4" /></g>
        </svg>
        <span className="wp-thumb-tag wp-pill">{ref} · {app?.words || (app ? app.state : "no preview")}</span>
        <div className="wp-thumb-ov">
          {app ? <WorkPreviewButton app={app} projectId={ctx.projectId} actor={ctx.actor} testid={`${testid}-preview-rail`} className="wp-btn" /> : null}
          <span className="wp-dim" style={{ fontSize: 11 }}>{app ? "Check it yourself before you merge" : "No change to preview yet"}</span>
        </div>
      </div>
    </section>
  );
}

function WhereItRuns({ w, deployments, testid }: { w: WorkItem; deployments: Deployments | null; testid: string }) {
  const envs = deployments?.environments ?? [];
  const mp = mergeWaitOf(w);
  const stg = envs.find((e) => e.key === "staging");
  const prod = envs.find((e) => e.key === "production");
  const env = (key: string, label: string, e: (typeof envs)[number] | undefined) => {
    const runs = e?.last_deploy?.pr ? (mp && e.last_deploy.pr !== mp.number ? `runs change #${e.last_deploy.pr}` : "runs this change") : "";
    return (
      <div className="wp-env" key={key} data-testid={`${testid}-env-${key}`}>
        <span className="wp-dot" data-s={e ? (e.health === "healthy" ? "done" : e.health === "down" ? "failed" : "todo") : "wait"} aria-hidden />
        <div style={{ minWidth: 0 }}>
          <div className="wp-env-nm">{label}</div>
          <div className="wp-env-sub">{e ? [e.host || e.address, runs, e.last_deploy?.commit ? `(${e.last_deploy.commit.slice(0, 7)})` : ""].filter(Boolean).join(" · ") : "no host chosen"}</div>
        </div>
        <div className="wp-env-r">
          {e ? <>{key === "staging" && mp ? <span className="wp-pill">after merge</span> : null}
            <span className="wp-env-sub">{[e.health, e.p95_ms ? `p95 ${Math.round(e.p95_ms)} ms` : ""].filter(Boolean).join(" · ")}</span></>
            : key === "production" && deployments?.production.action ? <a href={pxHref(deployments.production.action.href)} style={{ fontSize: 12 }}>{deployments.production.action.label}</a> : null}
        </div>
      </div>
    );
  };
  return (
    <section className="wp-card wp-box" aria-label="Where this change runs" data-testid={`${testid}-where`}>
      <h2 className="wp-box-h"><Stack size={16} aria-hidden />Where this change runs</h2>
      {env("staging", "Staging", stg)}
      {env("production", "Production", prod)}
    </section>
  );
}

function Attempts({ ctx, w, testid }: { ctx: PxCtx; w: WorkItem; testid: string }) {
  if (w.attempts.length <= 1) return null;
  const live = w.attempts.some((t) => t.state === "running" || t.state === "stalled");
  return (
    <section className="wp-card wp-box" aria-label="Attempts" data-testid={`${testid}-attempts`}>
      <details>
        <summary className="wp-att-sum"><span className="wp-dot" data-s={live ? "live" : "done"} aria-hidden />
          <span><b style={{ fontWeight: 600 }}>{w.attempt_count} attempts</b> <span className="wp-muted">· {attemptsLine(w)}</span></span><span className="wp-chev" aria-hidden>▾</span></summary>
        <div style={{ marginTop: 8 }}>
          {w.attempts.map((t) => (
            <button key={t.id} type="button" className="wp-att" onClick={() => ctx.open("work", t.id)} data-testid={`${testid}-attempt-${t.id}`}>
              <span className="wp-dot" data-s={t.state === "running" ? "live" : t.state === "done" ? "done" : t.state === "failed" ? "failed" : t.state === "needs_you" || t.state === "stalled" ? "wait" : "todo"} aria-hidden />
              <span>{t.label}</span>
              <span className="wp-r">{ATTEMPT_WORDS[t.state]} · {hhmm(t.created_at)}{(t.cost_usd || t.list_usd) ? ` · ${money((t.cost_usd || t.list_usd) ?? 0)}` : ""}</span>
            </button>
          ))}
        </div>
      </details>
    </section>
  );
}

function CostCard({ w, testid }: { w: WorkItem; testid: string }) {
  const pts = costPoints(w);
  const paid = (w.cost_usd ?? 0) > 0;
  const total = paid ? w.cost_usd ?? 0 : w.list_usd ?? pts.at(-1)?.total ?? 0;
  if (!total && !pts.length) return null;
  const max = Math.max(total, ...pts.map((p) => p.total), 0.01);
  const xs = pts.length > 1 ? pts.map((_, i) => (i / (pts.length - 1)) * 140) : [0, 140];
  const ys = (pts.length > 1 ? pts : [{ total: 0 }, { total }]).map((p) => 40 - (p.total / max) * 36);
  const line = xs.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)} ${ys[i].toFixed(1)}`).join(" ");
  return (
    <section className="wp-card wp-box" aria-label="Cost" data-testid={`${testid}-cost`}>
      <h2 className="wp-box-h"><CurrencyDollar size={16} aria-hidden />Cost{!paid && w.list_usd ? <span className="wp-r wp-pill">included in plan</span> : null}</h2>
      <div className="wp-cost">
        <div>
          <div className="wp-cost-big">{money(total)}</div>
          <div className="wp-env-sub">{paid ? "spent" : "list price"}{w.attempt_count > 1 ? ` · ${w.attempt_count} attempts` : ""}</div>
        </div>
        <svg className="wp-spark" viewBox="-4 0 148 44" preserveAspectRatio="none" role="img"
             aria-label={`Cost over the attempts: ${pts.map((p) => `${p.label} ${money(p.usd)}`).join(", ") || money(total)}`}>
          <path className="wp-spark-area" d={`${line} L${xs.at(-1)} 44 L0 44 Z`} />
          <path d={line} />
          <circle cx={xs.at(-1)} cy={ys.at(-1)} r="3" />
        </svg>
      </div>
      {pts.length > 1 ? (
        <div className="wp-cost-legend">{pts.map((p) => <span key={p.label}>{p.label.toLowerCase()} {money(p.usd)}{p.state === "running" ? " · running" : ""}</span>)}</div>
      ) : null}
    </section>
  );
}

// ── 5 Ask Helm ─────────────────────────────────────────────────────────────────────────────

function HelmDock({ ctx, w, noProdHost, testid }: { ctx: PxCtx; w: WorkItem; noProdHost: boolean; testid: string }) {
  const [q, setQ] = useState("");
  const name = ctx.overview.captain?.name ?? "BOSS";
  const about = `(About: ${w.label} · work ${w.id})`;
  const ask = (text: string) => ctx.ask(`${text.trim()}\n\n${about}`);
  return (
    <form className="wp-helm" aria-label={`Ask ${name} about this work`} data-testid={`${testid}-helm`}
          onSubmit={(e) => { e.preventDefault(); ask(q.trim() || `Tell me about "${w.label}".`); }}>
      <div className="wp-helm-chips">
        {helmSuggestions(w, { noProdHost }).map((s) => (
          <button key={s} type="button" className="wp-helm-chip" onClick={() => ask(s)} data-testid={`${testid}-helm-chip`}>{s}</button>
        ))}
      </div>
      <div className="wp-helm-in">
        {ctx.overview.captain ? <Face agent={ctx.overview.captain} size={24} /> : <ChatCircleDots size={18} aria-hidden />}
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={`Ask ${name} about this work…`} aria-label={`Ask ${name} about ${w.label}`} />
        <button type="button" className="wp-iconbtn" title="Full screen" aria-label={`Open ${name} full screen about this work`} data-testid={`${testid}-helm-full`}
                onClick={() => writePxUrl({ drawer: "captain", cap_draft: `${q.trim() ? `${q.trim()} ` : ""}${about}`, cap_full: "1", wp: null })}><ArrowsOut size={15} /></button>
        <button type="submit" className="wp-iconbtn" data-kind="send" aria-label="Ask"><PaperPlaneRight size={15} /></button>
      </div>
    </form>
  );
}

/** The ⋯ menu: the run, the build page, what it learned (moved off the panel's face). */
export function WorkMenu({ ctx, w, testid }: { ctx: PxCtx; w: WorkItem; testid: string }) {
  const l = w.links;
  return (
    <details className="wp-menu" data-testid={`${testid}-menu`}>
      <summary className="px-icon-btn" aria-label="More links" title="More"><DotsThree size={16} weight="bold" /></summary>
      <div className="wp-menu-list">
        {l.workflow_run ? <a href={pxHref(l.workflow_run)} data-testid={`${testid}-link-run`}><FlowArrow size={13} aria-hidden /> Workflow run</a> : null}
        {l.build ? <a href={pxHref(l.build)} data-testid={`${testid}-link-build`}><Code size={13} aria-hidden /> Build in Code Tasks</a> : null}
        {l.pr ? <ChangeLink url={l.pr.external_url} number={l.pr.number} className="" testid={`${testid}-link-pr`} /> : null}
        {l.log ? <a href={pxHref(l.log)} data-testid={`${testid}-link-log`}><Terminal size={13} aria-hidden /> Live log</a> : null}
        <button type="button" onClick={() => ctx.open("learned", w.id)} data-testid={`${testid}-link-learned`}><Graph size={13} aria-hidden /> What it learned</button>
      </div>
    </details>
  );
}

// ── the panel ──────────────────────────────────────────────────────────────────────────────

export function useFlow(ctx: PxCtx, w: WorkItem, story: WorkStory | null, deployments: Deployments | null) {
  const next = workNextStep(w);
  const crew = crewOf(ctx, w);
  const nodes = useMemo(() => flowNodes({ w, story, next, deployments, crew, hhmm }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [w, story, deployments, ctx.overview.captain]);
  return { next, nodes };
}

/** The full page: every section. `extra` is what only some work carries (a failed build's
 *  answers, a linked ticket), drawn under the header. */
export function WorkPanel({ ctx, w, story, deployments, extra, testid = "px-wp" }: {
  ctx: PxCtx; w: WorkItem; story: WorkStory | null; deployments: Deployments | null; extra?: React.ReactNode; testid?: string;
}) {
  const { next, nodes } = useFlow(ctx, w, story, deployments);
  const code = isCodeWork(w);
  const noProdHost = code && !(deployments?.environments ?? []).some((e) => e.key === "production");
  if (!code) {
    // WORK THAT CHANGES NO APP (an investigation, a job): its workflow's own steps, then what it
    // found or produced. No preview, change, merge, deployments or prod host — it deploys nothing.
    const oc = w.outcome ?? null;
    return (
      <div className="wp wp-page" data-testid={testid} data-shape={w.shape}>
        <Header ctx={ctx} w={w} story={story} next={next} testid={testid} />
        {extra}
        <WhoDidWhat nodes={nodes} logHref={w.links.workflow_run} logLabel="The run, step by step" testid={testid} />
        <div className="wp-grid">
          <div className="wp-col">
            {oc ? <Outcome w={w} oc={oc} testid={testid} /> : null}
          </div>
          <aside className="wp-col" aria-label="Evidence">
            {oc ? <OutputsCard oc={oc} w={w} testid={testid} /> : null}
            <Attempts ctx={ctx} w={w} testid={testid} />
            <CostCard w={w} testid={testid} />
          </aside>
        </div>
        <HelmDock ctx={ctx} w={w} noProdHost={false} testid={testid} />
      </div>
    );
  }
  return (
    <div className="wp wp-page" data-testid={testid}>
      <Header ctx={ctx} w={w} story={story} next={next} testid={testid} />
      {extra}
      <WhoDidWhat nodes={nodes} logHref={w.links.log || w.links.build} testid={testid} />
      <div className="wp-grid">
        <div className="wp-col">
          <Story w={w} story={story} testid={testid} />
          <Comments ctx={ctx} w={w} story={story} testid={testid} />
        </div>
        <aside className="wp-col" aria-label="Evidence">
          <PreviewCard ctx={ctx} w={w} testid={testid} />
          <WhereItRuns w={w} deployments={deployments} testid={testid} />
          <Attempts ctx={ctx} w={w} testid={testid} />
          <CostCard w={w} testid={testid} />
        </aside>
      </div>
      <HelmDock ctx={ctx} w={w} noProdHost={noProdHost} testid={testid} />
    </div>
  );
}

/** The drawer's short preview of the page: header, flow (as a stepper) and the primary action,
 *  with "Open" to the whole page. */
export function WorkPanelShort({ ctx, w, story, deployments, extra, onOpen, testid = "px-wp" }: {
  ctx: PxCtx; w: WorkItem; story: WorkStory | null; deployments: Deployments | null; extra?: React.ReactNode; onOpen: () => void; testid?: string;
}) {
  const { next, nodes } = useFlow(ctx, w, story, deployments);
  return (
    <div className="wp wp-short" data-testid={`${testid}-short`}>
      <Header ctx={ctx} w={w} story={story} next={next} short testid={testid} />
      {extra}
      {isCodeWork(w) ? <WhoDidWhat nodes={nodes} logHref={w.links.log || w.links.build} vertical testid={testid} />
        : <WhoDidWhat nodes={nodes} logHref={w.links.workflow_run} logLabel="The run, step by step" vertical testid={testid} />}
      <button type="button" className="wp-btn" onClick={onOpen} data-testid={`${testid}-open`}><Hammer size={14} aria-hidden /> Open the whole page <ArrowRight size={12} aria-hidden /></button>
    </div>
  );
}

