"use client";

/**
 * TEST RUN, from the designer (#512): pin a step's output so it does not run, choose which
 * spending steps may run for real, and start the SAVED version as a test.
 *
 * Every row says, before anything starts, what the test will do with that step: return its pin,
 * run it (it only reads), run it live (you allowed it, with every approval and budget rule a
 * normal run has), or refuse it. The server decides; this only says it first
 * (lib/workflow-test-run.ts).
 */
import React, { useMemo, useState } from "react";

import { useCan } from "../../../components/identity/identity-provider";
import { startTestRun } from "../../../lib/workflow-api";
import type { Capability, SpecStep } from "../../../lib/workflow-editor-types";
import {
  type PinDraft, parsePin, testRunRequest, testRunRows, wouldRefuse,
} from "../../../lib/workflow-test-run";

export function TestRunPanel({ projectId, workflowKey, version, steps, capabilities, dirty,
                               onClose, onStarted }: {
  projectId: string;
  workflowKey: string | null;
  version: number | null;
  steps: SpecStep[];
  capabilities: Capability[];
  /** Unsaved changes are not tested: a test runs the saved version. */
  dirty: boolean;
  onClose(): void;
  onStarted(runId: string): void;
}) {
  // A test run is a run: the same permission as Start (workflow_run:start).
  const startGate = useCan("workflow_run:start");
  const rows = useMemo(() => testRunRows(steps, capabilities), [steps, capabilities]);
  const [drafts, setDrafts] = useState<Record<string, PinDraft>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const built = testRunRequest(rows, drafts);
  const refused = built.ok ? wouldRefuse(rows, built.request) : [];
  const set = (id: string, patch: Partial<PinDraft>) =>
    setDrafts((d) => ({ ...d, [id]: { ...(d[id] ?? { text: "", live: false }), ...patch } }));

  const start = async () => {
    if (!built.ok || !workflowKey || !startGate.allowed) return;
    setBusy(true);
    setError(null);
    try {
      const run = await startTestRun(projectId, { workflow_key: workflowKey, version,
                                                  ...built.request });
      onStarted(run.id);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="wf-notice wf-test-run" role="dialog" aria-label="Test run"
             data-testid="wf-test-run">
      <header>
        <strong>Test run</strong>
        <span className="detail">
          {" "}Runs the saved version{version ? ` (v${version})` : ""} as a test. It is labelled a
          test and left out of your run list.
          {dirty ? " Unsaved changes are not included, save first to test them." : ""}
        </span>
      </header>
      <ul className="wf-test-run-steps">
        {rows.map((row) => {
          const draft = drafts[row.id];
          const parsed = parsePin(draft?.text ?? "");
          const pinned = parsed.ok && parsed.value !== null;
          const outcome = pinned ? "Returns the pinned output"
            : !row.refusedBecause ? (row.pinnable ? "Runs (it only reads)" : "Decides from the run")
            : draft?.live ? "Runs for real, with the usual approvals and budget"
            : `Refused: it ${row.refusedBecause}`;
          return (
            <li key={row.id} data-testid={`wf-test-run-step-${row.id}`}
                data-outcome={pinned ? "pinned" : row.refusedBecause && !draft?.live ? "refused" : "runs"}>
              <div><strong>{row.title}</strong> <span className="detail">{outcome}</span></div>
              {row.pinnable ? (
                <>
                  <label>
                    <span className="detail">Pinned output (JSON object; empty = not pinned)</span>
                    <textarea rows={2} spellCheck={false} value={draft?.text ?? ""}
                              aria-invalid={!parsed.ok || undefined}
                              data-testid={`wf-test-run-pin-${row.id}`}
                              onChange={(e) => set(row.id, { text: e.target.value })} />
                  </label>
                  {!parsed.ok ? <span role="alert" className="detail">{parsed.error}</span> : null}
                </>
              ) : null}
              {row.refusedBecause && !pinned ? (
                <label className="detail">
                  <input type="checkbox" checked={!!draft?.live}
                         data-testid={`wf-test-run-live-${row.id}`}
                         onChange={(e) => set(row.id, { live: e.target.checked })} />
                  {" "}Let it run for real
                </label>
              ) : null}
            </li>
          );
        })}
      </ul>
      {refused.length ? (
        <p className="detail" data-testid="wf-test-run-refused">
          The test will stop at the first of these: {refused.map((r) => r.title).join(", ")}.
          Pin them, or let them run for real.
        </p>
      ) : null}
      {error ? <p role="alert" className="detail">{error}</p> : null}
      <div>
        <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-test-run-start"
                aria-disabled={busy || !built.ok || !workflowKey || !startGate.allowed || undefined}
                data-tip={!startGate.allowed ? startGate.reason ?? undefined
                  : !workflowKey ? "Save the workflow first" : !built.ok ? "Fix the pins first" : undefined}
                onClick={() => { if (!busy && built.ok && workflowKey && startGate.allowed) void start(); }}>
          {busy ? "Starting…" : "Start test run"}
        </button>
        <button type="button" className="wf-link" onClick={onClose}>Close</button>
      </div>
    </section>
  );
}
