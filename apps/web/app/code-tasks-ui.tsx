"use client";

import { useState, useEffect, useActionState } from "react";
import { useRouter } from "next/navigation";
import type { CodeTaskRun, PreviewRec } from "@boss/api-client";

import {
  confirmCodeTaskFeedbackAction,
  createCodeTaskAction,
  startPreviewAction,
  submitCodeTaskFeedbackAction,
  overrideCodeTaskAction,
} from "./actions";
import { Markdown } from "./markdown";

function OverrideControls({ runId }: { runId: string }) {
  const [state, action, pending] = useActionState(overrideCodeTaskAction, null);
  return (
    <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <input type="hidden" name="run_id" value={runId} />
      <input name="reason" placeholder="Reason for override (recorded)" className="cap-input"
             style={{ minWidth: 220 }} />
      <button className="cap-btn" disabled={pending}>
        {pending ? "Overriding…" : "⚖ Waive failing gates & open PR"}
      </button>
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
  const hasLive = livePreviews.length > 0;
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      {/* Only offer "Run preview" (a heavy npm-install+build) when NO live preview exists yet.
          Once one is running, the primary action is Open preview; a fresh build is demoted to a
          subtle "Rebuild" link so we don't spawn duplicate builds that starve the api. */}
      {!hasLive ? (
        <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
          <input type="hidden" name="run_id" value={runId} />
          <button className="cap-btn" disabled={pending}>{pending ? "Starting preview…" : "▶ Run preview"}</button>
        </form>
      ) : null}
      {/* Persistent link(s) from the durable preview record — survive the console's auto-refresh */}
      {livePreviews.map((p) => (
        <span key={p.id} style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <a className="cap-btn primary" href={`${PREVIEW_API}/v1/previews/${encodeURIComponent(p.id)}/web/`} target="_blank" rel="noreferrer">Open preview ↗</a>
          <span className="cap-badge tone-blue">
            preview: {p.substrate === "preview_rootfs" ? "retained rootfs" : p.substrate === "preview_docker" ? "docker" : "host process"}
          </span>
          <PreviewLogs previewId={p.id} />
        </span>
      ))}
      {/* Rebuild — only when a preview already exists; replaces (does not stack) the current one. */}
      {hasLive ? (
        <form action={action} style={{ display: "inline-flex" }}>
          <input type="hidden" name="run_id" value={runId} />
          <button className="cap-btn" style={{ opacity: 0.7, fontSize: 12 }} disabled={pending}>
            {pending ? "Rebuilding…" : "↻ Rebuild preview"}
          </button>
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
      <button className="cap-btn primary" disabled={pending}>
        {pending ? "Dispatching…" : "Confirm & dispatch governed iteration"}
      </button>
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
        <button className="cap-btn" disabled={pending} style={{ justifySelf: "start" }}>
          {pending ? "Reviewing feedback…" : completed ? "Propose extension" : "Propose retry"}
        </button>
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
function useLiveRefresh(active: boolean, intervalMs = 4000) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => router.refresh(), intervalMs);
    return () => clearInterval(t);
  }, [active, intervalMs, router]);
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
        <button className="cap-btn primary" type="submit">Start coding task</button>
        <span className="detail">The agent edits files in an isolated sandbox; BOSS re-runs the tests and opens a gated PR.</span>
      </div>
    </form>
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

function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  return rs ? `${m}m ${rs}s` : `${m}m`;
}

function CodeTaskDetail({ run, previews = [], onBack }: { run: CodeTaskRun; previews?: PreviewRec[]; onBack: () => void }) {
  const st = run.state ?? {};
  const tests = st.tests ?? [];
  // One link per task: the most-recent running preview for this run (dedupe).
  const livePreviews = previews
    .filter((p) => p.run_id === run.id && p.status === "running")
    .sort((a, b) => (b.expires_at ?? "").localeCompare(a.expires_at ?? ""))
    .slice(0, 1);
  const live = run.status === "running" || run.status === "queued";
  useLiveRefresh(live);
  // Step timing: completed phases carry their duration; the current phase's elapsed is computed
  // live from phase_started_at (re-renders every ~4s via useLiveRefresh, so it ticks up).
  const _fc = (run.finalize_ctx ?? {}) as Record<string, unknown>;
  const phaseTimings = (_fc.phase_timings as { ms: number; phase: string }[] | undefined) ?? [];
  const _phaseStartedAt = _fc.phase_started_at as string | undefined;
  const currentPhaseMs = live && _phaseStartedAt
    ? Math.max(0, Date.now() - new Date(_phaseStartedAt).getTime()) : null;
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
          {PHASE_LABEL[run.phase] ?? run.phase} — running {currentPhaseMs != null ? fmtDuration(currentPhaseMs) : "…"}{" "}
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

      {run.error ? (
        <div className="ct-error">
          ⚠ {/quality ?gate/i.test(run.error)
            ? "Quality gate failed — the pipeline blocked the PR (fail-closed). See Review & quality gate below."
            : run.error}
        </div>
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
            <GateRow label="Preview available" ok={livePreviews.length > 0} skipped={livePreviews.length === 0} note={livePreviews.length > 0 ? "running — open below" : "run a preview"} />
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
            <span className="detail">▶ Preview available once a PR is open (a run must reach a green gate, or be overridden).</span>
          )}
        </div>
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

export function CodeTasksHome({ projectId, codeTasks, apiReachable, previews = [] }: {
  projectId: string;
  orgId?: string;
  codeTasks: CodeTaskRun[];
  apiReachable: boolean;
  previews?: PreviewRec[];
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Live-refresh while any task is queued/running so phases update without a manual reload.
  const anyLive = codeTasks.some((c) => c.status === "running" || c.status === "queued");
  useLiveRefresh(anyLive);

  if (!apiReachable) {
    return <p className="detail">Code tasks unavailable — API unreachable.</p>;
  }

  const active = selected ? codeTasks.find((c) => c.id === selected) : null;
  if (active) {
    return <CodeTaskDetail run={active} previews={previews} onBack={() => setSelected(null)} />;
  }

  const lastRepo = codeTasks[0]?.spec;

  return (
    <div className="ct">
      <div className="ct-home-bar">
        <span className="ct-home-count">{codeTasks.length} code task{codeTasks.length === 1 ? "" : "s"}</span>
        <div className="ct-home-actions">
          <button className="cap-btn primary" onClick={() => setShowNew((v) => !v)}>
            {showNew ? "Close" : "＋ New code task"}
          </button>
        </div>
      </div>

      {showNew ? (
        <NewCodeTaskForm projectId={projectId} defaultOwner={lastRepo?.owner} defaultRepo={lastRepo?.repo} />
      ) : null}

      {codeTasks.length === 0 ? (
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
              return (
                <button key={c.id} className="ct-card" onClick={() => setSelected(c.id)} style={{ gap: 6 }}>
                  <div style={{ display: "flex", gap: 10, alignItems: "flex-start", justifyContent: "space-between" }}>
                    <div style={{ fontWeight: 700, fontSize: 16, lineHeight: 1.35, color: "var(--ink)", flex: 1, minWidth: 0 }}>
                      <span style={{ color: "var(--muted)", fontWeight: 600, marginRight: 8 }}>{label}</span>
                      {c.title || "(untitled task)"}
                    </div>
                    <span className={`cap-badge tone-${statusTone(c.status)}`} style={{ flexShrink: 0 }}>{c.status}</span>
                  </div>
                  <div className="detail">{outcome}</div>
                  <div className="ct-card-foot">
                    <span className="detail">{c.spec?.owner}/{c.spec?.repo}{c.spec?.base_branch && c.spec.base_branch !== "main" ? ` @ ${c.spec.base_branch}` : ""}</span>
                    {st.profile ? <span className="detail">{st.profile}{st.risk_tier != null ? ` · t${st.risk_tier}` : ""}</span> : null}
                    {c.pull_request_url ? <span className="ct-card-pr">PR #{c.pull_request_number}</span> : null}
                    {(st.cost_usd_micros != null) ? <span className="detail">{fmtUsd(st.cost_usd_micros)}</span> : null}
                  </div>
                </button>
              );
            };
            // Group runs by lineage (finalize_ctx.root_run_id) so a decomposed build shows as ONE
            // collapsible bucket — one thing to look at, not N loose rows. Singletons render flat.
            const fc = (c: CodeTaskRun) => (c.finalize_ctx ?? {}) as Record<string, unknown>;
            const order: string[] = [];
            const groups = new Map<string, CodeTaskRun[]>();
            for (const c of codeTasks) {
              const key = (fc(c).root_run_id as string) || c.id;
              if (!groups.has(key)) { groups.set(key, []); order.push(key); }
              groups.get(key)!.push(c);
            }
            return order.map((key, gi) => {
              const members = groups.get(key)!;
              if (members.length === 1) return renderCard(members[0], `${gi + 1}.`);
              // Decomposed build → one bucket. Aggregate status + one PR/approval for the whole thing.
              const total = (fc(members[0]).decomposition_total as number) || members.length;
              const doneN = members.filter((m) => m.status === "completed").length;
              const groupStatus = members.some((m) => m.status === "blocked" || m.status === "failed")
                ? "blocked" : members.some((m) => m.status === "running" || m.status === "queued")
                ? "running" : doneN >= total ? "completed" : "running";
              const prRun = members.find((m) => m.pull_request_url);
              const name = (members.find((m) => m.title)?.title || "Decomposed build").replace(/^\[\d+\/\d+\]\s*/, "");
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
                      {name} <span style={{ color: "var(--muted)", fontWeight: 600 }}>· build ({total} tasks)</span>
                    </div>
                    <span className={`cap-badge tone-${statusTone(groupStatus)}`} style={{ flexShrink: 0 }}>{doneN}/{total} done</span>
                  </div>
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
