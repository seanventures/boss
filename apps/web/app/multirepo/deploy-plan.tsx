"use client";

/**
 * DEPLOY TOGETHER (multi-repo screen 8), in Infra › Deploys: a change's components ship as one
 * release, wave by wave in dependency order (old → new, the steps, how each wave is undone), with
 * one rollback that undoes them all in reverse order. `?area=infra&tab=deploys&changeset=<id>`.
 *
 * "Deploy all N" (deployment:create) starts a merged set whose plan is ready; when it is not, the
 * plan's own sentences say why and the button says the first of them. "Roll back all"
 * (deployment:rollback) asks first. Everything shown is the server's plan (engine
 * multirepo/release.plan); the list at the top of Deploys is the project's change-set releases.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  ArrowCounterClockwise, ArrowLeft, ArrowRight, Check, Eye, Heartbeat, LockKey, RocketLaunch, Warning,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import {
  changeTone, changeWord, multirepo, refusalMessage, shortName, type ChangeSetView, type DeployPlan,
} from "../../lib/multirepo";
import { showToast } from "../client-forms";

const RELEASE_STATES = ["merged", "deploying", "live", "rolled_back"];
const cap = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const STEP_ICON: Record<string, React.ReactNode> = {
  deploy: <RocketLaunch size={13} aria-hidden />, health: <Heartbeat size={13} aria-hidden />, check: <Check size={13} aria-hidden />,
};
const WAVE_TONE: Record<string, "ok" | "info" | "bad" | "warn" | "muted"> = {
  live: "ok", deploying: "info", failed: "bad", rolled_back: "warn", rolling_back: "warn", ready: "ok", planned: "muted",
};

/** The project's change-set releases, newest first: a compact list at the top of Deploys. */
export function ChangeSetReleases({ projectId, onOpen }: { projectId: string; onOpen: (id: string) => void }) {
  const [items, setItems] = useState<ChangeSetView[] | null>(null);
  useEffect(() => {
    let live = true;
    multirepo.changeSets(projectId, RELEASE_STATES).then((v) => { if (live) setItems(v); }).catch(() => { if (live) setItems([]); });
    return () => { live = false; };
  }, [projectId]);
  if (!items || !items.length) return null;
  return (
    <div className="mr-ui-card" data-testid="mr-releases" style={{ padding: "12px 14px", marginBottom: 12 }}>
      <div className="mr-ui-card-h" style={{ marginBottom: 8 }}>
        <h3>Changes across repositories</h3>
        <span className="mr-ui-sub">Each ships as one release, with one rollback</span>
      </div>
      <div className="mr-rel">
        {items.slice(0, 5).map((cs) => {
          const repos = cs.lanes.filter((l) => l.lane_state !== "unchanged").map((l) => shortName(l.full_name));
          return (
            <button key={cs.id} type="button" className="mr-rel-row" data-testid={`mr-release-${cs.id}`}
                    title="Open its deploy plan" onClick={() => onOpen(cs.id)}>
              <b data-user-text>{cs.title}</b>
              <span className="mr-ui-sub" data-user-text>{repos.join(" · ")}</span>
              <span className="mr-ui-spacer" />
              <span className="mr-ui-pill" data-tone={changeTone(cs.state)}>{changeWord(cs.state)}</span>
              <ArrowRight size={13} aria-hidden />
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function DeployPlanView({ projectId, changeSetId, onBack }: { projectId: string; changeSetId: string; onBack: () => void }) {
  const [plan, setPlan] = useState<DeployPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"deploy" | "rollback" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const guarded = useGuardedCall();

  const load = useCallback(async () => {
    try { setPlan(await multirepo.deployPlan(projectId, changeSetId)); setError(null); }
    catch (e) { setError(refusalMessage(e)); }
  }, [projectId, changeSetId]);
  useEffect(() => { void load(); }, [load]);
  const moving = plan?.state === "deploying" || plan?.state === "merging";
  useEffect(() => {
    if (!moving) return undefined;
    const t = window.setInterval(() => { if (document.visibilityState !== "hidden") void load(); }, 5000);
    return () => window.clearInterval(t);
  }, [moving, load]);

  const back = (
    <button type="button" className="mr-ui-link" onClick={onBack} data-testid="mr-dp-back"><ArrowLeft size={12} aria-hidden /> Deploys</button>
  );
  if (!plan) {
    return (
      <div className="mr-dp" data-testid="mr-deploy-plan">
        <div className="mr-cmp-crumb">Infra <span aria-hidden>›</span>{back}</div>
        {error ? (
          <div className="mr-ui-row" role="alert">
            <span className="mr-ui-error" data-testid="mr-dp-error">{error}</span>
            <button type="button" className="ag-btn" onClick={() => { setError(null); void load(); }}>Retry</button>
          </div>
        ) : <p className="mr-ui-empty">Loading…</p>}
      </div>
    );
  }

  const n = plan.waves.length;
  const title = plan.title.startsWith(`${plan.app_name}: `) ? plan.title.slice(plan.app_name.length + 2) : plan.title;
  const canStart = plan.state === "merged";
  const blockedWhy = !plan.ready ? plan.problems[0] ?? "The plan is not ready." : "";
  const rollbackWhy = plan.state === "rolled_back" ? "Already rolled back"
    : ["deploying", "live", "failed"].includes(plan.state) ? "" : "Nothing is deployed yet, so there is nothing to roll back";

  const run = async (kind: "deploy" | "rollback") => {
    setBusy(kind);
    setActionError(null);
    try {
      const next = await guarded(kind === "deploy" ? `Deploy ${title}.` : `Roll back ${title}.`, () =>
        (kind === "deploy" ? multirepo.deploy(projectId, changeSetId) : multirepo.rollback(projectId, changeSetId)));
      setPlan({ ...next, title: next.title || plan.title, app_name: next.app_name || plan.app_name, app_key: next.app_key || plan.app_key });
      setConfirming(false);
      showToast("success", kind === "deploy" ? "Deploying, wave by wave" : "Rolling back every component");
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setActionError(refusalMessage(e));
    } finally { setBusy(null); }
  };

  return (
    <div className="mr-dp" data-testid="mr-deploy-plan" data-state={plan.state}>
      <div className="mr-cmp-head">
        <div style={{ minWidth: 0, flex: "1 1 320px" }}>
          <div className="mr-cmp-crumb">Infra <span aria-hidden>›</span>{back}<span aria-hidden>›</span>
            <b data-user-text>{plan.app_name} · {title}</b></div>
          <p className="mr-ui-lead" data-testid="mr-dp-lead">
            {n === 1 ? "One component ships" : `${cap(numberWord(n))} components ship`} as one release, in order. If any step fails,
            everything goes back to what is live now.
          </p>
        </div>
        <div className="mr-csc-actions">
          {plan.host || plan.environment ? (
            <span className="mr-csc-chip" data-testid="mr-dp-target" title="Where the release goes">
              Target: {[plan.environment, plan.host].filter(Boolean).join(" · ")}
            </span>
          ) : null}
          <span className="mr-ui-pill" data-tone={changeTone(plan.state)} data-testid="mr-dp-state">{changeWord(plan.state)}</span>
          {canStart ? (
            <GatedButton permission="deployment:create" className="ag-btn ag-btn-primary" data-testid="mr-dp-deploy"
                         disabled={!plan.ready || busy !== null}
                         title={blockedWhy || "Deploys every component in order, with health checks between waves"}
                         onClick={() => void run("deploy")}>
              {busy === "deploy" ? "Starting…" : `Deploy all ${n}`}
            </GatedButton>
          ) : null}
        </div>
      </div>

      {plan.problems.length ? (
        <div className="mr-ui-note" data-tone="bad" role="status" data-testid="mr-dp-problems">
          <Warning size={15} weight="bold" aria-hidden />
          <div>{plan.problems.map((p, i) => <div key={i}>{p}</div>)}</div>
        </div>
      ) : null}
      {actionError ? (
        <div className="mr-ui-note" data-tone="bad" role="alert" data-testid="mr-dp-action-error">
          <Warning size={15} weight="bold" aria-hidden /><div>{actionError}</div>
        </div>
      ) : null}

      <div className="mr-waves" data-testid="mr-dp-waves">
        {plan.waves.map((w, i) => (
          <React.Fragment key={w.lane_id}>
            {i ? <div className="mr-cmp-arrow" aria-hidden title="Then"><ArrowRight size={16} /></div> : null}
            <div className="mr-wave" data-testid={`mr-wave-${w.order}`} data-status={w.status}>
              <h4>
                <span className="mr-cmp-ord" aria-label={`Wave ${w.order}`}>{w.order}</span>
                <span data-user-text>{cap(w.components.join(" + ")) || shortName(w.repository)}</span>
                <span className="mr-ui-spacer" />
                <span className="mr-ui-mono mr-ui-dim" data-user-text style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{w.repository}</span>
              </h4>
              <div className="mr-wave-ver" data-testid={`mr-wave-ver-${w.order}`}>
                <span data-user-text>{w.from || "—"}</span> → <b data-user-text>{w.to || "—"}</b>
                {w.status !== "planned" ? <> <span className="mr-ui-pill" data-tone={WAVE_TONE[w.status] ?? "muted"}>{w.status.replace(/_/g, " ")}</span></> : null}
              </div>
              {w.steps.map((s, j) => (
                <div key={j} className="mr-wstep">{STEP_ICON[s.icon] ?? <Check size={13} aria-hidden />}<span data-user-text>{s.text}</span></div>
              ))}
              {w.error ? <div className="mr-ui-error" role="status">{w.error}</div> : null}
              <div className="mr-wave-rb"><ArrowCounterClockwise size={13} aria-hidden /><span>Rollback: <span data-user-text>{w.rollback}</span></span></div>
            </div>
          </React.Fragment>
        ))}
      </div>

      <div className="mr-ui-grid2">
        <div className="mr-ui-card" data-testid="mr-dp-rollback">
          <div className="mr-ui-card-h">
            <h3>One rollback</h3>
            <span className="mr-ui-sub">Reverse order · one button</span>
            <span className="mr-ui-spacer" />
            <GatedButton permission="deployment:rollback" className="ag-btn mr-csc-stop" data-testid="mr-dp-rollback-all"
                         disabled={!!rollbackWhy || busy !== null} title={rollbackWhy || "Every component back to its previous release, newest first"}
                         onClick={() => setConfirming(true)}>
              <ArrowCounterClockwise size={13} aria-hidden /> Roll back all
            </GatedButton>
          </div>
          {confirming ? (
            <div className="mr-ui-note" data-tone="warn" role="alertdialog" aria-label="Roll back every component?" data-testid="mr-dp-rollback-confirm"
                 style={{ marginBottom: 10 }}>
              <Warning size={15} weight="bold" aria-hidden />
              <div className="mr-confirm">
                <span>Roll back all {n} components to their previous release, newest first?</span>
                <button type="button" className="ag-btn" onClick={() => setConfirming(false)} disabled={busy !== null}>Keep</button>
                <GatedButton permission="deployment:rollback" className="ag-btn mr-csc-stop" data-testid="mr-dp-rollback-yes"
                             disabled={busy !== null} onClick={() => void run("rollback")}>
                  {busy === "rollback" ? "Rolling back…" : "Roll back all"}
                </GatedButton>
              </div>
            </div>
          ) : null}
          {plan.rollback.map((r) => (
            <div key={r.order} className="mr-plan-row" data-testid={`mr-dp-rb-${r.order}`}>
              <span className="mr-dot-n" aria-hidden>{r.order}</span>
              <div style={{ minWidth: 0 }}>
                <b data-user-text style={{ fontFamily: "var(--sans)" }}>{r.text}</b>
                <small data-user-text>{r.repository}</small>
              </div>
              <span className="mr-ui-pill" data-tone={WAVE_TONE[r.status] ?? "muted"}>{r.status.replace(/_/g, " ")}</span>
            </div>
          ))}
        </div>
        <div className="mr-ui-col">
          <div className="mr-ui-note" data-testid="mr-dp-auto">
            <Eye size={15} aria-hidden />
            <div><b>Automatic rollback</b> {plan.auto_rollback || "If a deploy or its health check fails, every component already deployed goes back to its previous release."}</div>
          </div>
          <div className="mr-ui-note" data-testid="mr-dp-merged-together">
            <LockKey size={15} aria-hidden />
            <div>Merged together: the {n === 1 ? "PR merges" : `${numberWord(n)} PRs merge`} only after all pass at once. A failed merge reverts the ones that landed.
              {plan.approved_by ? <> Approved by <span data-user-text>{plan.approved_by}</span>.</> : null}</div>
          </div>
        </div>
      </div>
    </div>
  );
}

const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
function numberWord(n: number): string { return WORDS[n] ?? String(n); }
