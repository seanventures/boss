"use client";

import { useSearchParams } from "next/navigation";

import { consoleHref } from "./console-href";
import { appIdentity } from "./app-identity";
import { groupRunsIntoApps } from "./app-grouping";

import { useActionState, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { CodeTaskRun, LlmCallRow, PreviewRec, QaBenchmarkApp, QaBenchmarkReport } from "@boss/api-client";
import { operatorPreviewOpenHref } from "../lib/code-tasks";
import { structuredCodeTaskTests } from "../lib/code-tasks";

import {
  listCodeTasksAction,
  relaunchCodeTaskAction,
  confirmCodeTaskFeedbackAction,
  createCodeTaskAction,
  getQaBenchmarkReportAction,
  startQaBenchmarkAppAction,
  getCodeTaskActivityAction,
  startPreviewAction,
  retryCodeTaskWithStateAction,
  submitCodeTaskFeedbackAction,
  overrideCodeTaskAction,
  followUpCodeTaskAction,
  conveneMeetingAction,
  deployRunNowAction,
  retryDeploymentAction,
  runDeploymentsAction,
  setupDeploymentAction,
} from "./actions";
import { LiveDeployment } from "./deploy-ui";
import { Markdown } from "./markdown";
import { ClientRelativeTime } from "./client-relative-time";
import { JourneySpine } from "./journey-spine-ui";
import {
  ArrowSquareOut, ArrowsClockwise, ChatCircleDots, DotsThree, PencilSimple,
  PlayCircle, RocketLaunch, Sparkle,
} from "@phosphor-icons/react";

import { SubmitButton } from "./submit-button";

function RetryControls({ runId, failingGates = [] }: { runId: string; failingGates?: string[] }) {
  const [state, action, pending] = useActionState(retryCodeTaskWithStateAction, null);
  // Which stages the retry will SKIP, mirroring _repair_needs_write_tests on the server, so the
  // operator knows this resumes rather than restarting. Kept in sync with _TEST_GATES.
  const TEST_GATES = ["tests_strong_enough", "tests_written_first", "coder_did_not_edit_tests",
                      "written_tests_pass", "existing_tests_intact"];
  const reusesTests = failingGates.length > 0 && !failingGates.some((g) => TEST_GATES.includes(g));
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
        <input type="hidden" name="run_id" value={runId} />
        <SubmitButton className="cap-btn" disabled={pending} title="Run this task again, resuming from where it failed">
          {pending ? "Starting retry…" : "↻ Retry"}
        </SubmitButton>
      </form>
      <span className="detail">
        Resumes on the same branch and reuses the approved plan
        {reusesTests ? " and the existing test suite" : ""} — it does not rebuild from scratch.
      </span>
      {state?.runId ? (
        <span className="cap-badge tone-blue">attempt {state.attempt ?? "?"} started · {state.runId}</span>
      ) : null}
      {state?.error ? (
        <span className="detail" style={{ color: "#f85149" }}>{state.error}</span>
      ) : null}
    </span>
  );
}

function OverrideControls({ runId }: { runId: string }) {
  const [state, action, pending] = useActionState(overrideCodeTaskAction, null);
  return (
    <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <input type="hidden" name="run_id" value={runId} />
      <input name="reason" placeholder="Reason for override (recorded)" className="cap-input"
             style={{ minWidth: 220 }} />
      <SubmitButton className="cap-btn" disabled={pending}>
        {pending ? "Overriding…" : "⚖ Waive failing gates & open PR"}
      </SubmitButton>
      {state?.url ? (
        <a className="cap-btn primary" href={state.url} target="_blank" rel="noreferrer">PR opened →</a>
      ) : null}
      {state?.error ? <span className="detail" style={{ color: "#f85149" }}>⚠ {state.error}</span> : null}
    </form>
  );
}

const PREVIEW_API = process.env.NEXT_PUBLIC_BOSS_API_URL ?? "";

function PreviewLogs({ previewId }: { previewId: string }) {
  const [open, setOpen] = useState(false);
  const [log, setLog] = useState("");
  useEffect(() => {
    if (!open) return;
    const source = new EventSource(
      `${PREVIEW_API}/v1/previews/${encodeURIComponent(previewId)}/logs`,
    );
    source.addEventListener("log", (event) => {
      try { setLog((current) => (current + JSON.parse((event as MessageEvent).data)).slice(-12000)); }
      catch { setLog((current) => (current + (event as MessageEvent).data).slice(-12000)); }
    });
    source.addEventListener("end", () => source.close());
    source.onerror = () => source.close();
    return () => source.close();
  }, [open, previewId]);
  return (
    <span>
      <button type="button" className="cap-btn" onClick={() => setOpen((value) => !value)}>
        {open ? "Hide startup logs" : "Startup logs"}
      </button>
      {open ? (
        <pre style={{ width: "min(720px, 86vw)", maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap", marginTop: 8 }}>
          {log || "Waiting for preview output…"}
        </pre>
      ) : null}
    </span>
  );
}

function PreviewControls({ runId, livePreviews = [] }: { runId: string; livePreviews?: PreviewRec[] }) {
  const [state, action, pending] = useActionState(startPreviewAction, null);
  const hasLive = livePreviews.some((p) => p.status === "running" && operatorPreviewOpenHref(p));
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      {/* Only offer "Run preview" (a heavy npm-install+build) when NO live preview exists yet.
          Once one is running, the primary action is Open preview; a fresh build is demoted to a
          subtle "Rebuild" link so we don't spawn duplicate builds that starve the api. */}
      {!hasLive ? (
        <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
          <input type="hidden" name="run_id" value={runId} />
          <SubmitButton className="cap-btn" disabled={pending}>{pending ? "Starting preview…" : "▶ Run preview"}</SubmitButton>
        </form>
      ) : null}
      {/* Persistent link(s) from the durable preview record — survive the console's auto-refresh */}
      {livePreviews.map((p) => {
        const href = operatorPreviewOpenHref(p);
        return (
        <span key={p.id} style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          {href ? (
            <a className="cap-btn primary" href={href.startsWith("http") ? href : `${PREVIEW_API}${href}`} target="_blank" rel="noreferrer">
              {p.status === "verified" ? "View verified preview ↗" : "Open preview ↗"}
            </a>
          ) : (
            <span className="detail" data-preview-view="missing-url">
              {p.status === "verified"
                ? "verified-test — no operator URL (not running; View would 409)"
                : `preview ${p.status} — no operator URL`}
            </span>
          )}
          <span className="cap-badge tone-blue">
            preview: {p.status}
            {p.substrate === "preview_rootfs" ? " · retained rootfs" : p.substrate === "preview_docker" ? " · docker" : " · host process"}
          </span>
          {p.status === "running" ? <PreviewLogs previewId={p.id} /> : null}
        </span>
        );
      })}
      {/* Rebuild — only when a preview already exists; replaces (does not stack) the current one. */}
      {hasLive ? (
        <form action={action} style={{ display: "inline-flex" }}>
          <input type="hidden" name="run_id" value={runId} />
          <SubmitButton className="cap-btn" style={{ opacity: 0.7, fontSize: 12 }} disabled={pending}>
            {pending ? "Rebuilding…" : "↻ Rebuild preview"}
          </SubmitButton>
        </form>
      ) : null}
      {/* Freshly-started link (shows instantly, before the record is re-fetched) */}
      {state?.url && livePreviews.length === 0 ? (
        <a className="cap-btn primary" href={state.url} target="_blank" rel="noreferrer">Open preview ↗</a>
      ) : null}
      {/* Library/CLI build: no live URL — show the test-run result (passed) instead of an error. */}
      {state?.note ? (
        <details open className="detail" style={{ color: "#3fb950", width: "100%" }}>
          <summary style={{ cursor: "pointer" }}>✓ Ran tests — passed (library build, no live app)</summary>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, marginTop: 6, maxHeight: 320, overflow: "auto" }}>{state.note}</pre>
        </details>
      ) : null}
      {state?.error ? (
        <details open className="detail" style={{ color: "#f85149", width: "100%" }}>
          <summary style={{ cursor: "pointer" }}>⚠ Preview / tests failed</summary>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, marginTop: 6, maxHeight: 320, overflow: "auto" }}>{state.error}</pre>
        </details>
      ) : null}
    </span>
  );
}

function ConfirmFeedback({ threadId }: { threadId: string }) {
  const [state, action, pending] = useActionState(confirmCodeTaskFeedbackAction, null);
  return (
    <form action={action} style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <input type="hidden" name="thread_id" value={threadId} />
      <SubmitButton className="cap-btn primary" disabled={pending}>
        {pending ? "Dispatching…" : "Confirm & dispatch governed iteration"}
      </SubmitButton>
      {state?.run_id ? <span className="detail" style={{ color: "#3fb950" }}>Queued {state.run_id}</span> : null}
      {state?.error ? <span className="detail" style={{ color: "#f85149" }}>{state.error}</span> : null}
    </form>
  );
}

// Feedback is conversational and confirmation-gated. The agent clarifies vague
// requests, structures the fix, classifies its scope, then reuses the same branch/PR.
function FeedbackControls({ runId, status, anchor = "" }: { runId: string; status: string; anchor?: string }) {
  const completed = status === "completed";
  const [state, action, pending] = useActionState(submitCodeTaskFeedbackAction, null);
  return (
    <div style={{ display: "grid", gap: 8, maxWidth: 680 }}>
      <div className="ct-block-title">
        {completed ? "Extend this completed task on its existing PR" : "Diagnose and retry with feedback"}
      </div>
      <form action={action} style={{ display: "grid", gap: 7 }}>
        <input type="hidden" name="run_id" value={runId} />
        <input type="hidden" name="anchor" value={anchor} />
        <textarea name="message" className="cap-input" rows={2} required
          placeholder={completed
            ? "Describe the behavior to add or change. The agent will clarify and propose a scoped extension."
            : "Describe what failed or should change. Prior findings are carried forward automatically."}
          style={{ minWidth: 320, resize: "vertical" }} />
        <input name="expected" className="cap-input" placeholder="Expected behavior (helps avoid a clarification round)" />
        <SubmitButton className="cap-btn" disabled={pending} style={{ justifySelf: "start" }}>
          {pending ? "Reviewing feedback…" : completed ? "Propose extension" : "Propose retry"}
        </SubmitButton>
      </form>
      {state?.question ? (
        <div className="detail" style={{ color: "#d29922" }}>Agent question: {state.question}</div>
      ) : null}
      {state?.proposal ? (
        <div style={{ display: "grid", gap: 8, padding: 10, border: "1px solid rgba(88,166,255,.35)", borderRadius: 8 }}>
          <span className="cap-badge tone-blue">{state.proposal.classification.replace("_", " ")}</span>
          <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{state.proposal.fix_note}</pre>
          {state.thread_id ? <ConfirmFeedback threadId={state.thread_id} /> : null}
        </div>
      ) : null}
      {state?.error ? <span className="detail" style={{ color: "#f85149" }}>{state.error}</span> : null}
    </div>
  );
}

// Governed pipeline phases (in order). Older/simple-path phases are aliased so the stepper
// still lights up. Each label says what the agent is actually doing in that phase.
const PHASES = [
  "queued", "provisioning", "design", "awaiting_plan_approval",
  "write_tests", "implement", "run_tests", "review_panel", "check_tests", "done",
];
const PHASE_LABEL: Record<string, string> = {
  queued: "Queued",
  provisioning: "Cloning repo",
  design: "Designing plan",
  awaiting_plan_approval: "Awaiting plan approval",
  write_tests: "QA writing tests",
  implement: "Engineer implementing",
  run_tests: "Running tests",
  review_panel: "Adversarial review",
  check_tests: "Mutation check",
  done: "Done",
  // simple-path aliases
  coding: "Coding",
  validating: "Testing",
  opening_pr: "Opening PR",
};

// Poll the server (router.refresh re-runs the server fetch) while any task is mid-flight,
// so the phase/activity updates live instead of needing a manual refresh.
function useLiveRefresh(active: boolean, intervalMs = 4000, reload?: () => void | Promise<void>) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => {
      // Both: the router refresh updates every other server-rendered panel, and `reload`
      // re-reads the one list this component owns. The refresh alone was demonstrably not
      // enough — the server re-rendered and the client kept the old rows.
      router.refresh();
      void reload?.();
    }, intervalMs);
    return () => clearInterval(t);
  }, [active, intervalMs, router, reload]);
}

function statusTone(status: string): string {
  if (status === "completed") return "green";
  if (status === "failed") return "pink";
  if (status === "blocked") return "amber";
  return "blue"; // running
}

function fmtUsd(micros?: number): string {
  if (!micros && micros !== 0) return "—";
  return `$${(micros / 1e6).toFixed(2)}`;
}

function Flow({ phase, status }: { phase: string; status: string }) {
  const activeIdx = PHASES.indexOf(phase);
  return (
    <div className="ct-flow">
      {PHASES.map((p, i) => {
        const reached = status === "completed" ? true : i <= activeIdx;
        const current = i === activeIdx && status === "running";
        return (
          <span key={p} className="ct-flow-step">
            <span className={`ct-flow-dot${reached ? " on" : ""}${current ? " pulse" : ""}`} />
            <span className="ct-flow-label">{PHASE_LABEL[p]}</span>
            {i < PHASES.length - 1 ? <span className="ct-flow-arrow">→</span> : null}
          </span>
        );
      })}
    </div>
  );
}

function NewCodeTaskForm({ projectId, defaultOwner, defaultRepo }: {
  projectId: string;
  defaultOwner?: string;
  defaultRepo?: string;
}) {
  return (
    <form action={createCodeTaskAction} className="ct-form">
      <input type="hidden" name="project_id" value={projectId} />
      <div className="ct-form-row">
        <label className="mr-field-label">
          Title
          <input className="mr-select" name="title" placeholder="Add a temperature conversion module" required />
        </label>
      </div>
      <div className="ct-form-row">
        <label className="mr-field-label">
          Instructions for the coding agent
          <textarea className="mr-select" name="instructions" rows={4}
            placeholder="Create temperature.py with c_to_f(c) and f_to_c(f). Add test_temperature.py with unittest covering both." required />
        </label>
      </div>
      <div className="ct-form-grid">
        <label className="mr-field-label">
          Acceptance criteria (one per line)
          <textarea className="mr-select" name="acceptance_criteria" rows={3}
            placeholder={"defines c_to_f and f_to_c\ntests cover both\nunittest passes"} />
        </label>
        <label className="mr-field-label">
          Test commands (one per line — must pass to open a PR)
          <textarea className="mr-select" name="test_commands" rows={3}
            placeholder={"python3 -m unittest discover -s . -p test_*.py"} />
        </label>
      </div>
      <div className="ct-form-grid">
        <label className="mr-field-label">
          Owner
          <input className="mr-select" name="owner" defaultValue={defaultOwner} placeholder="paramg" />
        </label>
        <label className="mr-field-label">
          Repo
          <input className="mr-select" name="repo" defaultValue={defaultRepo} placeholder="boss-sandbox" />
        </label>
        <label className="mr-field-label">
          Base branch
          <input className="mr-select" name="base_branch" defaultValue="main" placeholder="main" />
        </label>
      </div>
      <div className="ct-form-actions">
        <SubmitButton className="cap-btn primary" type="submit">Start coding task</SubmitButton>
        <span className="detail">The agent edits files in an isolated sandbox; BOSS re-runs the tests and opens a gated PR.</span>
      </div>
    </form>
  );
}

/**
 * What the work must do, and how that is proven — for a benchmark or an ordinary task.
 *
 * The QA benchmark cards showed acceptance scenarios and verification commands; the task list
 * showed a title and a status badge, and threw the rest away. That was not a data gap: every
 * governed task already carries `spec.acceptance_criteria` and `spec.test_commands`, which are
 * the same two facts under different names. Two renderings of one idea meant the newer screen
 * looked like a different product, and the older one hid the only content that answers "what
 * was this supposed to do, and how do we know it did?".
 *
 * Shared rather than copied, because the next field added to one of them should appear in both.
 * Renders nothing when there is nothing to show — an empty disclosure is a promise of detail
 * that is not there.
 */
/**
 * Launch — on a task card, not only on a benchmark.
 *
 * The benchmark catalogue could start work; the task list could not, so running something
 * again meant retyping its instructions and acceptance criteria into the New Code Task form,
 * copying data BOSS already had. Same governed pipeline either way; the only difference is
 * where the spec comes from.
 *
 * Disabled while submitting, and it says so. The benchmark buttons are plain form submits with
 * no feedback, and goldeneye currently holds four HookWatch runs and two BugBox runs queued —
 * a button that looks inert gets pressed again, and here every press costs budget.
 */
/** Launch a benchmark, once.
 *
 * The button was a bare form submit: pressing it navigated nowhere, changed nothing on screen,
 * and the new run only appeared after the page happened to refresh. So it got pressed again —
 * goldeneye is holding four HookWatch runs and two BugBox runs, queued and identical. Every
 * one of those is a budgeted governed pipeline. Disabling on submit and saying "Starting…" is
 * the whole fix, and it is the same fix the delivery buttons needed.
 */
function BenchmarkLaunch({ benchmark, projectId, actor, defaultOwner, defaultRepo,
                          onLaunched, onOpen, startedId, startedKind, failed }: {
  benchmark: QaBenchmarkApp; projectId: string; actor: string;
  defaultOwner?: string; defaultRepo?: string;
  startedId?: string | null; failed?: string | null;
  startedKind?: "meeting" | "code" | "existing" | null;
  onOpen?: (runId: string) => void;
  onLaunched?: (key: string, runId?: string, error?: string,
                kind?: "meeting" | "code" | "existing") => void | Promise<void>;
}) {
  // The request's query string, so the FIRST render already carries the org.
  const search = useSearchParams()?.toString();
  const [sent, setSent] = useState(false);
  // What the CATALOG already knows: this benchmark has been built in this project. Read at
  // render, so the card never invites a press whose only effect would be a second copy. The
  // guard behind the button covers an API caller; this covers the person looking at it.
  const alreadyBuilt = benchmark.built?.run_id ?? null;
  // Prefer the running app over its pipeline page. "View BugBox" that opens a run -- and, when
  // the lookup was reporting failures as builds, a FAILED run -- answers a question nobody
  // asked. The run stays reachable from the card; the app is what "view" means.
  //
  // Built from the preview ID through the gateway, NEVER from app_previews.url: that column
  // holds the sandbox-internal address (http://127.0.0.1:43053), which is unreachable from a
  // browser. Handing it out is where the agent's advice to "open http://127.0.0.1:46181" came
  // from -- it read the column and repeated it faithfully.
  const builtPreviewUrl = benchmark.built?.preview_id
    ? `${PREVIEW_API}/v1/previews/${encodeURIComponent(benchmark.built.preview_id)}/web/`
    : null;

  // `force` is the difference between "build this" and "build it AGAIN". It is only ever set
  // by the second, explicit control -- so a plain press can never spend a second build.
  const launch = async (force = false) => {
    setSent(true);
    const data = new FormData();
    data.set("project_id", projectId);
    data.set("actor", actor);
    data.set("benchmark_id", benchmark.id);
    data.set("owner", defaultOwner ?? "");
    data.set("repo", defaultRepo ?? "");
    if (force) data.set("force", "true");
    let out: {
      run_id?: string; kind?: "meeting" | "code" | "existing"; error?: string;
    } | undefined;
    try {
      out = await startQaBenchmarkAppAction(data);
    } finally {
      setSent(false);
      await onLaunched?.(benchmark.id, out?.run_id,
                         out?.run_id ? undefined : (out?.error ?? "the API returned no run"),
                         out?.kind);
    }
  };

  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
      {/* Once launched this becomes a way IN to the run, exactly like the task cards. The
          state lives in the PARENT: held here it was destroyed by the reload the launch
          triggers, so the button reverted and the operator saw nothing happen. */}
      {(startedId && startedKind === "existing") || (!startedId && alreadyBuilt) ? (
        // The work already exists. Offering it is the useful answer; building a second copy
        // is still possible and now has to be asked for by name.
        <>
          {builtPreviewUrl ? (
            <a className="cap-btn primary" href={builtPreviewUrl}
               target="_blank" rel="noreferrer"
               title="Open the running app">
              View {benchmark.name} →
            </a>
          ) : (
            <button className="cap-btn primary"
                    onClick={() => onOpen?.((startedId || alreadyBuilt) as string)}
                    title="No preview is running; open the completed build instead">
              View {benchmark.name} →
            </button>
          )}
          <button className="cap-btn" disabled={sent} onClick={() => void launch(true)}
                  title="Convene a new panel and build a second copy — this costs a full run">
            {sent ? "Starting…" : "Build it again"}
          </button>
        </>
      ) : startedId ? (
        startedKind === "meeting" ? (
          // A meeting run id means nothing to the code-task viewer. Send people to the panel
          // that is actually running, rather than to a viewer that renders nothing.
          <a className="cap-btn primary" href={consoleHref({ area: "meetings" }, search)}
             title="Open the panel this launched">
            View meeting →
          </a>
        ) : (
          <button className="cap-btn primary" onClick={() => onOpen?.(startedId)}
                  title="Open the run this launched">
            View run →
          </button>
        )
      ) : (
        <button className="cap-btn primary" disabled={sent} onClick={() => void launch(false)}>
          {sent ? "Starting…" : `Launch ${benchmark.name}`}
        </button>
      )}
      <span className="detail">
        {sent ? "starting…"
              : failed ? failed
              : (startedId && startedKind === "existing") || (!startedId && alreadyBuilt)
                ? (builtPreviewUrl
                    ? "already built — this opens the running app; building again runs the "
                      + "whole panel and pipeline"
                    : "already built — no preview is running, so this opens the completed "
                      + "build; start a preview from there to see the app")
              : startedId
                ? (startedKind === "meeting"
                    ? `panel convened ${startedId.slice(0, 13)}… — it drafts a PRD, approves it `
                      + `automatically, then builds`
                    : `started ${startedId.slice(0, 13)}… — it builds the app before there is `
                      + `anything to preview`)
                // The profile the endpoint actually asks for. It said "Strict" while the
                // endpoint had been changed to balanced — a caption describing a decision
                // made somewhere else, which is how it was wrong in the first place.
                : "Convenes the PRD panel first, then builds · balanced profile"}
      </span>
    </div>
  );
}

/**
 * Change an app that already exists.
 *
 * The card's other control is "Run this task again", which rebuilds the same app from the same
 * prompt. There was nothing for the far more common case -- the app is mostly right and needs
 * one thing changed. Doing that meant finding the app's git branch by hand, which is not a
 * thing an operator can do, so in practice people asked for a rebuild and got a different app.
 *
 * The branch is never named here. The run knows which app it built; that is the point.
 */

/**
 * The card's PRIMARY action, chosen from the app's actual state.
 *
 * Every card led with "Run this build again" -- the most expensive thing on the page (a fresh
 * panel and a full pipeline, minutes and real money) offered as the obvious button, on apps
 * that were already built and running. Nobody wants that on a working app; what they want is
 * to SEE it. Meanwhile the preview, the cheap and useful action, was either absent or buried
 * in the spine.
 *
 * So the button answers the state:
 *   preview live      -> Open preview   (a link, straight to the running app)
 *   preview starting  -> Starting...    (disabled; it is already happening)
 *   built, no preview -> Run preview    (the common case, one click, seconds)
 *   still building    -> Building...    (disabled; nothing to preview yet)
 *   nothing built     -> Run build      (there IS no app; this is the honest action)
 * Rebuilding stays available, demoted to a secondary control, where a deliberate act belongs.
 */
/**
 * The attempt "Run preview" should act on, given an app's attempts newest-first.
 *
 * THE RUN THE BUTTON ACTS ON MUST BE THE RUN THE BUTTON WAS ENABLED BY. The card enables the
 * action from `anyShipped` — does ANY attempt have something to show — but fired it at the
 * newest attempt. For an app whose latest run is a blocked follow-up, that means enabling on
 * the strength of the completed run and then previewing the blocked one, which the API
 * rightly rejects with 409 "has not opened a pull request yet".
 *
 * Falls back to the newest attempt so the caller always has a run; when nothing has shipped
 * the card does not render the action at all.
 */
export function pickBuiltRun<T extends { status?: string; pull_request_url?: string | null }>(
  membersNewestFirst: T[],
): T {
  return membersNewestFirst.find(
    (m) => m.status === "completed" || m.pull_request_url) ?? membersNewestFirst[0];
}


/**
 * The app's origin, taken from whichever run in the group actually recorded one.
 *
 * The spine narrates `builtRun` — the newest COMPLETED run — which for a shipped app is usually
 * the deferred-findings FOLLOW-UP. A follow-up is created fresh rather than as a retry, so it
 * carried no `origin`, and the card reported "Research: none / Spec: none" for an app that came
 * from a panel and has a PRD sitting behind it.
 *
 * Where an app came from is a property of the APP, not of whichever run happens to be newest —
 * the same lesson as app_id (I21). The engine now carries origin across the follow-up hop; this
 * repairs the runs created before it did.
 */
export function appOrigin<T extends { finalize_ctx?: Record<string, unknown> | null }>(
  members: T[],
): Record<string, unknown> | undefined {
  for (const m of members) {
    const o = (m.finalize_ctx ?? {})["origin"];
    if (o && typeof o === "object") return o as Record<string, unknown>;
  }
  return undefined;
}

/** The most recent preview attempt for a run, or null. `previews` arrives newest-first. */
export function latestPreviewFor(previews: PreviewRec[], runId: string): PreviewRec | null {
  return previews.find((p) => p.run_id === runId) ?? null;
}

function AppPrimaryAction({ run, previewUrl, previewStarting, shipped, running, lastPreview }: {
  run: CodeTaskRun;
  previewUrl: string | null;
  previewStarting: boolean;
  shipped: boolean;
  running: boolean;
  lastPreview?: PreviewRec | null;
}) {
  const [state, action] = useActionState(startPreviewAction, null);
  const url = state?.url || previewUrl;

  if (url) {
    return (
      <a className="cap-btn primary" href={url} target="_blank" rel="noreferrer"
         onClick={(e) => e.stopPropagation()}
         title="Open the running app">
        <ArrowSquareOut size={15} weight="bold" aria-hidden /> Open preview
      </a>
    );
  }
  if (previewStarting) {
    return <button className="cap-btn primary" disabled>Preview starting…</button>;
  }
  if (running) {
    return (
      <button className="cap-btn primary" disabled
              title="The build is still running — there is nothing to preview yet">
        Building…
      </button>
    );
  }
  if (!shipped) {
    // No PR and no completed run: there is no app here yet. Offering "Run preview" would be a
    // button that can only fail.
    return null;
  }
  // A PREVIEW THAT FAILED MUST NOT LOOK LIKE ONE THAT IS STARTING.
  //
  // There was no branch for `failed`, so five consecutive failures rendered as the pending
  // label "Starting preview…" and the operator reported the preview as stuck. The button is
  // still "Run preview" — retrying is the right next move — but the last attempt's reason has
  // to be visible, or you retry blind.
  const failed = !state?.url && lastPreview?.status === "failed" ? lastPreview : null;

  return (
    <form action={action} onClick={(e) => e.stopPropagation()}>
      <input type="hidden" name="run_id" value={run.id} />
      <SubmitButton className="cap-btn primary" pendingLabel="Starting preview…">
        <PlayCircle size={15} weight="fill" aria-hidden />
        {failed ? " Retry preview" : " Run preview"}
      </SubmitButton>
      {state?.error ? <span className="detail"> {state.error}</span> : null}
      {state?.note ? <span className="detail"> {state.note}</span> : null}
      {failed && !state?.error ? (
        <span className="detail" data-testid="preview-failed">
          {" "}Last preview failed{failed.failure_kind ? ` (${failed.failure_kind})` : ""}
          {failed.error ? `: ${failed.error.slice(0, 200)}` : ""}
        </span>
      ) : null}
    </form>
  );
}


/**
 * Rebuild this app by writing a NEW spec first.
 *
 * The card used to offer "rebuild from scratch", which re-runs the identical brief. If you are
 * unhappy with the app you got, that is the one thing that cannot help: the same brief is what
 * produced it. Re-running a fixed spec is only honest when the spec is deliberately held
 * constant -- comparing benchmark runs, or recovering from a platform failure -- so it lives on
 * each attempt inside the card, not here.
 *
 * This convenes the PRD panel instead, seeded with the app, its repo, and the thing you say is
 * wrong. The panel debates, writes a PRD, and the existing approval path starts the build --
 * the same route "Launch BugBox" takes, so nothing new is invented in the pipeline.
 */

/**
 * Point this app at a VM — the association that makes every later deploy automatic.
 *
 * The link is a row in `deployment_targets`: (project, app_name, environment) → host_id, with
 * `target_ref` DERIVED from the host so the address has one writable home. On merge,
 * `delivery_pipeline.targets_for()` resolves the app's targets and ships to each. That is the
 * whole of "future deployments happen automatically" — the association IS the automation.
 *
 * Offered on the card because the alternative was a wizard on another screen, which is the
 * trip this work exists to remove. Shown only when the app has no destination yet.
 */
/**
 * ONE host lookup for the whole page, not one per card.
 *
 * SetupDeployment renders inside every app card, and each copy fired its own identical
 * `listDeploymentHostsAction`. With a handful of apps that was invisible; past about six it is
 * not, because a browser opens roughly six connections per origin and this page is already
 * megabytes of HTML competing for them. The requests queue, the effects do not resolve, and
 * EVERY card sits at "Checking for a host…" — which reads as "BOSS cannot reach the VM" when
 * the host list actually returns in about three seconds.
 *
 * The result is shared and briefly cached: the host list is the same for every card on the
 * page, so asking N times was never anything but waste.
 */
/**
 * Did this tab outlive the build it was served by?
 *
 * Next.js derives a fresh id for every server action on every build, so a tab left open across
 * a deploy calls ids the server no longer knows and gets UnrecognizedActionError. The page
 * looks broken and the message blames the host list, which is the one thing that was fine.
 *
 * This deploys often, so that is a normal event, not an edge case — say what it is and offer
 * the one action that fixes it.
 */
export function isStaleClient(error: unknown): boolean {
  const text = String(error ?? "");
  return (
    text.includes("UnrecognizedActionError") ||
    text.includes("was not found on the server") ||
    text.includes("Failed to find Server Action")
  );
}

const HOSTS_TTL_MS = 30_000;
const hostsCache = new Map<string, { at: number; promise: Promise<unknown> }>();

function loadHostsOnce(projectId: string): Promise<any> {
  const hit = hostsCache.get(projectId);
  const now = Date.now();
  if (hit && now - hit.at < HOSTS_TTL_MS) return hit.promise as Promise<any>;
  // A fetch with no timeout can hang forever, and this one is the ONLY thing standing between
  // the operator and the Deploy button — so it always settles, even if the answer is an error.
  // A ROUTE HANDLER, NOT A SERVER ACTION.
  //
  // listDeploymentHostsAction answered in ~2s and still timed out at 20s, because a Server
  // Action re-renders the current route and returns a fresh RSC payload alongside its value —
  // and this page takes 20-27s to render. The lookup was never slow; the reply was.
  // /api/deployment-hosts returns JSON and nothing else.
  const promise = Promise.race([
    fetch(`/api/deployment-hosts?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" })
      .then(async (r) => (r.ok
        ? { ok: true, data: await r.json() }
        : { ok: false, error: `HTTP ${r.status}` })),
    new Promise((resolve) =>
      setTimeout(() => resolve({ ok: false, error: "host list timed out after 20s" }), 20_000)),
  ]).catch((e) => ({ ok: false, error: String(e) }));
  hostsCache.set(projectId, { at: now, promise });
  return promise as Promise<any>;
}

function SetupDeployment({ run, projectId, onDone }: {
  run: CodeTaskRun; projectId: string; onDone: () => void;
}) {
  const [hosts, setHosts] = useState<any[]>([]);
  // AN EMPTY LIST IS NOT AN ANSWER UNTIL SOMEONE HAS ASKED.
  //
  // `hosts` starts empty and `ready` was `hosts.length > 0`, so between first paint and the
  // effect resolving, this component stated "No verified host yet" — a definitive claim of
  // absence made before it had looked. There WAS a verified host the whole time. On a console
  // page this size, hydration is slow enough that the false claim is what the operator reads.
  //
  // And `if (r.ok)` had no else: a failed lookup left `hosts` empty forever, so an
  // infrastructure failure was reported as a missing host, silently.
  //
  // Three states, because there are three: haven't asked, asked and it broke, asked and there
  // are none.
  const [loadState, setLoadState] = useState<"loading" | "error" | "loaded">("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const app = appIdentity(run);

  useEffect(() => {
    let live = true;
    setLoadState("loading"); setLoadError(null);
    void loadHostsOnce(projectId).then((r) => {
      if (!live) return;
      if (!r.ok) {
        setLoadState("error");
        setLoadError(r.error ?? "could not reach the host list");
        return;
      }
      setHosts((((r.data as any)?.hosts ?? []) as any[])
        // Only VERIFIED hosts. Offering an unproven one produces a target that fails at
        // deploy time, far from the moment the choice was made.
        .filter((h) => h.status === "verified"));
      setLoadState("loaded");
    }).catch((e) => {
      if (!live) return;
      setLoadState("error"); setLoadError(String(e));
    });
    return () => { live = false; };
  }, [projectId]);

  const ready = loadState === "loaded" && hosts.length > 0;
  return (
    <div className="ct-deploy-setup" data-testid="app-setup-deployment">
      {loadState === "loading" ? (
        <span className="detail">Checking for a host&hellip;</span>
      ) : loadState === "error" ? (
        isStaleClient(loadError) ? (
          // Not a host problem at all: this tab was served by an older build. Blaming the host
          // list here sent the operator to check a VM that was never down.
          <span className="detail">
            This tab is running an older version of the console (BOSS was redeployed).{" "}
            <button type="button" className="linklike" onClick={() => window.location.reload()}>
              Reload to continue
            </button>
          </span>
        ) : (
          // Blaming a missing host for a failed lookup sends the operator to onboard a host
          // they already have. Say which of the two actually happened.
          <span className="detail">
            Could not read the host list ({loadError}). The app may already have a host —
            retry before onboarding a new one.
          </span>
        )
      ) : !ready ? (
        // Naming the missing thing beats an empty picker: "no hosts" reads as a broken form.
        <span className="detail">
          No verified host yet. Onboard one in CI/CD → Hosts, then this app can be pointed at it.
        </span>
      ) : (
        <form
          style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}
          onSubmit={async (e) => {
            e.preventDefault();
            e.stopPropagation();
            const fd = new FormData(e.currentTarget as HTMLFormElement);
            fd.set("run_id", run.id);
            setBusy(true); setMsg(null);
            const r = await setupDeploymentAction(null, fd);
            setBusy(false);
            setMsg(r.ok
              ? `target set: ${(r.data as any)?.host ?? "host"} — deploy it once below`
              : (r.error ?? "could not set the target"));
            if (r.ok) onDone();
          }}
        >
          {/* "Target", not "host". `deployment_targets` is what the model calls this, and a
              button that says "host" while the data says "target" gives one concept two
              names — the kind of drift that later makes a docs page and a screen disagree.
              A host is the MACHINE; the target is this app's placement on it. */}
          <span className="detail">App&rsquo;s target:</span>
          <select name="host_id" aria-label="Host to deploy this app to">
            {hosts.map((h) => (
              <option key={h.id} value={h.id}>{h.name} ({h.arch ?? "?"})</option>
            ))}
          </select>
          {/* Defaulted from the card, editable: the app's DISPLAY name and the name a
              container is created under are not always the same string, and the second one
              is the one a deploy has to live with. */}
          <input name="app_name" defaultValue={app.name}
                 aria-label="App name on the host"
                 style={{ width: 130, fontSize: 12 }} />
          <input name="app_port" placeholder="port (auto)"
                 aria-label="App port — blank lets BOSS pick a free one"
                 style={{ width: 92, fontSize: 12 }} />
          <button className="cap-btn" type="submit" disabled={busy}>
            {busy ? "Saving…" : "Set target"}
          </button>
        </form>
      )}
      {msg ? <span className="detail" style={{ marginLeft: 6 }}>{msg}</span> : null}
    </div>
  );
}

/**
 * What this app SHIPPED, on the card where it was approved.
 *
 * The point of the delivery work is that approving a preview deploys the app without anyone
 * visiting the CI/CD screen — which only holds if the result is visible here. Otherwise
 * "autonomous" means "happens somewhere you have to go and look", which is the same number of
 * screens with an extra step of faith.
 *
 * Renders nothing when the app has never deployed: a card for a task that was never meant to
 * ship should not grow a permanently empty deployment row.
 */
/**
 * Retire the target this app is bound to — the undo the console never had.
 *
 * DELETE /v1/deployment-targets/{id} existed all along and nothing called it, so a wrong
 * binding (an app on another app's port, say) could only be fixed in the database. The API
 * retires rather than deletes, because deployment runs and health samples reference the row.
 */
async function retireTarget(targetId: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await fetch(`/api/deployment-targets?target_id=${encodeURIComponent(targetId)}`,
                          { method: "DELETE", cache: "no-store" });
    const d = await r.json().catch(() => ({}));
    return r.ok ? { ok: true } : { ok: false, error: d?.error ?? `HTTP ${r.status}` };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

function DeploymentLine({ run, projectId, deployable }: {
  run: CodeTaskRun; projectId: string;
  /** Has this app produced reviewed code? A blocked or still-building task has nothing to
   *  ship, and offering "Deploy now" there promises something the pipeline cannot honour —
   *  it would clone, build, transfer, and only then fail at the health gate. */
  deployable: boolean;
}) {
  const [linked, setLinked] = useState<string | null>(null);
  const [targetId, setTargetId] = useState<string | null>(null);
  const [deploys, setDeploys] = useState<any[]>([]);
  const [retrying, setRetrying] = useState(false);
  // Set the moment a deploy is accepted and cleared only when a live run is
  // visible. Without it the button re-enabled as soon as the POST returned —
  // before any deployment_run existed to render — so the card still said "not
  // deployed" with an ACTIVE button beside it, and a second click would start a
  // second deployment of the same commit.
  const [starting, setStarting] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await runDeploymentsAction(run.id, appIdentity(run).name);
    if (r.ok) {
      const d = (r.data as any) ?? {};
      setDeploys((d.deployments ?? []) as any[]);
      setLinked(d.linked_host ?? null);
      setTargetId(d.linked_target_id ?? null);
    }
  }, [run]);

  useEffect(() => { void load(); }, [load]);

  // Poll only while something is actually moving. A card that polls forever is a card that
  // keeps a browser and an API busy describing a deployment that finished yesterday.
  const latest = deploys[0];
  const live = latest && ["created", "running", "in_progress", "deploying"].includes(latest.status);
  // The deployment row now reports its own state, so the optimistic latch can stand down.
  useEffect(() => { if (latest) setStarting(false); }, [latest]);
  useEffect(() => {
    if (!live) return;
    const t = window.setInterval(() => { void load(); }, 4000);
    return () => window.clearInterval(t);
  }, [live, load]);

  // No deployment yet. That is either "no destination" (offer to link one) or "linked but
  // never shipped" (offer the first deploy). Rendering nothing here is what left an already
  // merged app with no way forward on its own card.
  // Nothing to ship, or still shipping it: the row would be an empty promise on a card that
  // already says BLOCKED or BUILDING two inches above.
  if (!deployable && !latest) return null;

  if (!latest) {
    return (
      <div className="ct-deploy-line" data-testid="app-deployment-none">
        {linked ? (
          <>
            <span className="cap-badge tone-amber">not deployed</span>
            <span className="detail">{linked}</span>
            <button className="cap-btn primary ct-deploy-action"
                    disabled={retrying || starting}
                    data-testid="app-deploy-first"
                    onClick={async (e) => {
                      e.stopPropagation();
                      setRetrying(true); setNote(null);
                      const fd = new FormData();
                      fd.set("target_id", targetId ?? "");
                      fd.set("run_id", run.id);
                      const r = await deployRunNowAction(null, fd);
                      setNote(r.ok ? null : (r.error ?? "deploy failed"));
                      if (r.ok) setStarting(true);
                      setRetrying(false);
                      await load();
                    }}>
              <RocketLaunch size={15} weight="fill" aria-hidden />{" "}
              {retrying || starting ? "Deploying…" : "Deploy now"}
            </button>
            {/* THE UNDO THAT WAS NEVER THERE.
                Once an app was bound to a host and port the card only ever offered "Deploy
                now" — a wrong binding (this app sat on another app's port) could be fixed
                nowhere but the database. Retiring keeps the history: deployment runs and
                health samples reference the target row. */}
            <button className="cap-btn ct-deploy-action"
                    disabled={retrying || starting || !targetId}
                    data-testid="app-target-retire"
                    title="Unbind this app from its host and port. Deployment history is kept."
                    onClick={async (e) => {
                      e.stopPropagation();
                      if (!targetId) return;
                      setRetrying(true); setNote(null);
                      const r = await retireTarget(targetId);
                      setNote(r.ok ? "target retired — pick a new one below" : (r.error ?? "could not retire"));
                      setRetrying(false);
                      await load();
                    }}>
              Unbind target
            </button>
            {note ? <span className="detail">{note}</span> : null}
          </>
        ) : (
          <SetupDeployment run={run} projectId={projectId} onDone={load} />
        )}
      </div>
    );
  }

  const ok = ["succeeded", "deployed", "completed"].includes(latest.status);
  const failed = ["failed", "rolled_back", "execute_failed", "blocked"].includes(latest.status);
  const where = latest.host_name || latest.environment || "target";

  return (
    <div className="ct-deploy-wrap">
    <div className="ct-deploy-line" data-testid="app-deployment"
         data-deploy-status={latest.status}>
      <span className={`cap-badge ${ok ? "tone-green" : failed ? "tone-red" : "tone-amber"}`}>
        {ok ? "deployed" : failed ? "deploy failed" : latest.status}
      </span>
      <span className="detail">
        {where}
        {latest.finished_at ? <> · <ClientRelativeTime iso={latest.finished_at} /></> : null}
      </span>
      {ok && latest.url ? (
        <a className="cap-btn" href={latest.url} target="_blank" rel="noreferrer">Open ↗</a>
      ) : null}
      {failed ? (
        <>
          {/* The reason, not just the verdict. "Deploy failed" with no cause is the message
              that sends someone to the CI/CD screen — the trip this line exists to remove. */}
          {latest.failure_reason ? (
            <span className="detail ct-deploy-reason" title={latest.failure_reason}>
              {String(latest.failure_reason).slice(0, 60)}
            </span>
          ) : null}
          <button className="cap-btn primary ct-deploy-action" disabled={retrying}
                  data-testid="app-deployment-retry"
                  title="Ship the same commit to the same target again"
                  onClick={async (e) => {
                    e.stopPropagation();
                    setRetrying(true); setNote(null);
                    const fd = new FormData();
                    fd.set("deployment_run_id", latest.id);
                    const r = await retryDeploymentAction(null, fd);
                    setNote(r.ok ? "retry started" : (r.error ?? "retry failed"));
                    setRetrying(false);
                    await load();
                  }}>
            <ArrowsClockwise size={15} weight="bold" aria-hidden />{" "}
            {retrying ? "Retrying…" : "Retry"}
          </button>
          {/* RETRY IS NOT ALWAYS THE ANSWER.
              A deployment that fails because the target itself is wrong — no domain, or the
              port belongs to another app — fails identically every time it is retried, and
              the card offered nothing else. Unbinding is the other move, and it was reachable
              only from the database. */}
          <button className="cap-btn ct-deploy-action"
                  disabled={retrying || !latest.deployment_target_id}
                  data-testid="app-deployment-unbind"
                  title="Unbind this app from its host and port. Deployment history is kept."
                  onClick={async (e) => {
                    e.stopPropagation();
                    const tid = latest.deployment_target_id;
                    if (!tid) return;
                    setRetrying(true); setNote(null);
                    const r = await retireTarget(String(tid));
                    setNote(r.ok ? "target retired — set a new one to deploy again"
                                 : (r.error ?? "could not retire"));
                    setRetrying(false);
                    await load();
                  }}>
            Unbind target
          </button>
          {note ? <span className="detail">{note}</span> : null}
        </>
      ) : null}
      {/* Steps, timings and logs, without leaving the card. Reuses the CI/CD stepper rather
          than drawing a second one: it renders real recorded state, and a lookalike here
          would be a second opinion about what a deployment is doing. */}
      {(live || expanded) && latest.id ? (
        <button className="cap-btn ct-deploy-toggle"
                onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}>
          {expanded ? "hide steps" : "steps"}
        </button>
      ) : null}
    </div>
    {(live || expanded) && latest.id ? (
      <div className="ct-deploy-steps" data-testid="app-deployment-steps">
        <LiveDeployment runId={latest.id} />
      </div>
    ) : null}
    </div>
  );
}

/**
 * The card's overflow menu.
 *
 * The action row had grown to five controls — preview, change, ask, feature, rebuild — all
 * competing at the same weight, so the one thing you usually want (see the app) had to be
 * found among four things you rarely want. One primary button stays visible; everything else
 * moves in here.
 *
 * Written rather than imported because the console has no menu primitive and this is the
 * first one. Kept to what a menu must actually do to be usable: close on Escape, close on an
 * outside click, move with the arrow keys, and announce itself. A div that merely looks like
 * a menu is the version that traps keyboard users.
 */
type MenuItem = {
  id: string;
  label: string;
  icon?: React.ReactNode;
  hint?: string;
  onSelect: () => void;
};

function ActionMenu({ items, label = "More actions" }:
                    { items: MenuItem[]; label?: string }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrap = useRef<HTMLDivElement | null>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => { if (open) itemRefs.current[active]?.focus(); }, [open, active]);

  if (!items.length) return null;

  return (
    <div className="ct-menu-wrap" ref={wrap}>
      <button type="button" className="cap-btn ct-menu-trigger"
              aria-haspopup="menu" aria-expanded={open} aria-label={label} title={label}
              onClick={(e) => { e.stopPropagation(); setActive(0); setOpen((v) => !v); }}>
        <DotsThree size={18} weight="bold" aria-hidden />
      </button>
      {open ? (
        <div className="ct-menu" role="menu" aria-label={label}
             onClick={(e) => e.stopPropagation()}
             onKeyDown={(e) => {
               if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => (i + 1) % items.length); }
               if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => (i - 1 + items.length) % items.length); }
             }}>
          {items.map((item, i) => (
            <button key={item.id} type="button" role="menuitem" className="ct-menu-item"
                    ref={(el) => { itemRefs.current[i] = el; }}
                    onClick={(e) => { e.stopPropagation(); setOpen(false); item.onSelect(); }}>
              {item.icon ? <span className="ct-menu-icon" aria-hidden>{item.icon}</span> : null}
              <span className="ct-menu-text">
                <span className="ct-menu-label">{item.label}</span>
                {/* What it costs / what it does, so the choice is made in the menu rather
                    than by opening each panel to find out. */}
                {item.hint ? <span className="ct-menu-hint">{item.hint}</span> : null}
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function RebuildWithSpec({ run, projectId, actor, mode = "rebuild",
                          open: openProp, onOpenChange }: {
  run: CodeTaskRun; projectId: string; actor: string;
  /** "rebuild" replaces the app; "feature" specs an addition to the one that exists. */
  mode?: "rebuild" | "feature";
  /** Controlled: when supplied, the panel is opened by something else (the actions menu)
   *  and this component renders no trigger of its own. Uncontrolled use is unchanged. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const controlled = openProp !== undefined;
  const [openState, setOpenState] = useState(false);
  const open = controlled ? !!openProp : openState;
  const setOpen = (v: boolean) => { if (controlled) onOpenChange?.(v); else setOpenState(v); };
  const [complaint, setComplaint] = useState("");
  const app = appIdentity(run);
  const repo = `${run.spec?.owner ?? ""}/${run.spec?.repo ?? ""}`.replace(/^\/$/, "");

  if (!open) {
    // Controlled: the menu owns the trigger, so render nothing until it opens us.
    if (controlled) return null;
    // The label MUST follow `mode`. Both modes rendered "Rebuild with a fresh spec", so the
    // card showed two identical buttons side by side where one ADDS to the app and the other
    // REPLACES it — a difference you could only discover by opening one. Only the expanded
    // panel distinguished them, which is too late to be a choice.
    const isFeature = mode === "feature";
    return (
      <button className="cap-btn" onClick={(e) => { e.stopPropagation(); setOpen(true); }}
              data-mode={mode}
              title={isFeature
                ? "Convene a panel to spec a new feature, then build it onto this app"
                : "Convene a panel to write a new PRD, then rebuild this app from it"}>
        <Sparkle size={15} weight="fill" aria-hidden />{" "}
        {isFeature ? "Add a feature" : "Rebuild with a fresh spec"}
      </button>
    );
  }

  // The panel is told WHICH app and WHERE it lives, so its PRD is about this product rather
  // than a fresh idea with the same name.
  // Two different asks, and the difference matters to the pipeline: a rebuild starts a new
  // app, a feature continues this one. The [boss:extend_run=...] marker is an explicit
  // contract with the decision handler — it routes the approved PRD to a follow-up on this
  // app's own branch instead of a fresh scaffold, and is stripped before the PRD is used.
  const directive = mode === "feature"
    ? `Add a feature to "${app.name}" — an existing, working application in `
      + `${repo || "this project"}. [boss:extend_run=${run.id}]\n\n`
      + `What is wanted: ${complaint.trim()}\n\n`
      + `Write a PRD for THIS FEATURE ONLY. The app already exists and must keep working: do `
      + `not redesign it, do not re-scaffold it, do not restate the whole product. Cover what `
      + `changes, what must not change, the acceptance scenarios for the new behaviour, and `
      + `the evidence required to prove them.`
    : `Rebuild "${app.name}" — an existing application in ${repo || "this project"}.\n\n`
      + `What is wrong with the current build: ${complaint.trim()}\n\n`
      + `Write a PRD for a replacement that fixes this. It must be a real, working full-stack `
      + `application: a server, a database with migrations and seed data, and a UI that is `
      + `visually finished — considered layout, typography, spacing and states (loading, empty, `
      + `error), not an unstyled prototype. Include acceptance scenarios and the evidence `
      + `required to prove them.`;

  return (
    <form action={conveneMeetingAction} className="ct-change-form"
          onClick={(e) => e.stopPropagation()}>
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actor} />
      <input type="hidden" name="meeting_type" value="prd_panel" />
      <input type="hidden" name="directive" value={directive} />
      <textarea
        className="ct-feedback-input"
        rows={3}
        autoFocus
        aria-label={`What is wrong with ${app.name}?`}
        value={complaint}
        onChange={(event) => setComplaint(event.target.value)}
        placeholder={mode === "feature"
          ? "What should it gain?\ne.g. bulk-close issues from the list, with an undo window"
          : "What is wrong with it?\ne.g. the UI is pale and unstyled — it needs a designed "
            + "layout, real typography, and proper empty/loading states"}
      />
      <div className="ct-actions">
        <SubmitButton className="cap-btn primary" pendingLabel="Convening the panel…"
                      disabled={!complaint.trim()}>
          Convene the panel
        </SubmitButton>
        <button type="button" className="cap-btn" onClick={() => setOpen(false)}>Cancel</button>
        <span className="detail">
          {mode === "feature"
            ? `Specs the feature, then builds it onto this app's own branch — costs a panel and a run.`
            : `Writes a new PRD for ${repo || "this app"}, then builds a replacement — costs a panel and a run.`}
        </span>
      </div>
    </form>
  );
}

function ChangeThisApp({ run, onStarted, onOpen, startedId }: {
  run: CodeTaskRun;
  startedId?: string | null;
  onOpen?: (runId: string) => void;
  onStarted?: (parentId: string, runId?: string, error?: string) => void | Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    const data = new FormData();
    data.set("run_id", run.id);
    data.set("instructions", text);
    let out: { run_id?: string; error?: string } | undefined;
    try {
      out = await followUpCodeTaskAction(data);
    } finally {
      setBusy(false);
      if (out?.run_id) {
        setOpen(false);
        setText("");
      } else {
        setError(out?.error ?? "the API returned no run");
      }
      // Keyed apart from the relaunch button: both report through the same parent map, and
      // sharing a key made one control's result appear under the other.
      await onStarted?.(`chg:${run.id}`, out?.run_id, out?.error);
    }
  };

  if (startedId) {
    return (
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button className="cap-btn" onClick={() => onOpen?.(startedId)}
                title="Open the change this started">
          View change →
        </button>
        <span className="detail">
          building on this app&apos;s existing code — nothing is rebuilt
        </span>
      </div>
    );
  }

  if (!open) {
    return (
      // A button, not a bare link. The card is a grid, so an unwrapped <button> stretched to
      // the full width and centred its own label -- the control looked like a stray caption
      // floating between the spine and the primary action. It is also the most common real
      // action on a finished app (change it), so a muted underline understated it.
      <button className="cap-btn ct-change-btn" onClick={() => setOpen(true)}
              title="Ask for a change; it builds on the code that already exists">
        <PencilSimple size={15} weight="bold" aria-hidden /> Request a change
      </button>
    );
  }

  return (
    <div className="ct-change-form">
      <textarea
        aria-label="What should change about this app?"
        className="ct-feedback-input"
        rows={3}
        autoFocus
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder={"What should change?\ne.g. save expenses to a database instead of "
                     + "React state, so they survive a reload"}
      />
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button className="cap-btn primary" disabled={busy || !text.trim()}
                onClick={() => void submit()}>
          {busy ? "Starting…" : "Start this change"}
        </button>
        <button className="cap-btn" disabled={busy} onClick={() => setOpen(false)}>
          Cancel
        </button>
        <span className="detail">
          {error
            ? error
            : "starts on this app\u2019s own branch — the existing code is already there"}
        </span>
      </div>
    </div>
  );
}

function LaunchTask({ run, projectId, label = "Run this task again", onLaunched, onOpen,
                     startedId, failed }: {
  run: CodeTaskRun; projectId: string; label?: string;
  // Opens the run this card started. Without it the button ends as a disabled control naming
  // an id you cannot reach — it tells you something happened and gives you no way to see it,
  // which is a dead end wearing the costume of a confirmation.
  onOpen?: (runId: string) => void;
  // What this card started, and any failure, both held by the PARENT.
  //
  // They lived here as local state and vanished on every launch: the reload re-renders the
  // list, this component is remounted, and "Launched ✓" reverted to the original label with
  // nothing on screen having changed — which is the exact condition that makes someone press
  // the button again. State that must outlive a re-render cannot live inside the thing being
  // re-rendered.
  startedId?: string | null; failed?: string | null;
  onLaunched?: (sourceRunId: string, newRunId?: string, error?: string) => void | Promise<void>;
}) {
  const [sent, setSent] = useState(false);
  const router = useRouter();
  const spec = run.spec ?? {};
  // Offering to re-run work that is still going is an invitation to waste money. The API
  // would refuse it anyway — the duplicate guard returns the in-flight run — but a button
  // that looks available and then quietly does something else is the worse failure: it
  // teaches the operator that controls here are decorative.
  const inFlight = run.status === "running" || run.status === "queued";
  const st = run.state ?? {};
  // The profile the original actually RAN with. `spec.profile` is an explicit per-task
  // override; `state.profile` is what the engine resolved when nothing was set. Reproducing
  // the effective one is what makes this a re-run rather than a new task that happens to
  // share a title.
  const profile = (spec as Record<string, unknown>).profile as string | undefined
    ?? (st as Record<string, unknown>).profile as string | undefined;
  const riskTier = (spec as Record<string, unknown>).risk_tier as number | undefined
    ?? (st as Record<string, unknown>).risk_tier as number | undefined;
  // No instructions means nothing to run: an empty relaunch would be a differently-named
  // no-op that still costs a governed run.
  if (!spec.instructions) return null;
  // A plain <form action={serverAction}> submits and never comes back: the button sat on
  // "Starting…" indefinitely and the new run did not appear, because nothing re-read the
  // list. The run WAS created each time — the only broken part was saying so. Awaiting the
  // action and refreshing means the button resolves and the row shows up.
  const launch = async () => {
    setSent(true);
    const data = new FormData();
    data.set("project_id", projectId);
    data.set("title", run.title ?? "Relaunched task");
    data.set("instructions", spec.instructions ?? "");
    data.set("acceptance_criteria", (spec.acceptance_criteria ?? []).join("\n"));
    data.set("test_commands", (spec.test_commands ?? []).join("\n"));
    data.set("owner", spec.owner ?? "");
    data.set("repo", spec.repo ?? "");
    data.set("base_branch", spec.base_branch ?? "");
    if (profile) data.set("profile", profile);
    if (riskTier != null) data.set("risk_tier", String(riskTier));
    let out: { run_id?: string; error?: string } | undefined;
    try {
      out = await relaunchCodeTaskAction(data);
    } finally {
      setSent(false);
      // No router.refresh(): it re-renders the server tree and remounts this card, which is
      // what erased the launch it had just recorded. The panel reloads its own list instead.
      await onLaunched?.(run.id, out?.run_id,
                         out?.run_id ? undefined : (out?.error ?? "the API returned no run"));
    }
  };

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      {/* Once it has launched, the control becomes a way IN to what it started.
          It used to stay as a disabled "Launched ✓" — which stops the second press, but
          leaves you holding a run id and no way to look at it. Pressing the thing you just
          pressed should now show you the result of pressing it. */}
      {startedId ? (
        <button className="cap-btn primary"
                onClick={(e) => { e.stopPropagation(); onOpen?.(startedId); }}
                title="Open the run this launched">
          View run →
        </button>
      ) : (
        <button className="cap-btn primary" disabled={sent || inFlight}
                onClick={(e) => { e.stopPropagation(); void launch(); }}
                title={inFlight
                  ? "This task is still running — wait for it to finish before running it again"
                  : "Start a NEW governed run from this task's spec — the original keeps its history"}>
          {sent ? "Starting…" : inFlight ? "Running…" : label}
        </button>
      )}
      <span className="detail">
        {/* State the profile this will ACTUALLY use.
            This caption was copied wholesale from the benchmark card, including its claim
            about rigor — accurate there, since that endpoint forces the strict profile, and a
            statement about someone else's button here. The currency build ran `lean`, the
            relaunch was adopting the project default, and the label agreed with neither. */}
        {sent ? "starting…"
              : failed ? failed
              : inFlight ? "still running — re-run it once it finishes"
              : startedId
                // Name the run. The previous text promised a position in a list this
                // component cannot see, and when the list did not move it read as a failure.
                // "Launched" implied there was something to look at. There is not, yet: this
                // starts a governed CODING run — plan, tests, implement, review, gated PR —
                // and only once that produces a build is there an app to preview. Saying so
                // here is the difference between a confirmation and a riddle.
                ? `started ${startedId.slice(0, 13)}… — it plans, tests and implements before `
                  + `there is anything to preview`
                // The profile and gating used to be spelled out here on EVERY card, in the
                // same words, taking a line to say something that never changed and that
                // nothing could be done about. The profile now appears once, as a pill in the
                // meta row.
                : ""}
      </span>
    </div>
  );
}

function ContractDetails({ criteria, verification, label = "Acceptance criteria & verification" }: {
  criteria?: string[] | null; verification?: string[] | null; label?: string;
}) {
  const items = (criteria ?? []).filter(Boolean);
  const checks = (verification ?? []).filter(Boolean);
  if (!items.length && !checks.length) return null;
  return (
    <details onClick={(e) => e.stopPropagation()}>
      <summary className="detail" style={{ cursor: "pointer" }}>{label}</summary>
      {items.length > 0 && (
        <ul className="detail" style={{ margin: "6px 0 0", paddingLeft: 20 }}>
          {items.map((item, i) => <li key={i}>{item}</li>)}
        </ul>
      )}
      {checks.length > 0 && (
        <p className="detail" style={{ margin: "6px 0 0" }}>Verification: {checks.join(" · ")}</p>
      )}
    </details>
  );
}

function QaBenchmarkCatalog({ benchmarks, builtBenchmarkIds = new Set<string>(), projectId, actor, defaultOwner, defaultRepo,
                             launched, onLaunched, onOpen }: {
  benchmarks: QaBenchmarkApp[];
  /** Benchmarks that appear among the project's runs — the grid's answer, reused here. */
  builtBenchmarkIds?: Set<string>;
  projectId: string;
  actor: string;
  defaultOwner?: string;
  defaultRepo?: string;
  // Held by CodeTasksHome, keyed by benchmark id, because a launch reloads the list and any
  // state inside these cards goes with it.
  launched?: Record<string, {
    run_id?: string; error?: string; kind?: "meeting" | "code" | "existing";
  }>;
  onLaunched?: (key: string, runId?: string, error?: string,
                kind?: "meeting" | "code" | "existing") => void | Promise<void>;
  onOpen?: (runId: string) => void;
}) {
  if (!benchmarks.length) return null;
  // ONE CARD PER APP, INCLUDING THESE.
  //
  // A benchmark is a thing you can build; once built it IS an app, and that app already has a
  // card below carrying its runs, its PR and its preview. Showing both produced two BugBox
  // cards in identical styling -- indistinguishable at a glance, and the catalogue one was the
  // less useful of the pair. The catalogue now offers what is not built yet; the built ones are
  // named in a line pointing at the app that replaced them.
  // Read from the runs themselves, not only from the catalogue's own `built` field: ApproveIt
  // had been built twice and still offered a Launch card because that field was empty. One
  // question -- "has this been built?" -- must have one answer wherever it is asked.
  const isBuilt = (b: QaBenchmarkApp) =>
    Boolean(b.built?.run_id) || Boolean(launched?.[b.id]?.run_id) || builtBenchmarkIds.has(b.id);
  const builtAlready = benchmarks.filter(isBuilt);
  const notBuiltYet = benchmarks.filter((b) => !isBuilt(b));
  return (
    <div className="ct-block" style={{ marginTop: 12 }}>
      <div className="ct-block-title">End-to-end QA benchmarks</div>
      <p className="detail" style={{ marginTop: 4 }}>
        These are pinned product contracts, not canned demos. Launching one creates a strict governed code task with a durable plan, evidence bundle, and final verification report.
      </p>
      {/* The benchmark catalogue: the THIRD card path, and the last one still rendering as
          full-width rows while everything below it had become tiles. Three render paths for
          the same kind of object is how this page kept looking half-converted — a section
          nobody had touched sat directly above the section that had, and the difference read
          as breakage rather than as an omission. */}
      {builtAlready.length ? (
        <p className="detail" style={{ marginTop: 10 }}>
          Already built: {builtAlready.map((b) => b.name).join(" · ")} — each is an app below,
          with its runs inside. Rebuild it from there.
        </p>
      ) : null}
      {notBuiltYet.length === 0 ? null : (
      <div className="mr-grid ct-tiles" style={{ marginTop: 10 }}>
        {notBuiltYet.map((benchmark) => (
          <article key={benchmark.id} className="mr-meeting-card">
            <div className="mr-meeting-card-head">
              <span className="cap-badge tone-blue">{benchmark.tagline}</span>
            </div>
            <span className="mr-meeting-title">{benchmark.name}</span>
            <p className="detail" style={{ margin: 0 }}>{benchmark.summary}</p>
            <p className="detail" style={{ margin: 0 }}>Exercises: {benchmark.focus.join(" · ")}</p>
            <ContractDetails criteria={benchmark.acceptance_scenarios}
                             verification={benchmark.verification_commands}
                             label="Acceptance scenarios & required evidence" />
            <BenchmarkLaunch benchmark={benchmark} projectId={projectId} actor={actor}
                             defaultOwner={defaultOwner} defaultRepo={defaultRepo}
                             onLaunched={onLaunched} onOpen={onOpen}
                             startedId={launched?.[benchmark.id]?.run_id}
                             startedKind={launched?.[benchmark.id]?.kind}
                             failed={launched?.[benchmark.id]?.error} />
          </article>
        ))}
      </div>
      )}
    </div>
  );
}

// Three states, not two: passed (green ✓), failed (red ✗), or NOT RUN (muted –). A skipped
// check must never read as a failure — that's what made a lean build look like it "failed".
function GateRow({ label, ok, note, skipped }: { label: string; ok?: boolean; note?: string; skipped?: boolean }) {
  const mark = skipped ? "–" : ok ? "✓" : "✗";
  const color = skipped ? "var(--muted)" : ok ? "#3fb950" : "#f85149";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
      <span style={{ color, fontWeight: 700, width: 14, textAlign: "center" }}>{mark}</span>
      <span style={{ color: skipped ? "var(--muted)" : "var(--ink)" }}>{label}</span>
      <span className="detail">— {skipped ? (note && note !== "skipped" ? note : "not run at this rigor") : (note || (ok ? "passed" : "needs attention"))}</span>
    </div>
  );
}

function QaBenchmarkEvidence({ runId, benchmarkName }: { runId: string; benchmarkName: string }) {
  const [report, setReport] = useState<QaBenchmarkReport | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void getQaBenchmarkReportAction(runId).then((value) => {
      if (!cancelled) {
        setReport(value);
        setLoaded(true);
      }
    });
    return () => { cancelled = true; };
  }, [runId]);
  const tone = report?.passed ? "green" : "amber";
  return (
    <div className="ct-block">
      <div className="ct-block-title">
        QA benchmark evidence <span className={`cap-badge tone-${tone}`}>{report?.passed ? "certified" : "not certified"}</span>
      </div>
      <p className="detail" style={{ marginTop: 4 }}>
        {benchmarkName} is certified only from recorded evidence; a completed run alone is insufficient.
      </p>
      {!loaded ? <p className="detail">Loading benchmark report…</p> : null}
      {loaded && !report ? <p className="detail">Benchmark report is unavailable; no completion claim is shown.</p> : null}
      {report ? (
        <>
          <div style={{ display: "grid", gap: 5, marginTop: 8 }}>
            {report.results.map((result) => (
              <GateRow key={result.command} label={result.label} ok={result.status === "pass"}
                skipped={result.status === "not_recorded"}
                note={`${result.command}${result.exit_code != null ? ` · exit ${result.exit_code}` : ""}`} />
            ))}
          </div>
          <div style={{ marginTop: 12, display: "grid", gap: 5 }}>
            <div className="ct-block-title" style={{ fontSize: 12.5 }}>
              Benchmark scorecard <span className={`cap-badge tone-${report.scorecard.overall_score == null ? "muted" : report.scorecard.overall_status === "complete" ? "green" : "amber"}`}>
                {report.scorecard.overall_score == null ? "unscored" : `${report.scorecard.overall_score}/100 · ${report.scorecard.overall_status}`}
              </span>
            </div>
            <p className="detail" style={{ margin: 0 }}>
              Evidence coverage: {report.scorecard.evidence_coverage_pct}% weighted. Missing dimensions stay unscored and do not improve the overall result.
            </p>
            {report.scorecard.dimensions.map((dimension) => (
              <div key={dimension.id} style={{ display: "flex", justifyContent: "space-between", gap: 10, fontSize: 13 }}>
                <span>{dimension.label} <span className="detail">({dimension.weight}%)</span></span>
                <span className={dimension.status === "scored" ? "detail" : "detail"}>
                  {dimension.status === "scored" ? `${dimension.score}/100` : "not scored"}
                </span>
              </div>
            ))}
            <p className="detail" style={{ margin: 0 }}>{report.scorecard.tuning_note}</p>
          </div>
          <p className="detail" style={{ marginTop: 8 }}>{report.message}</p>
        </>
      ) : null}
    </div>
  );
}

function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  return rs ? `${m}m ${rs}s` : `${m}m`;
}

// Per-stage model-call detail (the internal "why is this slow"): model, turns, latency,
// retries, cost, errors per governed stage. Auto-refreshes while the task is live.
function CodeTaskActivity({ runId, live }: { runId: string; live: boolean }) {
  const [open, setOpen] = useState(live);
  const [calls, setCalls] = useState<LlmCallRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const load = async () => {
      const data = await getCodeTaskActivityAction(runId);
      if (!cancelled) {
        setCalls(data);
        setLoaded(true);
      }
    };
    void load();
    const id = live ? setInterval(load, 4000) : undefined;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [runId, open, live]);
  return (
    <div className="ct-activity-wrap">
      <button type="button" className="cap-btn" onClick={() => setOpen((o) => !o)}>
        {open ? "Hide agent activity" : "Show agent activity"}
      </button>
      {open ? (
        <div className="ct-activity">
          {!loaded ? (
            <p className="detail">Loading…</p>
          ) : calls.length === 0 ? (
            <p className="detail">No model calls recorded yet — the current stage hasn’t returned a call.</p>
          ) : (
            calls.map((c, i) => (
              <div className={`ct-call${c.ok ? "" : " err"}`} key={i}>
                <span className="ct-call-stage">{PHASE_LABEL[c.stage] ?? c.stage}</span>
                <span className="ct-call-model">{c.model || c.engine}</span>
                <span>{c.turns} turn{c.turns === 1 ? "" : "s"}</span>
                <span className="ct-call-lat">{fmtDuration(c.latency_ms)}</span>
                {c.retries > 0 ? (
                  <span className="ct-call-retry">{c.retries} retr{c.retries === 1 ? "y" : "ies"}</span>
                ) : null}
                <span className="ct-call-cost">${(c.cost_usd_micros / 1e6).toFixed(3)}</span>
                {!c.ok ? <span className="ct-call-err">✗ {(c.error ?? "error").slice(0, 60)}</span> : null}
              </div>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

function CodeTaskDetail({ run, previews = [], onBack }: { run: CodeTaskRun; previews?: PreviewRec[]; onBack: () => void }) {
  const st = run.state ?? {};
  const tests = structuredCodeTaskTests(st);
  // Operator View: running first, then verified-test with an open_url.
  // verified-test without a URL is still shown so we can be honest (no invented View).
  const livePreviews = previews
    .filter((p) => p.run_id === run.id && (p.status === "running" || p.status === "verified"))
    .sort((a, b) => {
      if (a.status === "running" && b.status !== "running") return -1;
      if (b.status === "running" && a.status !== "running") return 1;
      return (b.expires_at ?? "").localeCompare(a.expires_at ?? "");
    })
    .slice(0, 2);
  const live = run.status === "running" || run.status === "queued";
  useLiveRefresh(live);
  // Step timing: completed phases carry their duration; the current phase's elapsed is computed
  // live from phase_started_at (re-renders every ~4s via useLiveRefresh, so it ticks up).
  // Compute only after mount so SSR HTML matches the client's first paint (BOSS-346 / #418).
  const _fc = (run.finalize_ctx ?? {}) as Record<string, unknown>;
  const phaseTimings = (_fc.phase_timings as { ms: number; phase: string }[] | undefined) ?? [];
  const _phaseStartedAt = _fc.phase_started_at as string | undefined;
  const [currentPhaseMs, setCurrentPhaseMs] = useState<number | null>(null);
  useEffect(() => {
    if (!live || !_phaseStartedAt) {
      setCurrentPhaseMs(null);
      return;
    }
    const tick = () => {
      const started = new Date(_phaseStartedAt).getTime();
      if (Number.isNaN(started)) {
        setCurrentPhaseMs(null);
        return;
      }
      setCurrentPhaseMs(Math.max(0, Date.now() - started));
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [live, _phaseStartedAt]);
  const totalMs = phaseTimings.reduce((s, t) => s + (t.ms || 0), 0) + (currentPhaseMs ?? 0);
  const hasGateEvidence = Boolean(
    st.review_summary || st.adversarial_verdict || st.security_verdict || st.qa_summary || st.impl_summary,
  );
  const gateTone =
    run.status === "completed" ? "green" : run.status === "blocked" ? "amber" : run.status === "failed" ? "pink" : "muted";
  const gateLabel =
    run.status === "completed" ? "passed" : run.status === "blocked" ? "gated" : run.status === "failed" ? "failed" : run.status;
  return (
    <div className="ct-detail">
      <button className="mr-back" onClick={onBack}>← All code tasks</button>
      <div className="ct-detail-head">
        <h3>{run.title}</h3>
        <span className={`cap-badge tone-${statusTone(run.status)}`}>{run.status}</span>
      </div>
      {/* What the work must do, and how it is proven.
          This was on the LIST cards, which is what made them unreadable at tile size — and
          when I removed it I said it lived "one click away in the task view". It did not: the
          detail view had never shown it, so the only content saying what a task was FOR was
          simply gone. A test caught the claim; this makes it true. */}
      <ContractDetails criteria={run.spec?.acceptance_criteria}
                       verification={run.spec?.test_commands} />
      <div className="ct-meta">
        <span><b>Repo</b> {run.spec?.owner}/{run.spec?.repo}</span>
        <span><b>Engine</b> {st.engine ?? "—"}</span>
        <span><b>Model</b> {run.usage?.model ?? "—"}</span>
        <span><b>Cost</b> {fmtUsd(st.cost_usd_micros ?? run.usage?.cost_usd_micros)}</span>
        <span><b>Turns</b> {st.turns ?? "—"}</span>
        <span className="cap-badge tone-blue">
          built: {(run.finalize_ctx?.execution_substrate ?? st.substrate) === "microvm"
            ? `micro-VM ${run.finalize_ctx?.sandbox_task_id ?? ""}`.trim()
            : "API host"}
        </span>
      </div>

      <Flow phase={run.phase} status={run.status} />

      {live ? (
        <div className="detail" style={{ margin: "8px 0", color: "#58a6ff", fontWeight: 600 }}>
          <span style={{ animation: "pulse 1.5s infinite" }}>●</span>{" "}
          {PHASE_LABEL[run.phase] ?? run.phase} — running{" "}
          <span>{currentPhaseMs != null ? fmtDuration(currentPhaseMs) : "…"}</span>{" "}
          <span className="detail" style={{ fontWeight: 400 }}>(auto‑refreshing every 4s)</span>
        </div>
      ) : null}

      {(phaseTimings.length > 0 || currentPhaseMs != null) ? (
        <div className="ct-block" style={{ margin: "8px 0" }}>
          <div className="ct-block-title">Step timing</div>
          <div style={{ display: "grid", gap: 4, marginTop: 6 }}>
            {phaseTimings.map((t, i) => (
              <div key={i} style={{ display: "flex", justifyContent: "space-between", fontSize: 13 }}>
                <span style={{ color: "var(--ink)" }}>✓ {PHASE_LABEL[t.phase] ?? t.phase}</span>
                <span className="detail">{fmtDuration(t.ms)}</span>
              </div>
            ))}
            {live && currentPhaseMs != null ? (
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13 }}>
                <span style={{ color: "#58a6ff", fontWeight: 600 }}>● {PHASE_LABEL[run.phase] ?? run.phase} (running)</span>
                <span style={{ color: "#58a6ff", fontWeight: 600 }}>{fmtDuration(currentPhaseMs)}</span>
              </div>
            ) : null}
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, borderTop: "1px solid var(--border)", paddingTop: 4, marginTop: 2 }}>
              <span style={{ color: "var(--muted)" }}>Total{live ? " so far" : ""}</span>
              <span className="detail">{fmtDuration(totalMs)}</span>
            </div>
          </div>
        </div>
      ) : null}

      <CodeTaskActivity runId={run.id} live={live} />

      {run.error ? (
        <div className="ct-error">
          ⚠ {/quality ?gate/i.test(run.error)
            ? "Quality gate failed — the pipeline blocked the PR (fail-closed). See Review & quality gate below."
            : run.error}
        </div>
      ) : null}

      {run.finalize_ctx?.qa_benchmark ? (
        <QaBenchmarkEvidence runId={run.id} benchmarkName={run.finalize_ctx.qa_benchmark.name} />
      ) : null}

      {hasGateEvidence ? (
        <div className="ct-block">
          <div className="ct-block-title">
            Steps &amp; quality checks{" "}
            <span className={`cap-badge tone-${gateTone}`}>{gateLabel}</span>
          </div>
          {((run.finalize_ctx as Record<string, unknown> | undefined)?.advisory_failing_gates as unknown[] | undefined)?.length ? (
            <p className="detail" style={{ marginTop: 4 }}>
              ⓘ First-pass build: quality checks below are <b>advisory</b> — a ✗ did not block the PR.
              Shipped so you can preview now; use <b>Retry</b> to improve and clear the ✗.
            </p>
          ) : null}
          <div style={{ display: "grid", gap: 6, margin: "8px 0" }}>
            <GateRow label="Code built / tests pass" ok={st.tests_passed} skipped={st.tests_passed == null && st.test_exit_code == null} note={st.test_exit_code != null ? `exit ${st.test_exit_code}` : undefined} />
            <GateRow label="Code review (adversarial)" ok={st.adversarial_verdict === "survived"} skipped={!st.adversarial_verdict || st.adversarial_verdict === "skipped" || st.adversarial_verdict === "not_run"} note={st.adversarial_verdict} />
            <GateRow label="Security clean" ok={st.security_verdict === "clean"} skipped={!st.security_verdict || st.security_verdict === "skipped" || st.security_verdict === "not_run"} note={st.security_verdict} />
            <GateRow label="No regressions" ok={st.regressions_ok} skipped={st.regressions_ok == null} note={st.mutation_caught ? "mutation caught" : undefined} />
            <GateRow label="Consistency" ok={["consistent", "pass", "survived"].includes(st.consistency_verdict ?? "")} skipped={!st.consistency_verdict || st.consistency_verdict === "skipped"} note={st.consistency_verdict} />
            <GateRow label="Pull request opened" ok={!!run.pull_request_url} skipped={!run.pull_request_url && run.status !== "completed"} note={run.pull_request_number ? `PR #${run.pull_request_number}` : undefined} />
            <GateRow
              label="Preview available"
              ok={livePreviews.some((p) => p.status === "running" && operatorPreviewOpenHref(p))}
              skipped={livePreviews.length === 0}
              note={
                livePreviews.some((p) => p.status === "running" && operatorPreviewOpenHref(p))
                  ? "running — open below"
                  : livePreviews.some((p) => p.status === "verified" && operatorPreviewOpenHref(p))
                    ? "verified-test has a View URL"
                    : livePreviews.some((p) => p.status === "verified")
                      ? "verified-test — no operator URL"
                      : "run a preview"
              }
            />
          </div>
          <p className="detail">
            Independent reviewer: {st.reviewer_independent ? "yes" : "no"} · Independent test author:{" "}
            {st.test_author_independent ? "yes" : "no"}
            {st.profile ? ` · profile: ${st.profile}` : ""}
            {st.risk_tier != null ? ` · risk tier ${st.risk_tier}` : ""}
          </p>
          {(st.build_engine || st.review_engine) ? (
            <p className="detail">
              🧠 Builder: <b>{st.build_engine ?? "—"}</b>
              {st.model ? ` (${st.model})` : ""} · Adversary: <b>{st.review_engine ?? st.build_engine ?? "—"}</b>
              {st.review_model ? ` (${st.review_model})` : ""}
              {st.review_engine && st.build_engine && st.review_engine !== st.build_engine
                ? " — cross-model review (no self-grading)"
                : " — same model builds + reviews"}
            </p>
          ) : null}
          {st.review_summary ? (
            <div className="ct-summary" style={{ marginTop: 8 }}>
              <div className="ct-block-title" style={{ fontSize: 12.5 }}>🔎 Adversarial reviewer findings</div>
              <Markdown text={st.review_summary} />
            </div>
          ) : null}
          {st.security_summary ? (
            <div className="ct-summary" style={{ marginTop: 8 }}>
              <div className="ct-block-title" style={{ fontSize: 12.5 }}>
                🔒 Security review {st.security_verdict ? `— ${st.security_verdict}` : ""}
              </div>
              <Markdown text={st.security_summary} />
            </div>
          ) : null}
          {st.consistency_summary ? (
            <details style={{ marginTop: 8 }}>
              <summary className="detail" style={{ cursor: "pointer" }}>Consistency review</summary>
              <div className="ct-summary"><Markdown text={st.consistency_summary} /></div>
            </details>
          ) : null}
          {st.qa_summary ? (
            <details style={{ marginTop: 8 }}>
              <summary className="detail" style={{ cursor: "pointer" }}>QA / test-author summary</summary>
              <div className="ct-summary"><Markdown text={st.qa_summary} /></div>
            </details>
          ) : null}
          {st.impl_summary ? (
            <details style={{ marginTop: 6 }}>
              <summary className="detail" style={{ cursor: "pointer" }}>Implementation summary</summary>
              <div className="ct-summary"><Markdown text={st.impl_summary} /></div>
            </details>
          ) : null}
          {(st.impl_files ?? st.test_files) ? (
            <p className="detail" style={{ marginTop: 6 }}>
              {st.test_files?.length ? `Test files: ${st.test_files.join(", ")}. ` : ""}
              {st.impl_files?.length ? `Impl files: ${st.impl_files.join(", ")}.` : ""}
            </p>
          ) : null}
        </div>
      ) : null}

      {st.summary ? (
        <div className="ct-block">
          <div className="ct-block-title">Agent summary</div>
          <div className="ct-summary"><Markdown text={st.summary} /></div>
        </div>
      ) : null}

      <div className="ct-block">
        <div className="ct-block-title">Diff summary <span className="detail">(the patch stays in your GitHub PR)</span></div>
        <div className="ct-diff">
          <span className="ct-diff-stat">{st.files_changed ?? 0} files</span>
          <span className="ct-diff-add">+{st.additions ?? 0}</span>
          <span className="ct-diff-del">−{st.deletions ?? 0}</span>
        </div>
        {(st.paths ?? []).length ? (
          <ul className="ct-paths">
            {(st.paths ?? []).map((p) => <li key={p}><code>{p}</code></li>)}
          </ul>
        ) : null}
      </div>

      <div className="ct-block">
        <div className="ct-block-title">
          Tests <span className={`cap-badge tone-${st.tests_passed ? "green" : "muted"}`}>{st.tests_passed ? "passed" : "not passed"}</span>
        </div>
        {tests.length ? (
          <ul className="ct-tests">
            {tests.map((t, i) => (
              <li key={i}>
                <span className={`ct-test-dot ${t.exit_code === 0 && !t.timed_out ? "ok" : "fail"}`} />
                <code>{t.command}</code>
                <span className="detail">exit {t.exit_code ?? "—"}{t.timed_out ? " · timed out" : ""}{t.duration_ms != null ? ` · ${t.duration_ms}ms` : ""}</span>
              </li>
            ))}
          </ul>
        ) : <p className="detail">No test results recorded.</p>}
      </div>

      <div className="ct-block">
        <div className="ct-block-title">Outcome</div>
        <div className="ct-outcome">
          {run.pull_request_url ? (
            <a className="cap-btn" href={run.pull_request_url} target="_blank" rel="noreferrer">
              View PR #{run.pull_request_number} →
            </a>
          ) : <span className="detail">No PR opened.</span>}
          {run.decision_id ? (
            <span className="ct-gate">🔒 Merge gated by Decision <code>{run.decision_id}</code> — approve in the Decisions inbox.</span>
          ) : null}
        </div>
      </div>

      <div className="ct-block">
        <div className="ct-block-title">Run it / iterate</div>
        {/* Row: preview the built app */}
        <div className="ct-outcome" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          {run.pull_request_url ? (
            <PreviewControls runId={run.id} livePreviews={livePreviews} />
          ) : (
            <span className="detail">▶ Preview available once a PR is open. Checks do not have to pass — a run ships its PR with any findings attached — so this appears as soon as the run produces a change.</span>
          )}
        </div>
        {/* Row: plain operator-triggered retry. Separate from the feedback flow below, which
            asks an agent to diagnose and propose one — when the findings are already on screen
            the operator just wants to run it again. */}
        {run.status !== "running" && run.status !== "queued" ? (
          <div className="ct-outcome" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
            <RetryControls
              runId={run.id}
              failingGates={(run.finalize_ctx?.advisory_failing_gates
                ?? run.finalize_ctx?.failing_gates
                ?? []) as string[]}
            />
          </div>
        ) : null}
        {/* Separate row: iterate on the same task with feedback */}
        {(run.status === "completed" || run.status === "failed" || run.status === "blocked") ? (
          <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid rgba(255,255,255,0.09)" }}>
            <FeedbackControls
              runId={run.id}
              status={run.status}
              anchor={livePreviews[0] ? `/v1/previews/${livePreviews[0].id}/web/` : ""}
            />
          </div>
        ) : null}
        {(run.status === "failed" || run.status === "blocked") ? (
          <div style={{ marginTop: 8 }}>
            <OverrideControls runId={run.id} />
            <p className="detail" style={{ marginTop: 4 }}>
              ⚖ Human override: accept the failing finding(s) and open the PR from the work already
              produced — no re‑run. The PR still carries its own merge gate.
            </p>
          </div>
        ) : null}
        <p className="detail" style={{ marginTop: 4 }}>
          Preview runs the built app on a local URL (1‑box, auto‑expires). Retry / iterate re‑runs
          this task <strong>on its existing code</strong> (same branch &amp; PR) — the model builds on
          prior work, adding your feedback and any prior findings, so context isn&apos;t lost.
        </p>
      </div>
    </div>
  );
}

/** Runs BOSS starts on itself: quality passes, scorecard sweeps, profile checks.
 *
 *  They are legitimate work and they are nobody's TASK. Left in the list they outnumber the
 *  real ones and the screen stops being about what the operator is building.
 */
//  Deliberately narrow. An earlier version also matched "follow-up: deferred review", which
//  cut the card count from 48 to 2 — those are REAL work (the run that gave ApproveIt its
//  backend was one of them). Hiding the operator's own follow-ups to reduce clutter defeats
//  the point of the list.
const INTERNAL_RUN = /^(captain:|quality improve pass|goldeneye profile check)/i;

/** What KIND of attempt this is, so a stack of them reads as a story rather than repetition. */
function attemptKind(run: CodeTaskRun): string {
  const t = run.title ?? "";
  if (/^Follow-up:\s*deferred review findings/i.test(t)) return "follow-up";
  const retry = t.match(/·\s*retry (\d+)\s*$/i);
  if (retry) return `retry ${retry[1]}`;
  const fctx = (run.finalize_ctx ?? {}) as Record<string, unknown>;
  if (fctx.is_followup) return "follow-up";
  if (typeof fctx.attempt === "number" && fctx.attempt > 1) return `attempt ${fctx.attempt}`;
  return "initial run";
}

/** Meeting titles are whole directives; a card shows the lead, not the document. */
function leadLine(raw: string): string {
  const text = (raw || "").replace(/^PRD meeting:\s*/, "").replace(/\s+/g, " ").trim();
  if (text.length <= 84) return text;
  const stop = text.slice(0, 110).search(/[.:;]\s/);
  if (stop > 20) return text.slice(0, stop);
  const cut = text.lastIndexOf(" ", 81);
  return `${text.slice(0, cut > 20 ? cut : 81)}…`;
}


function isInternalRun(run: { title?: string }): boolean {
  return INTERNAL_RUN.test((run.title ?? "").trim());
}

export function CodeTasksHome({ projectId, codeTasks, meetings = [], apiReachable, previews = [], previewBlockedReason, benchmarks = [], actor = "executive_console", githubPlane, githubPlaneLabel, githubFullName }: {
  projectId: string;
  orgId?: string;
  codeTasks: CodeTaskRun[];
  meetings?: { id: string; title?: string; status?: string; created_at?: string; task_id?: string | null }[];
  apiReachable: boolean;
  previews?: PreviewRec[];
  previewBlockedReason?: string;
  benchmarks?: QaBenchmarkApp[];
  actor?: string;
  githubPlane?: string | null;
  githubPlaneLabel?: string | null;
  githubFullName?: string | null;
}) {
  // The request's query string, so the FIRST render already carries the org.
  const search = useSearchParams()?.toString();
  const [selected, setSelected] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Which app card has its spec panel open, and in which mode. KEYED BY RUN ID: the cards are
  // a .map(), so a single boolean here would open the panel on every app at once.
  const [specPanel, setSpecPanel] = useState<Record<string, "feature" | "rebuild" | undefined>>({});

  /**
   * Open the Ask BOSS dock for ONE app, pre-loaded with what the card already knows.
   *
   * Was an inline handler on a button in the action row; extracted unchanged when that row
   * became a menu. The facts matter: the console renders them inches away, so the agent should
   * not have to rediscover the repo, branch or preview it is being asked about.
   */
  /**
   * The run BOSS should actually deploy, and the preview that proves one exists.
   *
   * The context block named ONE run -- whichever card was clicked. Asked to "deploy Living
   * Colors to live", BOSS saw status "cancelled (implement)", concluded there was no build, and
   * offered to re-run it. Told "the Open preview works fine, why don't you check the last
   * successful", it looked at the newest run again and reported an infra failure. Both answers
   * were faithful to the one run it was given.
   *
   * An app is a LINEAGE, not a run. The card already knows every run in that lineage and
   * whether a preview is serving, so the deployable one is stated here rather than left for the
   * agent to go and infer.
   */
  const deployableFacts = useCallback((m: CodeTaskRun, lineage?: CodeTaskRun[]): string[] => {
    const group = (lineage?.length ? lineage : [m]).filter(Boolean);
    const done = (r: CodeTaskRun) => ["completed", "succeeded", "passed"].includes(String(r.status ?? ""));
    // Newest first: the card list is already ordered, but do not depend on it.
    const shipped = group.filter(done).sort((a, b) =>
      String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")));
    const live = group.map((r) => livePreviewFor(r.id)).find(Boolean) ?? null;
    const facts: string[] = [];
    if (group.length > 1) facts.push(`runs_in_this_app: ${group.length} (this app is a lineage, not one run)`);
    if (shipped.length) {
      facts.push(
        `latest_successful_run: ${shipped[0].id} (status ${shipped[0].status})`,
        "DEPLOY THIS ONE. Do not judge deployability from the run named above if it failed or " +
        "was cancelled — a later or earlier run in the same app may have succeeded.",
      );
    } else {
      facts.push("latest_successful_run: none in this app");
    }
    if (live) facts.push(`live_preview: ${live} (a preview is serving, so a build exists)`);
    return facts;
  }, [previews]);

  const askBossAbout = useCallback((m: CodeTaskRun, lineage?: CodeTaskRun[]) => {
    const rootRunId = (m.finalize_ctx as Record<string, unknown> | undefined)?.root_run_id;
    const pr = (m as unknown as { pull_request_url?: string }).pull_request_url;
    const facts = [
      `[app context] You are talking about ONE app in BOSS.`,
      `run_id: ${m.id}`,
      `app: ${appIdentity(m).name}`,
      `repo: ${m.spec?.owner ?? "?"}/${m.spec?.repo ?? "?"}`,
      `branch: ${rootRunId ? `boss/code/${rootRunId}` : `boss/code/${m.id}`}`,
      `status: ${m.status ?? "?"}${m.phase ? ` (${m.phase})` : ""}`,
      pr ? `pull_request: ${pr}` : "pull_request: none",
      ...deployableFacts(m, lineage),
      // This line used to end "...with run_id X when you need more", which reads as an
      // instruction to STAY on that run. It is why "no, use the latest working one" changed
      // nothing: the block outranked the operator. The run is now a starting point, and the
      // operator's correction is explicitly above it.
      `Start from run_id ${m.id} (inspect_app, diagnose_preview, diagnose_deployment), but it is`
      + ` a STARTING POINT, not a restriction — the other runs above are part of the same app.`,
      `If the operator points you somewhere else ("use the last successful one"), DO THAT. Their`
      + ` correction outranks this block. Never repeat a conclusion they have just rejected.`,
    ].join("\n");
    try {
      window.sessionStorage.setItem(`boss.agent.facts.${m.id}`, facts);
    } catch { /* private mode: the dock still works, with less context */ }
    const url = new URL(window.location.href);
    url.searchParams.set("agent", m.id);
    window.history.pushState({}, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, []);

  // The gateway URL for a run's live preview, or null. Built from the preview ID, never from
  // app_previews.url — that column holds the sandbox-internal address (127.0.0.1:PORT), which
  // is reachable from the API process and from nowhere a browser runs.
  const livePreviewFor = (runId: string): string | null => {
    const p = previews.find(
      (x) => x.run_id === runId && (x.status === "running" || x.status === "starting"),
    );
    return p ? `${PREVIEW_API}/v1/previews/${encodeURIComponent(p.id)}/web/` : null;
  };

  // The panel owns its list.
  //
  // It was rendered from a server prop and refreshed with `router.refresh()`, which re-renders
  // on the server while the client keeps the old rows — the same defect that left deleted
  // deployment targets on screen. So a relaunch said "started — it is at the top of this list"
  // and the list did not move, which is exactly the situation that makes someone press the
  // button again.
  // What each card has launched, keyed by the card's own run id. Held here because the list
  // re-renders after a launch and any state inside a card is lost with it.
  const [launched, setLaunched] = useState<
    Record<string, { run_id?: string; error?: string; kind?: "meeting" | "code" | "existing" }>
  >({});
  const [tasks, setTasks] = useState<CodeTaskRun[]>(codeTasks);
  useEffect(() => { setTasks(codeTasks); }, [codeTasks]);
  const noteLaunch = useCallback(async (sourceId: string, newId?: string, error?: string,
                                        kind?: "meeting" | "code" | "existing") => {
    setLaunched((m) => ({ ...m, [sourceId]: { run_id: newId, error, kind } }));
    await reloadRef.current?.();
  }, []);

  // Opening a run the list has not caught up with yet must still work: fetch first, then
  // select. Selecting an id that is absent silently renders the list again, which reads as
  // the button doing nothing — the failure this whole sequence keeps producing.
  const openRun = useCallback(async (runId: string) => {
    await reloadRef.current?.();
    setSelected(runId);
  }, []);

  const reload = useCallback(async () => {
    const fresh = await listCodeTasksAction(projectId);
    if (fresh && !fresh.error && Array.isArray(fresh.code_tasks)) setTasks(fresh.code_tasks);
  }, [projectId]);
  // The callback is created before `reload` exists, so it reaches it through a ref rather
  // than forcing the two into one declaration order that reads backwards.
  const reloadRef = useRef<null | (() => Promise<void>)>(null);
  useEffect(() => { reloadRef.current = reload; }, [reload]);

  // Poll while anything is live so phases advance on their own. Reads from the panel's own
  // state, so a run started a moment ago keeps the poller alive rather than needing the
  // server prop to catch up first.
  const anyLive = tasks.some((c) => c.status === "running" || c.status === "queued");
  useLiveRefresh(anyLive, 4000, reload);

  if (!apiReachable) {
    return <p className="detail">Code tasks unavailable — API unreachable.</p>;
  }

  const previewListError = previewBlockedReason ? (
    <p className="detail" role="status" data-preview-list="error">
      Preview list unavailable — {previewBlockedReason}. View links cannot be offered until the list loads.
    </p>
  ) : null;

  const active = selected ? tasks.find((c) => c.id === selected) : null;
  if (active) {
    return (
      <>
        {previewListError}
        <CodeTaskDetail run={active} previews={previews} onBack={() => setSelected(null)} />
      </>
    );
  }

  const lastRepo = tasks[0]?.spec;

  return (
    <div className="ct">
      {previewListError}
      <div className="ct-home-bar">
        <span className="ct-home-count">{tasks.length} code task{tasks.length === 1 ? "" : "s"}</span>
        {githubPlane ? (
          <span className="detail" data-testid="code-tasks-github-plane">
            {githubPlane === "mission_control_binding"
              ? `GitHub: Mission Control binding${githubFullName ? ` (${githubFullName})` : ""}`
              : githubPlane === "legacy_env"
                ? `GitHub: settings token${githubFullName ? ` (${githubFullName})` : ""} — connect in GitHub Mission Control for the project binding`
                : githubPlaneLabel || `GitHub: ${githubPlane}`}
          </span>
        ) : null}
        <div className="ct-home-actions">
          <button className="cap-btn primary" onClick={() => setShowNew((v) => !v)}>
            {showNew ? "Close" : "＋ New code task"}
          </button>
        </div>
      </div>

      {showNew ? (
        <NewCodeTaskForm projectId={projectId} defaultOwner={lastRepo?.owner} defaultRepo={lastRepo?.repo} />
      ) : null}

      <QaBenchmarkCatalog
        benchmarks={benchmarks}
        builtBenchmarkIds={new Set(
          codeTasks
            .map((c) => ((c.finalize_ctx ?? {}) as Record<string, unknown>).qa_benchmark as
              { id?: string } | undefined)
            .map((b) => b?.id)
            .filter((id): id is string => Boolean(id)),
        )}
        projectId={projectId}
        actor={actor}
        launched={launched}
        onLaunched={noteLaunch}
        onOpen={(id) => void openRun(id)}
        defaultOwner={lastRepo?.owner}
        defaultRepo={lastRepo?.repo}
      />

      {tasks.length === 0 ? (
        <p className="detail">No code tasks yet. Start one — the agent clones the repo, edits files, runs your tests, and opens a gated PR.</p>
      ) : (
        // THE grid. Meetings lay their cards out three-across at minmax(320px, 1fr); mine
        // stacked one per row, which is why the same CSS class still read as a full-width row
        // and the spine stretched across two thousand pixels. The class was right; the
        // container was not.
        <div className="mr-grid ct-tiles">
          {/* PANELS THAT ARE STILL SITTING.
              A meeting produces no code run until its PRD is approved, so for the whole time a
              panel is debating -- minutes, sometimes longer -- Factory showed nothing and the
              work looked lost. These are that gap, rendered as first-class cards in the same
              grid, and they disappear on their own once the build they authorise starts. */}
          {meetings
            .filter((m) => m.status === "running" || m.status === "queued" || m.status === "convened")
            .map((m) => (
              <article key={m.id} className="mr-meeting-card">
                <div className="mr-meeting-card-head">
                  <span className="cap-badge tone-amber">panel sitting</span>
                  {m.created_at ? <ClientRelativeTime iso={m.created_at} /> : null}
                </div>
                <span className="mr-meeting-title">{leadLine(m.title ?? "Untitled panel")}</span>
                <p className="detail" style={{ margin: "4px 0 0" }}>
                  Debating the PRD. The build starts on approval — nothing to preview yet.
                </p>
                <JourneySpine
                  run={{ id: m.id, status: "running", phase: "spec",
                         finalize_ctx: { origin: { kind: "meeting", task_id: m.task_id ?? undefined } } }}
                />
                <div className="mr-meeting-foot">
                  <a className="cap-btn primary"
                     href={consoleHref({ area: "meetings", meeting: m.id }, search)}
                     onClick={(e) => e.stopPropagation()}>
                    Watch the panel →
                  </a>
                </div>
              </article>
            ))}

          {(() => {
            // Render one run as a card. `label` is the row prefix (a number, or "n/N" for a slice).
            const renderCard = (c: CodeTaskRun, label: string) => {
              const st = c.state ?? {};
              const failing = (c.error ?? "").match(/failing gates: (\[[^\]]*\])/)?.[1];
              let outcome: string;
              if (c.status === "completed") {
                const gp = [
                  st.tests_passed ? "tests ✓" : null,
                  st.adversarial_verdict === "survived" ? "review ✓" : null,
                  st.security_verdict === "clean" ? "security ✓" : null,
                  st.mutation_caught ? "mutation ✓" : null,
                ].filter(Boolean).join(" · ");
                outcome = gp || "all gates passed";
              } else if (c.status === "blocked" && c.phase === "awaiting_gate_override") {
                outcome = `⚖ gate blocked — ${failing ? `failing: ${failing}` : "needs override/retry"}`;
              } else if (c.status === "failed" || c.status === "blocked") {
                outcome = failing ? `✗ failing: ${failing}` : (c.error ?? c.status).slice(0, 80);
              } else {
                outcome = `running — ${PHASE_LABEL[c.phase] ?? c.phase}`;
              }
              // An <article> rather than a <button>: the card now contains a disclosure, and a
              // <details> inside a <button> is invalid markup whose clicks fight each other.
              // The header keeps the click and the keyboard affordance the button had.
              const openTask = () => setSelected(c.id);
              return (
                // The Meeting card, literally: same class, same CSS, same hover lift. Asked
                // many times for "the Meeting card look", I kept approximating it with classes
                // of my own; the Meeting cards already had the treatment, so the honest answer
                // was to USE it rather than build something adjacent and hope it read the same.
                <article key={c.id} className="mr-meeting-card">
                  <div
                    className="mr-meeting-card-head"
                    role="button"
                    tabIndex={0}
                    onClick={openTask}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTask(); } }}
                  >
                    <span className={`cap-badge tone-${statusTone(c.status)}`}>{c.status}</span>
                    <span className="mr-meeting-date">{label}</span>
                  </div>
                  <span className="mr-meeting-title" role="button" tabIndex={0}
                        onClick={openTask}
                        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); openTask(); } }}>
                    {c.title || "(untitled task)"}
                  </span>
                  <div className="oc-sub">{outcome}</div>
                  {/* The spine, at the size it was always meant to be: the centre of gravity of
                      the card, not a strip added to it. The acceptance criteria and test
                      commands that used to sit here moved to the task view — this face carries
                      only what you need to decide whether to open it. */}
                  <div className="oc-spine">
                    <JourneySpine run={c as never} previewUrl={livePreviewFor(c.id)} large />
                  </div>
                  <div className="oc-actions">
                    <LaunchTask run={c} projectId={projectId} onLaunched={noteLaunch}
                                     onOpen={(id) => void openRun(id)}
                                     startedId={launched[c.id]?.run_id}
                                     failed={launched[c.id]?.error}
                                     label="Re-run with the same spec" />
                    <ChangeThisApp run={c} onStarted={noteLaunch}
                                   onOpen={(id) => void openRun(id)}
                                   startedId={launched[`chg:${c.id}`]?.run_id} />
                  </div>
                  {/* Meta as PILLS, the way the Meeting cards do it — the treatment that
                      already reads well here. Plus the fact that was missing entirely: WHEN.
                      A list of tasks with no times gives no sense of what is recent, so
                      everything looks equally current and nothing looks stale. */}
                  <div className="mr-meeting-foot">
                    {/* Rendered only when the field is actually there. The list endpoint does
                        NOT return created_at/updated_at — I added this fed by a field that does
                        not exist, which put `undefined` into a client component, crashed
                        hydration, and dropped every card in the browser while the server HTML
                        still contained them. */}
                    {(c.updated_at ?? c.created_at) ? (
                      <ClientRelativeTime iso={(c.updated_at ?? c.created_at) as string}
                                          className="oc-when" />
                    ) : null}
                    {/* The panel that specced this, as a pill you can actually see and click.
                        It existed only as a spine node, which reads "none" on every historical
                        run — so in practice the link to a meeting was invisible. */}
                    {((c.finalize_ctx as Record<string, unknown> | undefined)?.origin as
                        { kind?: string; task_id?: string } | undefined)?.kind === "meeting" ? (
                      <a className="mr-meeting-pill accent"
                         // ?meeting= addresses the panel; ?task= only reached the meetings
                         // list, so this pill looked slow when it was simply wrong.
                         href={consoleHref({
                           area: "meetings",
                           meeting: ((c.finalize_ctx as Record<string, unknown>).origin as
                             { meeting_run_id?: string }).meeting_run_id,
                         }, search)}
                         onClick={(e) => e.stopPropagation()}
                         title="Open the panel that specced this app">
                        ◇ from a panel ↗
                      </a>
                    ) : null}
                    <span className="mr-meeting-pill">{c.spec?.owner}/{c.spec?.repo}</span>
                    {st.profile ? (
                      <span className="mr-meeting-pill accent">
                        {st.profile}{st.risk_tier != null ? ` · t${st.risk_tier}` : ""}
                      </span>
                    ) : null}
                    {c.pull_request_url ? (
                      <a className="mr-meeting-pill accent" href={c.pull_request_url} target="_blank"
                         rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                        PR #{c.pull_request_number} ↗
                      </a>
                    ) : null}
                    {(st.cost_usd_micros != null) ? (
                      <span className="mr-meeting-pill">{fmtUsd(st.cost_usd_micros)}</span>
                    ) : null}
                  </div>
                </article>
              );
            };
            // Group runs by lineage (finalize_ctx.root_run_id) so a decomposed build shows as ONE
            // collapsible bucket — one thing to look at, not N loose rows. Singletons render flat.
            const fc = (c: CodeTaskRun) => (c.finalize_ctx ?? {}) as Record<string, unknown>;
            const benchGroupTotal = (members: CodeTaskRun[],
                                     f: (c: CodeTaskRun) => Record<string, unknown>) =>
              (f(members[0]).decomposition_total as number) || members.length;
            // ONE RULE FOR "SAME APP", not a chain of fallbacks.
            //
            // This was a first-match chain (app_id || qa:bench || root_run_id || …) that got a
            // new clause each time a duplicate was reported. It fixed the case in front of me
            // every time and left the siblings split: ApproveIt still showed three times,
            // because each of its runs carried a DIFFERENT one of those fields and the chain
            // stops at the first. Identity is a relation, not a priority order -- see
            // app-grouping.ts, where any shared token joins two runs and a base_branch of
            // boss/code/<run> finally links a change to the app it changes.
            const appGroups = groupRunsIntoApps(tasks.filter((c) => !isInternalRun(c)));
            const order: string[] = appGroups.map((members) => members[0].id);
            const groups = new Map<string, CodeTaskRun[]>(
              appGroups.map((members) => [members[0].id, members]));
            const fcOf = fc;
            void fcOf;
            return order.map((key, gi) => {
              const members = groups.get(key)!;
              // Singletons used to render as a bare run card, so an app looked like one thing
              // until it had a second attempt and then became another. One shape for every app.
              // Decomposed build → one bucket. Aggregate status + one PR/approval for the whole thing.
              // A benchmark bucket counts ATTEMPTS; a decomposed build counts its planned
              // parts. Reusing "N tasks" for both would call three attempts a three-part build.
              const total = benchGroupTotal(members, fc);
              const doneN = members.filter((m) => m.status === "completed").length;
              // The app's BEST state, not its worst.
              //
              // Any failed attempt turned the whole card red, so an app that shipped a working
              // pull request on attempt two still read BLOCKED because attempt three failed.
              // That is how "View ApproveIt" came to point at a failure: the card described the
              // most recent mishap rather than what the operator actually has.
              //
              // Shipped beats running beats failed. A failure is still visible — it is on the
              // attempt inside, and named in the summary line.
              const anyShipped = members.some(
                (m) => m.status === "completed" || m.pull_request_url);
              const anyRunning = members.some(
                (m) => m.status === "running" || m.status === "queued");
              const anyBroken = members.some(
                (m) => m.status === "blocked" || m.status === "failed" || m.status === "cancelled");
              const groupStatus = anyShipped
                ? "completed" : anyRunning ? "running" : anyBroken ? "blocked" : "running";
              const prRun = members.find((m) => m.pull_request_url);
              // THE RUN THE BUTTON ACTS ON MUST BE THE RUN THE BUTTON WAS ENABLED BY.
              // `anyShipped` asks whether ANY attempt produced something previewable, but the
              // action was wired to members[0] — the NEWEST attempt. On an app whose latest
              // run is a blocked follow-up, that enabled "Run preview" on the strength of the
              // completed run and then fired it at the blocked one, which 409s with
              // "has not opened a pull request yet". Members are newest-first, so this is the
              // most recent attempt that actually has something to show.
              const builtRun = pickBuiltRun(members);
              const benchGroup = (fc(members[0]).qa_benchmark as { name?: string } | undefined);
              const name = benchGroup?.name
                ? `${benchGroup.name} — QA benchmark`
                : (members.find((m) => m.title)?.title || "Decomposed build")
                    .replace(/^\[\d+\/\d+\]\s*/, "");
              const open = expandedGroups.has(key);
              return (
                // The GROUPED card, on the same Meeting styling as the single one.
                //
                // Two card paths existed and I restyled only one, so a list that is mostly
                // groups still showed the old markup — the operator saw no change at all and
                // reasonably concluded the work had not been done. The identical mistake as
                // the fetch limit, the housekeeping filter and the preview URL earlier: fix
                // one path, miss its twin.
                <div key={key} className="mr-meeting-card" style={{ cursor: "default" }}>
                  <div
                    className="mr-meeting-card-head"
                    role="button"
                    onClick={() => setExpandedGroups((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; })}
                    style={{ cursor: "pointer" }}
                  >
                    {/* The kind of thing this is, in the badge the benchmark cards use. */}
                    {appIdentity(members[0]).kind ? (
                      <span className="cap-badge tone-blue">{appIdentity(members[0]).kind}</span>
                    ) : null}
                    <span className={`cap-badge tone-${statusTone(groupStatus)}`}>
                      {anyShipped ? "shipped" : anyRunning ? "building" : groupStatus}
                    </span>
                    {/* What the operator has, in words: how many attempts, and whether the
                        most recent one went wrong. Both facts at once, so a working app is not
                        hidden behind its latest failure. */}
                    <span className="mr-meeting-date">
                      {total} {total === 1 ? "attempt" : "attempts"}
                      {anyShipped && anyBroken ? " · last one failed" : ""}
                    </span>
                  </div>
                  <span className="mr-meeting-title" role="button"
                        onClick={() => setExpandedGroups((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; })}
                        style={{ cursor: "pointer" }}>
                    {open ? "▾" : "▸"} {benchGroup?.name ? name : appIdentity(members[0]).name}
                  </span>
                  {appIdentity(members[0]).blurb ? (
                    <p className="detail" style={{ margin: "2px 0 0" }}>
                      {appIdentity(members[0]).blurb}
                    </p>
                  ) : null}
                  {/* The spine describes the app, so it tracks the run that built it. On
                      members[0] the card contradicted itself: the badge read SHIPPED while
                      the spine underneath read "Build blocked · Ship not yet", because it was
                      narrating a blocked follow-up instead of the completed run behind PR #83.
                      "last one failed" in the header is what reports the newest attempt. */}
                  {/* Origin comes from the GROUP: the run being narrated is often a follow-up,
                      which records none of its own. See appOrigin. */}
                  <JourneySpine
                    run={{
                      ...builtRun,
                      finalize_ctx: {
                        ...(builtRun.finalize_ctx ?? {}),
                        ...(appOrigin(members) ? { origin: appOrigin(members) } : {}),
                      },
                    } as never}
                    previewUrl={livePreviewFor(builtRun.id)}
                    large
                  />
                  {/* ONE action row. These were two block-level children of a grid, so they
                      stacked full-width and the weaker of the two sat centred above the
                      stronger -- the change control read as a caption rather than a choice.
                      Side by side, "change it" and "build it again" are what they are: two
                      things you can do to this app, ranked. */}
                  {/* Ranked by what someone actually wants: see it, change it, and only then
                      rebuild it. "Run this build again" was the primary control on every card
                      including working apps -- the most expensive action in the most prominent
                      slot. */}
                  <div className="ct-actions">
                    <AppPrimaryAction
                      run={builtRun}
                      previewUrl={livePreviewFor(builtRun.id)}
                      previewStarting={previews.some(
                        (x) => x.run_id === builtRun.id && x.status === "starting")}
                      lastPreview={latestPreviewFor(previews, builtRun.id)}
                      shipped={anyShipped}
                      running={anyRunning}
                    />
                    {/* Everything that is not "see the app" lives in one menu.
                        Five same-weight buttons meant the common action competed with four
                        rare ones. "Request a change" is gone on purpose: it and the feature
                        panel did the same job from the operator's side, and the panel is the
                        one that writes a spec first. */}
                    <ActionMenu
                      label="More actions for this app"
                      items={[
                        {
                          id: "ask",
                          label: "Ask BOSS",
                          hint: "reads the run, repo and preview",
                          icon: <ChatCircleDots size={15} weight="fill" />,
                          onSelect: () => askBossAbout(members[0], members),
                        },
                        {
                          id: "feature",
                          label: "Add a feature",
                          hint: "specs it first, then builds onto this app",
                          icon: <Sparkle size={15} weight="fill" />,
                          onSelect: () => setSpecPanel((m) => ({ ...m, [members[0].id]: "feature" })),
                        },
                        {
                          id: "rebuild",
                          label: "Rebuild with a fresh spec",
                          hint: "replaces this app — writes a new PRD",
                          icon: <Sparkle size={15} weight="fill" />,
                          onSelect: () => setSpecPanel((m) => ({ ...m, [members[0].id]: "rebuild" })),
                        },
                      ]}
                    />
                    {/* The panel the menu opens. Controlled, so the menu owns the trigger. */}
                    {/* Shipped state, where the change was approved. */}
                    {/* Deploying ships a BUILT artifact, so it takes the same run preview
                        does. Pointed at members[0] it offered to deploy a blocked follow-up
                        that has no reviewed build, and "Set target" answered "this task is
                        'blocked' — there is no reviewed build to deploy" for an app whose
                        completed run was sitting right there under PR #83. */}
                    <DeploymentLine run={builtRun} projectId={projectId}
                                    deployable={anyShipped && !anyRunning} />
                    {specPanel[members[0].id] ? (
                      <RebuildWithSpec run={members[0]} projectId={projectId} actor={actor}
                                       mode={specPanel[members[0].id]} open
                                       onOpenChange={(o) => {
                                         if (!o) setSpecPanel((m) => ({ ...m, [members[0].id]: undefined }));
                                       }} />
                    ) : null}
                    {/* Re-running the IDENTICAL brief is not a card-level action.
                        The only honest uses for it are holding the spec constant on purpose --
                        comparing two runs of a benchmark, or re-running after a platform
                        failure where nothing about the product was learned. Both are things
                        you do deliberately from a specific attempt, never because the app in
                        front of you looks wrong. Offered from the card it reads as the obvious
                        remedy for a bad result, which is the one thing it cannot fix: the same
                        brief is what produced it. It stays on each attempt inside the card. */}
                  </div>
                  <div className="ct-card-foot">
                    <span className="detail">{members[0].spec?.owner}/{members[0].spec?.repo}</span>
                    {/* What this app COST, summed across every run of it.
                        The Apps area existed largely to answer this and was otherwise a second
                        view of the same feeds; deleting it would have quietly taken the one
                        number it alone carried. Now the card that groups the runs also totals
                        them. */}
                    {(() => {
                      const spend = members.reduce(
                        (n, m) => n + (m.usage?.cost_usd_micros ?? 0), 0);
                      return spend > 0 ? (
                        <span className="detail"
                              title="Total model spend across every run of this app">
                          {fmtUsd(spend)} spent
                        </span>
                      ) : null;
                    })()}
                    {prRun?.pull_request_url ? <span className="ct-card-pr">PR #{prRun.pull_request_number}</span>
                      : <span className="detail">one approval when the build finishes</span>}
                  </div>
                  {open ? (
                    <div style={{ display: "grid", gap: 8, marginTop: 6, paddingLeft: 10, borderLeft: "2px solid var(--border)" }}>
                      {/* Named by KIND. "•/5" told you an attempt existed and nothing about
                          what it was; "follow-up" and "retry 1" tell the story of the app. */}
                      <span className="detail">
                        {members.length === 1
                          ? "The run that built it"
                          : `${members.length} runs on this app, newest first`}
                      </span>
                      {members.map((m) => renderCard(m, attemptKind(m)))}
                    </div>
                  ) : null}
                </div>
              );
            });
          })()}
        </div>
      )}
    </div>
  );
}
