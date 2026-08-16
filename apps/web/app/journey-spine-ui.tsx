"use client";

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
    origin?: { kind?: string; task_id?: string; title?: string } | null;
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

export function JourneySpine({ run, previewUrl, large = false }: { run: Run; previewUrl?: string | null; large?: boolean }) {
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
  const meetingHref = origin?.task_id
    ? `?area=meetings&task=${encodeURIComponent(origin.task_id)}`
    : "?area=meetings";

  const nodes: {
    key: string; label: string; sub?: string; state: NodeState; href?: string; title: string;
  }[] = [
    {
      key: "research",
      label: "Research",
      sub: fromDiscovery ? "open dossier ↗" : "none",
      state: fromDiscovery ? "done" : "todo",
      href: fromDiscovery ? "?area=discovery" : undefined,
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

  return (
    <div className={`sp${large ? " lg" : ""}`} aria-label="Progress through research, spec, build and ship">
      {nodes.map((n, i) => (
        <NodeAndLink key={n.key} node={n} next={nodes[i + 1]} last={i === nodes.length - 1} />
      ))}
    </div>
  );
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
      <span className="sp-dot" />
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
