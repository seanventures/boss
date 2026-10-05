"use client";

/**
 * The deploy drawer (`deploy=<kind>:<id>`, spec §4.6): everything one deploy is — NEEDS YOU,
 * STEPS, FAILED, PLAN, PREFLIGHT, LOG, HEALTH CHECK, PROMOTION and its actions. One poll
 * (`useDeployDetail`): every 3 s while the deploy is live, none once it settles; an unchanged
 * answer is a 304 and no re-render. Nothing here refreshes the console — the promotion line
 * moves with this poll, not with a page refresh every 5 s as the old timeline did.
 */
import React, { useState } from "react";

import { showToast } from "../client-forms";
import { buildHref, changeHref, logsHref, useDeployDetail, type DeployDetail, type StepState } from "../../lib/infra-view";
import { goConsole } from "./deploy-actions";
import { StepDots } from "./deploy-row";
import { afterWrite, decideDeploy, deployTarget, redeployDelivery, rollbackDeploy, runHealthCheck } from "./infra-actions";
import { CopyButton, ErrorLine, Menu, copyText, dur } from "./infra-util";
import { Drawer, DrawerSection, Skeleton, StateWord } from "./kit";

export { DeployRail, DeployRowView } from "./deploy-row";

const STEP_GLYPH: Record<StepState, string> = { done: "✓", running: "◐", failed: "✗", pending: "○", skipped: "–" };
const LOG_LINES = 200;
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** A console href followed in place (keeps the org). */
function ConsoleLink({ href, children, testId, tip }: { href: string; children: React.ReactNode; testId?: string; tip?: string }) {
  return (
    <a className="inf-link" href={href} data-testid={testId} title={tip}
       onClick={(e) => { if (!(e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0)) { e.preventDefault(); goConsole(href); } }}>
      {children}
    </a>
  );
}

export function DeployDrawer({ projectId, actor, deployRef, onClose, onOpen, onChanged }: {
  projectId: string; actor: string; deployRef: string; onClose: () => void;
  /** Open another deploy (a rollback or redeploy's new ref). */
  onOpen?: (ref: string) => void;
  /** A write here succeeded: the list re-reads too. */
  onChanged?: () => void;
}) {
  const poll = useDeployDetail(projectId, deployRef);
  const d = poll.data;
  const row = d?.row;
  const head = row ? (
    <span className="inf-dd-head">
      <strong className="inf-trunc" data-copy="data">{row.app} · {row.env}</strong>
      <StateWord w={{ word: row.word, tone: row.tone }} testId="inf-deploy-state" />
      {row.sha7 ? <code className="inf-mono" data-copy="data" title={row.refName ? `Ref ${row.refName}` : undefined}>{row.sha7}</code> : null}
      <Menu label="⋯" testId="inf-deploy-menu" tip="More" items={[
        { key: "link", label: "Copy link", testId: "inf-deploy-copy-link", onPick: () => void copyText(window.location.href) },
        { key: "open", label: "Open app ↗", testId: "inf-deploy-menu-open", hidden: !d?.openUrl,
          onPick: () => { if (d?.openUrl) window.open(d.openUrl, "_blank", "noopener"); } },
      ]} />
    </span>
  ) : <strong>Deploy</strong>;

  return (
    <Drawer label={row ? `${row.app} ${row.env} deploy` : "Deploy"} testId="inf-deploy-drawer" width={520} onClose={onClose} head={head}>
      {!d && poll.loading ? <Skeleton testId="inf-deploy-loading" /> : null}
      {!d && poll.error ? <ErrorLine error={poll.error} testId="inf-deploy-load-error" /> : null}
      {d ? <DeployBody d={d} projectId={projectId} actor={actor} refresh={() => afterWrite(poll.refresh, onChanged)}
                       onOpen={onOpen} /> : null}
    </Drawer>
  );
}

function DeployBody({ d, projectId, actor, refresh, onOpen }: {
  d: DeployDetail; projectId: string; actor: string; refresh: () => void; onOpen?: (ref: string) => void;
}) {
  const row = d.row;
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showError, setShowError] = useState(false);
  const [checks, setChecks] = useState<Record<string, unknown>[] | null>(null);
  const kindId = row.ref.slice(row.ref.indexOf(":") + 1);

  const run = async (key: string, fn: () => Promise<{ ok: boolean; error?: string; data?: unknown }>, toast: (data: Record<string, unknown>) => string) => {
    setBusy(key); setError(null);
    const r = await fn();
    setBusy(null);
    if (!r.ok) { setError(r.error ?? "Couldn't do that"); return null; }
    const data = (r.data ?? {}) as Record<string, unknown>;
    showToast("success", toast(data));
    refresh();
    return data;
  };
  const decide = (verdict: "approve" | "reject") => row.decisionId
    ? void run(verdict, () => decideDeploy(row.decisionId as string, actor, verdict), () => (verdict === "approve" ? "Deploy approved" : "Deploy rejected"))
    : undefined;
  const rollback = async () => {
    const data = await run("rollback", () => rollbackDeploy(projectId, row.ref, actor),
      (x) => {
        // A rollback is planned, then approved: the server says which (its `result`).
        if (typeof x.result === "string" && x.result) return x.result;
        const sha = typeof x.sha7 === "string" ? x.sha7 : row.rollbackToSha7;
        return sha ? `Rolling back to ${sha}` : "Rolling back";
      });
    if (data && typeof data.ref === "string") onOpen?.(data.ref);
  };
  // REDEPLOY THE SAME THING. A delivery deploy has its own redeploy route (a new run of the
  // same receipt); a stack or registry deploy is planned again on its target at the same commit
  // through the routed Deploy, so it goes through the same approval as any other plan.
  const redeploy = async () => {
    const data = await run("redeploy", () => (row.kind === "delivery"
      ? redeployDelivery(kindId, actor)
      : deployTarget(projectId, row.targetId ?? "", actor, row.sha7 ?? row.refName)), () => "Redeploy planned · Approve");
    if (data && typeof data.ref === "string") onOpen?.(data.ref);
  };
  const healthCheck = async () => {
    const data = await run("health", () => runHealthCheck(projectId, kindId, actor), () => "Checked");
    if (data) {
      const results = (data.results ?? data.health_check_results ?? data.checks) as unknown;
      if (Array.isArray(results)) setChecks(results as Record<string, unknown>[]);
    }
  };

  const logLines = (d.logTail ?? "").split("\n");
  const log = logLines.slice(-LOG_LINES).join("\n");
  const healthRows = checks ?? d.healthCheckResults;

  return (
    <>
      <ErrorLine error={error} testId="inf-deploy-error" />

      {row.can.approve || row.can.reject ? (
        <DrawerSection label="Needs you" testId="inf-deploy-needs">
          <div className="inf-actions">
            {row.can.approve ? (
              <button type="button" className="ag-btn ag-btn-primary" disabled={busy !== null} data-testid="inf-deploy-approve"
                      onClick={() => decide("approve")}>{busy === "approve" ? "Approving…" : "Approve"}</button>
            ) : null}
            {row.can.reject ? (
              <button type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-deploy-reject"
                      onClick={() => decide("reject")}>{busy === "reject" ? "Rejecting…" : "Reject"}</button>
            ) : null}
          </div>
        </DrawerSection>
      ) : null}

      <DrawerSection label="Steps" testId="inf-deploy-steps">
        {d.waitingOn ? <div className="inf-dd-now"><span className="inf-muted">Now:</span> <span data-copy="data">{d.waitingOn}</span></div> : null}
        {d.steps.length ? (
          <ol className="inf-dd-steps">
            {d.steps.map((s, i) => (
              <li key={i} data-s={s.state}>
                <span className="inf-dd-glyph" data-s={s.state} data-anim={s.state === "running" ? "" : undefined} aria-label={s.state}>
                  {STEP_GLYPH[s.state]}
                </span>
                <span className="inf-trunc" data-copy="data" title={s.title}>{s.title}</span>
                <span className="inf-num inf-faint">{dur(s.durationMs)}</span>
              </li>
            ))}
          </ol>
        ) : <StepDots steps={row.steps} />}
      </DrawerSection>

      {d.failure ? (
        <DrawerSection label="Failed" testId="inf-deploy-failed"
                       actions={d.failure.error ? (
                         <button type="button" className="inf-link" aria-expanded={showError} data-testid="inf-deploy-show-error"
                                 onClick={() => setShowError((v) => !v)}>{showError ? "Hide error" : "Show error"}</button>
                       ) : null}>
          <span className="inf-dd-fail" data-copy="data">{d.failure.line}</span>
          {showError ? <pre className="inf-dlog inf-dlog-short" data-copy="data" data-testid="inf-deploy-error-raw">{d.failure.error}</pre> : null}
        </DrawerSection>
      ) : null}

      {d.plan ? (
        <DrawerSection label="Plan" testId="inf-deploy-plan">
          <details className="inf-dd-plan">
            <summary>{plural(d.plan.steps.length, "step")}{d.plan.envKeys.length ? ` · ${plural(d.plan.envKeys.length, "env key")}` : ""}</summary>
            <ol className="inf-dd-plan-steps" data-testid="inf-deploy-plan-steps">
              {d.plan.steps.map((s, i) => <li key={i} className="inf-mono" data-copy="data">{s}</li>)}
            </ol>
            {d.plan.envKeys.length ? (
              <div className="ag-chips" data-testid="inf-deploy-env-keys" title="Names only. Values stay in the secret store.">
                {d.plan.envKeys.map((k) => <code key={k} data-copy="data">{k}</code>)}
              </div>
            ) : null}
            {d.plan.health ? (
              <div className="inf-muted" data-copy="data">
                Health {String(d.plan.health.path ?? d.plan.health.url ?? "")}{d.plan.health.expect !== undefined ? ` → ${String(d.plan.health.expect)}` : ""}
              </div>
            ) : null}
          </details>
        </DrawerSection>
      ) : null}

      {d.preflight.length ? (
        <DrawerSection label="Preflight" testId="inf-deploy-preflight">
          <ul className="inf-dchecks">
            {d.preflight.map((f, i) => (
              <li key={i} data-ok={f.ok ? "1" : "0"}>
                <span aria-label={f.ok ? "Passed" : "Failed"}>{f.ok ? "✓" : "✗"}</span>
                <span className="inf-trunc" data-copy="data" title={f.detail ?? f.remedy ?? undefined}>{f.label}</span>
              </li>
            ))}
          </ul>
        </DrawerSection>
      ) : null}

      <DrawerSection label="Log" testId="inf-deploy-log" actions={d.logTail ? (
        <span className="inf-actions">
          <CopyButton text={d.logTail} label="Copy" testId="inf-deploy-log-copy" />
          <ConsoleLink href={logsHref("deployment", row.ref)} testId="inf-deploy-full-log">Open full log ↗</ConsoleLink>
        </span>
      ) : null}>
        {d.logTail ? <pre className="inf-dlog" data-copy="data" tabIndex={0}>{log}</pre>
          : <span className="inf-muted">No log yet</span>}
      </DrawerSection>

      {row.kind === "registry" ? (
        <DrawerSection label="Health check" testId="inf-deploy-health-check" actions={(
          <button type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-deploy-run-check"
                  onClick={() => void healthCheck()}>{busy === "health" ? "Checking…" : "Run check"}</button>
        )}>
          {healthRows.length ? (
            <ul className="inf-dchecks">
              {healthRows.map((h, i) => {
                const ok = h.ok === true || h.status === "passed" || h.passed === true;
                const name = String(h.name ?? h.url ?? h.kind ?? `Check ${i + 1}`);
                const extra = [h.status !== undefined && typeof h.status === "number" ? String(h.status) : "",
                  typeof h.latency_ms === "number" ? `${Math.round(h.latency_ms)} ms` : ""].filter(Boolean).join(" · ");
                return (
                  <li key={i} data-ok={ok ? "1" : "0"}>
                    <span aria-label={ok ? "Passed" : "Failed"}>{ok ? "✓" : "✗"}</span>
                    <span className="inf-trunc" data-copy="data" title={name}>{name}</span>
                    {extra ? <span className="inf-num inf-faint">{extra}</span> : null}
                  </li>
                );
              })}
            </ul>
          ) : <span className="inf-muted">Not checked yet</span>}
        </DrawerSection>
      ) : null}

      {d.promotion ? (
        <DrawerSection label="Promotion" testId="inf-deploy-promotion">
          <ol className="inf-promo" aria-label="Promotion">
            {d.promotion.phases.map((p) => (
              <li key={p.key} data-s={p.state} title={`${p.label} · ${p.state}`}>
                <span className="inf-promo-dot" data-s={p.state} aria-hidden />
                <span>{p.label}</span>
              </li>
            ))}
          </ol>
        </DrawerSection>
      ) : null}

      <div className="inf-actions inf-dd-actions">
        {row.can.rollback ? (
          <button type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-deploy-rollback-to"
                  title="Redeploy the last commit that went live here" onClick={() => void rollback()}>
            {busy === "rollback" ? "Rolling back…" : row.rollbackToSha7 ? `Roll back to ${row.rollbackToSha7}` : "Roll back"}
          </button>
        ) : null}
        {row.can.redeploy ? (
          <button type="button" className="ag-btn" disabled={busy !== null || (row.kind !== "delivery" && !row.targetId)}
                  data-testid="inf-deploy-redeploy" title={row.kind === "delivery" ? "Run this deploy again" : "Plan this commit again on its target"}
                  onClick={() => void redeploy()}>{busy === "redeploy" ? "Planning…" : "Redeploy"}</button>
        ) : null}
        {d.openUrl ? (
          <a className="ag-btn" href={d.openUrl} target="_blank" rel="noreferrer" data-testid="inf-deploy-open-app">Open app ↗</a>
        ) : null}
        {d.changesetId ? (
          <ConsoleLink href={changeHref(d.changesetId)} testId="inf-deploy-open-change" tip="Review this deploy's change in Cloud">Open change ↗</ConsoleLink>
        ) : null}
        {row.runId ? (
          <ConsoleLink href={buildHref(row.runId)} testId="inf-deploy-build" tip="Open the build that made this">Build ↗</ConsoleLink>
        ) : null}
      </div>
    </>
  );
}
