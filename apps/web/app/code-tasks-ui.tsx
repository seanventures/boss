"use client";

import { useState, useEffect, useActionState } from "react";
import { useRouter } from "next/navigation";
import type { CodeTaskRun } from "@boss/api-client";

import { createCodeTaskAction, retryCodeTaskAction, startPreviewAction, overrideCodeTaskAction } from "./actions";
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

function PreviewControls({ runId }: { runId: string }) {
  const [state, action, pending] = useActionState(startPreviewAction, null);
  return (
    <form action={action} style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
      <input type="hidden" name="run_id" value={runId} />
      <button className="cap-btn" disabled={pending}>{pending ? "Starting preview…" : "▶ Run preview"}</button>
      {state?.url ? (
        <a className="cap-btn primary" href={state.url} target="_blank" rel="noreferrer">Open {state.url} →</a>
      ) : null}
      {state?.error ? <span className="detail" style={{ color: "#f85149" }}>⚠ {state.error}</span> : null}
    </form>
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

function GateRow({ label, ok, note }: { label: string; ok?: boolean; note?: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
      <span style={{ color: ok ? "#3fb950" : "#f85149", fontWeight: 700, width: 14, textAlign: "center" }}>{ok ? "✓" : "✗"}</span>
      <span style={{ color: "var(--ink)" }}>{label}</span>
      {note ? <span className="detail">— {note}</span> : null}
    </div>
  );
}

function CodeTaskDetail({ run, onBack }: { run: CodeTaskRun; onBack: () => void }) {
  const st = run.state ?? {};
  const tests = st.tests ?? [];
  const live = run.status === "running" || run.status === "queued";
  useLiveRefresh(live);
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
      </div>

      <Flow phase={run.phase} status={run.status} />

      {live ? (
        <div className="detail" style={{ margin: "8px 0", color: "#58a6ff", fontWeight: 600 }}>
          <span style={{ animation: "pulse 1.5s infinite" }}>●</span>{" "}
          {PHASE_LABEL[run.phase] ?? run.phase} — in progress…{" "}
          <span className="detail" style={{ fontWeight: 400 }}>(auto‑refreshing every 4s)</span>
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
            Review &amp; quality gate{" "}
            <span className={`cap-badge tone-${gateTone}`}>{gateLabel}</span>
          </div>
          <div style={{ display: "grid", gap: 6, margin: "8px 0" }}>
            <GateRow label="Written tests pass" ok={st.tests_passed} note={st.test_exit_code != null ? `exit ${st.test_exit_code}` : undefined} />
            <GateRow label="Code review (adversarial)" ok={st.adversarial_verdict === "survived"} note={st.adversarial_verdict} />
            <GateRow label="Security clean" ok={st.security_verdict === "clean"} note={st.security_verdict} />
            <GateRow label="No regressions" ok={st.regressions_ok} note={st.mutation_caught ? "mutation caught" : undefined} />
            {st.consistency_verdict ? (
              <GateRow label="Consistency" ok={["consistent", "pass", "survived"].includes(st.consistency_verdict)} note={st.consistency_verdict} />
            ) : null}
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
        <div className="ct-outcome" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          <PreviewControls runId={run.id} />
          {(run.status === "failed" || run.status === "blocked") ? (
            <form action={retryCodeTaskAction} style={{ display: "inline" }}>
              <input type="hidden" name="run_id" value={run.id} />
              <button className="cap-btn">↻ Retry (carry findings forward)</button>
            </form>
          ) : null}
        </div>
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
          Preview runs the built app on a local URL (1‑box, auto‑expires). Retry resumes this task
          as a new attempt with its prior findings as feedback.
        </p>
      </div>
    </div>
  );
}

export function CodeTasksHome({ projectId, codeTasks, apiReachable }: {
  projectId: string;
  orgId?: string;
  codeTasks: CodeTaskRun[];
  apiReachable: boolean;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  // Live-refresh while any task is queued/running so phases update without a manual reload.
  const anyLive = codeTasks.some((c) => c.status === "running" || c.status === "queued");
  useLiveRefresh(anyLive);

  if (!apiReachable) {
    return <p className="detail">Code tasks unavailable — API unreachable.</p>;
  }

  const active = selected ? codeTasks.find((c) => c.id === selected) : null;
  if (active) {
    return <CodeTaskDetail run={active} onBack={() => setSelected(null)} />;
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
        <div className="ct-grid">
          {codeTasks.map((c) => {
            const st = c.state ?? {};
            return (
              <button key={c.id} className="ct-card" onClick={() => setSelected(c.id)}>
                <div className="ct-card-head">
                  <span className={`cap-badge tone-${statusTone(c.status)}`}>{c.status}</span>
                  <span className="ct-card-phase">{PHASE_LABEL[c.phase] ?? c.phase}</span>
                </div>
                <div className="ct-card-title">{c.title}</div>
                <div className="ct-card-repo">{c.spec?.owner}/{c.spec?.repo}</div>
                <div className="ct-card-foot">
                  {c.pull_request_url ? (
                    <span className="ct-card-pr">PR #{c.pull_request_number}</span>
                  ) : <span className="detail">no PR</span>}
                  {typeof st.files_changed === "number" ? (
                    <span className="detail">{st.files_changed} files · +{st.additions ?? 0}/−{st.deletions ?? 0}</span>
                  ) : null}
                  <span className={`ct-card-tests ${st.tests_passed ? "ok" : ""}`}>
                    {st.tests_passed ? "tests ✓" : ""}
                  </span>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
