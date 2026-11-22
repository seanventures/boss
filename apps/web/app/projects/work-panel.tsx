"use client";

/**
 * THE WORK PANEL v2 (approved wireframe 2026-11-22): one task, opened from its Work row as a
 * drawer (`drawer=work:<id>`; `&wp=full` lays it over the whole screen).
 *
 *   header      the task's title · the workflow's NAME · Workflow #N · Build · Change #N · Preview,
 *               one status chip, and Kicked off / Ready or Elapsed / Waiting on you / Cost / Attempt
 *   stages      Plan · Build · Ship · Live, grouping the workflow's own steps (lib work-stages):
 *               one status, one reason, at most one link to that stage's screen
 *   what next   one line, only the buttons that apply; amber only when it is the person's move
 *   iterations  earlier attempts, latest first, each with its times and links
 *   asked       what was asked and produced (collapsed)
 *
 * Step-by-step logs and agent briefs stay on the Workflow run screen; findings on the Change.
 */
import React, { useMemo } from "react";
import { ArrowsIn, ArrowsOut, ArrowSquareOut, Broadcast, CaretDown, GitMerge } from "@phosphor-icons/react";

import { pxHref, usePxRead, type WorkItem } from "../../lib/project-experience";
import { localWith } from "../../lib/local-time";
import { parseValidation, validationPath, type Validation } from "../../lib/validation-review";
import { changeRef } from "../../lib/repo-links";
import { finishedAt, iterationLinks, rootBuildOf, tidyIds, workChangeOf, workLinks, type WorkLink } from "../../lib/work-links";
import { checksHrefOf, nextLine, panelChip, panelStages, panelTimes, readyAt, type NextButton, type Stage } from "../../lib/work-stages";
import type { WorkTask } from "../../lib/work-tasks";
import { checkerWords } from "../workflows/run-work-line";
import { WorkPreviewButton } from "./work-preview";
import { useWorkStory } from "./drawers-more";
import type { PxCtx } from "./project-page";
import "./work-panel.css";

const DT: Intl.DateTimeFormatOptions = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false };
const dt = (iso: string | null | undefined) => (iso ? localWith(iso, DT) : "");
const hhmm = (iso: string) => localWith(iso, { hour: "2-digit", minute: "2-digit", hour12: false });
const ICON: Record<Stage["state"], (i: number) => string> = {
  done: () => "✓", you: () => "!", fail: () => "!", live: (i) => String(i + 1), todo: (i) => String(i + 1), absent: () => "–",
};

/** The change's validation record (the checker line: "gwlint 9 → 0 open"). */
function useValidation(projectId: string, w: WorkItem): Validation | null {
  const ch = workChangeOf(w);
  return usePxRead<Validation | null>(projectId && ch?.number ? validationPath(projectId, ch.number) : null, parseValidation, { everyMs: 60_000 }).data ?? null;
}

/** The header's links: Workflow #N · Build <id> · Change #N · Preview (only the ones that exist). */
function headerLinks(w: WorkItem): WorkLink[] {
  const build = rootBuildOf(w);
  return workLinks(w).map((l) => (l.key === "build" && build?.id ? { ...l, label: `Build ${build.id.replace(/^crun_/, "").slice(0, 6)}` } : l));
}

export function WorkPanel({ ctx, w, task, testid = "wp" }: { ctx: PxCtx; w: WorkItem; task: WorkTask | null; testid?: string }) {
  const validation = useValidation(ctx.demo ? "" : ctx.projectId, w);
  const story = useWorkStory(ctx, w.id).data;
  const checks = checkerWords(validation);
  const stages = useMemo(() => panelStages(w, { checks }), [w, checks.join("|")]); // eslint-disable-line react-hooks/exhaustive-deps
  const chip = panelChip(w, stages);
  const history = useMemo(() => iterationsOf(w, task), [w, task]);
  const times = panelTimes(w, attemptOf(w, task));
  const next = nextLine(w, stages, { hhmm, checksHref: checksHrefOf(w) });
  const links = headerLinks(w);
  const built = story?.explained?.built ?? null;
  const ch = workChangeOf(w);
  return (
    <div className="wp2" data-testid={testid}>
      <div className="wp2-top">
        <div className="wp2-ttl">
          <b className="wp2-title" data-testid={`${testid}-title`} title={w.title && w.title !== w.label ? w.title : undefined}>{w.label}</b>
          <div className="wp2-wf">
            {w.workflow ? <span className="wp2-name" data-testid={`${testid}-workflow`}>{w.workflow}</span> : <span className="wp2-name">{w.source_label || "Build"}</span>}
            {links.length ? <span aria-hidden>·</span> : null}
            {links.filter((l) => l.key !== "preview").map((l) => (
              <a key={l.key} href={pxHref(l.href)} title={l.title} data-testid={`${testid}-link-${l.key}`} {...(l.external ? { target: "_blank", rel: "noreferrer noopener" } : {})}>{l.label}</a>
            ))}
            {w.preview_app && w.preview_app.state !== "none" ? (
              <WorkPreviewButton app={w.preview_app} projectId={ctx.projectId} actor={ctx.actor} testid={`${testid}-preview`} className="wp2-linkbtn" />
            ) : w.links.preview ? (
              <a href={pxHref(w.links.preview)} target="_blank" rel="noreferrer noopener" data-testid={`${testid}-link-preview`}>Preview <ArrowSquareOut size={11} aria-hidden /></a>
            ) : null}
          </div>
        </div>
        <span className="wp2-chip" data-tone={chip.tone} data-testid={`${testid}-chip`}><span className="wp2-dot" aria-hidden />{chip.word}</span>
      </div>
      <div className="wp2-times" data-testid={`${testid}-times`}>
        {times.kicked ? <span>Kicked off <b>{dt(times.kicked)}</b></span> : null}
        {times.ready ? <span>Ready <b>{dt(times.ready)}</b></span> : null}
        {times.elapsed ? <span>Elapsed <b>{times.elapsed}</b></span> : null}
        {times.finished ? <span>Finished <b>{dt(times.finished)}</b></span> : null}
        {times.waiting ? <span>Waiting on you <b>{times.waiting}</b></span> : null}
        {times.cost ? <span title={times.costTitle}>Cost <b>{times.cost}</b></span> : null}
        {times.attempt > 1 ? <span>Attempt <b>{times.attempt}</b></span> : null}
      </div>

      <ol className="wp2-stages" aria-label="Stages" data-testid={`${testid}-stages`}>
        {stages.map((s, i) => (
          <li key={s.key} className="wp2-stg" data-s={s.state} data-testid={`${testid}-stage-${s.key}`}
              title={s.steps.length ? s.steps.map((x) => x.name).join(" · ") : undefined}>
            <div className="wp2-hd"><span className="wp2-ic" aria-hidden>{ICON[s.state](i)}</span>{s.name}</div>
            {s.steps.length ? (
              <div className="wp2-steps">{s.steps.map((x, j) => (
                <React.Fragment key={`${x.name}-${j}`}>{j ? " · " : ""}<span data-s={x.state} data-you={x.you || undefined}>{x.name}{x.state === "done" && s.state !== "done" ? " ✓" : ""}</span></React.Fragment>
              ))}</div>
            ) : null}
            <div className="wp2-why" data-testid={`${testid}-stage-${s.key}-why`}>{tidyIds(s.reason)}</div>
            {s.link ? (
              <a className="wp2-act" href={pxHref(s.link.href)} data-screen={s.link.screen} data-testid={`${testid}-stage-${s.key}-link`}>
                {s.state === "live" && w.state === "running" ? <><span className="wp2-dot" data-live aria-hidden /> Watch live · </> : null}{s.link.label} ›</a>
            ) : <span className="wp2-act" data-off>{s.state === "absent" ? "" : "Not started"}</span>}
          </li>
        ))}
      </ol>

      <div className="wp2-next" data-tone={next.tone} data-testid={`${testid}-next`}>
        <div className="wp2-say"><b data-testid={`${testid}-say`}>{next.say}</b>{next.sub ? <span>{tidyIds(next.sub)}</span> : null}</div>
        {next.buttons.map((b) => <NextBtn key={b.testid} b={b} testid={`${testid}-next-${b.testid}`} />)}
      </div>

      {history.length > 1 ? (
        <details className="wp2-more" data-testid={`${testid}-iterations`}>
          <summary>Iterations · {history.length} <span className="wp2-faint">latest first</span> <CaretDown size={11} aria-hidden /></summary>
          <div className="wp2-iter">
            {history.map((h) => (
              <div key={h.id} data-current={h.id === w.id || undefined} data-testid={`${testid}-iter-${h.id}`}>
                {/* The run's own name is its Workflow link (never said twice). */}
                {h.links.find((l) => l.key === "run") ? (
                  <a className="wp2-iter-name" href={pxHref(h.links.find((l) => l.key === "run")!.href)} data-testid={`${testid}-iter-${h.id}-run`}>{h.name}</a>
                ) : <b>{h.name}</b>}
                <span>{[dt(h.start), h.end ? hhmm(h.end) : ""].filter(Boolean).join(" → ")}</span>
                {h.links.filter((l) => l.key !== "run").map((l) => (
                  <a key={l.key} href={pxHref(l.href)} title={l.title} data-testid={`${testid}-iter-${h.id}-${l.key}`} {...(l.external ? { target: "_blank", rel: "noreferrer noopener" } : {})}>{l.label}</a>
                ))}
                <span className="wp2-faint">{h.word}</span>
              </div>
            ))}
          </div>
        </details>
      ) : null}

      <details className="wp2-more" data-testid={`${testid}-asked`}>
        <summary>What was asked and produced <CaretDown size={11} aria-hidden /></summary>
        <ul>
          <li><b>Asked:</b> {clip(w.detail && w.detail !== w.label ? w.detail : w.title || w.label, 220)}</li>
          <li><b>Produced:</b> {[ch?.number ? changeRef(ch.external_url, ch.number) : "", checks.join(" · "),
                                  built?.followUps.length ? `${built.followUps.length} follow-up${built.followUps.length === 1 ? "" : "s"} for later` : ""].filter(Boolean).join(" · ") || "Nothing yet"}</li>
          {w.state === "needs_you" ? <li><b>Left for you:</b> {w.action_label || "a decision"}</li> : null}
        </ul>
      </details>
    </div>
  );
}

/** Which attempt this is: its place among the task's runs, else its builds' count. */
export function attemptOf(w: WorkItem, task: WorkTask | null): number {
  const runs = task ? [task.latest, ...task.earlier] : [w];
  if (runs.length > 1) { const i = runs.findIndex((r) => r.id === w.id); return i < 0 ? runs.length : runs.length - i; }
  return Math.max(1, w.attempt_count);
}

function clip(t: string, n: number): string {
  const x = (t || "").replace(/\s+/g, " ").trim();
  return x.length <= n ? x : `${x.slice(0, n).replace(/\s+\S*$/, "")}…`;
}

function NextBtn({ b, testid }: { b: NextButton; testid: string }) {
  return (
    <a className="px-btn" data-kind={b.kind === "plain" ? undefined : b.kind} href={pxHref(b.href)} data-testid={testid}
       {...(b.external ? { target: "_blank", rel: "noreferrer noopener" } : {})}>
      {b.testid === "watch" ? <Broadcast size={13} aria-hidden /> : b.testid === "move" && /merge/i.test(b.label) ? <GitMerge size={13} aria-hidden /> : null}{b.label}</a>
  );
}

type Iteration = { id: string; name: string; start: string; end: string; word: string; links: WorkLink[] };
const WORD: Record<string, string> = { needs_you: "waiting", running: "running", stalled: "stalled", done: "done", failed: "failed", planned: "queued", handed_on: "handed on" };

/** The task's attempts, latest first: its workflow runs when it ran more than once, else the
 *  builds inside its one run (a retry, a follow-up). */
export function iterationsOf(w: WorkItem, task: WorkTask | null): Iteration[] {
  const runs = task ? [task.latest, ...task.earlier] : [w];
  if (runs.length > 1) {
    return runs.map((r) => ({
      id: r.id, name: r.run_number ? `Workflow #${r.run_number}` : r.label, start: r.created_at, end: finishedAt(r) || (r.state === "needs_you" ? readyAt(r) : ""),
      word: /cancel/i.test(r.status ?? "") ? "cancelled" : r.links.merged_pr ? "merged" : WORD[r.state] ?? r.state,
      links: workLinks(r).filter((l) => l.key !== "preview"),
    }));
  }
  return w.attempts.map((t) => ({
    id: t.id, name: t.label, start: t.created_at, end: finishedAt(t), word: /cancel/i.test(t.status) ? "cancelled" : WORD[t.state] ?? t.state,
    links: iterationLinks(t),
  }));
}

/** The drawer's ⤢: the panel over the whole screen (`&wp=full`), and back. */
export function FullToggle({ full, onToggle, testid }: { full: boolean; onToggle: () => void; testid: string }) {
  return (
    <button type="button" className="px-icon-btn" onClick={onToggle} aria-pressed={full} data-testid={testid}
            aria-label={full ? "Back to the drawer" : "Open full screen"} title={full ? "Back to the drawer" : "Open full screen"}>
      {full ? <ArrowsIn size={15} /> : <ArrowsOut size={15} />}</button>
  );
}
