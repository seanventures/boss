"use client";

import React, { useCallback, useEffect, useState } from "react";
import { FilePdf, Hammer, Warning } from "@phosphor-icons/react";
import { apiWrite } from "../lib/api-write";

/**
 * What the whole run cost, who did it, and what went wrong — read at the end, in one place.
 *
 * WHY THIS EXISTS. The console could show $2.29 for a build while the research and the panel
 * that produced its spec, $1.74 between them, were recorded in two other tables and shown
 * nowhere. Asked for directly:
 *
 *     "at the end of the workflow I want to generate the report about the issues during the
 *      research, meeting and build - summary will be good, what went wrong including
 *      functionality and system issues, cost consumption, which agent contributed... I wanted
 *      the nice summary just like how we did for research like a downloadable pdf format and
 *      expandable reading"
 *
 * SUMMARY OPEN, DETAIL CLOSED. The four numbers that answer "how did it go" are always
 * visible; everything else is behind a disclosure. A report that opens as four screens of
 * tables is one nobody reads twice.
 */

type Finding = { severity: string; area: string; finding: string; action: string };
type Step = {
  step_id: string; label: string; status: string; attempt: number;
  usd: number; seconds: number | null; work_status?: string | null;
  pull_request_number?: number | null;
  approved_by?: string | null; approved_automatically?: boolean;
};
type Contributor = {
  role: string; calls: number; turns: number; usd: number; models: string[];
};
type Screen = { name: string; does: string };
type Criterion = { id: string; text: string };
type NextStep = { priority: string; area: string; item: string; why: string };
type Product = {
  name?: string;
  prd?: { summary?: string };
  screens?: Screen[];
  acceptance?: Criterion[];
  preview_url?: string;
  preview_status?: string;
  build?: {
    summary?: string; review_summary?: string; files_changed?: number;
    test_files?: string[]; commands_run?: { command: string; ok: boolean }[];
    verdicts?: Record<string, string | null>; pull_request_url?: string | null;
  };
};

export type RunReport = {
  ok: boolean;
  error?: string;
  workflow?: string;
  status?: string;
  elapsed_minutes?: number;
  working_minutes?: number;
  total_usd?: number;
  steps?: Step[];
  contributors?: Contributor[];
  findings?: Finding[];
  product?: Product;
  next_steps?: NextStep[];
  host?: { state?: string };
};

const AREA_TITLES: Record<string, string> = {
  system: "System",
  functionality: "Functionality",
  governance: "Governance",
  efficiency: "Efficiency",
};

function duration(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  return m > 0 ? `${m}m ${seconds % 60}s` : `${seconds}s`;
}

export function RunReportPanel(
  { runId, projectId }: { runId: string; projectId: string },
) {
  const [report, setReport] = useState<RunReport | null>(null);
  const [pdfHref, setPdfHref] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState("");
  const [building, setBuilding] = useState(false);
  const [builtRunId, setBuiltRunId] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(
          `/api/workflow-report?run_id=${encodeURIComponent(runId)}`
            + `&project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" },
        );
        const body = (await res.json()) as RunReport;
        if (alive) setReport(body);
      } catch {
        /* A report that breaks the page it reports on is worse than no report. */
      }
    })();
    return () => { alive = false; };
  }, [runId, projectId]);

  // THE PDF IS MADE ON REQUEST, NOT ON EVERY RENDER. Publishing stores the report as an
  // artifact; the artifact route renders it. Doing that for every run anybody merely looked
  // at would fill the project with documents nobody asked for.
  const download = useCallback(async () => {
    setSaving(true);
    setProblem("");
    try {
      const res = await apiWrite(
        `/api/workflow-report?run_id=${encodeURIComponent(runId)}`
          + `&project_id=${encodeURIComponent(projectId)}`,
        { method: "POST" },
      );
      const body = await res.json();
      if (!body?.artifact_id) {
        setProblem(body?.error || "the report could not be saved");
        return;
      }
      setPdfHref(`/api/artifacts/${body.artifact_id}/pdf`);
    } catch {
      setProblem("the report could not be saved");
    } finally {
      setSaving(false);
    }
  }, [runId, projectId]);


  // BUILD FROM THIS — turn the findings into the next run, rather than into a note somebody
  // retypes. The directive is composed from the ranked items, so what gets built is exactly
  // what this report said was wrong.
  //
  // `from_run` carries the finished research forward: the server resolves it and adopts the
  // dossier, so the fix-up run argues from evidence already paid for instead of commissioning
  // it again. It deliberately does NOT adopt the panel — a new panel argues the fix.
  const buildFromThis = useCallback(async () => {
    const items = report?.next_steps ?? [];
    if (!items.length) return;
    setBuilding(true);
    setProblem("");
    try {
      const name = report?.product?.name || "this app";
      const lines = items
        .filter((n) => n.priority !== "P3")
        .map((n) => `- [${n.priority}] ${n.item}`)
        .join("\n");
      // A P1 means something is unproven or unsafe, not merely untidy, so it gets the fuller
      // path — panel, spec review, preview and a UX review — rather than the quick one.
      const workflowKey = items.some((n) => n.priority === "P1")
        ? "major_feature" : "minor_feature";
      const res = await apiWrite("/api/delivery-workflows", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "start",
          project_id: projectId,
          workflow_key: workflowKey,
          actor: "executive_console",
          from_run: runId,
          input: {
            title: `Fix: ${name}`,
            directive:
              `Fix what the last run's report found in ${name}.\n\n${lines}\n\n`
              + "Close each item above. Where an item says something was never verified, "
              + "verify it — do not restate the claim. Do not add features.",
          },
        }),
      });
      const body = await res.json();
      const started = body?.run_id || body?.id || body?.run?.id;
      if (!started) {
        setProblem(body?.error || body?.detail || "the run could not be started");
        return;
      }
      setBuiltRunId(String(started));
    } catch {
      setProblem("the run could not be started");
    } finally {
      setBuilding(false);
    }
  }, [report, projectId, runId]);

  if (!report) return null;
  if (!report.ok) {
    return (
      <div className="rr" data-testid="run-report">
        <p className="rr-empty">{report.error || "no report for this run yet"}</p>
      </div>
    );
  }

  const product = report.product ?? {};
  const build = product.build ?? {};
  const steps = report.steps ?? [];
  const done = steps.filter((s) => s.status === "succeeded" || s.status === "skipped").length;
  const nextSteps = report.next_steps ?? [];
  const urgent = nextSteps.filter((n) => n.priority === "P1");
  const findings = report.findings ?? [];
  const serious = findings.filter((f) => f.severity !== "info");
  const lost = Math.max(0, (report.elapsed_minutes ?? 0) - (report.working_minutes ?? 0));
  const ran = (build.commands_run ?? []).map((c) => c.command);

  const money = `${done}/${steps.length} · ${report.working_minutes}min · $${(report.total_usd ?? 0).toFixed(2)}`;

  return (
    /* THE WHOLE REPORT IS COLLAPSED, and the summary line has to earn the click on its own:
       the app's name, the headline numbers, and whether anything is urgent. Feedback on
       the first version was that it is a lot of text — and it is, which is the point of a
       report and the reason none of it should be open until somebody asks for it. */
    <details className="rr rr-root" data-testid="run-report">
      <summary className="rr-root-summary">
        {/* A HEADING, INSIDE THE SUMMARY. Valid — <summary> takes flow content — and it is
            what keeps the panel findable as a landmark once the whole report is collapsed.
            Dropping to a <span> for the type would trade that away for nothing. The type is
            the eyebrow, the same choice Setup makes for "Already on this install". */}
        <h3 className="eyebrow rr-title">Run report</h3>
        <span className="rr-root-name">{product.name ?? report.workflow}</span>
        <span className="rr-count">{money}</span>
        {urgent.length ? (
          <span className="rr-root-flag" data-testid="run-report-urgent-count">
            <Warning size={13} weight="bold" aria-hidden /> {urgent.length} P1
          </span>
        ) : null}
      </summary>
      <div className="rr-head">
        {pdfHref ? (
          <a className="rr-pdf" href={pdfHref} data-testid="run-report-pdf">
            <FilePdf size={15} weight="bold" aria-hidden /> Open the PDF
          </a>
        ) : (
          <button type="button" className="rr-pdf" onClick={download} disabled={saving}
                  data-testid="run-report-save">
            <FilePdf size={15} weight="bold" aria-hidden />
            {saving ? "Preparing…" : "Download as PDF"}
          </button>
        )}
      </div>

      {/* WHAT IT IS, before any number. The report is about the thing that was built. */}
      {product.prd?.summary ? (
        <p className="rr-lede" data-testid="run-report-summary">{product.prd.summary}</p>
      ) : null}
      {product.preview_url ? (
        <p className="rr-try">
          <a href={product.preview_url} data-testid="run-report-preview">Try it</a>
          <span className="detail"> · preview {product.preview_status ?? "unknown"}</span>
        </p>
      ) : null}

      {problem ? <p className="rr-problem">{problem}</p> : null}

      {/* P1s are the reason to open this panel at all, so they are not behind a disclosure. */}
      {urgent.length ? (
        <div className="rr-urgent" data-testid="run-report-urgent">
          {urgent.map((n, i) => (
            <p key={i}><Warning size={13} weight="bold" aria-hidden /> <b>P1</b> — {n.item}</p>
          ))}
        </div>
      ) : null}

      <details className="rr-section">
        <summary>What shipped<span className="rr-count">
          {build.files_changed ? `${build.files_changed} files` : "—"}
        </span></summary>
        <p className="rr-facts">
          {build.files_changed ? `${build.files_changed} files changed` : null}
          {build.test_files?.length ? ` · ${build.test_files.length} test files` : null}
          {build.pull_request_url ? (
            <> · <a href={build.pull_request_url}>pull request</a></>
          ) : null}
        </p>
        {build.summary ? (
          <p className="rr-note"><b>Engineer&rsquo;s note.</b> {build.summary}</p>
        ) : null}
      </details>

      {product.screens?.length ? (
        <details className="rr-section">
          <summary>How to use it<span className="rr-count">{product.screens.length} screens</span></summary>
          <ol className="rr-screens">
            {product.screens.map((screen) => (
              <li key={screen.name}><b>{screen.name}</b> — {screen.does}</li>
            ))}
          </ol>
        </details>
      ) : null}

      <details className="rr-section">
        <summary>Does it work<span className="rr-count">
          {ran.length ? `${(build.test_files ?? []).length} written` : "—"}
        </span></summary>
        <table className="rr-table">
          <tbody>
            {Object.entries(build.verdicts ?? {}).map(([name, verdict]) => (
              <tr key={name}>
                <td>{name}</td>
                <td data-verdict={verdict ?? "unknown"}>{verdict ?? "not recorded"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {build.review_summary ? (
          <p className="rr-note"><b>What the reviewer found.</b> {build.review_summary}</p>
        ) : null}
        <p className="rr-note">
          <b>Tests.</b> {(build.test_files ?? []).length} file(s) written. Recorded
          verification: {ran.length ? ran.map((c) => <code key={c}>{c}</code>) : "nothing"}.
        </p>
      </details>

      {product.acceptance?.length ? (
        <details className="rr-section">
          <summary>Acceptance criteria<span className="rr-count">{product.acceptance.length}</span></summary>
          <ul className="rr-acs">
            {product.acceptance.map((ac) => (
              <li key={ac.id}><b>{ac.id}</b> — {ac.text}</li>
            ))}
          </ul>
          {/* Never ticked without evidence: a tick beside a criterion nobody checked is the
              exact claim a reader repeats to somebody else. */}
          <p className="rr-note rr-caveat">
            Not verified against the build — nothing in this run tested them.
          </p>
        </details>
      ) : null}

      {nextSteps.length ? (
        <details className="rr-section">
          <summary>What to do next<span className="rr-count">{nextSteps.length}</span></summary>
          {builtRunId ? (
            <p className="rr-built" data-testid="run-report-built">
              Started — run <code>{builtRunId}</code> is fixing these.
            </p>
          ) : (
            <button type="button" className="rr-build" onClick={buildFromThis}
                    disabled={building} data-testid="run-report-build">
              <Hammer size={15} weight="bold" aria-hidden />
              {building ? "Starting…" : "Build from this"}
            </button>
          )}
          <ul className="rr-next">
            {nextSteps.map((n, i) => (
              <li key={i} data-priority={n.priority}>
                <b>{n.priority}</b> {n.item}
                <span>{n.why}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <details className="rr-section">
        <summary>
          Run facts
          <span className="rr-count">
            {done}/{steps.length} · {report.working_minutes}min · ${(report.total_usd ?? 0).toFixed(2)}
          </span>
        </summary>
        {lost > 0 ? (
          <p className="rr-note" data-testid="run-report-lost">
            The machine took {lost.toFixed(1)} of the {report.elapsed_minutes} elapsed minutes.
          </p>
        ) : null}
        {serious.map((f, i) => (
          <p key={i} className="rr-note"><b>{f.area}</b> — {f.finding}</p>
        ))}
        <table className="rr-table">
          <thead><tr><th>Agent</th><th>Turns</th><th>Cost</th></tr></thead>
          <tbody>
            {(report.contributors ?? []).map((c) => (
              <tr key={c.role}>
                <td>{c.role}</td>
                <td>{c.turns || "—"}</td>
                <td>${c.usd.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </details>
  );
}
