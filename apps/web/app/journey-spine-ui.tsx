"use client";

// Imported explicitly: the test runner transforms this file with the classic JSX runtime,
// so React must be in scope. Without it the spine renders in the app and throws in tests --
// which is how a delivery test started failing on a change to a card in Factory.
import React from "react";
import {
  FileText, GitMerge, GitPullRequest, Heartbeat, type Icon, MagnifyingGlass,
  RocketLaunch, UploadSimple, Wrench,
} from "@phosphor-icons/react";

import { useSearchParams } from "next/navigation";

import { consoleHref } from "./console-href";

/**
 * The journey spine: one outcome's life, on its own card.
 *
 * Research → Spec → Build → Ship. Each node is filled when that phase happened, breathing when
 * it is happening now, hollow when it has not been reached — and a link when there is somewhere
 * to go. So a card answers "what is this, how far along, and where do I look next" without the
 * reader knowing that BOSS keeps discoveries, meetings and runs in three different places.
 *
 * Deliberately quiet. It sits between the title and the card's existing controls and must never
 * compete with them: an earlier attempt at this rendered a second, read-only list of cards ABOVE
 * the working ones, which buried every real control under a wall of dead ones. This adds a strip
 * INSIDE the card that already works, so drill-in, preview and relaunch come along by
 * construction rather than by being reimplemented.
 *
 * It reads only what a run already carries — `finalize_ctx.origin` for where the work began,
 * lineage for its siblings, phase and status for where it is. Nothing is invented: a phase with
 * no evidence renders hollow rather than guessed.
 */

type Run = {
  id: string;
  status?: string;
  phase?: string;
  pull_request_url?: string | null;
  finalize_ctx?: {
    origin?: { kind?: string; task_id?: string; meeting_run_id?: string; title?: string } | null;
    root_run_id?: string;
    is_followup?: boolean;
  } | null;
};

type NodeState = "done" | "now" | "todo" | "blocked";

/** Phases that mean the agent is still writing code. */
const BUILD_PHASES = new Set([
  "queued", "design", "review_plan", "write_tests", "implement", "run_tests",
  "review_code", "review_panel", "check_tests", "consistency_check", "coding",
]);

export type SpineNode = {
  key: string; label: string; sub?: string; state: NodeState; href?: string; title: string;
};

/**
 * The spine primitive: a row of phases, each done / happening now / not reached / stopped.
 *
 * Kept free of anything about code runs so delivery can use the SAME component rather than a
 * lookalike. Two components that are meant to look identical drift the moment one is touched;
 * the earlier version of this page had three card styles for that exact reason.
 */
export function Spine({ nodes, large = false, label }: {
  nodes: SpineNode[]; large?: boolean; label?: string;
}) {
  return (
    <div className={`sp${large ? " lg" : ""}`} aria-label={label ?? "Progress"}>
      {nodes.map((n, i) => (
        <NodeAndLink key={n.key} node={n} next={nodes[i + 1]} last={i === nodes.length - 1} />
      ))}
    </div>
  );
}

export function JourneySpine({ run, previewUrl, large = false }: { run: Run; previewUrl?: string | null; large?: boolean }) {
  // The request's query string, so the FIRST render already carries the org.
  const search = useSearchParams()?.toString();
  const fctx = run.finalize_ctx ?? {};
  const origin = fctx.origin ?? null;
  const status = run.status ?? "";
  const failed = status === "failed" || status === "blocked" || status === "cancelled";
  const shipped = Boolean(run.pull_request_url);
  const running = status === "running" || status === "queued";

  // Research and Spec are only claimed when the run RECORDS having come from them. A run
  // started directly has neither, and says so by leaving them hollow — better than implying a
  // panel that never met.
  const fromMeeting = origin?.kind === "meeting";
  const fromDiscovery = origin?.kind === "discovery";
  // A meeting's own id, so Spec lands on THAT panel rather than the meetings list. Falls back
  // to the area when only the origin kind is known.
  // ?meeting= is what the meetings area reads. Passing the task id sent every Spec click to
  // the meetings HOME page, which looks like a slow link rather than a wrong one.
  const meetingHref = origin?.meeting_run_id
    ? consoleHref({ area: "meetings", meeting: origin.meeting_run_id }, search)
    : consoleHref({ area: "meetings" }, search);

  const nodes: SpineNode[] = [
    {
      key: "research",
      label: "Research",
      sub: fromDiscovery ? "open dossier ↗" : "none",
      state: fromDiscovery ? "done" : "todo",
      href: fromDiscovery ? consoleHref({ area: "discovery" }, search) : undefined,
      title: fromDiscovery
        ? "Started from a research dossier — open Discovery"
        : "No research phase: this work did not start from a dossier",
    },
    {
      key: "spec",
      label: "Spec",
      sub: fromMeeting ? "open PRD ↗" : "none",
      state: fromMeeting ? "done" : "todo",
      href: fromMeeting ? meetingHref : undefined,
      title: fromMeeting
        ? "Specced by an agent panel — open the meeting and its PRD"
        : "No panel: this task was written directly",
    },
    {
      key: "build",
      label: "Build",
      sub: failed ? (status || "stopped") : running ? (run.phase || "working") : "done",
      // Only a run that genuinely stopped is red. Every Build node was rendering red because
      // most of the history is failed runs, so the strip read as "everything is broken" and the
      // colour stopped meaning anything.
      state: failed ? "blocked" : running ? "now" : "done",
      title: failed
        ? `Stopped during build (${status})`
        : running
          ? `Building now — ${run.phase ?? "in progress"}`
          : "Build finished",
    },
    {
      key: "ship",
      label: "Ship",
      sub: previewUrl ? "open app ↗" : shipped ? "open PR ↗" : "not yet",
      state: shipped || previewUrl ? "done" : "todo",
      href: previewUrl ?? run.pull_request_url ?? undefined,
      title: previewUrl
        ? "Open the running app"
        : shipped
          ? "Open the pull request"
          : "Nothing shipped yet",
    },
  ];

  return <Spine nodes={nodes} large={large}
                label="Progress through research, spec, build and ship" />;
}

// Real icons from the set the nav already uses.
//
// These were hand-written SVG paths, justified as saving hydration on 240 components. The
// measurement did not support it (6.51MB -> 6.49MB, i.e. nothing) and the paths rendered as
// unreadable fragments at 13px -- an icon nobody can identify is worse than no icon. Phosphor
// draws them correctly at any size and is already a dependency.
const ICONS: Record<string, Icon> = {
  research: MagnifyingGlass,
  spec: FileText,
  build: Wrench,
  ship: RocketLaunch,
  // Delivery's phases, in the same vocabulary.
  change: GitPullRequest,
  merge: GitMerge,
  deploy: UploadSimple,
  live: Heartbeat,
};

function PhaseIcon({ phase }: { phase: string }) {
  const Glyph = ICONS[phase];
  return Glyph ? <Glyph size={15} weight="fill" aria-hidden /> : null;
}

function NodeAndLink({
  node, next, last,
}: {
  node: { key: string; label: string; sub?: string; state: NodeState; href?: string; title: string };
  next?: { state: NodeState };
  last: boolean;
}) {
  const inner = (
    <>
      {/* An icon, not a bare dot. Four identical dots differing only in colour made the four
          phases indistinguishable at a glance -- the reader had to parse the labels to know
          which stage they were looking at. */}
      <span className="sp-dot"><PhaseIcon phase={node.key} /></span>
      <span className="sp-label">{node.label}</span>
      {node.sub ? <span className="sp-sub">{node.sub}</span> : null}
    </>
  );

  const cls = `sp-node ${node.state}`;
  const isExternal = node.href?.startsWith("http");

  return (
    <>
      {node.href ? (
        <a
          className={cls}
          href={node.href}
          title={node.title}
          {...(isExternal ? { target: "_blank", rel: "noreferrer" } : {})}
          // The card behind this is clickable; a node click means the node, not the card.
          onClick={(e) => e.stopPropagation()}
        >
          {inner}
        </a>
      ) : (
        <span className={cls} title={node.title}>{inner}</span>
      )}
      {!last ? (
        // The connector is solid only where progress has actually reached.
        <span className={`sp-link${node.state === "done" && next?.state !== "todo" ? " done" : ""}`} />
      ) : null}
    </>
  );
}
