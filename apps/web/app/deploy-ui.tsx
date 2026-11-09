"use client";

/**
 * Deployment plans and execution, watched.
 *
 * A deploy request creates a reviewable ChangeSet. Execution is a separate approved action;
 * only a run that is actually running shows progress and step logs.
 *
 * Three parts:
 *   LiveDeployment   , polls a running deployment and draws its steps, timings and logs
 *   DeployWizard     , choose a target and ref, then show its ChangeSet review step
 *   RecentDeployments, the last N DEPLOYMENTS (not every code task that opened a PR)
 */

import React, { useCallback, useEffect, useRef, useState } from "react";

import { consoleHref } from "./console-href";
import { changeHref } from "../lib/infra-view";
import {
  deployTargetAction, getDeliveryDefaultsAction, getDeploymentAction, listRefsAction,
  redeployAction,
} from "./actions";

import { callAction } from "../lib/call-action";
import { detailText } from "../lib/api-error";
import { GatedButton } from "../components/gated";
type Step = {
  step: string; label: string; status: "pending" | "running" | "ok" | "failed" | "skipped";
  duration_ms?: number | null; log?: string; error?: string;
};
/**
 * Where a deployment's change is reviewed: Infra › Cloud › Changes, opened on that change when
 * the id is known (changeHref), else the Changes list. Keeps the rest of the query (org=).
 */
export function changeReviewHref(changesetId?: string | null, search?: string): string {
  const to = changesetId ? changeHref(changesetId) : "?area=infra&view=cloud&cloud=changes";
  return consoleHref(Object.fromEntries(new URLSearchParams(to.slice(1))), search);
}

type Run = {
  deployment_run_id: string; status: string; app_name?: string; environment?: string;
  commit_ref?: string | null; trigger?: string | null; failure_reason?: string | null;
  duration_ms?: number | null; url?: string | null; steps: Step[]; running: boolean;
  changeset_id?: string | null;
  /** A governed host deployment: one decision in Approvals, then BOSS applies it. */
  governed?: boolean; decision_id?: string | null; plan_review?: PlanReview | null;
};

/** What a host deployment plan will do, as the API records it (governed_host_deploy.py).
 *  Secrets are listed by NAME and reference only. */
export type PlanReview = {
  app_name?: string; environment?: string; host?: string; address?: string;
  commit_ref?: string | null; repo?: string | null; app_port?: number | null;
  url?: string | null; steps?: string[]; rollback?: string[];
  secret_refs?: { name: string; ref: string }[];
  cost?: { expected_cost_usd_micros?: number; note?: string };
};

/** Approvals, where a host deployment's one decision waits. */
export const approvalsHref = () =>
  consoleHref({ area: "decisions", decision_filter: "needs_action" });

/** Statuses a governed host deployment passes through before it has a verdict. */
const WAITING = ["awaiting_approval", "approved"];
const DONE_OK = ["succeeded", "deployed", "healthy"];

const box: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,.09)", borderRadius: 8, padding: 12,
};
const btn: React.CSSProperties = {
  fontSize: 12, padding: "5px 12px", borderRadius: 6, cursor: "pointer",
  border: "1px solid rgba(255,255,255,.18)", background: "transparent", color: "inherit",
};
const input: React.CSSProperties = {
  fontSize: 12, padding: "5px 8px", borderRadius: 6, background: "rgba(255,255,255,.04)",
  border: "1px solid rgba(255,255,255,.14)", color: "inherit", minWidth: 170,
};

const ms = (v?: number | null) =>
  v == null ? "" : v < 1000 ? `${v}ms` : v < 60000 ? `${(v / 1000).toFixed(1)}s`
    : `${Math.floor(v / 60000)}m ${Math.round((v % 60000) / 1000)}s`;

/** Spinner drawn with SVG SMIL, this console applies a per-area CSS allow-list, so a new
 *  keyframe would need registering in three places and silently do nothing until it was. */
function Spin({ size = 13 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" aria-hidden="true">
      <circle cx="20" cy="20" r="16" fill="none" stroke="currentColor" strokeWidth="5"
              opacity="0.2" />
      <path d="M20 4 a16 16 0 0 1 16 16" fill="none" stroke="currentColor" strokeWidth="5"
            strokeLinecap="round">
        <animateTransform attributeName="transform" type="rotate" from="0 20 20" to="360 20 20"
                          dur="0.9s" repeatCount="indefinite" />
      </path>
    </svg>
  );
}

const MARK: Record<Step["status"], { icon: string; color: string }> = {
  pending: { icon: "○", color: "#6e7681" },
  running: { icon: "", color: "#58a6ff" },
  ok: { icon: "✓", color: "#3fb950" },
  failed: { icon: "✕", color: "#f85149" },
  skipped: { icon: "–", color: "#8b949e" },
};

/** One step: state, how long it took, and its log when it matters. */
function StepRow({ s, elapsed }: { s: Step; elapsed: number }) {
  // The failed step is the only one anyone opens, so it opens itself.
  const [open, setOpen] = useState(s.status === "failed");
  useEffect(() => { if (s.status === "failed") setOpen(true); }, [s.status]);
  const mark = MARK[s.status] ?? MARK.pending;
  const hasLog = !!(s.log || s.error);

  return (
    <div style={{ borderTop: "1px solid var(--boss-c-08e14993c0)", padding: "7px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
        <span style={{ color: mark.color, width: 14, display: "inline-flex" }}>
          {s.status === "running" ? <Spin /> : mark.icon}
        </span>
        <span style={{ opacity: s.status === "pending" ? 0.5 : 1, flex: 1 }}>{s.label}</span>
        <span className="detail" style={{ fontSize: 12 }}>
          {/* A running step shows the CLOCK, not a guess: it is the only honest number while
              the work is still going. */}
          {s.status === "running" ? `${elapsed}s` : ms(s.duration_ms)}
        </span>
        {hasLog && (
          <button onClick={() => setOpen(!open)}
                  style={{ ...btn, padding: "1px 7px", fontSize: 11 }}>
            {open ? "hide" : "log"}
          </button>
        )}
      </div>
      {s.error && (
        <div style={{ color: "var(--boss-c-f85149)", fontSize: 12, margin: "4px 0 0 24px" }}>{s.error}</div>
      )}
      {open && s.log && (
        <pre style={{ margin: "6px 0 0 24px", fontSize: 11, whiteSpace: "pre-wrap",
                      background: "var(--boss-c-12b43a06c4)", padding: 8, borderRadius: 6,
                      maxHeight: 220, overflow: "auto" }}>{s.log}</pre>
      )}
    </div>
  );
}

export function LiveDeployment({ runId, onFinished }: {
  runId: string; onFinished?: (run: Run) => void;
}) {
  const [run, setRun] = useState<Run | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const done = useRef(false);

  // Poll while running. Stops on a terminal state rather than polling a finished deployment
  // forever, a console that keeps asking after the answer is final is just noise on the API.
  useEffect(() => {
    let live = true;
    let ticks = 0;
    let lastStatus = "";
    const tick = async () => {
      const d = await callAction(getDeploymentAction, undefined, "deployment:read")(runId);
      if (!live || !d || d.error) return;
      lastStatus = d.status;
      setRun(d);
      const isPlan = ["created", "planned"].includes(d.status);
      // Waiting on approval (or queued after it) is not finished: keep watching, so the row
      // turns live by itself once somebody approves.
      const waiting = WAITING.includes(d.status);
      if ((!d.running || isPlan) && !waiting && !done.current) {
        done.current = true;
        if (!isPlan) onFinished?.(d);
      }
    };
    tick();
    // Every 2s while it runs; every 6s while it waits for a person.
    const poll = setInterval(() => {
      ticks += 1;
      if (done.current) return;
      if (WAITING.includes(lastStatus) && ticks % 3) return;
      tick();
    }, 2000);
    const clock = setInterval(() => setElapsed((n) => n + 1), 1000);
    return () => { live = false; clearInterval(poll); clearInterval(clock); };
  }, [runId, onFinished]);

  if (!run) {
    return (
      <div style={box}>
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", fontSize: 13 }}>
          <Spin /> loading deployment record…
        </span>
      </div>
    );
  }

  const failed = ["failed", "blocked", "rolled_back"].includes(run.status);
  const planned = run.status === "planned" || run.status === "created";
  const awaiting = run.status === "awaiting_approval";
  const queued = run.status === "approved";
  const rejected = run.status === "rejected";
  const deployed = DONE_OK.includes(run.status);
  const active = run.running && !planned;
  const tone = active ? "#58a6ff" : failed ? "#f85149"
    : planned || awaiting || queued ? "#d29922" : deployed ? "#3fb950" : "#8b949e";
  const runningStep = run.steps.find((s) => s.status === "running");

  return (
    <div style={{ ...box, ...(failed ? { borderColor: "var(--boss-c-4e6d71bb7a)" } : {}) }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ color: tone, fontWeight: 600, display: "inline-flex",
                       alignItems: "center", gap: 7 }}>
          {active && <Spin size={14} />}
          {active ? "Deploying"
            : run.status === "rolled_back" ? "Rolled back"
            : failed ? "Deployment failed"
            : awaiting ? "Waiting for approval"
            : queued ? "Approved, starting"
            : rejected ? "Rejected, nothing deployed"
            : planned ? "Awaiting ChangeSet review" : deployed ? "Deployed" : run.status}
        </span>
        <strong style={{ fontSize: 13 }}>{run.app_name}</strong>
        {run.environment && <span className="cap-badge tone-blue">{run.environment}</span>}
        {run.commit_ref && (
          <span className="detail" style={{ fontFamily: "monospace", fontSize: 12 }}>
            {run.commit_ref.slice(0, 12)}
          </span>
        )}
        <span style={{ flex: 1 }} />
        <span className="detail" style={{ fontSize: 12 }}>
          {active ? `${elapsed}s elapsed` : ms(run.duration_ms) ? `took ${ms(run.duration_ms)}` : ""}
        </span>
      </div>

      {active && runningStep && (
        <div className="detail" style={{ fontSize: 12, marginTop: 4 }}>
          {runningStep.label}…
        </div>
      )}

      <div style={{ marginTop: 8 }}>
        {run.steps.map((s) => (
          <StepRow key={s.step} s={s}
                   elapsed={s.status === "running" ? elapsed : 0} />
        ))}
      </div>

      {planned && (
        <div className="detail" style={{ fontSize: 12, marginTop: 8 }}>
          No deployment is running. Review, approve, and apply its ChangeSet in{" "}
          <a href={changeReviewHref(run.changeset_id)}>ProviderOps ChangeSet Review</a>.
        </div>
      )}
      {awaiting && (
        <div style={{ marginTop: 8 }}>
          {run.plan_review && <PlanReviewDetails review={run.plan_review} />}
          <div className="detail" style={{ fontSize: 12, marginTop: 6 }}>
            Nothing runs until it is approved.{" "}
            <a href={approvalsHref()}>Open in Approvals</a>
          </div>
        </div>
      )}
      {queued && (
        <div className="detail" style={{ fontSize: 12, marginTop: 8 }}>
          Approved. BOSS starts it in a moment; steps appear here.
        </div>
      )}
      {deployed && run.url && (
        <div style={{ marginTop: 10 }}>
          <a href={`http://${run.url}/`} target="_blank" rel="noreferrer"
             style={{ fontSize: 13 }}>Open {run.url} ↗</a>
        </div>
      )}
      {/* The run-level reason ONLY when no step owns the failure. When a step failed it has
          already shown the same text twice over (its error line and its log), and printing it
          a third time reads as three different problems. */}
      {failed && run.failure_reason && !run.steps.some((s) => s.status === "failed") && (
        <div className="detail" style={{ color: "var(--boss-c-f85149)", fontSize: 12, marginTop: 8 }}>
          {readableFailure(run.failure_reason).slice(0, 400)}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Deploy wizard
// --------------------------------------------------------------------------- //
type WizardTarget = {
  id: string; app_name: string; environment: string; repo: string; status: string;
  repo_owner?: string | null; domains?: string[] | null; app_entry?: string | null;
  auto_deploy?: boolean; paused?: boolean;
};

export type PlannedDeployment = {
  deployment_run_id: string;
  changeset_id: string;
  next_action: "approve_and_apply_changeset" | "approve_decision";
  /** A host deployment: the one decision to approve, and what it will do. */
  decision_id?: string;
  review?: PlanReview;
};

export function plannedDeploymentFrom(value: unknown): PlannedDeployment | null {
  if (!value || typeof value !== "object") return null;
  const out = value as Record<string, unknown>;
  if (out.status !== "planned" || typeof out.deployment_run_id !== "string"
      || !out.deployment_run_id || typeof out.changeset_id !== "string"
      || !out.changeset_id) return null;
  if (out.next_action === "approve_decision" && typeof out.decision_id === "string"
      && out.decision_id) {
    return {
      deployment_run_id: out.deployment_run_id,
      changeset_id: out.changeset_id,
      next_action: "approve_decision",
      decision_id: out.decision_id,
      review: (out.plan_review && typeof out.plan_review === "object"
        ? out.plan_review : {}) as PlanReview,
    };
  }
  if (out.next_action !== "approve_and_apply_changeset") return null;
  return {
    deployment_run_id: out.deployment_run_id,
    changeset_id: out.changeset_id,
    next_action: "approve_and_apply_changeset",
  };
}

/** What a host deployment plan will do: where, which commit, rollback and cost. */
export function PlanReviewDetails({ review }: { review: PlanReview }) {
  const where = [review.host, review.environment].filter(Boolean).join(", ");
  const micros = review.cost?.expected_cost_usd_micros ?? 0;
  return (
    <div className="detail" style={{ fontSize: 12, display: "grid", gap: 4 }}
         data-testid="plan-review">
      <div>
        <strong>{review.app_name || "App"}</strong>{where && <> → {where}</>}
        {review.commit_ref && <> at <code>{String(review.commit_ref).slice(0, 12)}</code></>}
        {review.app_port ? <> on port {review.app_port}</> : null}
      </div>
      {!!review.steps?.length && <div>Steps: {review.steps.join(" → ")}</div>}
      {!!review.secret_refs?.length && (
        <div>Secrets (by reference): {review.secret_refs.map((s) => s.name).join(", ")}</div>
      )}
      {!!review.rollback?.length && <div>Rollback: {review.rollback[0]}</div>}
      <div>Cost: ${(micros / 1_000_000).toFixed(2)}{review.cost?.note ? `, ${review.cost.note}` : ""}</div>
    </div>
  );
}

export function ChangeSetNextStep({ plan }: { plan: PlannedDeployment }) {
  if (plan.next_action === "approve_decision") {
    return (
      <div style={{ ...box, borderColor: "var(--boss-c-a26981c4ba)" }} role="status">
        <strong>Plan ready, waiting for approval</strong>
        <div style={{ marginTop: 6 }}>
          <PlanReviewDetails review={plan.review || {}} />
        </div>
        <div className="detail" style={{ fontSize: 12, marginTop: 6 }}>
          Nothing has deployed. <a href={approvalsHref()}>Approve it in Approvals</a>; BOSS
          then deploys and health-checks it here.
        </div>
      </div>
    );
  }
  return (
    <div style={{ ...box, borderColor: "var(--boss-c-a26981c4ba)" }} role="status">
      <strong>Deployment plan created</strong>
      <div className="detail" style={{ fontSize: 12, marginTop: 6 }}>
        Nothing has deployed. ChangeSet <code>{plan.changeset_id}</code> is ready for review.
        Next, open <a href={changeReviewHref(plan.changeset_id)}>ProviderOps ChangeSet Review</a>,
        approve it, then apply it. Deployment run <code>{plan.deployment_run_id}</code>
        records the plan.
      </div>
    </div>
  );
}

export function DeployWizard({ targets }: { targets: WizardTarget[] }) {
  const [open, setOpen] = useState(false);
  const [targetId, setTargetId] = useState("");
  const [ref, setRef] = useState("");
  const [refs, setRefs] = useState<{ name: string; sha: string }[]>([]);
  const [refsError, setRefsError] = useState<string | null>(null);
  const [defaultOwner, setDefaultOwner] = useState("");
  const [plan, setPlan] = useState<PlannedDeployment | null>(null);
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const active = targets.filter((t) => t.status === "active");
  const target = active.find((t) => t.id === targetId);

  // Who owns the repo BOSS clones when a target does not say. Fetched once, on open.
  useEffect(() => {
    if (!open || defaultOwner) return;
    callAction(getDeliveryDefaultsAction, undefined, "deployment:read")().then((d) => { if (d?.owner) setDefaultOwner(d.owner); });
  }, [open, defaultOwner]);

  /** Branches for a target's repo.
   *
   * The owner comes from the repo string, else the target, else the project default. The first
   * version used only the repo string, which for `boss-sandbox` produced an empty owner, a
   * request to `/repos//boss-sandbox/refs`, a 404, and a branch picker that silently listed
   * nothing. A picker that cannot fail visibly is worse than a plain text box. */
  const loadRefs = useCallback(async (t: WizardTarget, fallbackOwner: string) => {
    const repo = t.repo || "";
    if (!repo) return;
    const [owner, name] = repo.includes("/")
      ? [repo.split("/")[0], repo.split("/").slice(1).join("/")]
      : [t.repo_owner || fallbackOwner, repo];
    if (!owner) { setRefsError("no repo owner configured for this app"); return; }
    setRefsError(null);
    const d = await callAction(listRefsAction, undefined, "repository:read")(owner, name);
    if (d?.error || !d?.branches) {
      setRefsError(`could not list branches for ${owner}/${name}`);
      return;
    }
    setRefs(d.branches);
    if (d.default_branch) setRef((r) => r || d.default_branch);
  }, []);

  const start = async () => {
    if (!targetId) { setErr("choose which app to deploy"); return; }
    setErr(null); setStarting(true);
    const out = await callAction(deployTargetAction, undefined, "deployment:create")(targetId, ref);
    setStarting(false);
    if (out?.error) {
      setErr(typeof out.error === "string" ? out.error : detailText(out.error) || "It did not go through.");
      return;
    }
    const nextPlan = plannedDeploymentFrom(out);
    if (!nextPlan) {
      setErr("Deployment request did not return a reviewable ChangeSet. No deployment was started.");
      return;
    }
    setPlan(nextPlan);
  };

  if (!open) {
    return <button style={{ ...btn, padding: "7px 16px" }} onClick={() => setOpen(true)}>
      Plan a deployment
    </button>;
  }

  return (
    <section className="ct-block">
      <div className="ct-block-title">Plan a deployment</div>

      {!plan && (
        <div style={{ ...box, display: "grid", gap: 12 }}>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <label style={{ display: "grid", gap: 3 }}>
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>app</span>
              <select style={input} value={targetId}
                      onChange={(e) => {
                        setTargetId(e.target.value);
                        const t = active.find((x) => x.id === e.target.value);
                        setRefs([]); setRef(""); setRefsError(null);
                        if (t) loadRefs(t, defaultOwner);
                      }}>
                <option value="">,  choose , </option>
                {active.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.app_name}, {t.environment}
                  </option>
                ))}
              </select>
            </label>

            <label style={{ display: "grid", gap: 3 }}>
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>
                branch or sha
              </span>
              {/* A real select once the branches are known: a datalist looks like an empty
                  text box when the fetch fails, which is precisely how a broken picker hides.
                  Falls back to free text so a raw sha is still deployable. */}
              {refs.length > 0 ? (
                <select style={input} value={ref} onChange={(e) => setRef(e.target.value)}>
                  {/* BOSS leaves a `boss/code/crun_…` branch behind for every code task it has
                      ever run, thirty of them here, all named after an id nobody recognises.
                      Grouped rather than dropped: deploying one is legitimate (that is how you
                      ship a fix before it merges), but it must not bury `main`. */}
                  {refs.filter((r) => !r.name.startsWith("boss/")).map((r) => (
                    <option key={r.name} value={r.name}>{r.name}, {r.sha.slice(0, 7)}</option>
                  ))}
                  {refs.some((r) => r.name.startsWith("boss/")) && (
                    <optgroup label="BOSS work branches">
                      {refs.filter((r) => r.name.startsWith("boss/")).map((r) => (
                        <option key={r.name} value={r.name}>
                          {r.name}, {r.sha.slice(0, 7)}
                        </option>
                      ))}
                    </optgroup>
                  )}
                </select>
              ) : (
                <input style={input} value={ref} placeholder="main"
                       onChange={(e) => setRef(e.target.value)} />
              )}
            </label>
          </div>

          {refsError && (
            <div className="detail" style={{ fontSize: 12, color: "var(--boss-c-d29922)" }}>
              {refsError}, type a branch or sha instead.
            </div>
          )}

          {/* Describe the request accurately before an operator submits it. */}
          {target && (
            <div className="detail" style={{ fontSize: 12 }}>
              Plan deployment of <strong>{target.app_name}</strong> from{" "}
              <code>{target.repo_owner || defaultOwner || "?"}/{target.repo}@
              {ref || "default branch"}</code>
              {target.app_entry && <> (<code>{target.app_entry}</code>)</>} to{" "}
              <code>{target.environment}</code>
              {target.domains?.[0] && <> at <code>{target.domains[0]}</code></>}.
              For a provider-backed target, BOSS creates a ChangeSet to review, approve, and
              apply before any deployment runs; for a host, a plan you approve in Approvals.
            </div>
          )}

          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <GatedButton permission="deployment:create" style={btn} disabled={starting} onClick={start}>
              {starting ? "planning…" : "Create deployment plan"}
            </GatedButton>
            <button style={btn} onClick={() => setOpen(false)}>Cancel</button>
            {err && <span role="alert" className="detail"
                          style={{ color: "var(--boss-c-f85149)", fontSize: 12 }}>{err}</span>}
          </div>
        </div>
      )}

      {plan && (
        <div style={{ display: "grid", gap: 10 }}>
          <ChangeSetNextStep plan={plan} />
          <div>
            <button style={btn} onClick={() => { setPlan(null); setErr(null); }}>
              Plan another
            </button>
            <button style={{ ...btn, marginLeft: 8 }} onClick={() => setOpen(false)}>Close</button>
          </div>
        </div>
      )}
    </section>
  );
}


/** A failure reason a person can read.
 *
 * The health verdict is stored as the raw JSON the checker produced, so a failed deployment
 * reported `{"healthy": false, "checks": [{"url": ..., "error": ...}]}`, accurate, and the
 * kind of thing an operator has to decode rather than read. The JSON is still one click away
 * on the failing step; the row gets the sentence.
 */
function readableFailure(reason: string): string {
  const text = reason.trim();
  if (!text.startsWith("{")) return text;
  try {
    const parsed = JSON.parse(text);
    const check = (parsed.checks || [])[0];
    if (!check) return text;
    const what = check.error
      ? check.error
      : `returned ${check.status}, expected ${check.expected}`;
    return `health check failed, ${check.url || "the app"} ${what}`;
  } catch {
    return text;
  }
}

// --------------------------------------------------------------------------- //
// Recent deployments
// --------------------------------------------------------------------------- //
type Deployment = {
  id: string; status: string; app_name: string; environment: string;
  commit_ref: string | null; trigger: string | null; duration_ms: number | null;
  finished_at: string | null; failure_reason: string | null; url: string | null;
  changeset_id?: string | null;
  decision_id?: string | null;
};

export function RecentDeployments({ deployments }: { deployments?: Deployment[] | null }) {
  const [openId, setOpenId] = useState<string | null>(null);
  // A redeploy request creates a new plan; keep its review details beside the source run.
  const [plannedFrom, setPlannedFrom] = useState<Record<string, PlannedDeployment>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const rows = deployments ?? [];

  const again = async (id: string) => {
    setBusy(id);
    setErrors((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
    const out = await callAction(redeployAction, undefined, "deployment:create")(id);
    setBusy(null);
    if (out?.error) {
      setErrors((current) => ({ ...current, [id]: String(out.error) }));
      return;
    }
    const nextPlan = plannedDeploymentFrom(out);
    if (!nextPlan) {
      setErrors((current) => ({ ...current,
        [id]: "Redeploy request did not return a reviewable ChangeSet. No deployment was started." }));
      return;
    }
    setPlannedFrom((current) => ({ ...current, [id]: nextPlan }));
    setOpenId(null);
  };

  const tone = (s: string) =>
    s === "succeeded" ? "#3fb950" : ["failed", "blocked", "rolled_back"].includes(s) ? "#f85149"
      : s === "running" ? "#58a6ff" : WAITING.includes(s) ? "#d29922" : "#8b949e";
  // `created` is what a plan-only run is called in the database. Shown as "planned", because
  // "created" alongside "succeeded" and "failed" reads as a deployment that went nowhere
  // rather than one that was never asked to go anywhere.
  const label = (s: string) => (s === "created" ? "planned"
    : s === "awaiting_approval" ? "awaiting approval" : s.replace("_", " "));
  // A host deployment that is waiting or running is watched in place: the row turns live
  // when it is approved, shows its steps, and ends with the URL or the rollback.
  const watched = (s: string) => WAITING.includes(s) || s === "running";

  return (
    <section className="ct-block">
      <div className="ct-block-title">Deployment plans and history</div>
      {rows.length === 0 && (
        <div className="detail">No deployment plans or runs yet.</div>
      )}
      {rows.map((d) => (
        <div key={d.id} style={{ borderTop: "1px solid var(--boss-c-bdbe0baeec)", padding: "8px 0" }}>
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap",
                        fontSize: 13 }}>
            <span style={{ color: tone(d.status), fontWeight: 600, minWidth: 78 }}>
              {label(d.status)}
            </span>
            <strong>{d.app_name}</strong>
            <span className="cap-badge tone-blue">{d.environment}</span>
            <span className="detail" style={{ fontFamily: "monospace", fontSize: 12 }}>
              {d.commit_ref ? d.commit_ref.slice(0, 12) : "—"}
            </span>
            <span className="detail" style={{ fontSize: 12 }}>{d.trigger || "—"}</span>
            <span className="detail" style={{ fontSize: 12 }}>{ms(d.duration_ms) || "—"}</span>
            <span style={{ flex: 1 }} />
            {/* Steps and logs are one click away for ANY past deployment, not only the one
                you happened to be watching when it ran. */}
            <button style={{ ...btn, padding: "2px 9px", fontSize: 11 }}
                    onClick={() => setOpenId(openId === d.id ? null : d.id)}>
              {openId === d.id ? "hide details"
                : ["planned", "created", "awaiting_approval"].includes(d.status)
                  ? "plan details" : "steps & logs"}
            </button>
            {/* Request a new reviewable plan while preserving this run's history. */}
            <GatedButton permission="deployment:create" style={{ ...btn, padding: "2px 9px", fontSize: 11 }}
                    disabled={busy === d.id}
                    title="Create a new ChangeSet for redeployment review"
                    onClick={() => again(d.id)}>
              {busy === d.id ? "planning…" : "plan redeploy"}
            </GatedButton>
          </div>
          {d.status === "awaiting_approval" && openId !== d.id && (
            <div className="detail" style={{ fontSize: 12, marginTop: 4 }}>
              Nothing runs until it is approved. <a href={approvalsHref()}>Open in Approvals</a>
            </div>
          )}
          {["planned", "created"].includes(d.status) && !plannedFrom[d.id] && (
            <div className="detail" style={{ fontSize: 12, marginTop: 4 }}>
              No deployment is running. Review its ChangeSet in{" "}
              <a href={changeReviewHref(d.changeset_id)}>ProviderOps ChangeSet Review</a>.
            </div>
          )}
          {errors[d.id] && (
            <div role="alert" className="detail" style={{ color: "var(--boss-c-f85149)", fontSize: 12, marginTop: 8 }}>
              {errors[d.id]}
            </div>
          )}
          {plannedFrom[d.id] && (
            <div style={{ marginTop: 8 }}>
              <ChangeSetNextStep plan={plannedFrom[d.id]} />
            </div>
          )}
          {d.failure_reason && openId !== d.id && (
            <div className="detail" style={{ color: "var(--boss-c-f85149)", fontSize: 12, marginTop: 3 }}>
              {readableFailure(d.failure_reason).slice(0, 200)}
            </div>
          )}
          {(openId === d.id || (watched(d.status) && d.status !== "awaiting_approval")) && (
            <div style={{ marginTop: 8 }}><LiveDeployment runId={d.id} /></div>
          )}
        </div>
      ))}
    </section>
  );
}
