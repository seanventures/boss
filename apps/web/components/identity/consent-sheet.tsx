"use client";

/**
 * JUST-IN-TIME CONSENT (IDENTITY.md §7.3 2): work a person starts needs more than their standing
 * grants cover. The sheet names the agents, the abilities and the repos, and creates a grant —
 * task-bound ("for this build only", ends with the run) or standing (30 days). Never more than
 * the person holds: the API intersects with their live permissions and each agent's ceiling.
 *
 * Opened by `openConsent(detail)` from anywhere (the seam S3's build start uses when the API
 * answers `409 consent_required`), and rendered once by <ConsentHost/> in the shell.
 */
import React, { useEffect, useState } from "react";
import { GithubLogo } from "@phosphor-icons/react";

import { AgentAvatar } from "../../app/agent-avatar";
import { IdentityError, idPost } from "../../lib/identity/api";
import { PERMISSION_META, isPermission, type Permission } from "../../lib/identity/permissions.gen";
import { useGuardedCall } from "./identity-provider";
import { ErrorLine, Perm, Sheet } from "./kit";

export const CONSENT_EVENT = "boss:consent-required";

export type ConsentRequest = {
  agents: { id: string; name: string; roleKey?: string }[];
  /** What the work is ("Interview Scorecard"). */
  task: string;
  permissions: string[];
  projects: string[];
  github: { repo: string; summary: string } | null;
  /** Called with the new grant id, or null when the person cancels. */
  onDecided?: (grantId: string | null) => void;
};

export function openConsent(detail: ConsentRequest): void {
  window.dispatchEvent(new CustomEvent<ConsentRequest>(CONSENT_EVENT, { detail }));
}

function names(list: { name: string }[]): string {
  if (list.length <= 1) return list[0]?.name ?? "This agent";
  return `${list.slice(0, -1).map((a) => a.name).join(", ")} and ${list[list.length - 1].name}`;
}

export function ConsentSheet({ request, onClose }: { request: ConsentRequest; onClose: () => void }) {
  const [busy, setBusy] = useState<"task" | "standing" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const perms = request.permissions.filter(isPermission) as Permission[];
  const neverDelegable = perms.filter((p) => !PERMISSION_META[p].delegable);

  const decide = async (kind: "task" | "standing") => {
    setBusy(kind);
    setError(null);
    try {
      const grant = await guarded(`Let ${names(request.agents)} act for you.`, () => idPost<{ id: string }>("/v1/identity/me/delegations", {
        agents: request.agents.map((a) => a.id), permissions: perms, projects: request.projects,
        kind, days: kind === "standing" ? 30 : null, purpose: request.task,
      }));
      request.onDecided?.(grant.id);
      onClose();
    } catch (e) {
      setError(e instanceof IdentityError ? e.message : "That didn't work");
      setBusy(null);
    }
  };

  return (
    <Sheet
      title={`Let ${names(request.agents)} act for you?`}
      icon={<span className="idn-stack" aria-hidden>{request.agents.slice(0, 3).map((a) => <AgentAvatar key={a.id} agent={a.roleKey ?? a.name} size={26} />)}</span>}
      onClose={() => { request.onDecided?.(null); onClose(); }}
      testId="consent-sheet"
    >
      <p className="idn-lede2" data-copy="prose">
        To build <b>{request.task}</b> they need a little of your access, and never more than you have.
      </p>
      <div className="idn-need">
        {perms.filter((p) => PERMISSION_META[p].delegable).map((p) => <Perm key={p} p={p} sm />)}
        {request.github ? <span className="idn-chip"><GithubLogo size={13} aria-hidden /> {request.github.repo} · {request.github.summary}</span> : null}
      </div>
      {neverDelegable.length ? (
        <div className="idn-note" data-copy="prose">Agents never get {neverDelegable.join(", ")}. You keep those.</div>
      ) : null}
      <div className="idn-opts">
        <button type="button" className="idn-btn primary" onClick={() => decide("task")} disabled={!!busy} data-autofocus>
          {busy === "task" ? "Granting…" : "For this build only"}
        </button>
        <button type="button" className="idn-btn" onClick={() => decide("standing")} disabled={!!busy}>
          {busy === "standing" ? "Granting…" : "For 30 days"}
        </button>
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
