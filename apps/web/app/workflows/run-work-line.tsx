"use client";

/**
 * THE RUN SCREEN'S ONE HEADER LINE (design #287, screen 3): Change #N · Preview · Staging status ·
 * Prod status · "Your move: <one button>" when a person must act, plus — when the change has a
 * validation record — one checker line "gwlint 9 → 0 open · Change #N › Checks".
 *
 * The Work panel used to carry these; it is gone, and the run screen is where a Work row opens.
 * The facts are the work item this run is (GET …/work), the project's deployments and the
 * change's validation packet — the same reads and helpers the panel used (workNextStep,
 * workChangeOf, the deployments' environments). Nothing to say → nothing drawn.
 */
import React from "react";
import { ArrowSquareOut, GitMerge } from "@phosphor-icons/react";

import {
  PX_PATHS, parseDeployments, parseWork, pxHref, usePxRead,
  type Deployments, type Environment, type WorkItem, type WorkList,
} from "../../lib/project-experience";
import { workNextStep, type NextStep } from "../../lib/work-next-step";
import { workChangeOf, workForRun } from "../../lib/work-links";
import { changeHref, changeRef, linkTarget } from "../../lib/repo-links";
import { parseValidation, validationPath, type Validation } from "../../lib/validation-review";
import "./run-work-line.css";

const HEALTH: Record<string, { word: string; s: string }> = {
  healthy: { word: "healthy", s: "ok" }, degraded: { word: "degraded", s: "warn" }, down: { word: "down", s: "bad" }, unknown: { word: "unknown", s: "" },
};

/** "Staging healthy" / "Prod —": an environment in two words. */
export function envWords(e: Environment | undefined): { word: string; s: string } {
  if (!e) return { word: "—", s: "" };
  return HEALTH[e.health] ?? HEALTH.unknown;
}

/** The one button a person presses, or null when nothing waits on them: the merge (the change,
 *  or BOSS's merge decision), else BOSS's decision. Never the run itself (we are on it). */
export function yourMove(w: WorkItem, next: NextStep | null = workNextStep(w)): { label: string; href: string; external: boolean } | null {
  if (!next || next.secondary) return null;
  const l = w.links;
  const href = next.kind === "merge" ? (l.decision ? pxHref(next.href || l.decision) : next.href) : l.decision ? pxHref(l.decision) : "";
  if (!href) return null;
  return { label: next.label, href, external: /^https?:\/\//.test(href) };
}

/** The checker line from the change's validation packet: "gwlint 9 → 0 open" for each checker. */
export function checkerWords(v: Validation | null): string[] {
  const p = v?.packet;
  if (!p?.validators.length) return [];
  return p.validators.map((x) => `${x.id} ${x.before} → ${x.after_open} open`);
}

export function RunWorkLine({ projectId, runId }: { projectId: string; runId: string }) {
  const work = usePxRead<WorkList>(projectId ? PX_PATHS.work(projectId) : null, parseWork, { everyMs: 30_000 }).data;
  const w = work ? workForRun(work.items, runId) : null;
  const deps = usePxRead<Deployments>(w ? PX_PATHS.deployments(projectId) : null, parseDeployments, { everyMs: 30_000 }).data;
  const ch = w ? workChangeOf(w) : null;
  const val = usePxRead<Validation | null>(ch?.number ? validationPath(projectId, ch.number) : null, parseValidation, { everyMs: 60_000 }).data ?? null;
  if (!w) return null;
  return <RunWorkLineView w={w} deployments={deps} validation={val} />;
}

/** The line itself, from the facts (tested on its own). */
export function RunWorkLineView({ w, deployments, validation = null }: { w: WorkItem; deployments: Deployments | null; validation?: Validation | null }) {
  const ch = workChangeOf(w);
  const chHref = ch ? changeHref(ch.external_url) : "";
  const preview = w.links.preview || w.preview_app?.href || "";
  const envs = deployments?.environments ?? [];
  const stg = envWords(envs.find((e) => e.key === "staging"));
  const prod = envWords(envs.find((e) => e.key === "production"));
  const move = yourMove(w);
  const checks = checkerWords(validation);
  const checksHref = chHref && !/^https?:\/\//.test(chHref) ? `${chHref}${chHref.includes("?") ? "&" : "?"}tab=checks` : "";
  return (
    <div className="rwl" data-testid="run-work-line">
      <div className="rwl-line">
        {ch && chHref ? (
          <a className="rwl-link" href={chHref} {...linkTarget(ch.external_url)} data-testid="rwl-change">{changeRef(ch.external_url, ch.number)}</a>
        ) : null}
        {preview ? (
          <a className="rwl-link" href={pxHref(preview)} target="_blank" rel="noreferrer noopener" data-testid="rwl-preview">
            Preview <ArrowSquareOut size={11} aria-hidden /></a>
        ) : null}
        <span className="rwl-env" data-s={stg.s} data-testid="rwl-staging"><i aria-hidden />Staging {stg.word}</span>
        <span className="rwl-env" data-s={prod.s} data-testid="rwl-prod"><i aria-hidden />Prod {prod.word}</span>
        {move ? (
          <span className="rwl-move" data-testid="rwl-move">Your move:
            <a className="wf-btn wf-btn-primary rwl-btn" href={move.href} {...(move.external ? { target: "_blank", rel: "noreferrer noopener" } : {})}
               data-testid="rwl-move-btn">{/merge/i.test(move.label) ? <GitMerge size={13} aria-hidden /> : null}{move.label}</a>
          </span>
        ) : null}
      </div>
      {checks.length ? (
        <div className="rwl-checks" data-testid="rwl-checks">
          <span>{checks.join(" · ")}</span>
          {checksHref && ch ? <a className="rwl-link" href={checksHref} data-testid="rwl-checks-link">{changeRef(ch.external_url, ch.number)} › Checks</a> : null}
        </div>
      ) : null}
    </div>
  );
}
