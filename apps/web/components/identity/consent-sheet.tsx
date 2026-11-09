"use client";

/**
 * JUST-IN-TIME CONSENT (IDENTITY.md §7.3 2): work a person starts needs more than their standing
 * grants cover. The sheet names the agents, the abilities and the repos, and creates a grant , 
 * task-bound ("for this build only", ends with the run) or standing (the policy's default days, as
 * the API offers them). Never more than
 * the person holds: the API intersects with their live permissions and each agent's ceiling.
 *
 * Opened by `openConsent(detail)` from anywhere (the seam S3's build start uses when the API
 * answers `409 consent_required`), and rendered once by <ConsentHost/> in the shell.
 *
 * A MULTI-REPO APP'S FIRST BUILD (the 409 carries `multirepo`) asks a different question , 
 * "Which repositories may the agents touch?", with exactly the app's repositories and their
 * access, the ones the run cannot reach, who uses it and how the token works (mockup 09).
 * "Remember for <app>" picks the standing grant; unticked, the grant ends with the run.
 */
import React, { useEffect, useState } from "react";
import { FileCode, GithubLogo, LockKey, ShieldCheck } from "@phosphor-icons/react";

import { AgentAvatar } from "../../app/agent-avatar";
import { idPost, refusalSentence } from "../../lib/identity/api";
import { CONSENT_EVENT, grantBodyFor, type ConsentDuration, type ConsentRequest } from "../../lib/identity/consent";
import { isPermission, type Permission } from "../../lib/identity/permissions.gen";
import { useGuardedCall } from "./identity-provider";
import { ErrorLine, Perm, Sheet } from "./kit";

export { CONSENT_EVENT, askConsent, consentFromBody, consentFromError, grantBodyFor, withConsent } from "../../lib/identity/consent";
export type { ConsentRequest } from "../../lib/identity/consent";

export function openConsent(detail: ConsentRequest): void {
  window.dispatchEvent(new CustomEvent<ConsentRequest>(CONSENT_EVENT, { detail }));
}

function names(list: { name: string }[]): string {
  if (list.length <= 1) return list[0]?.name ?? "This agent";
  return `${list.slice(0, -1).map((a) => a.name).join(", ")} and ${list[list.length - 1].name}`;
}

/**
 * WHY A GRANT WAS REFUSED, IN WORDS (round 3: the sheet said only "That didn't work" when the API
 * answered 403 github_access_lacking, a person with no GitHub access of their own granting an
 * agent push on a repo). The one identity fallback (lib/identity/api.ts refusalSentence): the
 * API's own sentence when it wrote one, else the refusal's words, which name the repo, the
 * project and the permissions from its body.
 */
export function consentRefusal(e: unknown): string {
  return refusalSentence(e);
}

export function ConsentSheet({ request, onClose }: { request: ConsentRequest; onClose: () => void }) {
  if (request.multirepo) return <MultiRepoConsentSheet request={request} onClose={onClose} />;
  return <PlainConsentSheet request={request} onClose={onClose} />;
}

function PlainConsentSheet({ request, onClose }: { request: ConsentRequest; onClose: () => void }) {
  const [busy, setBusy] = useState<ConsentDuration["kind"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  // Shown as chips when the catalogue knows them; SENT as asked, every one (grantBodyFor).
  const known = request.permissions.filter(isPermission) as Permission[];
  const other = request.permissions.filter((p) => !isPermission(p));
  // The choices the API offered (a schedule offers only a standing grant).
  const durations = request.durations.length ? request.durations : [{ kind: "standing" as const, label: "Standing", days: null }];

  const decide = async (duration: ConsentDuration) => {
    setBusy(duration.kind);
    setError(null);
    try {
      const grant = await guarded(`Let ${names(request.agents)} act for you.`, () =>
        idPost<{ id: string }>("/v1/identity/me/delegations", grantBodyFor(request, duration)));
      request.onDecided?.(grant.id);
      onClose();
    } catch (e) {
      setError(consentRefusal(e));
      setBusy(null);
    }
  };

  return (
    <Sheet
      // ABOVE WHATEVER ASKED FOR IT: a start from a sheet (Add a project, a template's sheet)
      // waits on this answer; drawn inside the shell's stacking context it sat under that sheet,
      // unseen, and the press hung on "Creating…".
      layer="step-up"
      title={`Let ${names(request.agents)} act for you?`}
      icon={<span className="idn-stack" aria-hidden>{request.agents.slice(0, 3).map((a) => <AgentAvatar key={a.id} agent={a.role_key ?? a.name} size={26} />)}</span>}
      onClose={() => { request.onDecided?.(null); onClose(); }}
      testId="consent-sheet"
    >
      <p className="idn-lede2" data-copy="prose">
        To run <b>{request.task}</b> they need a little of your access, and never more than you have.
      </p>
      <div className="idn-need">
        {known.map((p) => <Perm key={p} p={p} sm />)}
        {other.map((p) => <Perm key={p} p={p} sm />)}
        {request.github ? <span className="idn-chip"><GithubLogo size={13} aria-hidden /> {request.github.repos.join(", ") || request.github.repo}, {request.github.summary}</span> : null}
      </div>
      <div className="idn-opts" data-testid="consent-durations">
        {durations.map((d, i) => (
          <button key={d.kind} type="button" className={i === 0 ? "idn-btn primary" : "idn-btn"} onClick={() => decide(d)} disabled={!!busy}
            data-autofocus={i === 0 || undefined} data-testid={`consent-${d.kind}`}>
            {busy === d.kind ? "Granting…" : d.label}
          </button>
        ))}
      </div>
      <ErrorLine error={error} />
      <p className="idn-fine" data-copy="prose">They can't merge, approve or deploy to production. Revoke any time in My account › Agents.</p>
    </Sheet>
  );
}

/** What each agent does with the access, by its role (shown beside its face). */
const ROLE_USE: Record<string, string> = {
  builder: "builds", engineering_captain: "builds", engineer: "builds", developer: "builds",
  reviewer: "reviews", code_reviewer: "reviews", security: "reviews", security_reviewer: "reviews", qa: "tests",
  release_manager: "deploys", sre: "deploys", deployer: "deploys", planner: "plans", pm: "plans",
};

function MultiRepoConsentSheet({ request, onClose }: { request: ConsentRequest; onClose: () => void }) {
  const mr = request.multirepo!;
  const task = request.durations.find((d) => d.kind === "task") ?? null;
  const standing = request.durations.find((d) => d.kind === "standing") ?? null;
  // Remember = the standing grant; unticked = this run only. One of them may be all the API offers.
  const [remember, setRemember] = useState<boolean>(Boolean(standing));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const choice: ConsentDuration | null = remember ? (standing ?? task) : (task ?? standing);
  const fixed = !task || !standing;
  const org = mr.repos[0]?.full_name.split("/")[0] ?? "";
  const shown = mr.unreachable.slice(0, 8);
  const n = mr.repos.length;

  const allow = async () => {
    if (!choice) return;
    setBusy(true);
    setError(null);
    try {
      const grant = await guarded(`Let ${names(request.agents)} use ${n} ${n === 1 ? "repository" : "repositories"} for ${mr.app_name}.`, () =>
        idPost<{ id: string }>("/v1/identity/me/delegations", grantBodyFor(request, choice)));
      request.onDecided?.(grant.id);
      onClose();
    } catch (e) {
      setError(consentRefusal(e));
      setBusy(false);
    }
  };
  const cancel = () => { request.onDecided?.(null); onClose(); };

  return (
    <Sheet side wide onClose={cancel} testId="consent-sheet"
           title={<>
             <span className="mr-cs-area-name mr-ui-area-name" data-testid="consent-mr-eyebrow"><ShieldCheck size={13} aria-hidden />Before the build starts</span>
             <span className="mr-cs-title">Which repositories may the agents touch?</span>
           </>}>
      <p className="mr-cs-subtitle" data-testid="consent-mr-subtitle">
        Run {mr.run_label || request.task}, <span data-user-text>{mr.app_name}</span>, exactly the app&apos;s repositories
      </p>
      <div className="mr-cs-body" data-testid="consent-multirepo">
        <div>
          <div className="mr-ui-area-name">Allowed for this run</div>
          <div className="mr-cs-list">
            {mr.repos.map((r) => (
              <div key={r.full_name} className="mr-cs-grant" data-access={r.access} data-testid={`consent-mr-repo-${r.full_name}`}>
                <span className={`mr-ui-gh${r.access === "read" ? " muted" : ""}`} aria-hidden>
                  {r.access === "read" ? <FileCode size={15} /> : <GithubLogo size={15} weight="bold" />}
                </span>
                <div>
                  <b data-user-text>{r.full_name}</b>
                  <small>{r.words || (r.access === "read" ? "Read only" : "Read, Write on boss/*, Pull requests")}</small>
                </div>
                <span className="mr-ui-pill" data-tone={r.access === "write" ? "ok" : "muted"}>{r.access === "write" ? "Write" : "Read"}</span>
              </div>
            ))}
          </div>
        </div>
        {mr.unreachable.length ? (
          <div data-testid="consent-mr-unreachable">
            <div className="mr-ui-area-name">Not reachable <span>{mr.unreachable.length} other {mr.unreachable.length === 1 ? "repository" : "repositories"}{org ? ` in ${org}` : ""}</span></div>
            <div className="mr-cs-chips">
              {shown.map((u) => (
                <span key={u} className="mr-ui-chip static unreachable" title="This run's token cannot reach it" data-user-text>
                  <LockKey size={11} aria-hidden />{u}
                </span>
              ))}
              {mr.unreachable.length > shown.length ? (
                <span className="mr-ui-chip static" title={mr.unreachable.slice(shown.length).join(", ")}>+{mr.unreachable.length - shown.length} more</span>
              ) : null}
            </div>
          </div>
        ) : null}
        {request.agents.length ? (
          <div>
            <div className="mr-ui-area-name">Who uses it</div>
            <div className="mr-cs-who" data-testid="consent-mr-who">
              {request.agents.map((a) => (
                <span key={a.id} className="mr-cs-agent" title={a.name}>
                  <AgentAvatar agent={a.role_key ?? a.name} size={24} />
                  <b>{a.name}</b>{ROLE_USE[a.role_key ?? ""] ? <span>{ROLE_USE[a.role_key ?? ""]}</span> : null}
                </span>
              ))}
            </div>
          </div>
        ) : null}
        <dl className="mr-cs-kv" data-testid="consent-mr-how">
          <dt>How</dt>
          <dd>{mr.how || `GitHub App token limited to these ${n} repositories`}
            <span className="mr-ui-tipq" role="img" aria-label="GitHub enforces the limit"
                  title="Installation access token with repository_ids set to the app's repositories and per-repository permissions. GitHub enforces the limit, not BOSS.">?</span>
          </dd>
          <dt>Lasts</dt><dd>{mr.lasts || "1 hour, renewed per step, ends with the run"}</dd>
          <dt>Never</dt><dd>{mr.never || "push to a base branch, change settings, delete branches it did not create"}</dd>
        </dl>
      </div>
      <ErrorLine error={error} />
      <div className="mr-cs-foot">
        <label className="mr-cs-remember" title={fixed ? (standing ? "Only a standing grant is offered here" : "This grant ends with the run")
          : remember ? `${standing?.label ?? "Standing"}: later builds of ${mr.app_name} do not ask again` : "For this run only: it ends with the run"}>
          <input type="checkbox" checked={remember && Boolean(standing)} disabled={fixed || busy}
                 data-testid="consent-mr-remember" onChange={(e) => setRemember(e.target.checked)} />
          Remember for <span data-user-text>{mr.app_name}</span>
        </label>
        <span className="mr-ui-spacer" />
        <button type="button" className="idn-btn" onClick={cancel} disabled={busy} data-testid="consent-mr-cancel">Cancel</button>
        <button type="button" className="idn-btn primary" onClick={() => void allow()} disabled={busy || !choice}
                data-autofocus data-testid="consent-mr-allow" data-kind={choice?.kind}>
          {busy ? "Granting…" : `Allow ${n} ${n === 1 ? "repository" : "repositories"}`}
        </button>
      </div>
    </Sheet>
  );
}

/** Mounted once in the shell; opens the sheet for every `boss:consent-required`. */
export function ConsentHost() {
  const [request, setRequest] = useState<ConsentRequest | null>(null);
  useEffect(() => {
    const on = (e: Event) => setRequest((e as CustomEvent<ConsentRequest>).detail);
    window.addEventListener(CONSENT_EVENT, on);
    return () => window.removeEventListener(CONSENT_EVENT, on);
  }, []);
  return request ? <ConsentSheet request={request} onClose={() => setRequest(null)} /> : null;
}
