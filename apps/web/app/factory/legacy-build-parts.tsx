"use client";

/**
 * The build's working parts that the Factory cockpit (factory spec §4.5.5-§4.5.7) restyles:
 * preview controls and logs, deployment, retry, waiver, change requests, the benchmark
 * catalogue, and the pre-redesign build detail. MOVED VERBATIM out of code-tasks-ui.tsx when
 * that file became the Factory shell, so their behaviour is unchanged; the cockpit imports
 * them from here and replaces them one at a time.
 */

import { changeHref, isLocalChangeUrl, linkTarget, reviewChangeWords } from "../../lib/repo-links";
import { useSearchParams } from "next/navigation";
import { PROJECT_ID } from "../../lib/client";
import { PrMadeBy, useBuildWork, useStageFaces } from "../agents/build-faces";

import { consoleHref } from "../console-href";
import { HostAdviceBanner, useHostAdvice } from "../host-advice";
import { onChanged, notifyChanged } from "../changed";
import { appIdentity, groupIdentity } from "../app-identity";

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
import { groupRunsIntoApps } from "../app-grouping";

import React, { useActionState, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useConsoleRefresh } from "../use-console-refresh";
import type { CodeTaskRun, LlmCallRow, PreviewRec, QaBenchmarkApp, QaBenchmarkReport } from "@boss/api-client";
import { forgetRunDeployment, loadRunDeployment } from "../../lib/run-deployments";
import { operatorPreviewOpenHref } from "../../lib/code-tasks";
import { ApprovePreview } from "../previews-ui";
import { structuredCodeTaskTests } from "../../lib/code-tasks";

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
} from "../actions";
import {
  ChangeSetNextStep, LiveDeployment, changeReviewHref, plannedDeploymentFrom, type PlannedDeployment,
} from "../deploy-ui";
import { Markdown } from "../markdown";
import { ClientRelativeTime } from "../client-relative-time";
import { JourneySpine } from "../journey-spine-ui";
import {
  ArrowSquareOut, ArrowsClockwise, ChatCircleDots, DotsThree, GitPullRequest,
  ListMagnifyingGlass, PencilSimple, PlayCircle, RocketLaunch, Sparkle, TerminalWindow,
} from "@phosphor-icons/react";

import { SubmitButton } from "../submit-button";
import { GatedButton } from "../../components/gated";
import { apiWrite } from "../../lib/api-write";
import { openDockAbout } from "../../lib/dock-subject";
import { ActionForm } from "../client-forms";
import { displayTitle } from "../../lib/directive-subject";
import { browserHref, previewWebHref } from "../../lib/browser-links";

/** Whether Retry can do anything: not while it runs, and not on a build that finished clean —
 *  the same rule as the engine's refuse_finished_retry. */
export function isRetryable(run: {
  status?: string | null;
  finalize_ctx?: Record<string, unknown> | null;
}): boolean {
  const status = String(run.status ?? "");
  if (status === "running" || status === "queued") return false;
  if (status !== "completed") return true;
  return failingGatesOf(run.finalize_ctx).length > 0;
}

/**
 * The gates a finished build recorded as failing, read the way the engine reads them.
 *
 * code_task_service.refuse_finished_retry says `advisory_failing_gates or failing_gates`: an
 * EMPTY advisory list falls through to failing_gates. This said `??`, which stops at an empty
 * array, so a build with no advisory failures but real failing gates had its Retry hidden
 * while the engine would have accepted it.
 */
export function failingGatesOf(ctx: Record<string, unknown> | null | undefined): string[] {
  for (const gates of [ctx?.advisory_failing_gates, ctx?.failing_gates]) {
    if (Array.isArray(gates) && gates.length > 0) return gates.map(String);
  }
  return [];
}

import { callAction } from "../../lib/call-action";
import { useOnScreen } from "../../lib/area-shown";
export function RetryControls(
  { runId, failingGates = [], compact = false }:
  { runId: string; failingGates?: string[]; compact?: boolean },
) {
  const [state, action, pending] = useActionState(callAction(retryCodeTaskWithStateAction, undefined, "code_task:retry"), null);
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
    // AND THE ANSWER, HERE TOO. The compact form rendered neither state.error nor state.runId,
    // so a refused retry and a started one looked identical: nothing happened.
    return (
      <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center",
                                     flexWrap: "wrap" }} title={explains}>
        <input type="hidden" name="run_id" value={runId} />
        <SubmitButton permission="code_task:retry" className="cap-btn" disabled={pending}>
          <ArrowsClockwise size={15} weight="bold" aria-hidden />
          {pending ? "Retrying…" : "Retry build"}
        </SubmitButton>
        {state?.runId ? (
          <span className="cap-badge tone-blue" data-testid="ct-retry-started">
            attempt {state.attempt ?? "?"} started · {state.runId}
          </span>
        ) : null}
        {state?.error ? (
          <span className="detail" role="alert" style={{ color: "#f85149" }}
                data-testid="ct-retry-error">{state.error}</span>
        ) : null}
      </form>
    );
  }

  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
        <input type="hidden" name="run_id" value={runId} />
        <SubmitButton permission="code_task:retry" className="cap-btn" disabled={pending} title="Run this task again, resuming from where it failed">
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

export function OverrideControls({ runId }: { runId: string }) {
  const [state, action, pending] = useActionState(callAction(overrideCodeTaskAction, undefined, "code_task:override"), null);
  return (
    <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <input type="hidden" name="run_id" value={runId} />
      <input name="reason" placeholder="Reason for override (recorded)" className="cap-input"
             style={{ minWidth: 220 }} />
      <SubmitButton permission="code_task:override" className="cap-btn" disabled={pending}>
        {pending ? "Overriding…" : "⚖ Waive failing gates & open PR"}
      </SubmitButton>
      {state?.url ? (
        <a className="cap-btn primary" href={state.url} target="_blank" rel="noreferrer">PR opened →</a>
      ) : null}
      {state?.error ? <span className="detail" style={{ color: "#f85149" }}>⚠ {state.error}</span> : null}
    </form>
  );
}


export function PreviewLogs({ previewId }: { previewId: string }) {
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

export function PreviewControls({ runId, livePreviews = [] }: { runId: string; livePreviews?: PreviewRec[] }) {
  const [state, action, pending] = useActionState(callAction(startPreviewAction, undefined, "preview:manage"), null);
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
          <SubmitButton permission="preview:manage" className="cap-btn" disabled={pending}>
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
            <>
              <a className="cap-btn primary" href={browserHref(href)} target="_blank" rel="noreferrer">
                <ArrowSquareOut size={15} weight="bold" aria-hidden />
                {p.status === "verified" ? "View verified preview" : "Open preview"}
              </a>
              {p.demo_logins?.length ? (
                <span className="detail" data-testid="preview-demo-logins"
                      title="BOSS generated this password for the app's demo users; reveal it in Project › Settings › Demo logins">
                  Demo login: {p.demo_logins.join(", ")} · <a href={consoleHref({ area: "project", tab: "settings", section: "demo_logins", app: undefined, build: undefined })}
                                                             data-testid="preview-demo-logins-moved">Moved to Project › Settings</a>
                </span>
              ) : null}
            </>
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
          <SubmitButton permission="preview:manage" className="cap-btn" style={{ opacity: 0.7, fontSize: 12 }} disabled={pending}>
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

export function ConfirmFeedback({ threadId, workflowName }: { threadId: string; workflowName?: string }) {
  const [state, action, pending] = useActionState(callAction(confirmCodeTaskFeedbackAction, undefined, "code_task:steer"), null);
  return (
    <form action={action} style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      <input type="hidden" name="thread_id" value={threadId} />
      <SubmitButton permission="code_task:steer" className="cap-btn primary" disabled={pending}>
        {pending ? "Dispatching…"
                 : workflowName ? `Confirm & start the ${workflowName} workflow`
                                : "Confirm & run inline"}
      </SubmitButton>
      {state?.run_id ? (
        <span className="detail" style={{ color: "#3fb950" }} data-testid="ct-dispatched">
          {state.path === "workflow" && state.workflow_run_id ? (
            <>Started the {state.workflow_name} workflow — <a
                href={`?area=delivery_workflows&wfr=${encodeURIComponent(state.workflow_run_id)}`}
                onClick={(e) => {
                  // The same navigation Build uses: keep the org and project in the URL and
                  // let the console switch areas without a full reload.
                  e.preventDefault();
                  const url = new URL(window.location.href);
                  url.searchParams.set("area", "delivery_workflows");
                  url.searchParams.set("wfr", String(state.workflow_run_id));
                  window.history.pushState({}, "", url.toString());
                  window.dispatchEvent(new PopStateEvent("popstate"));
                  window.dispatchEvent(new CustomEvent("boss:navigate"));
                }}>
                open it under Workflows</a> (build {state.run_id})</>
          ) : state.path === "workflow" ? (
            <>Queued {state.run_id}, but no workflow run was recorded for it — check Workflows.</>
          ) : (
            <>Running inline in the factory as {state.run_id}</>
          )}
        </span>
      ) : null}
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
export function FeedbackControls({ runId, status, anchor = "", projectId }: {
  runId: string; status: string; anchor?: string; projectId: string;
}) {
  const completed = status === "completed";
  const [state, action, pending] = useActionState(callAction(submitCodeTaskFeedbackAction, undefined, "code_task:steer"), null);
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
              {/* INLINE IS THE DEFAULT, AND SAYS WHAT IT MEANS. "Follow the inner pipeline only"
                  read as a flavour of workflow; it is the absence of one — the change runs
                  straight in the factory and nothing appears under Workflows. */}
              <option value="">Inline change — no workflow run</option>
              {workflows.filter((w) => w.active_version_id).map((w) => (
                <option key={w.id} value={w.active_version_id as string}>
                  {w.name}{w.is_catalogue ? " (standard)" : ""}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="ct-iterate-actions">
          <SubmitButton permission="code_task:steer" className="cap-btn primary" disabled={pending}>
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
          {/* WHICH PATH, BEFORE ANYTHING RUNS. The confirm button said "governed iteration"
              either way, so an inline change looked like a workflow until it had run. */}
          <p className="ct-proposal-path" data-testid="ct-proposal-path"
             data-path={state.proposal.path ?? "inline"}>
            {state.proposal.path === "workflow"
              ? <>Runs as the <b>{state.proposal.workflow_name}</b> workflow — it will appear
                  under Workflows.</>
              : <>Runs as an <b>inline change</b> in the factory — no workflow run. Pick a
                  workflow above to run it through one instead.</>}
          </p>
          <pre>{state.proposal.fix_note}</pre>
          {state.thread_id ? (
            <ConfirmFeedback threadId={state.thread_id}
                             workflowName={state.proposal.path === "workflow"
                               ? state.proposal.workflow_name : undefined} />
          ) : null}
        </div>
      ) : null}
      {state?.error ? <p className="ct-iterate-note" data-tone="bad">{state.error}</p> : null}
    </div>
  );
}

// Governed pipeline phases (in order). Older/simple-path phases are aliased so the stepper
// still lights up. Each label says what the agent is actually doing in that phase.
export const PHASES = [
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
export function useLiveRefresh(active: boolean, intervalMs = 4000, reload?: () => void | Promise<void>) {
  const refresh = useConsoleRefresh();
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    if (!active || !onScreen) return;
    const t = setInterval(() => {
      // Both: the console refresh updates every other server-rendered panel, and `reload`
      // re-reads the one list this component owns. The refresh alone was demonstrably not
      // enough — the server re-rendered and the client kept the old rows. ASKED, not issued:
      // a bare 4s router.refresh() against a 20s render kept the console's refresh queue
      // full, and nothing on screen committed (lib/console-refresh.ts).
      refresh("poll");
      void reload?.();
    }, intervalMs);
    return () => clearInterval(t);
  }, [active, intervalMs, refresh, reload, onScreen]);
}

export function statusTone(status: string): string {
  if (status === "completed") return "green";
  if (status === "failed") return "pink";
  if (status === "blocked") return "amber";
  return "blue"; // running
}

export function fmtUsd(micros?: number): string {
  if (!micros && micros !== 0) return "—";
  return `$${(micros / 1e6).toFixed(2)}`;
}

export function Flow({ phase, status, faces }: { phase: string; status: string;
  /** The agent that did each stage, from the build's signed work (Agent World), by phase. */
  faces?: Record<string, React.ReactNode> }) {
  const activeIdx = PHASES.indexOf(phase);
  return (
    <div className="ct-flow">
      {PHASES.map((p, i) => {
        const reached = status === "completed" ? true : i <= activeIdx;
        const current = i === activeIdx && status === "running";
        return (
          <span key={p} className="ct-flow-step">
            <span className={`ct-flow-dot${reached ? " on" : ""}${current ? " pulse" : ""}`} />
            {faces?.[p] ? <span className="ag-stage-face" data-testid={`ct-flow-face-${p}`}>{faces[p]}</span> : null}
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
export function useProjectWorkflows(projectId: string) {
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
export function startAProjectHref(): string {
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
 * no feedback, and reference host currently holds four HookWatch runs and two BugBox runs queued —
 * a button that looks inert gets pressed again, and here every press costs budget.
 */
/** Launch a benchmark, once.
 *
 * The button was a bare form submit: pressing it navigated nowhere, changed nothing on screen,
 * and the new run only appeared after the page happened to refresh. So it got pressed again —
 * reference host is holding four HookWatch runs and two BugBox runs, queued and identical. Every
 * one of those is a budgeted governed pipeline. Disabling on submit and saying "Starting…" is
 * the whole fix, and it is the same fix the delivery buttons needed.
 */
export function BenchmarkLaunch({ benchmark, projectId, actor, defaultOwner, defaultRepo,
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
    ? previewWebHref(benchmark.built.preview_id)
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
      out = await callAction(startQaBenchmarkAppAction, undefined, "code_task:create")(data);
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
          <GatedButton permission="code_task:create" className="cap-btn" disabled={sent} onClick={() => void launch(true)}
                  title="Convene a new panel and build a second copy — this costs a full run">
            {sent ? "Starting…" : "Build it again"}
          </GatedButton>
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
        <GatedButton permission="code_task:create" className="cap-btn primary" disabled={sent} onClick={() => void launch(false)}>
          {sent ? "Starting…" : `Launch ${benchmark.name}`}
        </GatedButton>
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

export function loadHostsOnce(projectId: string): Promise<any> {
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

export function SetupDeployment({ run, projectId, actor, onDone }: {
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
          No verified host yet. Onboard one in Infra → Hosts, then this app can be pointed at it.
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
              ? `target set: ${r.data?.host ?? "host"} — plan its first deployment below`
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
          <GatedButton permission="deployment_target:manage" className="cap-btn" type="submit" disabled={busy}>
            {busy ? "Saving…" : "Set target"}
          </GatedButton>
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
export async function appDeployOp(body: Record<string, unknown>): Promise<{ ok: boolean; data?: any; error?: string }> {
  try {
    const r = await apiWrite("/api/app-deploy", {
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

export async function retireTarget(targetId: string, actor: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await apiWrite(
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

export function DeploymentLine({ run, projectId, actor, deployable }: {
  run: CodeTaskRun; projectId: string; actor: string;
  /** Has this app produced reviewed code? A blocked or still-building task has nothing to
   *  ship, and offering a deployment plan there promises something the pipeline cannot honour. */
  deployable: boolean;
}) {
  const [linked, setLinked] = useState<string | null>(null);
  const [targetId, setTargetId] = useState<string | null>(null);
  const [deploys, setDeploys] = useState<any[]>([]);
  const [retrying, setRetrying] = useState(false);
  const [plan, setPlan] = useState<PlannedDeployment | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  // Whether the binding has been READ yet. Without this, `targetId` is null for the first
  // render and the card would flash "not bound" at every app on load.
  const [loaded, setLoaded] = useState(false);
  const [lookupError, setLookupError] = useState<string | null>(null);
  const lookupSequence = useRef(0);

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
    const sequence = ++lookupSequence.current;
    // A refresh can fail after a successful read. Hide actions while it is in flight, and do
    // not leave the old binding armed if the current answer fails.
    setLoaded(false);
    setLookupError(null);
    try {
      const d = await loadRunDeployment(run.id, appIdentity(run).name);
      if (sequence !== lookupSequence.current) return;
      if (d.error) throw new Error(d.error);
      setDeploys((d.deployments ?? []) as any[]);
      setLinked(d.linked_host ?? null);
      setTargetId(d.linked_target_id ?? null);
      setLoaded(true);
    } catch (error) {
      if (sequence !== lookupSequence.current) return;
      // `error` means nobody answered, not "this app has no deployments". Clear every stale
      // field so a failed refresh cannot leave Deploy, Set target, Retry, or Unbind active.
      setDeploys([]);
      setLinked(null);
      setTargetId(null);
      setPlan(null);
      setNote(null);
      setLookupError(String((error as Error)?.message ?? error));
    }
  }, [run]);

  // Polling must see the world as it is now, not the batch window's 2.5s-old answer.
  const reload = useCallback(async () => {
    forgetRunDeployment(run.id);
    await load();
  }, [run.id, load]);

  useEffect(() => {
    void load();
    return () => { lookupSequence.current += 1; };
  }, [load]);

  // AND WHEN THE CHANGE WAS MADE SOMEWHERE ELSE.
  //
  // This card shows the app's target and its deployments — exactly the rows the Workflows
  // screen and BOSS also change. Setting a target from the workflow left this card showing the
  // old answer until its own poll happened to run, or for ever once the card stopped polling.
  useEffect(() => onChanged(() => { void reload(); }, "factory"), [reload]);

  // Poll only while something is actually moving. A card that polls forever is a card that
  // keeps a browser and an API busy describing a deployment that finished yesterday.
  const latest = deploys[0];
  const live = latest && ["running", "in_progress", "deploying"].includes(latest.status);
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    if (!live || !onScreen) return;
    const t = window.setInterval(() => { void reload(); }, 4000);
    return () => window.clearInterval(t);
  }, [live, reload, onScreen]);

  // No deployment yet. That is either "no destination" (offer to link one) or "linked but
  // never shipped" (offer the first deployment plan). Rendering nothing here is what left an already
  // merged app with no way forward on its own card.
  // Nothing to ship, or still shipping it: the row would be an empty promise on a card that
  // already says BLOCKED or BUILDING two inches above.
  if (lookupError) {
    return (
      <div className="ct-deploy-wrap">
        <div className="ct-deploy-line" data-testid="app-deployment-unreadable">
          <span className="cap-badge tone-red">deployment unavailable</span>
          <span className="detail" role="alert">
            Could not check deployment ({lookupError}). Retry before changing its target or shipping.
          </span>
          <button type="button" className="cap-btn ct-deploy-action"
                  data-testid="app-deployment-check-retry"
                  onClick={(event) => { event.stopPropagation(); void reload(); }}>
            <ArrowsClockwise size={15} weight="bold" aria-hidden />{" "}Retry check
          </button>
        </div>
      </div>
    );
  }
  if (!deployable && !latest) return null;
  if (!loaded) {
    return (
      <div className="ct-deploy-wrap">
        <div className="ct-deploy-line" data-testid="app-deployment-checking">
          <span className="detail">Checking deployment…</span>
        </div>
      </div>
    );
  }

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
      <div className="ct-deploy-wrap">
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
            <GatedButton permission="deployment:create" className="cap-btn primary ct-deploy-action"
                    disabled={retrying || !!plan}
                    data-testid="app-deploy-first"
                    title={plan
                      ? "This deployment ChangeSet is awaiting review"
                      : "Create a ChangeSet to review before deploying this app"}
                    onClick={async (e) => {
                      e.stopPropagation();
                      setRetrying(true); setNote(null); setPlan(null);
                      const r = await appDeployOp({
                        op: "deploy",
                        actor,
                        project_id: projectId,
                        run_id: run.id,
                        target_id: targetId ?? "",
                      });
                      const nextPlan = r.ok ? plannedDeploymentFrom(r.data) : null;
                      setPlan(nextPlan);
                      setNote(r.ok
                        ? (nextPlan ? null : "Deployment request did not return a reviewable ChangeSet. No deployment was confirmed.")
                        : (r.error ?? "deployment planning failed"));
                      setRetrying(false);
                      await reload();
                    }}>
              <RocketLaunch size={15} weight="fill" aria-hidden />{" "}
              {retrying ? "Planning…" : "Plan deployment"}
            </GatedButton>
            {/* THE UNDO THAT WAS NEVER THERE.
                Once an app was bound to a host and port the card only ever offered "Deploy
                now" — a wrong binding (this app sat on another app's port) could be fixed
                nowhere but the database. Retiring keeps the history: deployment runs and
                health samples reference the target row. */}
            <GatedButton permission="deployment_target:manage" className="cap-btn ct-deploy-action"
                    disabled={retrying || !targetId}
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
            </GatedButton>
            {note ? <span className="detail">{note}</span> : null}
          </>
        ) : (
          <SetupDeployment run={run} projectId={projectId} actor={actor} onDone={load} />
        )}
      </div>
      {plan ? <ChangeSetNextStep plan={plan} /> : null}
      </div>
    );
  }

  const ok = ["succeeded", "deployed", "completed"].includes(latest.status);
  const failed = ["failed", "rolled_back", "execute_failed", "blocked"].includes(latest.status);
  const planned = ["created", "planned"].includes(latest.status);
  const where = latest.host_name || latest.environment || "target";

  return (
    <div className="ct-deploy-wrap">
    <div className="ct-deploy-line" data-testid="app-deployment"
         data-deploy-status={latest.status}>
      <span className={`cap-badge ${ok ? "tone-green" : failed ? "tone-red" : "tone-amber"}`}>
        {ok ? "deployed" : failed ? "deploy failed"
          : planned ? "awaiting ChangeSet review" : latest.status}
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
          <GatedButton permission="deployment:create" className="cap-btn primary ct-deploy-action" disabled={retrying || !!plan}
                  data-testid="app-deployment-retry"
                  title="Create a new ChangeSet for the same commit and target"
                  onClick={async (e) => {
                    e.stopPropagation();
                    setRetrying(true); setNote(null); setPlan(null);
                    const r = await appDeployOp({ op: "retry", actor, deployment_run_id: latest.id });
                    const nextPlan = r.ok ? plannedDeploymentFrom(r.data) : null;
                    setPlan(nextPlan);
                    setNote(r.ok
                      ? (nextPlan ? null : "Redeploy request did not return a reviewable ChangeSet. No deployment was confirmed.")
                      : (r.error ?? "redeploy planning failed"));
                    setRetrying(false);
                    await reload();
                  }}>
            <ArrowsClockwise size={15} weight="bold" aria-hidden />{" "}
            {retrying ? "Planning…" : "Plan retry"}
          </GatedButton>
          {/* RETRY IS NOT ALWAYS THE ANSWER.
              A deployment that fails because the target itself is wrong — no domain, or the
              port belongs to another app — fails identically every time it is retried, and
              the card offered nothing else. Unbinding is the other move, and it was reachable
              only from the database. */}
          <GatedButton permission="deployment_target:manage" className="cap-btn ct-deploy-action"
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
          </GatedButton>
          {note ? <span className="detail">{note}</span> : null}
        </>
      ) : null}
      {planned && !plan ? (
        <span className="detail">No deployment is running. Review its ChangeSet in{" "}
          <a href={changeReviewHref((latest as { changeset_id?: string | null }).changeset_id)}>ProviderOps ChangeSet Review</a>.
        </span>
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
    {plan ? <ChangeSetNextStep plan={plan} /> : null}
    </div>
  );
}



export function RebuildWithSpec({ run, projectId, actor, mode = "rebuild",
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
    <ActionForm permission="meeting:convene" action={conveneMeetingAction} className="ct-change-form"
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
    </ActionForm>
  );
}



export function ContractDetails({ criteria, verification, label = "Acceptance criteria & verification" }: {
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

export function QaBenchmarkCatalog({ benchmarks, builtBenchmarkIds = new Set<string>(), projectId, actor, defaultOwner, defaultRepo,
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
export function GateRow({ label, ok, note, skipped }: { label: string; ok?: boolean; note?: string; skipped?: boolean }) {
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

export function QaBenchmarkEvidence({ runId, benchmarkName }: { runId: string; benchmarkName: string }) {
  const [report, setReport] = useState<QaBenchmarkReport | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void callAction(getQaBenchmarkReportAction, undefined, "code_task:read")(runId).then((value) => {
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

export function fmtDuration(ms: number): string {
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

export function agentLabel(role: string | null | undefined): string {
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
export function CodeTaskSteps({ runId, live, phaseTimings, currentPhase, currentPhaseMs, totalMs,
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
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const data = await callAction(getCodeTaskActivityAction, undefined, "code_task:read")(runId);
      if (!cancelled) { setCalls(data); setLoaded(true); }
    };
    void load();
    const id = live && onScreen ? setInterval(load, 4000) : undefined;
    return () => { cancelled = true; if (id) clearInterval(id); };
  }, [runId, live, onScreen]);

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
export function WorkflowLink({ run }: { run: CodeTaskRun }) {
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

export function CodeTaskDetail({ run, previews = [] }: { run: CodeTaskRun; previews?: PreviewRec[] }) {
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
  // WHO BUILT IT, from the build's signed work when a workflow run started it: one face per
  // stage on the ribbon (the assigned engineer, not a stage→role table) and the PR's credits.
  const originRunId = String(((_fc.origin ?? {}) as Record<string, unknown>).workflow_run_id ?? "") || null;
  const buildWork = useBuildWork(PROJECT_ID, originRunId, run.id, live);
  const stageFaces = useStageFaces(buildWork.stages, PHASE_STAGES);
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

      <Flow phase={run.phase} status={run.status} faces={stageFaces} />

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
            <a className="cap-btn" href={changeHref(run.pull_request_url)} {...linkTarget(run.pull_request_url)}>
              {isLocalChangeUrl(run.pull_request_url) ? reviewChangeWords(run.pull_request_url) ?? `Review change #${run.pull_request_number} ›` : `View PR #${run.pull_request_number} →`}
            </a>
          ) : <span className="detail">No PR opened.</span>}
          {run.pull_request_url ? (
            <PrMadeBy chain={buildWork.chain} runId={originRunId} projectId={PROJECT_ID}
                      title={appIdentity(run).name || `PR #${run.pull_request_number}`} />
          ) : null}
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
const INTERNAL_RUN = /^(captain:|quality improve pass)/i;

/** What KIND of attempt this is, so a stack of them reads as a story rather than repetition. */
export function attemptKind(run: CodeTaskRun): string {
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
export function leadLine(raw: string): string {
  // The ask, not the panel's scaffolding (lib/directive-subject.ts) — cutting at the first
  // ". " of a wrapped directive gave "--- WHAT THIS PANEL IS FOR --- You are the panel…".
  const text = displayTitle(raw);
  if (text.length <= 84) return text;
  const stop = text.slice(0, 110).search(/[.:;]\s/);
  if (stop > 20) return text.slice(0, stop);
  const cut = text.lastIndexOf(" ", 81);
  return `${text.slice(0, cut > 20 ? cut : 81)}…`;
}


export function isInternalRun(run: { title?: string }): boolean {
  return INTERNAL_RUN.test((run.title ?? "").trim());
}

export { ApprovePreview } from "../previews-ui";
export { LiveDeployment } from "../deploy-ui";
