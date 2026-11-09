"use client";

import { changeHref, isLocalChangeUrl } from "../lib/repo-links";
import { GatedButton } from "../components/gated";
import { usd } from "../lib/money";
import { humanDuration } from "../lib/datetime";
import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowsOut, FilePdf, Hammer, Warning, X } from "@phosphor-icons/react";
import { apiWrite } from "../lib/api-write";
import { askConsent, consentFromBody } from "../lib/identity/consent";
import { StartRefusal, asReadiness, type PendingRefusal } from "./workflow-readiness-ui";

/**
 * What the whole run cost, who did it, and what went wrong, read at the end, in one place.
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
  waiting_minutes?: number;
  list_usd?: number;
  total_usd?: number;
  steps?: Step[];
  contributors?: Contributor[];
  findings?: Finding[];
  product?: Product;
  next_steps?: NextStep[];
  host?: { state?: string };
  /** The run's benchmark record (GET /v1/workflow-runs/{id}/benchmark): measured, null = unmeasured. */
  benchmark?: Benchmark | null;
};

export type Benchmark = {
  scenario: string | null; stack: string | null; source_commit: string | null; models: string[];
  run_id: string; result: "success" | "failure" | null; pr_url: string | null;
  tests_passed: number | null; tests_total: number | null; human_approvals: number | null;
  manual_interventions: number | null; elapsed_minutes: number | null; model_cost_usd: number | null;
  retries: number | null; deployment_smoke_test: "pass" | "fail" | "not-run" | null;
};

/** The benchmark record's rows, in the §8 order. A value BOSS did not measure reads "not measured". */
export function benchmarkRows(b: Benchmark): [string, string][] {
  const v = (x: unknown): string => (x === null || x === undefined || x === "" ? "not measured"
    : Array.isArray(x) ? (x.length ? x.join(", ") : "not measured") : String(x));
  return [
    ["Scenario", v(b.scenario)], ["Stack", v(b.stack)], ["Source commit", v(b.source_commit)],
    ["Models", v(b.models)], ["Run", v(b.run_id)], ["Result", v(b.result ?? "still running")],
    ["Pull request", v(b.pr_url)],
    ["Tests", b.tests_total === null || b.tests_passed === null ? "not measured" : `${b.tests_passed}/${b.tests_total} passed`],
    ["Human approvals", v(b.human_approvals)], ["Manual interventions", v(b.manual_interventions)],
    ["Elapsed", b.elapsed_minutes === null ? "not measured" : humanDuration(Number(b.elapsed_minutes) * 60)],
    ["Model cost", b.model_cost_usd === null ? "not measured" : `$${b.model_cost_usd.toFixed(2)}`],
    ["Retries", v(b.retries)], ["Deployment smoke test", v(b.deployment_smoke_test)],
  ];
}

export function BenchmarkRecord({ benchmark, open }: { benchmark: Benchmark; open?: boolean }) {
  return (
    <details className="rr-section" data-testid="run-report-benchmark" open={open}>
      <summary>Benchmark record</summary>
      <table className="rr-table">
        <tbody>
          {benchmarkRows(benchmark).map(([k, val]) => (
            <tr key={k}><td>{k}</td><td data-user-text>{val}</td></tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

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

/** The run's own "Build on this research" verdict, as the server sent it with the run. */
type ResearchAction = { key: string; enabled: boolean } | null | undefined;

/** A run has a report once one of its steps has finished, the rule the panel is shown by. */
export function runHasReport(steps: Array<{ status: string }>): boolean {
  return steps.some((s) => s.status === "succeeded" || s.status === "skipped");
}

/**
 * THE WHOLE REPORT, OPEN, IN A DIALOG. The collapsed panel keeps the run page short, but the
 * operator found the report "buried and not exposed": a finished run is read for what was done,
 * and that should be one press away, every section already open. Same panel, `expanded`.
 */
export function RunReportDialog(
  { runId, projectId, researchAction, onClose }:
    { runId: string; projectId: string; researchAction?: ResearchAction; onClose: () => void },
) {
  const titleId = useId();
  const sheetRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    const opener = typeof document !== "undefined" ? document.activeElement as HTMLElement | null : null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
      if (e.key !== "Tab" || !sheetRef.current) return;
      // Focus stays inside the dialog: Tab past the last control comes back to the first.
      const items = Array.from(sheetRef.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'));
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !sheetRef.current.contains(active))) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && (active === last || !sheetRef.current.contains(active))) {
        e.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, [onClose]);
  const dialog = (
    <div className="bx-np-backdrop rr-dialog-backdrop" data-testid="run-report-dialog-backdrop"
         onPointerDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={sheetRef} className="rr-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}
           data-testid="run-report-dialog">
        <div className="rr-dialog-head">
          <h2 id={titleId} className="rr-dialog-title">Run report</h2>
          <button ref={closeRef} type="button" className="rr-dialog-close" aria-label="Close"
                  data-testid="run-report-dialog-close" onClick={onClose}>
            <X size={16} weight="bold" aria-hidden />
          </button>
        </div>
        <RunReportPanel runId={runId} projectId={projectId} researchAction={researchAction} expanded />
      </div>
    </div>
  );
  return typeof document === "undefined" ? dialog : createPortal(dialog, document.body);
}

/** "Open report": the press that opens the dialog, for the run's own header. */
export function OpenRunReportButton(
  { runId, projectId, researchAction, className, testId }:
    { runId: string; projectId: string; researchAction?: ResearchAction; className?: string; testId?: string },
) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className={className ?? "rr-open"} data-testid={testId ?? "run-open-report"}
              title="The whole report, what was built, tests, cost, who did it, every section open"
              onClick={() => setOpen(true)}>
        <ArrowsOut size={13} weight="bold" aria-hidden /> Open report
      </button>
      {open ? (
        <RunReportDialog runId={runId} projectId={projectId} researchAction={researchAction}
                         onClose={() => setOpen(false)} />
      ) : null}
    </>
  );
}

export function RunReportPanel(
  { runId, projectId, researchAction, expanded = false }:
    { runId: string; projectId: string; researchAction?: ResearchAction;
      /** Every section open and no outer disclosure, the dialog's mode. */
      expanded?: boolean },
) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const closeDialog = useCallback(() => setDialogOpen(false), []);
  // The press must not fold or unfold the <details> it sits in. Cancelled natively, on the
  // button itself, so it is cancelled before the click reaches the <details> whatever the
  // order React's delegated handler would run in.
  const keepFolded = useCallback((el: HTMLButtonElement | null) => {
    el?.addEventListener("click", (e) => e.preventDefault());
  }, []);
  const [report, setReport] = useState<RunReport | null>(null);
  const [pdfHref, setPdfHref] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState("");
  const [refusal, setRefusal] = useState<PendingRefusal | null>(null);
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
      setPdfHref(`/v1/artifacts/${encodeURIComponent(String(body.artifact_id))}/pdf`);
    } catch {
      setProblem("the report could not be saved");
    } finally {
      setSaving(false);
    }
  }, [runId, projectId]);


  // BUILD FROM THIS, turn the findings into the next run, rather than into a note somebody
  // retypes. The directive is composed from the ranked items, so what gets built is exactly
  // what this report said was wrong.
  //
  // `from_run` carries the finished research forward: the server resolves it and adopts the
  // dossier, so the fix-up run argues from evidence already paid for instead of commissioning
  // it again. It deliberately does NOT adopt the panel, a new panel argues the fix.
  //
  // ONLY WHEN THE SERVER OFFERS IT. This sent `from_run` whenever the report had next steps , 
  // a second "Build on this research" deciding for itself, and the server refused it on every
  // run still going ("this run is still going…") and on every run with no completed research
  // ("this run did not research anything"), which is every fix-up run this button starts
  // (review of 0fd6c45). The research is carried exactly when the run's own
  // `build_on_research` action is enabled; otherwise the fix-up starts without it.
  const carriesResearch = researchAction?.key === "build_on_research"
    && researchAction.enabled === true;
  const buildFromThis = useCallback(async (startAnyway: string[] = []) => {
    const items = report?.next_steps ?? [];
    if (!items.length) return;
    setBuilding(true);
    setProblem("");
    setRefusal(null);
    try {
      const name = report?.product?.name || "this app";
      const lines = items
        .filter((n) => n.priority !== "P3")
        .map((n) => `- [${n.priority}] ${n.item}`)
        .join("\n");
      // A P1 means something is unproven or unsafe, not merely untidy, so it gets the fuller
      // path, panel, spec review, preview and a UX review, rather than the quick one.
      const workflowKey = items.some((n) => n.priority === "P1")
        ? "major_feature" : "minor_feature";
      const send = () => apiWrite("/api/delivery-workflows", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "start",
          project_id: projectId,
          workflow_key: workflowKey,
          actor: "executive_console",
          ...(carriesResearch ? { from_run: runId } : {}),
          input: {
            title: `Fix: ${name}`,
            directive:
              `Fix what the last run's report found in ${name}.\n\n${lines}\n\n`
              + "Close each item above. Where an item says something was never verified, "
              + "verify it, do not restate the claim. Do not add features.",
          },
          ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
        }),
      });
      let res = await send();
      let body = await res.json();
      // Needs the person's consent first (409 consent_required): ask, then re-send the same start.
      const asked = !res.ok ? consentFromBody(body) : null;
      if (asked && await askConsent(asked)) { res = await send(); body = await res.json(); }
      const started = body?.run_id || body?.id || body?.run?.id;
      if (!started) {
        // CANNOT RUN HERE YET: the checklist and "Fix anyway", like every other start door.
        // This showed the summary paragraph with no way past it (review of the readiness
        // change).
        const readiness = asReadiness(body?.readiness);
        if (readiness) {
          setRefusal({ label: "Fix", message: String(body?.error ?? ""), readiness,
                       retry: (keys) => buildFromThis(keys) });
          return;
        }
        const said = body?.error || body?.detail;
        setProblem(typeof said === "string" && said ? said : "the run could not be started");
        return;
      }
      setBuiltRunId(String(started));
    } catch {
      setProblem("the run could not be started");
    } finally {
      setBuilding(false);
    }
  }, [report, projectId, runId, carriesResearch]);

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

  // WORKING, WAITING AND WHAT IT COST, SAID APART. A run that waited three hours at its approval
  // read "192.2min, $0.00" for three minutes of work and four model calls on a subscription.
  const waited = report.waiting_minutes ?? 0;
  const listed = report.list_usd ?? 0;
  const billed = report.total_usd ?? 0;
  // In the dialog nothing is folded: the root is a plain block and every section starts open.
  const Root = expanded ? "div" : "details";
  const Summary = expanded ? "div" : "summary";
  const open = expanded ? true : undefined;
  const money = `${done}/${steps.length}, ${report.working_minutes}min working`
    + (waited >= 1 ? `, waited ${humanDuration(waited * 60)} for a person` : "")
    + `, ${usd(billed)}${listed > billed ? ` (${usd(listed)} at list price, on the subscription)` : ""}`;

  return (
    /* THE WHOLE REPORT IS COLLAPSED, and the summary line has to earn the click on its own:
       the app's name, the headline numbers, and whether anything is urgent. Feedback on
       the first version was that it is a lot of text, and it is, which is the point of a
       report and the reason none of it should be open until somebody asks for it. */
    <Root className={expanded ? "rr rr-expanded" : "rr rr-root"}
          data-testid={expanded ? "run-report-expanded" : "run-report"}>
      <Summary className="rr-root-summary">
        {/* A HEADING, INSIDE THE SUMMARY. Valid, <summary> takes flow content, and it is
            what keeps the panel findable as a landmark once the whole report is collapsed.
            Dropping to a <span> for the type would trade that away for nothing. The type is
            the area-name, the same choice Setup makes for "Already on this install". */}
        <h3 className="area-name rr-title">Run report</h3>
        <span className="rr-root-name">{product.name ?? report.workflow}</span>
        <span className="rr-count">{money}</span>
        {urgent.length ? (
          <span className="rr-root-flag" data-testid="run-report-urgent-count">
            <Warning size={13} weight="bold" aria-hidden /> {urgent.length} P1
          </span>
        ) : null}
        {expanded ? null : (
          /* THE REPORT, ONE PRESS AWAY, every section open. A press on it never toggles the
             disclosure it sits on. */
          <button ref={keepFolded} type="button" className="rr-open rr-open-bar" data-testid="run-report-open"
                  title="Read the whole report, every section open"
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDialogOpen(true); }}>
            <ArrowsOut size={13} weight="bold" aria-hidden /> Open report
          </button>
        )}
      </Summary>
      <div className="rr-head">
        {pdfHref ? (
          <a className="rr-pdf" href={pdfHref} data-testid="run-report-pdf">
            <FilePdf size={15} weight="bold" aria-hidden /> Open the PDF
          </a>
        ) : (
          <GatedButton permission="workflow_run:control" type="button" className="rr-pdf" onClick={download} disabled={saving}
                  data-testid="run-report-save">
            <FilePdf size={15} weight="bold" aria-hidden />
            {saving ? "Preparing…" : "Download as PDF"}
          </GatedButton>
        )}
      </div>

      {/* WHAT IT IS, before any number. The report is about the thing that was built. */}
      {product.prd?.summary ? (
        <p className="rr-lede" data-testid="run-report-summary">{product.prd.summary}</p>
      ) : null}
      {product.preview_url ? (
        <p className="rr-try">
          <a href={product.preview_url} data-testid="run-report-preview">Try it</a>
          <span className="detail">, preview {product.preview_status ?? "unknown"}</span>
        </p>
      ) : null}

      {problem ? <p className="rr-problem">{problem}</p> : null}
      {refusal ? (
        <StartRefusal refusal={refusal} projectId={projectId} busy={building}
                      onDismiss={() => setRefusal(null)} />
      ) : null}

      {/* P1s are the reason to open this panel at all, so they are not behind a disclosure. */}
      {urgent.length ? (
        <div className="rr-urgent" data-testid="run-report-urgent">
          {urgent.map((n, i) => (
            <p key={i}><Warning size={13} weight="bold" aria-hidden /> <b>P1</b>, {n.item}</p>
          ))}
        </div>
      ) : null}

      <details className="rr-section" open={open}>
        <summary>What shipped<span className="rr-count">
          {build.files_changed ? `${build.files_changed} files` : "—"}
        </span></summary>
        <p className="rr-facts">
          {build.files_changed ? `${build.files_changed} files changed` : null}
          {build.test_files?.length ? `, ${build.test_files.length} test files` : null}
          {build.pull_request_url ? (
            <>, <a href={changeHref(build.pull_request_url)}>{isLocalChangeUrl(build.pull_request_url) ? "change set" : "pull request"}</a></>
          ) : null}
        </p>
        {build.summary ? (
          <p className="rr-note"><b>Engineer&rsquo;s note.</b> {build.summary}</p>
        ) : null}
      </details>

      {product.screens?.length ? (
        <details className="rr-section" open={open}>
          <summary>How to use it<span className="rr-count">{product.screens.length} screens</span></summary>
          <ol className="rr-screens">
            {product.screens.map((screen) => (
              <li key={screen.name}><b>{screen.name}</b>, {screen.does}</li>
            ))}
          </ol>
        </details>
      ) : null}

      <details className="rr-section" open={open}>
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
        <details className="rr-section" open={open}>
          <summary>Acceptance criteria<span className="rr-count">{product.acceptance.length}</span></summary>
          <ul className="rr-acs">
            {product.acceptance.map((ac) => (
              <li key={ac.id}><b>{ac.id}</b>, {ac.text}</li>
            ))}
          </ul>
          {/* Never ticked without evidence: a tick beside a criterion nobody checked is the
              exact claim a reader repeats to somebody else. */}
          <p className="rr-note rr-caveat">
            Not verified against the build, nothing in this run tested them.
          </p>
        </details>
      ) : null}

      {nextSteps.length ? (
        <details className="rr-section" open={open}>
          <summary>What to do next<span className="rr-count">{nextSteps.length}</span></summary>
          {builtRunId ? (
            <p className="rr-built" data-testid="run-report-built">
              Started, run <code>{builtRunId}</code> is fixing these.
            </p>
          ) : (
            <GatedButton permission="workflow:publish" type="button" className="rr-build" onClick={() => void buildFromThis()}
                    disabled={building} data-testid="run-report-build">
              <Hammer size={15} weight="bold" aria-hidden />
              {building ? "Starting…" : "Build from this"}
            </GatedButton>
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

      <details className="rr-section" open={open}>
        <summary>
          Run facts
          <span className="rr-count">
            {done}/{steps.length}, {report.working_minutes}min, {usd(report.total_usd ?? 0)}
          </span>
        </summary>
        {lost > 0 ? (
          <p className="rr-note" data-testid="run-report-lost">
            The machine took {lost.toFixed(1)} of the {report.elapsed_minutes} elapsed minutes.
          </p>
        ) : null}
        {serious.map((f, i) => (
          <p key={i} className="rr-note"><b>{f.area}</b>, {f.finding}</p>
        ))}
        <table className="rr-table">
          <thead><tr><th>Agent</th><th>Turns</th><th>Cost</th></tr></thead>
          <tbody>
            {(report.contributors ?? []).map((c) => (
              <tr key={c.role}>
                <td>{c.role}</td>
                <td>{c.turns || "—"}</td>
                <td>{usd(c.usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      {report.benchmark ? <BenchmarkRecord benchmark={report.benchmark} open={open} /> : null}
      {dialogOpen ? (
        <RunReportDialog runId={runId} projectId={projectId} researchAction={researchAction}
                         onClose={closeDialog} />
      ) : null}
    </Root>
  );
}
