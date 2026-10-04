"use client";

/**
 * Outputs (factory spec §4.5.5): eight rows, one line each — label · the server's word · the face
 * of the agent who made it — expanding in place, one at a time. Every collapsed value is the
 * server's `word`; nothing here counts tests (only commands are real) or draws a diff (hunks are
 * not served: `View diff ↗` goes to the PR).
 */
import { CaretDown, CaretRight, Check, X } from "@phosphor-icons/react";
import React, { useState } from "react";

import type { CodeTaskRun } from "@boss/api-client";
import type { Cockpit, CockpitOutputs, ReviewComment } from "../../lib/cockpit";
import { previewWebHref } from "../../lib/browser-links";
import { callAction } from "../../lib/call-action";
import { startPreviewAction } from "../actions";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { DeploymentLine } from "./legacy-build-parts";
import { AgFaceStack } from "../agents/agent-face";
import { nameOf, RunFace } from "../agents/run-face";
import { resolveComment, stopPreview } from "./cockpit-actions";
import { kilo, money, whoName } from "./cockpit-util";

export type OutputKey = "pr" | "checks" | "tests" | "changes" | "comments" | "preview" | "deploy" | "cost";

const LABELS: Record<OutputKey, string> = {
  pr: "Pull request", checks: "Checks", tests: "Tests", changes: "Changes", comments: "Comments",
  preview: "Preview", deploy: "Deploy", cost: "Cost",
};
const SEVERITY_ORDER = ["blocker", "major", "minor", "nit"] as const;
const SEVERITY_WORD: Record<string, string> = { blocker: "Blocker", major: "Major", minor: "Minor", nit: "Nit" };
const STATUS_WORD: Record<string, string> = { open: "Open", addressed: "Fixed", dismissed: "Dismissed" };

/** Which row opens by itself: failed checks, or the PR when it is waiting on you (§4.7). */
export function defaultOpen(c: Pick<Cockpit, "state" | "outputs">): OutputKey | null {
  const failed = c.outputs.checks?.gates.some((g) => !g.ok && !g.waivedBy) && !c.outputs.checks?.advisory;
  if (c.state.state === "needs_waiver" || (failed && c.state.state !== "pr_open")) return "checks";
  if (c.state.state === "pr_open") return "pr";
  return null;
}

function Maker({ roleKey, boss }: { roleKey?: string | null; boss?: boolean }) {
  if (roleKey) {
    return <span className="fx-maker" data-role={roleKey} title={`Made by ${nameOf(roleKey).name}`}><RunFace roleKey={roleKey} size={20} state="idle" /></span>;
  }
  if (boss) return <span className="fx-maker" data-face="boss" title="BOSS machinery. No agent at this station."><RunFace roleKey={null} variant="boss" size={20} state="idle" /></span>;
  return null;
}

function valueOf(key: OutputKey, o: CockpitOutputs): string {
  switch (key) {
    case "pr": return o.pr?.word || "Not yet";
    case "checks": return o.checks?.word || "Not yet";
    case "tests": return o.tests?.word || "Not run";
    case "changes": return o.changes?.word || "Not yet";
    case "comments": return o.comments.word || "None";
    case "preview": return o.preview?.word || "Not running";
    case "deploy": return o.deploy.word || "Not set up";
    case "cost": return o.cost.word || "—";
  }
}

export type OutputsProps = {
  projectId: string;
  actor: string;
  cockpit: Cockpit;
  open: OutputKey | null;
  onOpen: (key: OutputKey | null) => void;
  onLogs: (tab: "build" | "preview" | "deploy") => void;
  onRefresh: () => void;
};

export function Outputs({ projectId, actor, cockpit: c, open, onOpen, onLogs, onRefresh }: OutputsProps) {
  const o = c.outputs;
  const keys: OutputKey[] = ["pr", "checks", "tests", "changes", "comments", "preview", "deploy", "cost"];
  const makers: Partial<Record<OutputKey, React.ReactNode>> = {
    pr: <Maker roleKey={o.pr?.roleKey} />,
    tests: <Maker roleKey={o.tests?.roleKey} />,
    changes: <Maker roleKey={o.changes?.codeRoleKey ?? o.changes?.testsRoleKey} />,
    comments: (() => {
      const authors = [...new Set(o.comments.items.map((x) => x.authorRoleKey).filter(Boolean) as string[])];
      return authors.length ? <AgFaceStack roleKeys={authors} size={20} max={3} /> : null;
    })(),
    preview: o.preview ? <Maker boss /> : null,
    deploy: o.deploy.items.length ? <Maker boss /> : null,
    cost: o.cost.byAgent.length ? <AgFaceStack roleKeys={o.cost.byAgent.map((a) => a.roleKey).filter(Boolean) as string[]} size={20} max={3} /> : null,
  };
  return (
    <section className="fx-outputs" aria-labelledby="fx-outputs-h" data-testid="fx-outputs" tabIndex={-1}>
      <header className="fx-sec-head">
        <h2 id="fx-outputs-h" className="fx-sec-title">OUTPUTS</h2>
      </header>
      <span className="fx-ready" data-ready={o.mergeReady.ready || undefined} data-testid="fx-merge-ready"
            title="From checks, tests and review comments">
        {o.mergeReady.ready ? <Check size={12} weight="bold" aria-hidden /> : null}
        {o.mergeReady.word || "Building"}
      </span>
      <ul className="fx-out-list">
        {keys.map((k) => {
          const isOpen = open === k;
          return (
            <li key={k} className="fx-out" data-key={k} data-open={isOpen || undefined} data-testid={`fx-out-${k}`}>
              <button type="button" className="fx-out-head" aria-expanded={isOpen} data-testid={`fx-out-head-${k}`}
                      title={k === "changes" ? "Files only. Open the PR for the diff." : undefined}
                      onClick={() => onOpen(isOpen ? null : k)}>
                <span className="fx-out-label">{LABELS[k]}</span>
                <span className="fx-out-value" data-testid={`fx-out-value-${k}`}
                      title={k === "cost" && o.cost.word === "Included in plan" ? "Ran on a subscription. No metered spend."
                        : k === "cost" ? "Metered model spend so far"
                        : k === "checks" && c.outputs.checks?.advisory ? "First pass: checks advise, they don't block" : undefined}>
                  {valueOf(k, o)}
                </span>
                <span className="fx-out-maker">{makers[k]}</span>
                {isOpen ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
              </button>
              {isOpen ? (
                <div className="fx-out-body" data-testid={`fx-out-body-${k}`}>
                  {k === "pr" ? <PrBody c={c} /> : null}
                  {k === "checks" ? <ChecksBody c={c} /> : null}
                  {k === "tests" ? <TestsBody c={c} /> : null}
                  {k === "changes" ? <ChangesBody c={c} /> : null}
                  {k === "comments" ? <CommentsBody c={c} actor={actor} onRefresh={onRefresh} /> : null}
                  {k === "preview" ? <PreviewBody c={c} actor={actor} onLogs={() => onLogs("preview")} onRefresh={onRefresh} /> : null}
                  {k === "deploy" ? <DeployBody c={c} projectId={projectId} actor={actor} onLogs={() => onLogs("deploy")} /> : null}
                  {k === "cost" ? <CostBody c={c} /> : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function PrBody({ c }: { c: Cockpit }) {
  const pr = c.outputs.pr;
  if (!pr) return <span className="fx-muted">Not yet</span>;
  const card = c.attention.find((a) => a.decisionKind === "pull_request" || a.decisionId === pr.decisionId);
  return (
    <span className="fx-out-links">
      {pr.url ? <a className="ag-link" href={pr.url} target="_blank" rel="noreferrer" data-testid="fx-pr-open">Open PR ↗</a> : null}
      {pr.filesUrl || pr.url ? (
        <a className="ag-link" href={pr.filesUrl || `${pr.url}/files`} target="_blank" rel="noreferrer">View diff ↗</a>
      ) : null}
      {card?.decisionId ? (
        <a className="ag-link" href={`?area=decisions&decision=${encodeURIComponent(card.decisionId)}`} data-testid="fx-pr-review">Review ↗</a>
      ) : null}
      {pr.madeBy > 0 ? <span className="fx-muted">Made by {pr.madeBy} {pr.madeBy === 1 ? "agent" : "agents"}</span> : null}
    </span>
  );
}

function ChecksBody({ c }: { c: Cockpit }) {
  const checks = c.outputs.checks;
  if (!checks || !checks.gates.length) return <span className="fx-muted">{checks?.word || "After review"}</span>;
  return (
    <ul className="fx-gates">
      {checks.gates.map((g) => (
        <li key={g.name} className="fx-gate" data-ok={g.ok} data-testid={`fx-gate-${g.name}`} title={g.evidence || undefined}>
          {g.ok ? <Check size={12} weight="bold" aria-hidden /> : <X size={12} weight="bold" aria-hidden />}
          <span className="ag-sr">{g.ok ? "passed" : "failed"}</span>
          <span>{g.label}</span>
          {g.waivedBy ? <span className="fx-muted">Waived by {g.waivedBy}</span> : null}
          <Maker roleKey={g.roleKey} boss={!g.roleKey} />
        </li>
      ))}
    </ul>
  );
}

function TestsBody({ c }: { c: Cockpit }) {
  const t = c.outputs.tests;
  const [show, setShow] = useState(false);
  if (!t || !t.commands.length) return <span className="fx-muted">Not run</span>;
  return (
    <span className="fx-out-col">
      {t.commands.map((cmd, i) => (
        <span key={i} className="fx-cmd" data-ok={cmd.ok}>
          <span className="fx-mono" data-user-text>{cmd.command}</span>
          <span> · exit {cmd.exitCode ?? "?"}</span>
          <span> · {cmd.timedOut ? "timed out" : cmd.ok ? "ok" : "failed"}</span>
        </span>
      ))}
      {t.outputTail ? (
        <>
          <button type="button" className="ag-link" aria-expanded={show} data-testid="fx-tests-output" onClick={() => setShow((v) => !v)}>
            Show output {show ? "▴" : "▾"}
          </button>
          {show ? <pre className="fx-log fx-log-small" data-user-text>{t.outputTail}</pre> : null}
        </>
      ) : null}
    </span>
  );
}

function ChangesBody({ c }: { c: Cockpit }) {
  const ch = c.outputs.changes;
  if (!ch || (!ch.tests.length && !ch.code.length)) return <span className="fx-muted">Not yet</span>;
  const diff = ch.diffUrl || c.outputs.pr?.filesUrl || (c.outputs.pr?.url ? `${c.outputs.pr.url}/files` : undefined);
  const group = (name: string, files: { path: string; bytes?: number }[], roleKey: string | null) => files.length ? (
    <span className="fx-out-col" data-group={name}>
      <span className="fx-out-sub">{name} <Maker roleKey={roleKey} /></span>
      {files.map((f) => (
        <span key={f.path} className="fx-file">
          <span className="fx-mono fx-trunc" title={f.path} data-user-text>{f.path}</span>
          {typeof f.bytes === "number" ? <span className="fx-muted"> · {f.bytes} bytes</span> : null}
        </span>
      ))}
    </span>
  ) : null;
  return (
    <span className="fx-out-col">
      {group("Tests", ch.tests, ch.testsRoleKey)}
      {group("Code", ch.code, ch.codeRoleKey)}
      {diff ? <a className="ag-link" href={diff} target="_blank" rel="noreferrer">View diff ↗</a> : null}
    </span>
  );
}

function CommentsBody({ c, actor, onRefresh }: { c: Cockpit; actor: string; onRefresh: () => void }) {
  const items = c.outputs.comments.items;
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  if (!items.length) return <span className="fx-muted">None</span>;
  const act = async (cm: ReviewComment, status: "addressed" | "dismissed") => {
    setBusy(cm.id);
    setError("");
    const r = await resolveComment(c.run.id, cm.id, actor, status);
    setBusy(null);
    if (r.ok) { showToast("success", "Comment resolved"); notifyChanged("code-tasks"); onRefresh(); }
    else setError(r.error ?? "");
  };
  return (
    <span className="fx-out-col">
      {error ? <span className="fx-err" role="alert" data-user-text>{error}</span> : null}
      {SEVERITY_ORDER.map((sev) => {
        const group = items.filter((x) => x.severity === sev);
        if (!group.length) return null;
        return (
          <span key={sev} className="fx-out-col" data-severity={sev}>
            <span className="fx-out-sub">{SEVERITY_WORD[sev]}</span>
            {group.map((cm) => (
              <span key={cm.id} className="fx-cmt" data-status={cm.status} data-testid={`fx-comment-${cm.id}`}>
                <span className="fx-cmt-head">
                  <Maker roleKey={cm.authorRoleKey} />
                  {cm.path ? <span className="fx-mono fx-trunc" title={cm.path} data-user-text>{cm.path}{cm.line ? `:${cm.line}` : ""}</span> : null}
                  <span className="fx-muted">{STATUS_WORD[cm.status] ?? cm.status}</span>
                </span>
                <span className="fx-clamp" title={cm.body} data-user-text>{cm.body}</span>
                {cm.status === "open" ? (
                  <span className="fx-out-links">
                    <button type="button" className="ag-btn" disabled={busy !== null} data-testid={`fx-resolve-${cm.id}`}
                            onClick={() => void act(cm, "addressed")}>Resolve</button>
                    <button type="button" className="ag-link" disabled={busy !== null} data-testid={`fx-dismiss-${cm.id}`}
                            onClick={() => void act(cm, "dismissed")}>Dismiss</button>
                  </span>
                ) : null}
              </span>
            ))}
          </span>
        );
      })}
    </span>
  );
}

function PreviewBody({ c, actor, onLogs, onRefresh }: { c: Cockpit; actor: string; onLogs: () => void; onRefresh: () => void }) {
  const p = c.outputs.preview;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const start = async () => {
    setBusy(true);
    setError("");
    const form = new FormData();
    form.set("run_id", c.run.id);
    form.set("actor", actor);
    const res = await callAction(startPreviewAction)(null, form);
    setBusy(false);
    if (res?.error) setError(res.error);
    else { showToast("success", "Preview starting"); notifyChanged("previews"); onRefresh(); }
  };
  const stop = async () => {
    if (!p) return;
    setBusy(true);
    const r = await stopPreview(p.id, actor);
    setBusy(false);
    if (!r.ok) setError(r.error ?? ""); else { notifyChanged("previews"); onRefresh(); }
  };
  const canRun = ["pr_open", "merged", "deployed", "done_no_pr"].includes(c.state.state);
  const err = error ? <span className="fx-err" role="alert" title={error}>Couldn't start</span> : null;
  if (!p) {
    return (
      <span className="fx-out-links">
        <button type="button" className="ag-btn" disabled={busy || !canRun} data-testid="fx-preview-run"
                title={canRun ? undefined : "Runs once the build has finished"} onClick={() => void start()}>
          Run preview
        </button>
        {err}
      </span>
    );
  }
  const running = p.status === "running";
  return (
    <span className="fx-out-links">
      {running ? <a className="ag-link" href={previewWebHref(p.id)} target="_blank" rel="noreferrer" data-testid="fx-preview-open">Open ↗</a> : null}
      <button type="button" className="ag-btn" disabled={busy} data-testid="fx-preview-restart" onClick={() => void start()}>Restart</button>
      {running ? <button type="button" className="ag-link" disabled={busy} data-testid="fx-preview-stop" onClick={() => void stop()}>Stop</button> : null}
      <button type="button" className="ag-link" onClick={onLogs} data-testid="fx-preview-logs">Logs ▾</button>
      {p.expiresAt ? <span className="fx-muted" title={p.expiresAt}>Expires {new Date(p.expiresAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span> : null}
      {typeof p.stepsDone === "number" && p.stepsTotal ? <span className="fx-muted">{p.stepsDone} of {p.stepsTotal} steps</span> : null}
      {p.error ? <span className="fx-err" title={p.error}>Failed</span> : null}
      {err}
    </span>
  );
}

function DeployBody({ c, projectId, actor, onLogs }: { c: Cockpit; projectId: string; actor: string; onLogs: () => void }) {
  const deployable = ["pr_open", "merged", "deployed", "done_no_pr"].includes(c.state.state);
  return (
    <span className="fx-out-col">
      {/* Today's deployment logic, moved as it is (spec §4.5.5); restyled with the Factory later. */}
      <DeploymentLine run={{ id: c.run.id } as CodeTaskRun} projectId={projectId} actor={actor} deployable={deployable} />
      {c.outputs.deploy.items.length ? <button type="button" className="ag-link" onClick={onLogs}>Logs ▾</button> : null}
    </span>
  );
}

function CostBody({ c }: { c: Cockpit }) {
  const cost = c.outputs.cost;
  const stations = Object.entries(cost.byStation).filter(([, v]) => (v ?? 0) > 0);
  return (
    <span className="fx-out-col">
      {cost.byAgent.map((a, i) => (
        <span key={a.roleKey ?? i} className="fx-cost-row" data-role={a.roleKey ?? undefined}>
          <Maker roleKey={a.roleKey} boss={!a.roleKey} />
          <span>{whoName(a.roleKey)}</span>
          <span className="fx-muted">{a.turns} turns</span>
          <span className="fx-muted" title="Model input and output, in thousands">{kilo(a.tokens)}</span>
          {c.metered && a.usd > 0 ? <span title="Metered model spend so far">{money(a.usd)}</span> : null}
        </span>
      ))}
      {c.metered && stations.length ? (
        <span className="fx-out-links">
          {stations.map(([k, v]) => (
            <span key={k} className="fx-muted">{c.stations.find((s) => s.key === k)?.label ?? k} {money(v ?? 0)}</span>
          ))}
        </span>
      ) : null}
      <a className="ag-link" href={`/v1/code-tasks/${encodeURIComponent(c.run.id)}/retrospective`} target="_blank" rel="noreferrer">
        Retrospective ↗
      </a>
    </span>
  );
}
