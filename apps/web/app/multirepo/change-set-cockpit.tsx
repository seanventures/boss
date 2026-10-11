"use client";

/**
 * A CHANGE ACROSS REPOSITORIES, in the Factory cockpit (multi-repo screens 5 and 6). A build that
 * belongs to a multi-repo change set (GET /v1/runs/{run}/change-set answers mode "multi") opens
 * this instead of the one-repository cockpit; so does `?area=code_tasks&changeset=<id>`.
 *
 *   header   Factory › app › title · N repositories · minutes · $ so far · Steer · Stop
 *   tabs     Build (screen 5) · Pull requests (screen 6, `&tab=prs`)
 *   Build    Plan → Build · k of n → Review set → You approve → Deploy together;
 *            one lane per repository (Sandbox, Code, Tests, Review, PR), in dependency order;
 *            the Plan, what is happening now, and the grant (See consent: read-only sheet)
 *
 * Every word of state is the server's (engine multirepo/change_sets.view). Stop and the
 * cross-repo re-check need code_task:steer; everyone who may read builds may read this.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft, Check, CircleNotch, Clock, FileCode, GitBranch, GithubLogo, LockKey, ShieldCheck, Warning, X,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { Sheet } from "../../components/identity/kit";
import { useGuardedCall } from "../../components/identity/identity-provider";
import {
  CHANGE_MOVING, CHANGE_STOPPABLE, MULTIREPO_PLACES, changeTone, changeWord, diffWords, multirepo, multirepoConsentFrom,
  refusalMessage, shortName, type ChangeSetGrantView, type ChangeSetView, type Lane,
} from "../../lib/multirepo";
import { areaHref } from "../factory/cockpit-util";
import { formatDateTime } from "../../lib/datetime";
import { steerBuild } from "../factory/cockpit-actions";
import { showToast } from "../client-forms";
import { LinkedPrs } from "./linked-prs";
import { TipQ } from "./parts";

const POLL_MS = 5000;

/** The change a build belongs to: undefined while asking, null when it is an ordinary build. */
export function useRunChangeSet(projectId: string, runId: string): ChangeSetView | null | undefined {
  const [found, setFound] = useState<ChangeSetView | null | undefined>(undefined);
  useEffect(() => {
    let live = true;
    setFound(undefined);
    multirepo.changeSetOfRun(projectId, runId)
      .then((cs) => { if (live) setFound(cs.mode === "multi" ? cs : null); })
      .catch(() => { if (live) setFound(null); });
    return () => { live = false; };
  }, [projectId, runId]);
  return found;
}

/** The title without the app's name the server puts in front ("Storefront: Add saved carts"). */
export function changeTitle(cs: Pick<ChangeSetView, "title" | "app_name">): string {
  const prefix = `${cs.app_name}: `;
  return cs.title.startsWith(prefix) ? cs.title.slice(prefix.length) : cs.title;
}

/** The lanes that change something (an unchanged component opens no PR). */
export const touchedLanes = (cs: ChangeSetView) => cs.lanes.filter((l) => l.lane_state !== "unchanged");

function readTab(): "build" | "prs" {
  if (typeof window === "undefined") return "build";
  return new URLSearchParams(window.location.search).get("tab") === "prs" ? "prs" : "build";
}
function writeTab(tab: "build" | "prs"): void {
  try {
    const url = new URL(window.location.href);
    if (tab === "prs") url.searchParams.set("tab", "prs"); else url.searchParams.delete("tab");
    window.history.replaceState(null, "", url.toString());
  } catch { /* not a browser */ }
}

export function ChangeSetCockpit({ projectId, changeSetId, initial, actor, onClose }: {
  projectId: string; changeSetId: string; initial?: ChangeSetView | null; actor: string; onClose: () => void;
}) {
  const [cs, setCs] = useState<ChangeSetView | null>(initial ?? null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"build" | "prs">(readTab);
  const [grantOpen, setGrantOpen] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const [steering, setSteering] = useState(false);
  const [guidance, setGuidance] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const h1 = useRef<HTMLHeadingElement>(null);

  const load = useCallback(async () => {
    try {
      setCs(await multirepo.changeSet(projectId, changeSetId));
      setError(null);
    } catch (e) { setError(refusalMessage(e)); }
  }, [projectId, changeSetId]);
  useEffect(() => { if (!initial) void load(); }, [load, initial]);
  useEffect(() => { h1.current?.focus(); }, [cs?.id]);

  // Polled while it moves; a hidden tab does not ask.
  const moving = cs ? CHANGE_MOVING.has(cs.state) : false;
  useEffect(() => {
    if (!moving) return undefined;
    const t = window.setInterval(() => { if (document.visibilityState !== "hidden") void load(); }, POLL_MS);
    return () => window.clearInterval(t);
  }, [moving, load]);

  // Esc goes back (unless a sheet or a confirm has it).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || grantOpen) return;
      if (confirmStop) { setConfirmStop(false); return; }
      e.preventDefault();
      onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, grantOpen, confirmStop]);

  const back = (
    <button type="button" className="mr-ui-link" onClick={onClose} data-testid="mr-csc-back">
      <ArrowLeft size={12} aria-hidden /> Factory
    </button>
  );
  if (!cs) {
    return (
      <section className="mr-csc" data-testid="mr-csc" aria-busy={!error}>
        <div className="mr-cmp-crumb">{back}</div>
        {error ? (
          <div className="mr-ui-row" role="alert">
            <span className="mr-ui-error" data-testid="mr-csc-error">{error}</span>
            <button type="button" className="ag-btn" onClick={() => { setError(null); void load(); }}>Retry</button>
          </div>
        ) : <p className="mr-ui-empty">Loading…</p>}
      </section>
    );
  }

  const touched = touchedLanes(cs);
  const title = changeTitle(cs);
  const active = cs.lanes.find((l) => l.lane_state === "building") ?? null;
  const stoppable = CHANGE_STOPPABLE.has(cs.state);
  const prCount = touched.filter((l) => l.pr_number).length;
  const pick = (t: "build" | "prs") => { setTab(t); writeTab(t); };

  const stop = async () => {
    setBusy("stop");
    setActionError(null);
    try {
      setCs(await guarded(`Stop ${title}.`, () => multirepo.stop(projectId, cs.id)));
      setConfirmStop(false);
      showToast("success", "Stopped. The work so far is kept.");
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setActionError(refusalMessage(e));
    } finally { setBusy(null); }
  };
  const steer = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!active?.code_task_run_id || !guidance.trim()) return;
    setBusy("steer");
    const r = await steerBuild(active.code_task_run_id, actor, guidance.trim());
    setBusy(null);
    if (r.ok) { setGuidance(""); setSteering(false); showToast("success", `Sent to ${shortName(active.full_name)}. Read at its next step.`); }
    else setActionError(r.error ?? "That didn't work");
  };

  return (
    <section className="mr-csc" data-testid="mr-csc" data-change-set={cs.id} data-state={cs.state}>
      <div className="mr-csc-head">
        <div style={{ minWidth: 0, flex: "1 1 320px" }}>
          <div className="mr-cmp-crumb" data-testid="mr-csc-crumb">
            {back}<span aria-hidden>›</span>
            <a className="mr-ui-link" href={areaHref(MULTIREPO_PLACES.components(cs.app_key))} data-user-text>{cs.app_name}</a>
            <span aria-hidden>›</span>
            {tab === "prs" ? (
              <>
                <button type="button" className="mr-ui-link" onClick={() => pick("build")} data-user-text>{title}</button>
                <span aria-hidden>›</span><b>Pull requests</b>
              </>
            ) : <b data-user-text>{title}</b>}
          </div>
          <h1 ref={h1} tabIndex={-1} data-testid="mr-csc-title">
            <span data-user-text>{title}</span>
            {tab === "prs" ? <small>· {touched.length} linked pull {touched.length === 1 ? "request" : "requests"}, reviewed as one set</small> : null}
          </h1>
        </div>
        <div className="mr-csc-actions">
          <span className="mr-ui-pill" data-tone={changeTone(cs.state)} data-testid="mr-csc-state"
                title={cs.blocked_reason || cs.error || undefined}>{changeWord(cs.state)}</span>
          <span className="mr-csc-chip" data-testid="mr-csc-repos" title={touched.map((l) => l.full_name).join(", ")}>
            <GithubLogo size={12} aria-hidden />{touched.length} {touched.length === 1 ? "repository" : "repositories"}
          </span>
          <span className="mr-csc-chip" data-testid="mr-csc-minutes" title="Since the change started">
            <Clock size={12} aria-hidden />{cs.minutes} min
          </span>
          <span className="mr-csc-chip" data-testid="mr-csc-cost" title="Spent by every lane together">
            ${cs.cost_usd.toFixed(2)} {moving ? "so far" : ""}
          </span>
          {moving && active?.code_task_run_id ? (
            <GatedButton permission="code_task:steer" className="ag-btn" data-testid="mr-csc-steer" aria-expanded={steering}
                         title={`Guide the agents in ${shortName(active.full_name)}. They read it at their next step.`}
                         onClick={() => setSteering((v) => !v)}>Steer</GatedButton>
          ) : null}
          {stoppable ? (
            <GatedButton permission="code_task:steer" className="ag-btn mr-csc-stop" data-testid="mr-csc-stop"
                         title="Stops every lane. The work so far is kept." onClick={() => setConfirmStop(true)}>Stop</GatedButton>
          ) : null}
        </div>
      </div>

      {confirmStop ? (
        <div className="mr-ui-note" data-tone="warn" role="alertdialog" aria-label="Stop this change?" data-testid="mr-csc-stop-confirm">
          <Warning size={15} weight="bold" aria-hidden />
          <div className="mr-confirm">
            <span>Stop every lane of this change? Nothing merges; the branches stay.</span>
            <button type="button" className="ag-btn" onClick={() => setConfirmStop(false)} disabled={busy === "stop"}>Keep going</button>
            <GatedButton permission="code_task:steer" className="ag-btn mr-csc-stop" data-testid="mr-csc-stop-yes"
                         disabled={busy === "stop"} onClick={() => void stop()}>{busy === "stop" ? "Stopping…" : "Stop"}</GatedButton>
          </div>
        </div>
      ) : null}
      {steering && active?.code_task_run_id ? (
        <form className="mr-ui-row wrap" onSubmit={(e) => void steer(e)} data-testid="mr-csc-steer-box">
          <input className="mr-ui-input" style={{ flex: "1 1 280px" }} value={guidance} autoFocus
                 aria-label={`Guidance for ${active.full_name}`} placeholder={`What should ${shortName(active.full_name)} do differently?`}
                 onChange={(e) => setGuidance(e.target.value)} />
          <GatedButton permission="code_task:steer" type="submit" className="ag-btn ag-btn-primary"
                       disabled={!guidance.trim() || busy === "steer"}>Send</GatedButton>
        </form>
      ) : null}
      {actionError ? (
        <div className="mr-ui-note" data-tone="bad" role="alert" data-testid="mr-csc-action-error">
          <Warning size={15} weight="bold" aria-hidden /><div>{actionError}</div>
        </div>
      ) : null}

      <div className="mr-csc-tabs" role="tablist" aria-label="Change views">
        <button type="button" role="tab" className="mr-csc-tab" aria-selected={tab === "build"} data-testid="mr-csc-tab-build"
                title="The lanes, one per repository" onClick={() => pick("build")}>Build</button>
        <button type="button" role="tab" className="mr-csc-tab" aria-selected={tab === "prs"} data-testid="mr-csc-tab-prs"
                title="The linked pull requests, reviewed as one set" onClick={() => pick("prs")}>
          Pull requests{prCount ? <span className="mr-ui-pill" data-tone="muted">{prCount}</span> : null}
        </button>
      </div>

      {tab === "prs" ? (
        <div role="tabpanel" aria-label="Pull requests">
          <LinkedPrs cs={cs} projectId={projectId} onChanged={setCs} />
        </div>
      ) : (
        <div role="tabpanel" aria-label="Build" className="mr-csc">
          <StageStrip cs={cs} />
          {cs.state === "blocked" && cs.blocked_reason ? (
            <div className="mr-ui-note" data-tone="bad" role="status" data-testid="mr-csc-blocked">
              <Warning size={15} weight="bold" aria-hidden />
              <div><b>Set blocked.</b> {cs.blocked_reason} Nothing asks for approval until it is fixed.</div>
            </div>
          ) : cs.error && (cs.state === "failed" || cs.state === "rolled_back") ? (
            <div className="mr-ui-note" data-tone="bad" role="status" data-testid="mr-csc-failed">
              <Warning size={15} weight="bold" aria-hidden /><div>{cs.error}</div>
            </div>
          ) : null}
          <div>
            <div className="mr-ui-card-h" style={{ marginBottom: 10 }}>
              <h3>Build lanes</h3>
              <span className="mr-ui-sub">One sandbox per repository · in dependency order
                <TipQ tip="A lane starts when the lanes it depends on pass their tests. Independent lanes build in parallel." /></span>
            </div>
            <div className="mr-lanes" data-testid="mr-lanes">
              {cs.lanes.map((l) => <LaneRow key={l.id} lane={l} now={l === active} />)}
            </div>
          </div>
          <div className="mr-ui-grid2">
            <PlanCard cs={cs} />
            <div className="mr-ui-col">
              <NowCard cs={cs} active={active} />
              <GrantCard cs={cs} onOpen={() => setGrantOpen(true)} />
            </div>
          </div>
        </div>
      )}
      {grantOpen ? <GrantSheet projectId={projectId} cs={cs} onClose={() => setGrantOpen(false)} /> : null}
    </section>
  );
}

function StageStrip({ cs }: { cs: ChangeSetView }) {
  const steps = cs.stage.steps;
  return (
    <div className="mr-stages" data-testid="mr-stages" role="list" aria-label="Where the change is">
      {steps.map((s, i) => (
        <React.Fragment key={s.key}>
          {i ? <span className="mr-stage-line" aria-hidden data-done={steps[i - 1].state === "done" || undefined} /> : null}
          <span className="mr-stage" role="listitem" data-state={s.state} data-testid={`mr-stage-${s.key}`}
                data-you={s.key === "approve" && s.state !== "done" ? "" : undefined}
                aria-current={s.state === "active" ? "step" : undefined}>
            <span className="mr-stage-n" aria-hidden>
              {s.state === "done" ? <Check size={12} weight="bold" /> : s.state === "failed" ? <X size={12} weight="bold" /> : i + 1}
            </span>
            {s.label}
          </span>
        </React.Fragment>
      ))}
    </div>
  );
}

const STEP_ICON: Record<string, React.ReactNode> = {
  done: <Check size={12} weight="bold" />, failed: <X size={12} weight="bold" />,
  active: <CircleNotch size={13} weight="bold" className="mr-spin" />,
};

/** The lane's right side: its PR, what it is doing, or what it waits for. */
function LaneEnd({ lane }: { lane: Lane }) {
  const plan = lane.plan?.[0];
  const what = plan?.interface || plan?.change || "";
  if (lane.lane_state === "unchanged") {
    return <><span className="mr-ui-pill" data-tone="muted">Unchanged</span><span>No change here, so no PR</span></>;
  }
  if (lane.lane_state === "failed") {
    return <span className="mr-ui-pill" data-tone="bad" data-testid={`mr-lane-pill-${lane.order}`}>Failed</span>;
  }
  if (lane.lane_state === "waiting") {
    const dep = lane.waiting_for[0];
    return (
      <>
        <span className="mr-ui-pill" data-tone="muted" data-testid={`mr-lane-pill-${lane.order}`}>
          {dep ? `Waiting for ${lane.waiting_for.join(", ")}` : "Waiting"}
        </span>
        <span data-testid={`mr-lane-wait-${lane.order}`}>
          {dep ? `starts when ${lane.waiting_for.join(" and ")}’s tests pass` : "starts when a sandbox is free"}
        </span>
      </>
    );
  }
  if (lane.pr_number) {
    const merged = lane.lane_state === "merged";
    const reverted = lane.lane_state === "reverted";
    return (
      <>
        <span className="mr-ui-pill" data-tone={reverted ? "warn" : "ok"} data-testid={`mr-lane-pill-${lane.order}`}>
          PR #{lane.pr_number} {reverted ? "reverted" : merged ? "merged" : "open"}
        </span>
        {diffWords(lane) ? <span>{diffWords(lane)}</span> : null}
        {what ? <span className="mr-ui-sub" data-user-text>{what}</span> : null}
      </>
    );
  }
  const step = lane.steps.find((s) => s.state === "active");
  return (
    <>
      <span className="mr-ui-pill" data-tone="info" data-testid={`mr-lane-pill-${lane.order}`}>{step?.label ?? "Building"}</span>
      {diffWords(lane) ? <span>{diffWords(lane)}</span> : null}
      {what ? <span className="mr-ui-sub" data-user-text>{what}</span> : null}
    </>
  );
}

function LaneRow({ lane, now }: { lane: Lane; now: boolean }) {
  return (
    <div className="mr-lane" data-testid={`mr-lane-${lane.order}`} data-state={lane.lane_state} data-now={now || undefined}>
      <div className="mr-lane-who">
        <span className="mr-lane-ord" aria-label={`Lane ${lane.order}`}>{lane.order}</span>
        <div style={{ minWidth: 0 }} className="mr-ui-repo">
          <div>
            <b data-user-text title={lane.full_name}>{lane.full_name}</b>
            <small data-user-text>{lane.components.join(", ")}</small>
          </div>
        </div>
      </div>
      <div className="mr-lane-steps" role="list" aria-label={`${lane.full_name} steps`}>
        {lane.steps.map((s, i) => (
          <React.Fragment key={s.key}>
            {i ? <span className="mr-st-line" aria-hidden data-done={lane.steps[i - 1].state === "done" || undefined} /> : null}
            <span className="mr-st" role="listitem" data-state={s.state} data-testid={`mr-lane-${lane.order}-${s.key}`}
                  aria-label={`${s.label}: ${s.state}`}>
              <i aria-hidden>{STEP_ICON[s.state] ?? null}</i>{s.label}
            </span>
          </React.Fragment>
        ))}
      </div>
      <div className="mr-lane-end"><LaneEnd lane={lane} /></div>
      {lane.lane_state === "failed" && lane.error ? (
        <div className="mr-lane-err" role="status" data-testid={`mr-lane-error-${lane.order}`}>
          <Warning size={14} weight="bold" aria-hidden /><span>{lane.error}</span>
        </div>
      ) : null}
    </div>
  );
}

const PLAN_WORD: Record<string, [string, "ok" | "info" | "muted" | "bad" | "warn"]> = {
  ready: ["done", "ok"], merged: ["done", "ok"], building: ["building", "info"], waiting: ["next", "muted"],
  failed: ["failed", "bad"], unchanged: ["unchanged", "muted"], reverted: ["reverted", "warn"],
};

function PlanCard({ cs }: { cs: ChangeSetView }) {
  const items = cs.plan?.components ?? [];
  const laneOf = (name: string) => cs.lanes.find((l) => l.components.includes(name));
  return (
    <div className="mr-ui-card" data-testid="mr-csc-plan">
      <div className="mr-ui-card-h" style={{ marginBottom: 6 }}>
        <h3>Plan</h3>
        <span className="mr-ui-sub">What changes in each component{cs.plan?.by ? ` · by ${cs.plan.by}` : ""}</span>
      </div>
      {cs.plan?.summary ? <p className="mr-ui-sub" data-user-text style={{ margin: "0 0 6px" }}>{cs.plan.summary}</p> : null}
      {items.length ? items.map((c, i) => {
        const lane = laneOf(c.name);
        const word = c.unchanged ? PLAN_WORD.unchanged : PLAN_WORD[lane?.lane_state ?? "waiting"] ?? PLAN_WORD.waiting;
        return (
          <div key={c.name} className="mr-plan-row" data-testid={`mr-plan-${c.name}`}>
            <span className="mr-dot-n" aria-hidden>{i + 1}</span>
            <div style={{ minWidth: 0 }}>
              <b data-user-text>{c.name}</b>
              <small data-user-text>{c.unchanged ? "No change" : [c.change, c.interface].filter(Boolean).join(" · ") || "—"}</small>
            </div>
            <span className="mr-ui-pill" data-tone={word[1]} data-testid={`mr-plan-state-${c.name}`}>{word[0]}</span>
          </div>
        );
      }) : <p className="mr-ui-empty">The plan is being written.</p>}
    </div>
  );
}

function NowCard({ cs, active }: { cs: ChangeSetView; active: Lane | null }) {
  const step = active?.steps.find((s) => s.state === "active");
  let line: React.ReactNode;
  if (active) {
    line = (
      <div className="mr-now">
        <span className="mr-ui-gh" aria-hidden><GitBranch size={15} /></span>
        <div style={{ minWidth: 0 }}>
          <b>{step?.label ?? "Building"}</b> <span className="mr-ui-sub">in <span data-user-text>{active.full_name}</span></span>
          <small>{active.phase ? active.phase.replace(/_/g, " ") : "Working"}</small>
        </div>
      </div>
    );
  } else if (cs.state === "in_review") {
    line = <span>Waiting for your approval: one decision for every pull request.</span>;
  } else if (cs.state === "blocked") {
    line = <span className="mr-ui-error">{cs.blocked_reason || "Blocked."}</span>;
  } else {
    line = <span>{changeWord(cs.state)}{cs.error ? ` · ${cs.error}` : ""}</span>;
  }
  return (
    <div className="mr-ui-card" data-testid="mr-csc-now">
      <div className="mr-ui-eyebrow" style={{ marginBottom: 10 }}>Now</div>
      {line}
    </div>
  );
}

/** "contracts, api, web (write on boss/*) · infra (read)". */
export function grantWords(cs: ChangeSetView): string {
  const repos = cs.grant?.repos ?? [];
  const write = repos.filter((r) => r.access === "write").map((r) => shortName(r.full_name));
  const read = repos.filter((r) => r.access !== "write").map((r) => shortName(r.full_name));
  return [write.length ? `${write.join(", ")} (write on boss/*)` : "", read.length ? `${read.join(", ")} (read)` : ""]
    .filter(Boolean).join(" · ");
}

function GrantCard({ cs, onOpen }: { cs: ChangeSetView; onOpen: () => void }) {
  const words = grantWords(cs);
  return (
    <div className="mr-ui-note" data-testid="mr-csc-grant">
      <LockKey size={15} aria-hidden />
      <div>
        <b>Grant:</b> {words ? <span data-user-text>{words}</span> : "none recorded yet"}
        {cs.grant?.ended_at ? <> · ended</> : null}.{" "}
        <button type="button" className="mr-ui-link" onClick={onOpen} data-testid="mr-csc-see-consent"
                title="Which repositories this change's agents may touch, and how">See consent</button>
      </div>
    </div>
  );
}

/** The consent this change runs under, read only: the consent sheet's look without its buttons. */
function GrantSheet({ projectId, cs, onClose }: { projectId: string; cs: ChangeSetView; onClose: () => void }) {
  const [data, setData] = useState<ChangeSetGrantView | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    multirepo.grant(projectId, cs.id).then(setData).catch((e) => setError(refusalMessage(e)));
  }, [projectId, cs.id]);
  const mr = data ? multirepoConsentFrom(data.consent) : null;
  const grant = data?.grant ?? cs.grant;
  const repos = mr?.repos ?? (grant?.repos ?? []).map((r) => ({ full_name: r.full_name, access: r.access === "read" ? "read" as const : "write" as const, components: [], words: "" }));
  return (
    <Sheet side wide onClose={onClose} testId="mr-grant-sheet"
           title={<>
             <span className="mr-cs-eyebrow mr-ui-eyebrow"><ShieldCheck size={13} aria-hidden />This change&apos;s grant</span>
             <span className="mr-cs-title">Which repositories may the agents touch?</span>
           </>}>
      <p className="mr-cs-subtitle"><span data-user-text>{changeTitle(cs)}</span> · <span data-user-text>{cs.app_name}</span> · read only</p>
      {error ? <p className="mr-ui-error" role="alert">{error}</p> : null}
      <div className="mr-cs-body" data-testid="mr-grant-body">
        <div>
          <div className="mr-ui-eyebrow">Allowed for this change</div>
          <div className="mr-cs-list">
            {repos.map((r) => (
              <div key={r.full_name} className="mr-cs-grant" data-access={r.access} data-testid={`mr-grant-repo-${r.full_name}`}>
                <span className={`mr-ui-gh${r.access === "read" ? " muted" : ""}`} aria-hidden>
                  {r.access === "read" ? <FileCode size={15} /> : <GithubLogo size={15} weight="bold" />}
                </span>
                <div>
                  <b data-user-text>{r.full_name}</b>
                  <small>{r.words || (r.access === "read" ? "Read only" : "Read · Write on boss/* · Pull requests")}</small>
                </div>
                <span className="mr-ui-pill" data-tone={r.access === "write" ? "ok" : "muted"}>{r.access === "write" ? "Write" : "Read"}</span>
              </div>
            ))}
            {!repos.length && !error ? <p className="mr-ui-empty">{data ? "No grant recorded." : "Loading…"}</p> : null}
          </div>
        </div>
        {mr?.unreachable.length ? (
          <div>
            <div className="mr-ui-eyebrow">Not reachable <span>{mr.unreachable.length} other {mr.unreachable.length === 1 ? "repository" : "repositories"}</span></div>
            <div className="mr-cs-chips">
              {mr.unreachable.slice(0, 8).map((u) => (
                <span key={u} className="mr-ui-chip static" style={{ opacity: 0.65 }} data-user-text><LockKey size={11} aria-hidden />{u}</span>
              ))}
              {mr.unreachable.length > 8 ? <span className="mr-ui-chip static">+{mr.unreachable.length - 8} more</span> : null}
            </div>
          </div>
        ) : null}
        <dl className="mr-cs-kv">
          <dt>How</dt><dd>{mr?.how || `GitHub App token limited to these ${repos.length} repositories`}</dd>
          <dt>Lasts</dt><dd>{mr?.lasts || (grant?.token_lifetime_minutes ? `${grant.token_lifetime_minutes} min per token, ends with the run` : "Ends with the run")}</dd>
          <dt>Never</dt><dd>{mr?.never || "push to a base branch, change settings, delete branches it did not create"}</dd>
          {grant?.consented_by ? <><dt>Allowed by</dt><dd data-user-text>{grant.consented_by}{grant.remember ? ` · remembered for ${cs.app_name}` : " · this change only"}</dd></> : null}
          {grant?.ended_at ? <><dt>Ended</dt><dd>{formatDateTime(grant.ended_at)}</dd></> : null}
        </dl>
      </div>
      <div className="mr-cs-foot">
        <span className="mr-ui-sub">Change it from the app&apos;s next build: consent is asked again when its repositories change.</span>
        <span className="mr-ui-spacer" />
        <button type="button" className="idn-btn" onClick={onClose} data-autofocus>Close</button>
      </div>
    </Sheet>
  );
}
