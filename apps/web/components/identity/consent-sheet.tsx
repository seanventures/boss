"use client";

/**
 * JUST-IN-TIME CONSENT (IDENTITY.md §7.3 2): work a person starts needs more than their standing
 * grants cover. The sheet names the agents, the abilities and the repos, and creates a grant —
 * task-bound ("for this build only", ends with the run) or standing (the policy's default days, as
 * the API offers them). Never more than
 * the person holds: the API intersects with their live permissions and each agent's ceiling.
 *
 * Opened by `openConsent(detail)` from anywhere (the seam S3's build start uses when the API
 * answers `409 consent_required`), and rendered once by <ConsentHost/> in the shell.
 */
import React, { useEffect, useState } from "react";
import { GithubLogo } from "@phosphor-icons/react";

import { AgentAvatar } from "../../app/agent-avatar";
import { IdentityError, idPost } from "../../lib/identity/api";
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

export function ConsentSheet({ request, onClose }: { request: ConsentRequest; onClose: () => void }) {
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
      setError(e instanceof IdentityError ? (e.detail ?? e.message) : "That didn't work");
      setBusy(null);
    }
  };

  return (
    <Sheet
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
        {other.map((p) => <span key={p} className="idn-perm sm">{p}</span>)}
        {request.github ? <span className="idn-chip"><GithubLogo size={13} aria-hidden /> {request.github.repos.join(", ") || request.github.repo} · {request.github.summary}</span> : null}
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
