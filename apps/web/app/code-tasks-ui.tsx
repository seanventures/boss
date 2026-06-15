"use client";

import { useState } from "react";
import type { CodeTaskRun } from "@boss/api-client";

import { createCodeTaskAction } from "./actions";
import { Markdown } from "./markdown";

const PHASES = ["queued", "provisioning", "coding", "validating", "opening_pr", "done"];
const PHASE_LABEL: Record<string, string> = {
  queued: "Queued",
  provisioning: "Cloning",
  coding: "Coding",
  validating: "Testing",
  opening_pr: "Opening PR",
  done: "Done",
};

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

function CodeTaskDetail({ run, onBack }: { run: CodeTaskRun; onBack: () => void }) {
  const st = run.state ?? {};
  const tests = st.tests ?? [];
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

      {run.error ? <div className="ct-error">⚠ {run.error}</div> : null}

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
