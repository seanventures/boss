"use client";

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
} from "./actions";
import { Markdown } from "./markdown";
import { ClientRelativeTime } from "./client-relative-time";
import { JourneySpine } from "./journey-spine-ui";
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
          <a className="cap-btn primary" href="?area=meetings"
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
      <button className="oc-link" onClick={() => setOpen(true)}
              title="Ask for a change; it builds on the code that already exists">
        Request a change
      </button>
    );
  }

  return (
    <div style={{ display: "grid", gap: 8 }}>
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

function QaBenchmarkCatalog({ benchmarks, projectId, actor, defaultOwner, defaultRepo,
                             launched, onLaunched, onOpen }: {
  benchmarks: QaBenchmarkApp[];
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
  return (
    <div className="ct-block" style={{ marginTop: 12 }}>
      <div className="ct-block-title">End-to-end QA benchmarks</div>
      <p className="detail" style={{ marginTop: 4 }}>
        These are pinned product contracts, not canned demos. Launching one creates a strict governed code task with a durable plan, evidence bundle, and final verification report.
      </p>
      <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
        {benchmarks.map((benchmark) => (
          <article key={benchmark.id} style={{ border: "1px solid var(--border)", borderRadius: 10, padding: 12, display: "grid", gap: 7 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
              <strong>{benchmark.name}</strong>
              <span className="cap-badge tone-blue">{benchmark.tagline}</span>
            </div>
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

export function CodeTasksHome({ projectId, codeTasks, apiReachable, previews = [], previewBlockedReason, benchmarks = [], actor = "executive_console", githubPlane, githubPlaneLabel, githubFullName }: {
  projectId: string;
  orgId?: string;
  codeTasks: CodeTaskRun[];
  apiReachable: boolean;
  previews?: PreviewRec[];
  previewBlockedReason?: string;
  benchmarks?: QaBenchmarkApp[];
  actor?: string;
  githubPlane?: string | null;
  githubPlaneLabel?: string | null;
  githubFullName?: string | null;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

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
        <div style={{ display: "grid", gap: 10 }}>
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
                <article key={c.id} className="ct-card oc-card" style={{ textAlign: "left" }}>
                  <div
                    className="oc-head"
                    role="button"
                    tabIndex={0}
                    onClick={openTask}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTask(); } }}
                  >
                    <div className="oc-title">
                      <span className="oc-n">{label}</span>
                      {c.title || "(untitled task)"}
                    </div>
                    <span className={`cap-badge tone-${statusTone(c.status)}`} style={{ flexShrink: 0 }}>{c.status}</span>
                  </div>
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
                                     failed={launched[c.id]?.error} />
                    <ChangeThisApp run={c} onStarted={noteLaunch}
                                   onOpen={(id) => void openRun(id)}
                                   startedId={launched[`chg:${c.id}`]?.run_id} />
                  </div>
                  {/* Meta as PILLS, the way the Meeting cards do it — the treatment that
                      already reads well here. Plus the fact that was missing entirely: WHEN.
                      A list of tasks with no times gives no sense of what is recent, so
                      everything looks equally current and nothing looks stale. */}
                  <div className="oc-meta">
                    <ClientRelativeTime iso={c.updated_at ?? c.created_at} className="oc-when" />
                    <span className="oc-pill">{c.spec?.owner}/{c.spec?.repo}</span>
                    {st.profile ? (
                      <span className="oc-pill accent">
                        {st.profile}{st.risk_tier != null ? ` · t${st.risk_tier}` : ""}
                      </span>
                    ) : null}
                    {c.pull_request_url ? (
                      <a className="oc-pill" href={c.pull_request_url} target="_blank"
                         rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                        PR #{c.pull_request_number} ↗
                      </a>
                    ) : null}
                    {(st.cost_usd_micros != null) ? (
                      <span className="oc-pill">{fmtUsd(st.cost_usd_micros)}</span>
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
            const order: string[] = [];
            const groups = new Map<string, CodeTaskRun[]>();
            for (const c of tasks) {
              // Every run of one benchmark belongs together. Pressing Launch ApproveIt three
              // times over a week is three attempts at ONE thing — scored against the same
              // rubric, compared with each other — and scattering them as loose rows among
              // unrelated tasks loses exactly the comparison the benchmark exists to make.
              // Lineage still wins where it applies: a decomposed build stays one bucket.
              const bench = (fc(c).qa_benchmark as { id?: string } | undefined)?.id;
              const key = (fc(c).root_run_id as string)
                || (bench ? `qa:${bench}` : null)
                || c.id;
              if (!groups.has(key)) { groups.set(key, []); order.push(key); }
              groups.get(key)!.push(c);
            }
            return order.map((key, gi) => {
              const members = groups.get(key)!;
              if (members.length === 1) return renderCard(members[0], `${gi + 1}.`);
              // Decomposed build → one bucket. Aggregate status + one PR/approval for the whole thing.
              // A benchmark bucket counts ATTEMPTS; a decomposed build counts its planned
              // parts. Reusing "N tasks" for both would call three attempts a three-part build.
              const total = benchGroupTotal(members, fc);
              const doneN = members.filter((m) => m.status === "completed").length;
              const groupStatus = members.some((m) => m.status === "blocked" || m.status === "failed")
                ? "blocked" : members.some((m) => m.status === "running" || m.status === "queued")
                ? "running" : doneN >= total ? "completed" : "running";
              const prRun = members.find((m) => m.pull_request_url);
              const benchGroup = (fc(members[0]).qa_benchmark as { name?: string } | undefined);
              const name = benchGroup?.name
                ? `${benchGroup.name} — QA benchmark`
                : (members.find((m) => m.title)?.title || "Decomposed build")
                    .replace(/^\[\d+\/\d+\]\s*/, "");
              const open = expandedGroups.has(key);
              return (
                <div key={key} className="ct-card" style={{ gap: 6, cursor: "default" }}>
                  <div
                    role="button"
                    onClick={() => setExpandedGroups((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; })}
                    style={{ display: "flex", gap: 10, alignItems: "center", justifyContent: "space-between", cursor: "pointer" }}
                  >
                    <div style={{ fontWeight: 700, fontSize: 16, color: "var(--ink)", flex: 1, minWidth: 0 }}>
                      <span style={{ color: "var(--muted)", fontWeight: 600, marginRight: 8 }}>{gi + 1}. {open ? "▾" : "▸"}</span>
                      {name} <span style={{ color: "var(--muted)", fontWeight: 600 }}>
                        {benchGroup
                          ? `· ${total} ${total === 1 ? "attempt" : "attempts"}`
                          : `· build (${total} tasks)`}
                      </span>
                    </div>
                    <span className={`cap-badge tone-${statusTone(groupStatus)}`} style={{ flexShrink: 0 }}>
                      {benchGroup ? groupStatus : `${doneN}/${total} done`}
                    </span>
                  </div>
                  <ContractDetails criteria={members[0].spec?.acceptance_criteria}
                                   verification={members[0].spec?.test_commands} />
                  <ChangeThisApp run={members[0]} onStarted={noteLaunch}
                                 onOpen={(id) => void openRun(id)}
                                 startedId={launched[`chg:${members[0].id}`]?.run_id} />
                  <LaunchTask run={members[0]} projectId={projectId} onLaunched={noteLaunch}
                                   onOpen={(id) => void openRun(id)}
                                   startedId={launched[members[0].id]?.run_id}
                                   failed={launched[members[0].id]?.error}
                                   label="Run this build again" />
                  <div className="ct-card-foot">
                    <span className="detail">{members[0].spec?.owner}/{members[0].spec?.repo}</span>
                    {prRun?.pull_request_url ? <span className="ct-card-pr">PR #{prRun.pull_request_number}</span>
                      : <span className="detail">one approval when the build finishes</span>}
                  </div>
                  {open ? (
                    <div style={{ display: "grid", gap: 8, marginTop: 6, paddingLeft: 10, borderLeft: "2px solid var(--border)" }}>
                      {members.map((m) => renderCard(m, `${(fc(m).decomposition_index as number) ?? "•"}/${total}`))}
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
