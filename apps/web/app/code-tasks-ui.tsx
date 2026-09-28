"use client";

import { useSearchParams } from "next/navigation";
import { PROJECT_ID } from "../lib/client";

import { consoleHref } from "./console-href";
import { HostAdviceBanner, useHostAdvice } from "./host-advice";
import { onChanged, notifyChanged } from "./changed";
import { appIdentity, groupIdentity } from "./app-identity";

/**
 * Did this event come from something the operator meant to operate?
 *
 * The app card is a `role="button"` wrapping the whole tile, and the tile contains real
 * controls: a host dropdown, a name field, "Set target", "Run preview". A click on any of them
 * bubbled to the card and navigated into the detail page instead — pressing "Set target" left
 * the page rather than setting the target.
 *
 * The keyboard case was worse and silent: the card also answers Enter AND SPACE, so typing a
 * space into the app-name field navigated away mid-word.
 *
 * Testing the ORIGIN rather than stopping propagation on each control means a control added to
 * this card later is covered without anyone remembering to opt it out.
 */
export function fromInteractiveControl(event: React.SyntheticEvent): boolean {
  const target = event.target as HTMLElement | null;
  const hit = target?.closest?.("button, a, input, select, textarea, label, option");
  return !!hit && hit !== event.currentTarget;
}
import { groupRunsIntoApps } from "./app-grouping";

import { useActionState, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { CodeTaskRun, LlmCallRow, PreviewRec, QaBenchmarkApp, QaBenchmarkReport } from "@boss/api-client";
import { forgetRunDeployment, loadRunDeployment } from "../lib/run-deployments";
import { operatorPreviewOpenHref } from "../lib/code-tasks";
import { ApprovePreview } from "./previews-ui";
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
} from "./actions";
import { LiveDeployment } from "./deploy-ui";
import { Markdown } from "./markdown";
import { ClientRelativeTime } from "./client-relative-time";
import { JourneySpine } from "./journey-spine-ui";
import {
  ArrowSquareOut, ArrowsClockwise, ChatCircleDots, DotsThree, GitPullRequest,
  ListMagnifyingGlass, PencilSimple, PlayCircle, RocketLaunch, Sparkle, TerminalWindow,
} from "@phosphor-icons/react";

import { SubmitButton } from "./submit-button";

function RetryControls(
  { runId, failingGates = [], compact = false }:
  { runId: string; failingGates?: string[]; compact?: boolean },
) {
  const [state, action, pending] = useActionState(retryCodeTaskWithStateAction, null);
  // Which stages the retry will SKIP, mirroring _repair_needs_write_tests on the server, so the
  // operator knows this resumes rather than restarting. Kept in sync with _TEST_GATES.
  const TEST_GATES = ["tests_strong_enough", "tests_written_first", "coder_did_not_edit_tests",
                      "written_tests_pass", "existing_tests_intact"];
  const reusesTests = failingGates.length > 0 && !failingGates.some((g) => TEST_GATES.includes(g));
  const explains = "Resumes on the same branch and reuses the approved plan"
    + (reusesTests ? " and the existing test suite" : "")
    + " — it does not rebuild from scratch.";

  // COMPACT: for the action bar at the top, where the sentence beside it would push everything
  // after it off the edge. What it says still has to be knowable before the click, so it moves
  // into the tooltip.
  if (compact) {
    return (
      <form action={action} style={{ display: "inline-flex" }} title={explains}>
        <input type="hidden" name="run_id" value={runId} />
        <SubmitButton className="cap-btn" disabled={pending}>
          <ArrowsClockwise size={15} weight="bold" aria-hidden />
          {pending ? "Retrying…" : "Retry build"}
        </SubmitButton>
      </form>
    );
  }

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
  // WAITING AND BROKEN ARE NOT THE SAME STATE.
  //
  // This panel had one: "Waiting for preview output…", shown whenever `log` was empty. So a
  // stream that failed on connect looked identical to one that was about to deliver, and it
  // said "waiting" forever. `onerror` closed the connection without a word, which is how a
  // control that could never work looked like one still trying.
  const [state, setState] = useState<"connecting" | "streaming" | "ended" | "failed">("connecting");
  useEffect(() => {
    if (!open) return;
    setState("connecting");
    // THROUGH THE SERVER, not straight at the API.
    //
    // This pointed an EventSource at ${PREVIEW_API} directly. The browser cannot reach the API
    // in this deployment — every other call on the page is server-side — and the API requires a
    // Bearer key, which EventSource cannot send. Both problems disappear behind a same-origin
    // route handler that holds the key.
    const source = new EventSource(
      `/api/preview-logs?preview_id=${encodeURIComponent(previewId)}`,
    );
    source.addEventListener("open", () => setState("streaming"));
    source.addEventListener("log", (event) => {
      setState("streaming");
      try { setLog((current) => (current + JSON.parse((event as MessageEvent).data)).slice(-12000)); }
      catch { setLog((current) => (current + (event as MessageEvent).data).slice(-12000)); }
    });
    source.addEventListener("end", () => { setState("ended"); source.close(); });
    source.onerror = () => {
      // EventSource reconnects on a transient drop, and reports the same event for a permanent
      // failure. CLOSED means it has given up; anything else may still recover, and calling
      // that "failed" would flap the panel on every hiccup.
      if (source.readyState === EventSource.CLOSED) setState("failed");
      else setState((s) => (s === "streaming" ? "streaming" : "connecting"));
    };
    return () => source.close();
  }, [open, previewId]);

  const placeholder =
    state === "failed"
      ? "Lost the log stream for this preview. It may have expired, or the preview service is"
        + " unreachable — reopen this panel to try again."
      : state === "ended"
        ? "The preview finished writing its startup output, and it produced none."
        : "Waiting for preview output…";
  return (
    <span>
      <button type="button" className="cap-btn" onClick={() => setOpen((value) => !value)}>
        <TerminalWindow size={15} weight="bold" aria-hidden />
        {open ? "Hide startup logs" : "Startup logs"}
      </button>
      {open ? (
        <pre data-testid="preview-log" data-state={state}
             style={{ width: "min(720px, 86vw)", maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap", marginTop: 8 }}>
          {log || placeholder}
        </pre>
      ) : null}
    </span>
  );
}

function PreviewControls({ runId, livePreviews = [] }: { runId: string; livePreviews?: PreviewRec[] }) {
  const [state, action, pending] = useActionState(startPreviewAction, null);
  const hasLive = livePreviews.some((p) => p.status === "running" && operatorPreviewOpenHref(p));
  // Defensive as well as at the call site: a second identical "Open preview" is never useful,
  // whoever passes the list.
  const shown = livePreviews.slice(0, 1);
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      {/* Only offer "Run preview" (a heavy npm-install+build) when NO live preview exists yet.
          Once one is running, the primary action is Open preview; a fresh build is demoted to a
          subtle "Rebuild" link so we don't spawn duplicate builds that starve the api. */}
      {!hasLive ? (
        <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
          <input type="hidden" name="run_id" value={runId} />
          <SubmitButton className="cap-btn" disabled={pending}>
            <PlayCircle size={15} weight="fill" aria-hidden />
            {pending ? "Starting preview…" : "Run preview"}
          </SubmitButton>
        </form>
      ) : null}
      {/* Persistent link(s) from the durable preview record — survive the console's auto-refresh */}
      {shown.map((p) => {
        const href = operatorPreviewOpenHref(p);
        return (
        <span key={p.id} style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          {p.status === "starting" ? (
            // Nothing to open yet. A link that 404s is worse than no link.
            <span className="cap-btn" aria-disabled data-testid="preview-building">
              <ArrowsClockwise size={15} weight="bold" aria-hidden /> Building preview…
            </span>
          ) : href ? (
            <a className="cap-btn primary" href={href.startsWith("http") ? href : `${PREVIEW_API}${href}`} target="_blank" rel="noreferrer">
              <ArrowSquareOut size={15} weight="bold" aria-hidden />
              {p.status === "verified" ? "View verified preview" : "Open preview"}
            </a>
          ) : (
            <span className="detail" data-preview-view="missing-url">
              {p.status === "verified"
                ? "verified-test — no operator URL (not running; View would 409)"
                : `preview ${p.status} — no operator URL`}
            </span>
          )}
          {/* STARTING IS A STATE, NOT AN ABSENCE.
              A preview now exists from the moment the button is pressed, and its ledger says
              which substrate is being tried and how far it got — the same "what is done, what
              is pending" a deploy shows. Without this the operator watches a spinner and, when
              it fails, cannot tell whether one substrate was tried or three. */}
          {p.status === "starting" && (p.steps?.length ?? 0) > 0 ? (
            <details className="detail ct-preview-steps" data-testid="preview-steps">
              <summary style={{ cursor: "pointer" }}>
                {p.steps!.filter((x) => x.status === "ok").length}/{p.steps!.length} steps
              </summary>
              <ul style={{ margin: "6px 0 0", paddingLeft: 16 }}>
                {p.steps!.map((x, i) => (
                  <li key={i}>
                    {x.status === "ok" ? "✓" : x.status === "failed" ? "✗"
                      : x.status === "skipped" ? "–" : "…"}{" "}
                    {x.label || x.step}
                    {x.duration_ms ? ` · ${Math.round(x.duration_ms / 1000)}s` : ""}
                    {x.log ? <span className="detail"> — {x.log.slice(0, 120)}</span> : null}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
          <span className="cap-badge tone-blue ct-live-pill"
                data-live={p.status === "running" || p.status === "starting"}>
            {p.status === "running" || p.status === "starting"
              ? <i className="ct-live-dot" aria-hidden /> : null}
            preview: {p.status === "starting" ? "building…" : p.status}
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
            <ArrowsClockwise size={14} weight="bold" aria-hidden
                             className={pending ? "spin" : undefined} />
            {pending ? "Rebuilding…" : "Rebuild preview"}
          </SubmitButton>
        </form>
      ) : null}
      {/* Freshly-started link (shows instantly, before the record is re-fetched) */}
      {state?.url && livePreviews.length === 0 ? (
        <a className="cap-btn primary" href={state.url} target="_blank" rel="noreferrer">
          <ArrowSquareOut size={15} weight="bold" aria-hidden /> Open preview
        </a>
      ) : null}
      {/* Library/CLI build: no live URL — show the test-run result (passed) instead of an error. */}
      {state?.note ? (
        <details open className="detail" style={{ color: "#3fb950", width: "100%" }}>
          <summary style={{ cursor: "pointer" }}>✓ Ran tests — passed (library build, no live app)</summary>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, marginTop: 6, maxHeight: 320, overflow: "auto" }}>{state.note}</pre>
        </details>
      ) : null}
      {state?.error ? (() => {
        // A CLIENT TIMEOUT IS NOT A VERDICT.
        //
        // Starting a preview runs npm install, a build and a launch SYNCHRONOUSLY, and that
        // regularly outlasts the client's 120s deadline. The reply says so in its own words —
        // "It may still be working" — and it was being rendered under "⚠ Preview / tests
        // failed", which states the opposite. The operator is told the preview failed while
        // the preview is still building, and a minute later it is running.
        const stillWorking = /did not answer within/i.test(state.error);
        return (
          <details open className="detail"
                   style={{ color: stillWorking ? "#d29922" : "#f85149", width: "100%" }}
                   data-testid={stillWorking ? "preview-still-working" : "preview-failed"}>
            <summary style={{ cursor: "pointer" }}>
              {stillWorking
                ? "⏳ Still building — this took longer than the console waits"
                : "⚠ Preview / tests failed"}
            </summary>
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, marginTop: 6, maxHeight: 320, overflow: "auto" }}>{state.error}</pre>
          </details>
        );
      })() : null}
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

/**
 * WHAT SOMEONE ADDS TO A PROJECT, as starting points rather than a blank box.
 *
 * The panel was an empty textarea whose placeholder was the only guidance, and the first thing
 * anyone does with an app they have just built is add to it. These are the shapes that
 * request usually takes; picking one fills the fields in and leaves them editable.
 *
 * Deliberately a plain list: adding a template is one entry here, no other file touched.
 */
const FEATURE_TEMPLATES: Array<{
  key: string; label: string; hint: string; message: string; expected: string;
}> = [
  {
    key: "page",
    label: "A new screen",
    hint: "a page or view users navigate to",
    message: "Add a new page at /<route> that shows ",
    expected: "the page loads, is reachable from the nav, and renders real data from the API",
  },
  {
    key: "api",
    label: "An API endpoint",
    hint: "server route + storage",
    message: "Add an API endpoint that ",
    expected: "the endpoint returns the right shape, persists through the database, and is covered by a test",
  },
  {
    key: "data",
    label: "Store something new",
    hint: "a migration and the routes for it",
    message: "Persist ",
    expected: "the data survives a reload and a restart, through a migration rather than local state",
  },
  {
    key: "auth",
    label: "Accounts & access",
    hint: "sign-in, roles, permissions",
    message: "Add accounts so that ",
    expected: "a signed-out visitor cannot reach protected pages, and each user sees only their own data",
  },
  {
    key: "design",
    label: "Design & polish",
    hint: "layout, styling, responsiveness",
    message: "Improve the look of ",
    expected: "it reads clearly on a phone and a laptop, and matches the rest of the app",
  },
  {
    key: "bug",
    label: "Something is wrong",
    hint: "fix behaviour that is broken",
    message: "Fix: ",
    expected: "the described behaviour no longer happens, and a test proves it",
  },
];

// Feedback is conversational and confirmation-gated. The agent clarifies vague
// requests, structures the fix, classifies its scope, then reuses the same branch/PR.
function FeedbackControls({ runId, status, anchor = "", projectId }: {
  runId: string; status: string; anchor?: string; projectId: string;
}) {
  const completed = status === "completed";
  const [state, action, pending] = useActionState(submitCodeTaskFeedbackAction, null);
  const workflows = useProjectWorkflows(projectId);
  const [picked, setPicked] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [expected, setExpected] = useState("");

  const useTemplate = (t: typeof FEATURE_TEMPLATES[number]) => {
    setPicked(t.key);
    setMessage(t.message);
    setExpected(t.expected);
  };

  return (
    <div className="ct-iterate">
      <header className="ct-iterate-head">
        <h4>{completed ? "Add to this project" : "Diagnose and retry"}</h4>
        <p className="detail">
          {completed
            ? "Builds on the existing branch and pull request — the agent keeps the context it already has."
            : "Prior findings are carried forward automatically; nothing is rebuilt from scratch."}
        </p>
      </header>

      {completed ? (
        <div className="ct-templates" data-testid="ct-templates">
          {FEATURE_TEMPLATES.map((t) => (
            <button key={t.key} type="button" className="ct-template"
                    data-picked={picked === t.key}
                    onClick={() => useTemplate(t)}>
              <span className="ct-template-label">{t.label}</span>
              <span className="ct-template-hint">{t.hint}</span>
            </button>
          ))}
        </div>
      ) : null}

      <form action={action} className="ct-iterate-form">
        <input type="hidden" name="run_id" value={runId} />
        <input type="hidden" name="anchor" value={anchor} />
        <label className="mr-field-label">
          {completed ? "What should it do?" : "What went wrong?"}
          <textarea name="message" className="textarea" rows={3} required
            value={message} onChange={(e) => setMessage(e.target.value)}
            placeholder={completed
              ? "Describe the behaviour to add or change. The agent will clarify and propose a scoped extension."
              : "Describe what failed or should change."} />
        </label>
        <div className="ct-iterate-row">
          <label className="mr-field-label">
            Done when
            <input name="expected" className="input" value={expected}
                   onChange={(e) => setExpected(e.target.value)}
                   placeholder="How you will know it worked" />
          </label>
          {/* WHICH PATH THIS CHANGE FOLLOWS. Adding a feature is a piece of work like any
              other, and it deserves the same choice the New project form offers — a small
              addition through Minor Feature, something larger through a workflow with a spec
              and a sign-off in front of it. */}
          <label className="mr-field-label">
            Workflow
            <select className="mr-select" name="workflow_version_id"
                    data-testid="ct-iterate-workflow" defaultValue="">
              <option value="">Follow the inner pipeline only</option>
              {workflows.filter((w) => w.active_version_id).map((w) => (
                <option key={w.id} value={w.active_version_id as string}>
                  {w.name}{w.is_catalogue ? " (standard)" : ""}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="ct-iterate-actions">
          <SubmitButton className="cap-btn primary" disabled={pending}>
            {pending ? "Reviewing…" : completed ? "Propose the change" : "Propose retry"}
          </SubmitButton>
          <span className="detail">
            The agent scopes it, you confirm, then it runs on the same branch and PR.
          </span>
        </div>
      </form>

      {state?.question ? (
        <p className="ct-iterate-note" data-tone="ask">Agent question: {state.question}</p>
      ) : null}
      {state?.proposal ? (
        <div className="ct-proposal">
          <span className="cap-badge tone-blue">{state.proposal.classification.replace("_", " ")}</span>
          <pre>{state.proposal.fix_note}</pre>
          {state.thread_id ? <ConfirmFeedback threadId={state.thread_id} /> : null}
        </div>
      ) : null}
      {state?.error ? <p className="ct-iterate-note" data-tone="bad">{state.error}</p> : null}
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

/**
 * The workflows this project can be built with.
 *
 * Fetched here rather than threaded down as a prop: this form is the only thing that needs
 * them, and the Factory page is already 9.6 MB of server-rendered HTML — adding a list of 21
 * definitions to every render of it, for a form most visits never open, would be the wrong
 * trade.
 */
function useProjectWorkflows(projectId: string) {
  const [items, setItems] = useState<Array<{ id: string; name: string;
    active_version_id?: string | null; is_catalogue?: boolean }>>([]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(
          `/api/delivery-workflows?view=installed&project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (!res.ok) return;
        const body = await res.json();
        if (alive) setItems(body.items ?? []);
      } catch { /* the form still works; the picker just says nothing to choose */ }
    })();
    return () => { alive = false; };
  }, [projectId]);
  return items;
}

/** Where a project is started now: the page that shows what happens after you press it. */
function startAProjectHref(): string {
  const now = new URLSearchParams(
    typeof window !== "undefined" ? window.location.search : "");
  const next = new URLSearchParams();
  for (const key of ["org", "org_id"]) {
    const value = now.get(key);
    if (value) next.set(key, value);
  }
  next.set("area", "delivery_workflows");
  next.set("new", "1");
  return `?${next.toString()}`;
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
  // ONLY WHEN THERE IS NOTHING TO SHOW YET.
  //
  // This checked `running` FIRST, while the badge above it applies the opposite precedence —
  // "shipped beats running beats failed". An app with three attempts, one shipped and a
  // follow-up building, therefore rendered the badge SHIPPED and the button "Building…" at the
  // same time, and disabled the only useful control on a working app because a LATER attempt
  // was in flight. Both facts were true; the card just disagreed with itself.
  //
  // Shipped wins here too. A build in progress is real information, but it belongs beside the
  // action, not in place of it.
  if (running && !shipped) {
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
    // A REASON THAT WRAPS INSIDE ITS OWN CARD.
    //
    // These were inline spans in a non-wrapping form, so a 200-character build error ran
    // straight out of the card and across the task beside it — two failures overlapping into
    // one unreadable line, which is the state an operator is in precisely when they most need
    // to read it. The form wraps now, and the reason is a block that breaks anywhere and is
    // clamped; the full text is on `title` for a hover.
    <form action={action} onClick={(e) => e.stopPropagation()} className="preview-form">
      <input type="hidden" name="run_id" value={run.id} />
      <SubmitButton className="cap-btn primary" pendingLabel="Starting preview…">
        <PlayCircle size={15} weight="fill" aria-hidden />
        {failed ? " Retry preview" : " Run preview"}
      </SubmitButton>
      {state?.error ? <span className="detail preview-note">{state.error}</span> : null}
      {state?.note ? <span className="detail preview-note">{state.note}</span> : null}
      {failed && !state?.error ? (
        <span className="detail preview-note" data-testid="preview-failed"
              title={`Last preview failed${failed.failure_kind ? ` (${failed.failure_kind})` : ""}`
                     + (failed.error ? `: ${failed.error}` : "")}>
          Last preview failed{failed.failure_kind ? ` (${failed.failure_kind})` : ""}
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

function SetupDeployment({ run, projectId, actor, onDone }: {
  run: CodeTaskRun; projectId: string; actor: string; onDone: () => void;
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
            setBusy(true); setMsg(null);
            const r = await appDeployOp({
              op: "set-target",
              actor,
              run_id: run.id,
              host_id: fd.get("host_id"),
              app_name: fd.get("app_name"),
              app_port: fd.get("app_port"),
            });
            setBusy(false);
            setMsg(r.ok
              ? `target set: ${r.data?.host ?? "host"} — deploy it once below`
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
/**
 * Card deployment actions, via a route handler rather than a Server Action.
 *
 * "Set target" showed "Saving…" for minutes. The write takes milliseconds; the wait was the
 * REPLY — a Server Action re-renders the current route to respond, and setupDeploymentAction
 * additionally called revalidatePath("/"), on an area with a measured TTFB of ~115 seconds.
 */
async function appDeployOp(body: Record<string, unknown>): Promise<{ ok: boolean; data?: any; error?: string }> {
  try {
    const r = await fetch("/api/app-deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const d = await r.json().catch(() => ({}));
    // TELL THE REST OF THE CONSOLE. Setting a target here changes the same rows the Workflows
    // screen renders, and that screen had no way to learn it.
    if (r.ok) notifyChanged("factory", { op: String(body.op ?? "") });
    return r.ok ? { ok: true, data: d } : { ok: false, error: d?.error ?? `HTTP ${r.status}` };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

async function retireTarget(targetId: string, actor: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await fetch(
      `/api/deployment-targets?target_id=${encodeURIComponent(targetId)}`
        + `&actor=${encodeURIComponent(actor)}`,
      { method: "DELETE", cache: "no-store" },
    );
    const d = await r.json().catch(() => ({}));
    return r.ok ? { ok: true } : { ok: false, error: d?.error ?? `HTTP ${r.status}` };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

function DeploymentLine({ run, projectId, actor, deployable }: {
  run: CodeTaskRun; projectId: string; actor: string;
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
  // Whether the binding has been READ yet. Without this, `targetId` is null for the first
  // render and the card would flash "not bound" at every app on load.
  const [loaded, setLoaded] = useState(false);

  // A BATCHED ROUTE HANDLER, NOT A PER-CARD SERVER ACTION.
  //
  // This was `runDeploymentsAction(run.id, ...)`, called from every card's mount effect. A
  // Server Action re-renders the whole route to reply, so a list of forty apps cost forty
  // renders of a page that takes tens of seconds to render. Measured on the live Factory area
  // with no interaction at all: 82 invocations of this one action, 64.5s of action time, and
  // the page was not quiet for 74 seconds.
  //
  // Same reasoning as loadHostsOnce above; this is the per-card path that was missed, because
  // one extra call per card looks cheap right up until there are forty cards.
  const load = useCallback(async () => {
    const d = await loadRunDeployment(run.id, appIdentity(run).name);
    // `error` means nobody answered, which is NOT the same as "this app has no deployments".
    // Treating them alike is what once showed a deployed app as never deployed, so a failed
    // lookup leaves the previous state and `loaded` false rather than asserting absence.
    if (d.error) return;
    setDeploys((d.deployments ?? []) as any[]);
    setLinked(d.linked_host ?? null);
    setTargetId(d.linked_target_id ?? null);
    setLoaded(true);
  }, [run]);

  // Polling must see the world as it is now, not the batch window's 2.5s-old answer.
  const reload = useCallback(async () => {
    forgetRunDeployment(run.id);
    await load();
  }, [run.id, load]);

  useEffect(() => { void load(); }, [load]);

  // AND WHEN THE CHANGE WAS MADE SOMEWHERE ELSE.
  //
  // This card shows the app's target and its deployments — exactly the rows the Workflows
  // screen and BOSS also change. Setting a target from the workflow left this card showing the
  // old answer until its own poll happened to run, or for ever once the card stopped polling.
  useEffect(() => onChanged(() => { void reload(); }, "factory"), [reload]);

  // Poll only while something is actually moving. A card that polls forever is a card that
  // keeps a browser and an API busy describing a deployment that finished yesterday.
  const latest = deploys[0];
  const live = latest && ["created", "running", "in_progress", "deploying"].includes(latest.status);
  // The deployment row now reports its own state, so the optimistic latch can stand down.
  useEffect(() => { if (latest) setStarting(false); }, [latest]);
  useEffect(() => {
    if (!live) return;
    const t = window.setInterval(() => { void reload(); }, 4000);
    return () => window.clearInterval(t);
  }, [live, reload]);

  // No deployment yet. That is either "no destination" (offer to link one) or "linked but
  // never shipped" (offer the first deploy). Rendering nothing here is what left an already
  // merged app with no way forward on its own card.
  // Nothing to ship, or still shipping it: the row would be an empty promise on a card that
  // already says BLOCKED or BUILDING two inches above.
  if (!deployable && !latest) return null;

  // UNBOUND IS ABOUT THE BINDING, NOT THE HISTORY.
  //
  // This asked `if (!latest)` — no deployment runs — so an app that had ever deployed stayed
  // on the deployment row forever. After unbinding, the card therefore still showed "DEPLOY
  // FAILED · hetzner-ci-01" with a Retry button aimed at a target that no longer exists, and
  // the unbind looked like it had done nothing. It had: the target was retired.
  //
  // Deployment runs are permanent history and a retired target is current state; the card has
  // to follow the state and show the history as history.
  if (!latest || (loaded && !targetId)) {
    return (
      <div className="ct-deploy-line" data-testid="app-deployment-none">
        {latest && loaded && !targetId ? (
          <span className="detail ct-deploy-history" title={latest.failure_reason ?? undefined}>
            last deploy: {latest.status}
            {latest.finished_at ? <> · <ClientRelativeTime iso={latest.finished_at} /></> : null}
          </span>
        ) : null}
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
                      const r = await appDeployOp({
                        op: "deploy",
                        actor,
                        project_id: projectId,
                        run_id: run.id,
                        target_id: targetId ?? "",
                      });
                      setNote(r.ok ? null : (r.error ?? "deploy failed"));
                      if (r.ok) setStarting(true);
                      setRetrying(false);
                      await reload();
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
                      const r = await retireTarget(targetId, actor);
                      setNote(r.ok ? "target retired — pick a new one below" : (r.error ?? "could not retire"));
                      setRetrying(false);
                      await reload();
                    }}>
              Unbind target
            </button>
            {note ? <span className="detail">{note}</span> : null}
          </>
        ) : (
          <SetupDeployment run={run} projectId={projectId} actor={actor} onDone={load} />
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
                    const r = await appDeployOp({ op: "retry", actor, deployment_run_id: latest.id });
                    setNote(r.ok ? "retry started" : (r.error ?? "retry failed"));
                    setRetrying(false);
                    await reload();
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
                    const r = await retireTarget(String(tid), actor);
                    setNote(r.ok ? "target retired — set a new one to deploy again"
                                 : (r.error ?? "could not retire"));
                    setRetrying(false);
                    await reload();
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
        These are pinned product contracts, not canned demos. Launching one creates a strict governed project with a durable plan, evidence bundle, and final verification report.
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
//: Which stages belong to which pipeline phase.
//
// This is the join the first version of this table got wrong. It deduped by comparing a phase
// name to a stage name -- but those are the two different vocabularies that made the old
// screens look duplicated in the first place, so nothing matched and every piece of work was
// listed TWICE: once as "Engineer implementing 16m 42s" and once as "solo 16m 42s", with the
// total counting both.
const PHASE_STAGES: Record<string, string[]> = {
  provisioning: [],
  queued: [],
  design: ["standards", "spec", "plan", "review_plan", "design"],
  write_tests: ["write_tests"],
  implement: ["implement", "solo", "repair", "guest"],
  run_tests: ["check_tests", "run_all_tests"],
  review_panel: ["review_code", "security_check", "consistency_check"],
  check_tests: ["check_tests"],
};

//: Which agent owns a stage. Mirrors llm_telemetry.STAGE_ROLES on the engine side.
//
// Used as a FALLBACK when the API does not send a role — older deployments do not, and an
// "Agent" column full of dashes is worse than no column. The row prefers whatever the API
// says; this fills the gap rather than overriding it.
const STAGE_AGENT: Record<string, string> = {
  standards: "chief_architect", spec: "chief_architect", plan: "chief_architect",
  review_plan: "chief_architect", design: "chief_architect",
  write_tests: "qa", check_tests: "qa", run_all_tests: "qa",
  implement: "software_engineer", repair: "software_engineer",
  solo: "software_engineer", guest: "software_engineer",
  review_code: "adversarial_reviewer", consistency_check: "adversarial_reviewer",
  security_check: "security_reviewer",
};

/** How an agent key reads to a person. The API sends the key; nobody wants to read one. */
const AGENT_ACRONYMS: Record<string, string> = {
  qa: "QA", cto: "CTO", coo: "COO", cao: "CAO", ux: "UX", vp: "VP", pm: "PM", sre: "SRE",
};

function agentLabel(role: string | null | undefined): string {
  if (!role) return "—";
  // Title Case, except the words that are acronyms. "Qa" and "Cto" read as typos.
  return role
    .split("_")
    .map((w) => AGENT_ACRONYMS[w.toLowerCase()] ?? (w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

/**
 * What ran, who ran it, and what it cost — in ONE table.
 *
 * This replaces two blocks that listed the same steps in different vocabularies: "Step timing"
 * named phases (Engineer implementing, Running tests) and "agent activity" named stages (solo,
 * review_code). Same work, two names, stacked one above the other, so answering "what took 16
 * minutes and who was doing it" meant joining them by eye.
 *
 * Phases with no model call still appear — cloning a repo takes time and no agent — because a
 * timing table that silently drops the slow non-model steps mis-attributes the wall clock.
 */
function CodeTaskSteps({ runId, live, phaseTimings, currentPhase, currentPhaseMs, totalMs,
                        redelivered }: {
  runId: string;
  live: boolean;
  phaseTimings: { ms: number; phase: string }[];
  currentPhase: string | null;
  currentPhaseMs: number | null;
  totalMs: number;
  /** This run was re-delivered: an earlier attempt lost its lease and its work was replayed. */
  redelivered: boolean;
}) {
  const hostAdvice = useHostAdvice(PROJECT_ID);
  const [calls, setCalls] = useState<LlmCallRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const data = await getCodeTaskActivityAction(runId);
      if (!cancelled) { setCalls(data); setLoaded(true); }
    };
    void load();
    const id = live ? setInterval(load, 4000) : undefined;
    return () => { cancelled = true; if (id) clearInterval(id); };
  }, [runId, live]);

  // Phases carry the WALL CLOCK; calls carry who did it and what it cost. Nesting the calls
  // under their phase is what makes this one table instead of two lists of the same work.
  const used = new Set<number>();
  const groups = phaseTimings.map((t, i) => {
    const stages = PHASE_STAGES[t.phase] ?? [t.phase];
    const mine = calls
      .map((c, i) => ({ c, i }))
      .filter(({ c, i }) => !used.has(i) && stages.includes(c.stage));
    mine.forEach(({ i }) => used.add(i));
    // A phase that appears AGAIN after an earlier copy of itself, on a run the worker
    // re-delivered, is the interrupted attempt being redone — not new work.
    const replayed = redelivered && phaseTimings.slice(0, i).some((p) => p.phase === t.phase);
    return { phase: t.phase, ms: t.ms, calls: mine.map((x) => x.c), replayed };
  });
  // Calls whose stage no phase claimed still belong on the screen — dropping them would hide
  // spend, and hidden spend is the worst thing this table could do.
  const orphans = calls.filter((_, i) => !used.has(i));
  const spend = calls.reduce((n, c) => n + c.cost_usd_micros, 0) / 1e6;
  const longest = Math.max(1, ...phaseTimings.map((t) => t.ms));

  const CallRow = ({ c }: { c: LlmCallRow }) => (
    <div className={`ct-call-row${c.ok ? "" : " err"}`} data-testid={`ct-call-${c.stage}`}>
      <span className="ct-call-stage">{c.stage}</span>
      <span className="ct-call-agent">{agentLabel(c.role ?? STAGE_AGENT[c.stage])}</span>
      <span className="ct-call-model">{c.model || c.engine}</span>
      <span className="ct-call-num">{c.turns ? `${c.turns} turns` : ""}</span>
      <span className="ct-call-num">{c.cost_usd_micros ? `$${(c.cost_usd_micros / 1e6).toFixed(3)}` : ""}</span>
      {!c.ok ? <span className="ct-call-err">✗ {(c.error ?? "error").slice(0, 70)}</span> : null}
    </div>
  );

  return (
    <div className="ct-block ct-steps" data-testid="ct-steps">
      {/* WHY THIS BUILD IS SLOW, WHERE ITS TIMINGS ARE READ. The stage durations below are
          what get read as the pipeline being slow; when the machine took that time it has to
          say so in the same block. Only while the build is live, and only on an unhealthy
          host — it renders nothing otherwise. */}
      {live ? <HostAdviceBanner advice={hostAdvice} /> : null}
      <div className="ct-steps-head">
        <span className="ct-block-title">Steps — who ran what</span>
        <span className="ct-steps-total">
          <b>{fmtDuration(totalMs)}</b>{live ? " so far" : ""}
          {spend > 0 ? <> · <b>${spend.toFixed(2)}</b></> : null}
        </span>
      </div>

      <div className="ct-phases">
        {groups.map((g, i) => (
          <div className="ct-phase" key={i} data-testid={`ct-phase-${g.phase}`}>
            <div className="ct-phase-head">
              <span className="ct-phase-name">{PHASE_LABEL[g.phase] ?? g.phase}</span>
              {/* Duration as a bar as well as a number: it is the only way to see at a glance
                  that one step took half the run. */}
              <span className="ct-phase-bar" aria-hidden>
                <span style={{ width: `${Math.max(2, Math.round((g.ms / longest) * 100))}%` }} />
              </span>
              <span className="ct-phase-ms">{fmtDuration(g.ms)}</span>
            </div>
            {g.calls.length > 0 ? (
              <div className="ct-phase-calls">{g.calls.map((c, j) => <CallRow c={c} key={j} />)}</div>
            ) : loaded ? (
              // A REPLAYED PHASE IS NOT MACHINERY. When the run was re-delivered, the same
              // phase appears twice and the second copy finds no unclaimed call to show — so
              // this said "no model call — BOSS's own machinery" about thirty minutes of work
              // that was thrown away when a lease expired. Saying which it is turns an
              // inexplicable duration into a legible one.
              <div className="ct-phase-nocall" data-testid={g.replayed ? "ct-phase-replayed" : undefined}>
                {g.replayed
                  ? "restarted — the previous attempt was interrupted and its work was lost"
                  : "no model call — BOSS's own machinery"}
              </div>
            ) : null}
          </div>
        ))}

        {live && currentPhase && currentPhaseMs != null ? (
          <div className="ct-phase ct-phase-live" data-testid="ct-step-running">
            <div className="ct-phase-head">
              <span className="ct-phase-name">● {currentPhase}</span>
              <span className="ct-phase-bar" aria-hidden><span style={{ width: "100%" }} /></span>
              <span className="ct-phase-ms">{fmtDuration(currentPhaseMs)}</span>
            </div>
          </div>
        ) : null}

        {orphans.length > 0 ? (
          <div className="ct-phase" data-testid="ct-phase-other">
            <div className="ct-phase-head">
              <span className="ct-phase-name">Other calls</span>
              <span className="ct-phase-bar" aria-hidden />
              <span className="ct-phase-ms" />
            </div>
            <div className="ct-phase-calls">{orphans.map((c, j) => <CallRow c={c} key={j} />)}</div>
          </div>
        ) : null}
      </div>

      {loaded && calls.length === 0 ? (
        <p className="detail" style={{ marginTop: 8 }}>
          No model calls recorded for this run yet.
        </p>
      ) : null}
    </div>
  );
}

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

/**
 * Which workflow governs this project.
 *
 * The workflow runtime and the coding pipeline were built as separate systems: the console
 * showed 54 workflow definitions and a list of runs, with nothing on either screen saying that
 * they had anything to do with each other. The engine now resolves the governing workflow when
 * a run is created and records it, so this reads a fact rather than guessing one.
 *
 * SAYS "not linked" RATHER THAN NOTHING. A project with no mapping is the normal state today,
 * and an absent line is indistinguishable from a line that failed to load — which is the exact
 * ambiguity that made the rest of this console hard to trust.
 */
function WorkflowLink({ run }: { run: CodeTaskRun }) {
  const wf = (run.finalize_ctx as Record<string, unknown> | undefined)?.workflow as
    { name?: string; version?: number; workflow_key?: string; source?: string } | undefined;
  if (!wf?.name) {
    return (
      <span className="detail ct-workflow" data-testid="ct-workflow-none"
            title="No workflow is mapped to this project yet">
        no workflow linked
      </span>
    );
  }
  return (
    <span className="detail ct-workflow" data-testid="ct-workflow"
          title={wf.source === "default"
            ? "Nobody has mapped a workflow to this project, so it is on the standard one"
            : "Resolved from this project's workflow mapping when the run was created"}>
      workflow: <b>{wf.name}</b>{typeof wf.version === "number" ? ` v${wf.version}` : ""}
      {/* "Deliberately put on this workflow" and "got the standard one because nothing was
          chosen" are different facts, and only one of them is a decision. */}
      {wf.source === "default" ? <span className="ct-workflow-default"> (default)</span> : null}
    </span>
  );
}

function CodeTaskDetail({ run, previews = [] }: { run: CodeTaskRun; previews?: PreviewRec[] }) {
  const st = run.state ?? {};
  const tests = structuredCodeTaskTests(st);
  // Operator View: running first, then verified-test with an open_url.
  // verified-test without a URL is still shown so we can be honest (no invented View).
  const livePreviews = previews
    .filter((p) => p.run_id === run.id
      && (p.status === "running" || p.status === "verified" || p.status === "starting"))
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
      {/* No back button here. It said "← All code tasks" while the one above said "← All
          apps", and both called setSelected(null) — two names for one action, on one screen.
          The surviving control sits at the top of the split, outside the dockable pane. */}
      {/* NO BACK BUTTON HERE EITHER, and this is the third place it has been.
          It sat on this row — the RUN's header — while what it leaves is the APP. So it landed
          under the app's action bar, beside the run title, reading as a control on the run, and
          an operator had to scan past five buttons to find the way out. A back control belongs
          at the TOP-LEFT of the pane it leaves, above the title of the thing being shown; it is
          now on the app heading's eyebrow line, which already existed and was nearly empty. */}
      <div className="ct-detail-head">
        <h3>{run.title}</h3>
        <span className={`cap-badge tone-${statusTone(run.status)}`}>{run.status}</span>
        <WorkflowLink run={run} />
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

      {/* ONE TABLE.
          "Step timing" and "agent activity" listed the same steps twice, in two vocabularies —
          phase names above, stage names below — so the reader had to join them by eye to answer
          "what took 16 minutes, and who was doing it". They are one thing: a step, the agent
          that owns it, the model it used, and what it cost. */}
      <CodeTaskSteps runId={run.id} live={live} phaseTimings={phaseTimings}
                     redelivered={Array.isArray(_fc.redelivered_attempts)
                       && (_fc.redelivered_attempts as unknown[]).length > 0}
                     currentPhase={live ? (PHASE_LABEL[run.phase] ?? run.phase) : null}
                     currentPhaseMs={currentPhaseMs} totalMs={totalMs} />

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
        <div className="ct-block-title">Iterate</div>
        {/* PREVIEW AND RETRY USED TO BE REPEATED HERE.
            Both are in the action bar at the top of this page, so this block rendered a second
            "Open preview" and a second "Retry" for the same run — two controls that do the same
            thing, in two places, and the operator has to work out whether they differ. They do
            not. What is left here is the work that cannot be a button: describing a change and
            proposing it, overriding a finding, and the feedback that needs a form.
            Preview needs no explanation of its absence either: the bar above says "no PR yet"
            in the same place the preview controls would otherwise be. */}
        {/* Separate row: iterate on the same task with feedback */}
        {(run.status === "completed" || run.status === "failed" || run.status === "blocked") ? (
          <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid rgba(255,255,255,0.09)" }}>
            <FeedbackControls
              runId={run.id}
              status={run.status}
              projectId={run.project_id}
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

export function CodeTasksHome({ projectId, codeTasks, codeTaskTotal, meetings = [], apiReachable, previews = [], previewBlockedReason, benchmarks = [], actor = "executive_console", githubPlane, githubPlaneLabel, githubFullName }: {
  projectId: string;
  orgId?: string;
  codeTasks: CodeTaskRun[];
  /** How many exist, against how many arrived. Defaults to what arrived, so a caller that
   *  does not know the total never makes the screen claim tasks are missing. */
  codeTaskTotal?: number;
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
  // OPENED FROM THE HEADER STRIP.
  //
  // Selection is component state, so a project chosen in the strip had nowhere to land — the
  // strip could switch to Factory and then pointed at the whole list. `?run=` is the handoff,
  // read on mount and on every client-side navigation, which is how the rest of this console
  // moves (pushState + a popstate event, never a real navigation: this tree takes seconds to
  // paint).
  useEffect(() => {
    const read = () => {
      const run = new URLSearchParams(window.location.search).get("run");
      // Only ever SETS. Clearing on an absent param would fight the back button and snap the
      // operator out of a run they opened by clicking a card.
      if (run) setSelected(run);
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  // The run list inside an app docks away when the detail is what you are reading. There is no
  // app-level selection state: clicking an app opens its newest built run, and the page you
  // land on owns which run is showing.
  const [listDocked, setListDocked] = useState(false);
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

  /**
   * What to ask BOSS about THIS app, derived from what BOSS recorded about it.
   *
   * The dock offered four fixed prompts — "What changed today?", "Which runs failed and why?"
   * — which are the same four whatever you are looking at, so they never referred to the app
   * in front of you. Everything needed to do better is already on the run: the checks that did
   * not run, the findings graded below the blocking threshold and deferred, whether the tests
   * survived a mutation, the acceptance criteria, the files touched.
   *
   * Each suggestion names a real gap in THIS build. Nothing here is invented: a suggestion
   * only appears when the fact behind it is recorded.
   */
  const suggestionsFor = useCallback((m: CodeTaskRun, lineage?: CodeTaskRun[]): string[] => {
    const st = (m.state ?? {}) as Record<string, unknown>;
    const fc = (m.finalize_ctx ?? {}) as Record<string, unknown>;
    const app = appIdentity(m).name;
    const out: string[] = [];

    // Findings a reviewer raised and the pipeline shipped anyway — literally the bugs it knows
    // it left behind.
    const deferred = fc.deferred_findings ?? st.deferred_findings;
    if (Array.isArray(deferred) && deferred.length) {
      out.push(`Fix the ${deferred.length} deferred review finding${deferred.length === 1 ? "" : "s"} on ${app}`);
    }

    // Checks that never ran. At risk tier 0 no reviewer looks at the code at all.
    const advisory = fc.advisory_failing_gates;
    const notRun = [
      st.adversarial_verdict === "not_run" ? "adversarial review" : null,
      st.security_verdict === "not_run" ? "security review" : null,
      st.consistency_verdict === "not_run" ? "consistency check" : null,
    ].filter(Boolean) as string[];
    if (notRun.length) {
      out.push(`No ${notRun.join(", ")} ran on this build — what would they most likely find?`);
    } else if (Array.isArray(advisory) && advisory.length) {
      out.push(`${advisory.length} checks failed but shipped as advisory — which should block next time?`);
    }

    // The tests passed and did not notice a deliberately broken build.
    if (st.mutation_caught === false) {
      const n = Array.isArray(st.impl_files) ? (st.impl_files as unknown[]).length : 0;
      out.push(`The tests missed an injected bug${n ? ` across ${n} changed files` : ""} — where are they weakest?`);
    }

    // The contract it was accepted against.
    const criteria = (m.spec?.acceptance_criteria ?? []) as string[];
    if (criteria.length) {
      out.push(`Which of the ${criteria.length} acceptance criteria are not actually covered by a test?`);
    }

    // A failure worth explaining, from anywhere in the lineage.
    const broken = (lineage ?? [m]).find((r) => r.error);
    if (broken?.error) {
      out.push(`Why did ${attemptKind(broken)} fail — ${String(broken.error).slice(0, 60)}?`);
    }

    // And the forward-looking one, grounded in what this app actually is.
    out.push(`Given the brief and review notes for ${app}, what should we build next, and why?`);
    return out.slice(0, 5);
  }, []);

  /**
   * What this app IS, from what the pipeline wrote down: the brief it was built from, the
   * contract it was accepted against, what the implementer said it did, and what the reviewers
   * found. Bounded, because this rides on every message.
   */

  const askBossAbout = useCallback((m: CodeTaskRun, lineage?: CodeTaskRun[]) => {
    // NO FACTS BLOCK.
    //
    // This used to build ~20 lines of run context and stash it in sessionStorage so the chat
    // could prepend it to whatever the operator typed. They could see it — their question
    // buried under a wall of text they did not write — and it still failed the questions it
    // existed to answer, because a summary is not a PRD and the agent had no tool to fetch
    // the real one. The run id now travels as `external_ref` alone; the server resolves it
    // into the system prompt and the agent calls read_spec for the actual document.
    void lineage;
    try {
      window.sessionStorage.setItem(
        `boss.agent.suggests.${m.id}`, JSON.stringify(suggestionsFor(m, lineage)));
    } catch { /* private mode: the dock still works, with generic prompts */ }
    const url = new URL(window.location.href);
    url.searchParams.set("agent", m.id);
    // AND SAY WHICH APP THE PAGE IS ON, or a stale `?run=` will hijack it.
    //
    // The dock is opened by pushing a param and firing a SYNTHETIC popstate so the shell
    // notices. That event is a broadcast: every listener wakes, including this file's own
    // `?run=` reader, which "only ever SETS" so as not to fight the back button. Selection is
    // component state, so clicking a card never updated the URL — the `?run=` still in it
    // belongs to whatever app was opened before. Pressing Ask BOSS therefore re-applied that
    // old value and threw the operator into a different app, mid-question.
    //
    // Reported exactly that way: open "[latency-probe] add a formatBytes helper" (PR #125),
    // press Ask BOSS, and the page becomes "Database Schema Drift Detector" (PRs #114/#129).
    //
    // Writing the run we are asking ABOUT makes the URL describe one coherent state instead of
    // two contradictory ones, so the broadcast can only re-assert what is already true.
    url.searchParams.set("run", m.id);
    window.history.pushState({}, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, [suggestionsFor]);

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
  // A REFRESH MAY NEVER EMPTY A LIST THAT HAD CONTENT.
  //
  // This list has now vanished on the operator three separate ways: a fetch limit filled by
  // housekeeping, a poll that asked without the internal-run filter, and a server prop
  // arriving empty on re-render. Each had its own cause and each produced the same symptom —
  // tasks appear, then disappear a few seconds later — because in every case something
  // replaced a full list with an empty one.
  //
  // Fixing them one at a time has not worked, so the rule is enforced here instead: an empty
  // result never overwrites a non-empty list. Silence is not absence. If a project genuinely
  // has no tasks, the first render says so and nothing overwrites anything.
  const keepIfEmpty = useCallback((next: CodeTaskRun[]) => {
    setTasks((prev) => (next.length === 0 && prev.length > 0 ? prev : next));
  }, []);
  useEffect(() => { keepIfEmpty(codeTasks); }, [codeTasks, keepIfEmpty]);

  // Never below what is on screen: `tasks` can hold rows a later empty fetch chose not to
  // clobber, and "showing 12 of 8" would be worse than saying nothing.
  const shownTotal = Math.max(codeTaskTotal ?? tasks.length, tasks.length);

  // THE TAIL, ON DEMAND.
  //
  // The page renders the newest 200 server-side and that ceiling is deliberate: the feed is
  // 1.41 MB at 200, and the load that fetched more once took 111 seconds and starved the API's
  // healthcheck until Traefik evicted the container. So the rest is not fetched on every
  // render — it is fetched when somebody actually wants it, which is the only time it is worth
  // paying for.
  const [loadingAll, setLoadingAll] = useState(false);
  const [loadAllError, setLoadAllError] = useState<string | null>(null);
  const showAll = useCallback(async () => {
    setLoadingAll(true);
    setLoadAllError(null);
    try {
      const r = await fetch(
        `/api/code-tasks?project_id=${encodeURIComponent(projectId)}&limit=${shownTotal}`,
        { cache: "no-store" });
      const body = (await r.json()) as { code_tasks?: CodeTaskRun[]; error?: string };
      if (!r.ok) {
        setLoadAllError(body.error ?? `API returned ${r.status}`);
        return;
      }
      // Same rule as everywhere else on this screen: an empty result never replaces a list
      // that had content.
      keepIfEmpty(body.code_tasks ?? []);
    } catch (e) {
      setLoadAllError(String((e as Error)?.message ?? e));
    } finally {
      setLoadingAll(false);
    }
  }, [projectId, shownTotal, keepIfEmpty]);
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
    // A failed or empty refresh leaves what is on screen alone. The poll runs while a task is
    // live — exactly when someone is watching — so a blank frame here is the most visible
    // possible failure.
    if (fresh && !fresh.error && Array.isArray(fresh.code_tasks)) keepIfEmpty(fresh.code_tasks);
  }, [projectId, keepIfEmpty]);
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
    return <p className="detail">Projects unavailable — API unreachable.</p>;
  }

  const previewListError = previewBlockedReason ? (
    <p className="detail" role="status" data-preview-list="error">
      Preview list unavailable — {previewBlockedReason}. View links cannot be offered until the list loads.
    </p>
  ) : null;

  const active = selected ? tasks.find((c) => c.id === selected) : null;
  if (active) {
    // INSIDE AN APP: its runs on the left, the selected run's detail on the right.
    //
    // This is where the master-detail belongs. An earlier attempt put it on the Factory page,
    // which added a navigation layer IN FRONT of the app list instead of behind it — the list
    // is a list, and the split is what you find after clicking into one.
    //
    // Siblings are the other runs on the same app, so opening attempt 3 and then attempt 1 is
    // two clicks in one place rather than two round trips back to the list.
    const siblings = groupRunsIntoApps(tasks.filter((c) => !isInternalRun(c)))
      .find((m) => m.some((r) => r.id === active.id)) ?? [active];
    return (
      <>
        {previewListError}
        <div className="ct-split" data-docked={listDocked}>
          <aside className="ct-split-list">
            <div className="ct-split-listbar">
              <button type="button" className="cap-btn ct-dock-btn"
                      onClick={() => setListDocked((d) => !d)}
                      aria-expanded={!listDocked}
                      data-testid="ct-dock-toggle"
                      title={listDocked ? "Show the run list" : "Dock the run list"}>
                {listDocked ? "»" : "«"}
              </button>
            </div>
            <div className="ct-split-items" data-testid="ct-run-list">
              <span className="detail" style={{ padding: "0 2px" }}>
                {siblings.length === 1 ? "1 run" : `${siblings.length} runs, newest first`}
              </span>
              {siblings.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className="mr-meeting-card ct-app-item ct-run-item"
                  data-selected={m.id === active.id}
                  data-testid={`ct-run-item-${m.id}`}
                  onClick={() => setSelected(m.id)}
                >
                  <span className="mr-meeting-card-head">
                    <span className={`cap-badge tone-${statusTone(m.status)}`}>{m.status}</span>
                    <span className="mr-meeting-date">{attemptKind(m)}</span>
                  </span>
                  {/* WHAT THIS RUN WAS, not just that it existed. A column of identical status
                      chips tells you nothing about which attempt to open. */}
                  <span className="ct-run-item-title">
                    {(m.title || "Untitled run").replace(/^\[\d+\/\d+\]\s*/, "").slice(0, 70)}
                  </span>
                  <span className="detail ct-run-item-meta">
                    {m.pull_request_number ? `PR #${m.pull_request_number}` : "no PR"}
                    {(() => {
                      // fmtUsd TAKES MICROS. Dividing here divided it twice, so every run
                      // that cost less than a million dollars rendered "$0.00" — this build
                      // cost $1.55 and the card said nothing was spent.
                      const micros = Number((m.usage as Record<string, unknown> | undefined)?.cost_usd_micros ?? 0);
                      return micros ? ` · ${fmtUsd(micros)}` : "";
                    })()}
                  </span>
                </button>
              ))}
            </div>
          </aside>
          <section className="ct-split-detail">
            {/* THE ACTIONS, TOGETHER AND AT THE TOP.
                Preview, the PR and Ask BOSS are what an operator opens this page to do, and
                they were scattered down it — preview below the run list, the PR link inside a
                meta line, Ask BOSS only on the card back in the list. */}
            <div className="ct-detail-actionbar" data-testid="ct-detail-actionbar">
              <div className="ct-detail-heading">
                {/* THE WAY OUT, FIRST AND ABOVE THE TITLE.
                    Costs no vertical space — the eyebrow line was one short word — and it is
                    where a back control is looked for: top-left of the detail pane, above the
                    name of what you are looking at, outside the dockable list so docking cannot
                    take it away. Styled as a back control rather than another flat chip,
                    because it is the only thing in this header that NAVIGATES. */}
                <div className="ct-detail-eyebrow">
                  <button type="button" className="ct-back" onClick={() => setSelected(null)}
                          data-testid="ct-detail-back" title="Back to all projects">
                    <span aria-hidden>←</span> All projects
                  </button>
                  <span className="eyebrow">{appIdentity(active).kind || "App"}</span>
                </div>
                <h3>{appIdentity(active).name}</h3>
              </div>
              <div className="ct-detail-actions">
                {active.pull_request_url ? (
                  <a className="cap-btn" href={active.pull_request_url} target="_blank"
                     rel="noreferrer" data-testid="ct-detail-pr">
                    <GitPullRequest size={15} weight="bold" aria-hidden />
                    PR #{active.pull_request_number}
                  </a>
                ) : (
                  <span className="detail">no PR yet</span>
                )}
                {active.pull_request_url ? (
                  // The LIVE ones only. Passing the raw list rendered every expired preview
                  // this run ever had as a chip, and a run with fifteen of them pushed the
                  // action bar off the right of the screen. `livePreviews` is the filtered,
                  // capped list the detail view has always computed for exactly this reason —
                  // I passed the unfiltered one.
                  <PreviewControls
                    runId={active.id}
                    // ONE, NOT TWO. A run has one app, so two "Open preview" buttons side by
                    // side ask the operator to choose between things that are the same — and
                    // when a rebuild leaves the previous process running, that is exactly what
                    // appeared. The newest wins; `list_previews` returns newest first.
                    livePreviews={previews
                      .filter((p) => p.run_id === active.id
                        && (p.status === "running" || p.status === "verified"
                            || p.status === "starting"))
                      .slice(0, 1)}
                  />
                ) : null}
                {/* APPROVE & SHIP, BESIDE THE PR IT MERGES.
                    This was only ever in the separate "Previews" panel further down the page —
                    a second place to look for an action about the app already open, sitting
                    under a heading that called itself an operator fixture view. It renders
                    itself as nothing when there is no gate, says WHY when the gate refuses,
                    and states what the click will deploy before it is clicked. */}
                {(() => {
                  const live = previews.find(
                    (p) => p.run_id === active.id
                      && (p.status === "running" || p.status === "verified"));
                  return live ? <ApprovePreview preview={live} compact /> : null;
                })()}
                {/* RETRY, AT THE TOP WITH THE REST.
                    It was only in "Run it / iterate" further down the page — the same fault
                    the PR link and preview had before they were moved up: the thing you open
                    this page to do, somewhere you have to scroll to find. */}
                {active.status !== "running" && active.status !== "queued" ? (
                  <RetryControls runId={active.id} compact
                                 failingGates={(active.finalize_ctx?.advisory_failing_gates
                                   ?? active.finalize_ctx?.failing_gates
                                   ?? []) as string[]} />
                ) : null}
                {/* WHAT HAPPENED DURING THOSE 40 MINUTES.
                    A build runs for forty to ninety minutes and its longest phase used to be a
                    single row — "solo, 45 turns". This opens the log already scoped to THIS
                    run, so the question "what is it doing" is one click from the run rather
                    than a filter to reconstruct by hand. */}
                <button type="button" className="cap-btn"
                        onClick={() => {
                          const url = new URL(window.location.href);
                          url.searchParams.set("area", "activity");
                          url.searchParams.set("run", active.id);
                          window.history.pushState({}, "", url.toString());
                          window.dispatchEvent(new PopStateEvent("popstate"));
                        }}
                        data-testid="ct-detail-logs">
                  <ListMagnifyingGlass size={15} weight="bold" aria-hidden />
                  Activity log
                </button>
                <button type="button" className="cap-btn primary"
                        onClick={() => askBossAbout(active, siblings)}
                        data-testid="ct-detail-ask">
                  <ChatCircleDots size={15} weight="fill" aria-hidden />
                  Ask BOSS
                </button>
              </div>
            </div>
            <CodeTaskDetail run={active} previews={previews} />
          </section>
        </div>
      </>
    );
  }

  const lastRepo = tasks[0]?.spec;

  return (
    <div className="ct">
      {previewListError}
      <div className="ct-home-bar">
        <span className="ct-home-count">
          {tasks.length} project{tasks.length === 1 ? "" : "s"}
          {/* "200 code tasks" beside a project that has 244 is not a count, it is a window
              reported as a total. The operator's next thought is "where did the rest go", and
              nothing on the screen used to answer it. */}
          {shownTotal > tasks.length ? (
            <span className="ct-home-of" data-testid="ct-home-of">
              of {shownTotal} — showing the newest{" "}
              <button type="button" className="ct-home-all" onClick={() => void showAll()}
                      disabled={loadingAll} data-testid="ct-home-show-all">
                {loadingAll ? `Loading all ${shownTotal}…` : `Show all ${shownTotal}`}
              </button>
            </span>
          ) : null}
          {loadAllError ? (
            // The reason, not a shrug. A control that silently does nothing is worse than one
            // that says why it could not.
            <span className="ct-home-of ct-home-all-error" data-testid="ct-home-all-error">
              Could not load the rest — {loadAllError}
            </span>
          ) : null}
        </span>
        {githubPlane ? (
          <span className="detail" data-testid="code-tasks-github-plane">
            {githubPlane === "mission_control_binding"
              ? `GitHub: Mission Control binding${githubFullName ? ` (${githubFullName})` : ""}`
              : githubPlane === "legacy_env"
                ? `GitHub: settings token${githubFullName ? ` (${githubFullName})` : ""} — connect in GitHub Mission Control for the project binding`
                : githubPlaneLabel || `GitHub: ${githubPlane}`}
          </span>
        ) : null}
        {/* STARTING A PROJECT MOVED TO WORKFLOW, AND THIS STAYED — so there were two ways to
            do it, on two pages, one of which showed the steps that follow and one of which
            did not. Two entry points for one act is how an operator ends up unsure which of
            them is the real one. Factory is where the RESULTS live: the code, the pull
            request, the preview, and the panel for changing an app that already exists. */}
        <div className="ct-home-actions">
          <a className="cap-btn primary" href={startAProjectHref()}
             data-testid="ct-start-in-workflow">
            ＋ New project
          </a>
        </div>
      </div>

      {benchmarks.length > 0 ? <details className="ct-benchmark-disclosure">
      <summary>QA benchmarks <span>{benchmarks.length} examples</span></summary>
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
      </details> : null}

      {/* LOADING IS NOT ABSENCE.
          The list arrives after the page does, and an empty list rendered "No projects yet" —
          so during initialisation the console actively asserted there was nothing here, then
          filled in a moment later. The operator asked for an indicator during initialisation
          only, not a banner that pulses forever.

          `codeTaskTotal` is the server's own count, so the two states are distinguishable
          without guessing: it says twelve and we are holding none, therefore they are on the
          way. Once they arrive this disappears and does not come back. */}
      {tasks.length === 0 && (codeTaskTotal ?? 0) > 0 ? (
        <p className="detail" data-testid="ct-initialising">
          <span className="ct-loading-dot" aria-hidden>●</span>{" "}
          Loading {codeTaskTotal} project{codeTaskTotal === 1 ? "" : "s"}…
        </p>
      ) : tasks.length === 0 ? (
        <p className="detail">No projects yet. Start one — the agent clones the repo, edits files, runs your tests, and opens a gated PR.</p>
      ) : tasks.filter((c) => !isInternalRun(c)).length === 0 ? (
        // EVERYTHING IN THE WINDOW WAS HOUSEKEEPING.
        //
        // The grid rendered empty under a heading reading "200 code tasks" and said nothing,
        // so the operator's reasonable conclusion was that their apps had been deleted. They
        // had not — 200 of the 200 newest runs were "Captain: QUALITY IMPROVE PASS", and every
        // app they had built was older than the window. An empty grid beside a non-zero count
        // is a contradiction the screen has to explain rather than leave to be discovered.
        <p className="detail" data-testid="ct-all-internal">
          All {tasks.length} runs in this window are BOSS&apos;s own maintenance passes
          (&quot;Captain: …&quot;), so there is nothing here to show. Your apps still exist —
          they are older than this window.
        </p>
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

            const items = order.map((key, gi) => {
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
              // Nothing on this page is "open" any more — clicking an app navigates into it.
              const open = false;
              return (
                // The GROUPED card, on the same Meeting styling as the single one.
                //
                // Two card paths existed and I restyled only one, so a list that is mostly
                // groups still showed the old markup — the operator saw no change at all and
                // reasonably concluded the work had not been done. The identical mistake as
                // the fetch limit, the housekeeping filter and the preview URL earlier: fix
                // one path, miss its twin.
                <div key={key} className="mr-meeting-card ct-app-item" data-selected={open}
                     role="button" tabIndex={0}
                     onClick={(e) => {
                       if (fromInteractiveControl(e)) return;
                       setSelected(pickBuiltRun(members)?.id ?? members[0].id);
                     }}
                     onKeyDown={(e) => {
                       // A space typed into the app-name field is a space, not "open this app".
                       if (fromInteractiveControl(e)) return;
                       if (e.key === "Enter" || e.key === " ") {
                         e.preventDefault();
                         setSelected(pickBuiltRun(members)?.id ?? members[0].id);
                       }
                     }}
                     style={{ cursor: "pointer" }}>
                  <div className="mr-meeting-card-head">
                    {/* The kind of thing this is, in the badge the benchmark cards use. */}
                    {groupIdentity(members).kind ? (
                      <span className="cap-badge tone-blue">{groupIdentity(members).kind}</span>
                    ) : null}
                    <span className={`cap-badge tone-${statusTone(groupStatus)}`}>
                      {/* Both facts, when both are true. `anyShipped ? … : anyRunning ? …`
                          reported only the first one, so an app that had shipped AND had a
                          follow-up building announced itself as simply "shipped" — and the
                          in-flight work, which is the thing that is about to change what you
                          are looking at, appeared nowhere on the card. */}
                      {anyShipped && anyRunning ? "shipped · building"
                        : anyShipped ? "shipped"
                        : anyRunning ? "building" : groupStatus}
                    </span>
                    {/* What the operator has, in words: how many attempts, and whether the
                        most recent one went wrong. Both facts at once, so a working app is not
                        hidden behind its latest failure. */}
                    <span className="mr-meeting-date">
                      {total} {total === 1 ? "attempt" : "attempts"}
                      {anyShipped && anyBroken ? " · last one failed" : ""}
                    </span>
                  </div>
                  <span className="mr-meeting-title">
                    {benchGroup?.name ? name : groupIdentity(members).name}
                  </span>
                  {groupIdentity(members).blurb ? (
                    <p className="detail" style={{ margin: "2px 0 0" }}>
                      {groupIdentity(members).blurb}
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
                          icon: <ChatCircleDots size={15} weight="fill" aria-hidden />,
                          onSelect: () => askBossAbout(members[0], members),
                        },
                        {
                          id: "feature",
                          label: "Add a feature",
                          hint: "specs it first, then builds onto this app",
                          icon: <Sparkle size={15} weight="fill" aria-hidden />,
                          onSelect: () => setSpecPanel((m) => ({ ...m, [members[0].id]: "feature" })),
                        },
                        {
                          id: "rebuild",
                          label: "Rebuild with a fresh spec",
                          hint: "replaces this app — writes a new PRD",
                          icon: <Sparkle size={15} weight="fill" aria-hidden />,
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
                    <DeploymentLine run={builtRun} projectId={projectId} actor={actor}
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
                </div>
              );
            });

            // The Factory page is a LIST of apps. Clicking one opens it; the runs inside an app
            // are the task page's business, not this page's. An earlier version put the
            // master-detail here, which put a second navigation layer in front of the list
            // instead of behind it.
            return items;
          })()}
        </div>
      )}
    </div>
  );
}
