"use client";

/**
 * ACCESS REQUESTS (A13): what people asked for from an Access denied page, answered here. Approve
 * grants a role that carries the permission (the API binds it under every §6.11 rule); Deny
 * closes it. Shown only when there is something to answer; a failed read says so.
 */
import React, { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Check, HandWaving, X } from "@phosphor-icons/react";

import { showToast } from "../../client-forms";
import { CanButton, useGuardedCall, useIdentity } from "../../../components/identity/identity-provider";
import { Ago, ErrorLine, Perm, RoleChip, Sheet, useNow } from "../../../components/identity/kit";
import { ErrorState, type ReadFailure } from "../../../components/identity/states";
import { IdentityError, idPost } from "../../../lib/identity/api";
import type { AccessRole } from "../../../lib/identity/types";
import type { AccessRequestRow } from "../../../lib/identity/types-people";
import { grantReach } from "../../../lib/identity/grant-limit";
import { ExpiryField, expiresAt, type ExpiryChoice } from "./expiry";

function refusal(e: unknown): string {
  if (e instanceof IdentityError) {
    if (e.code === "step_up_cancelled") return "Not confirmed";
    if (e.code === "already_resolved") return "Someone already answered it";
    if (e.code === "role_lacks_permission") return "That role does not carry it";
    if (e.code === "binding_exists") return "They already have it";
    if (e.code === "scope_not_bindable") return "That role binds at org only";
    return e.message;
  }
  return "That didn't work";
}

/** Roles that carry the permission: the API's candidates, else the ones the console can see. */
export function candidateRoles(r: AccessRequestRow, roles: AccessRole[]): { id: string; key: string; name: string }[] {
  if (r.candidate_roles?.length) return r.candidate_roles;
  return roles.filter((x) => !x.system && x.permissions.includes(r.permission_key as AccessRole["permissions"][number])
    && (!r.project_id || x.bindable_scopes.includes("project")))
    .sort((a, b) => a.permissions.length - b.permissions.length)
    .map((x) => ({ id: x.id, key: x.key, name: x.name }));
}

function Approve({ request, roles, onClose }: { request: AccessRequestRow; roles: AccessRole[]; onClose: () => void }) {
  const guarded = useGuardedCall();
  const router = useRouter();
  const now = useNow();
  const { org, project } = useIdentity();
  // The grant limit (lib/identity/grant-limit.ts): what the approver holds where the request is.
  const held = useMemo(() => {
    const out = new Set<string>(org?.permissions ?? []);
    if (request.project_id && project?.scope.kind === "project" && project.scope.projectId === request.project_id) {
      for (const p of project.permissions) out.add(p);
    }
    return org ? out : null;
  }, [org, project, request.project_id]);
  const options = candidateRoles(request, roles).map((o) => {
    const full = roles.find((r) => r.id === o.id);
    return { ...o, reach: full ? grantReach(held, full) : { ok: true, missing: [], reason: "" } };
  });
  const [role, setRole] = useState(options.find((o) => o.reach.ok)?.id ?? "");
  const [expiry, setExpiry] = useState<ExpiryChoice>("30d");
  const [date, setDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title={`Give ${request.requester_name} access`} icon={<Check size={18} aria-hidden />} onClose={onClose} testId="request-approve-sheet">
      <p className="idn-lede2" data-copy="prose">They asked for <Perm p={request.permission_key} sm />{request.project_id ? ` in ${request.project_id}` : ""}. Grant it through a role:</p>
      <div className="idn-field">
        <span className="idn-label" id="ar-role">Role</span>
        {options.length ? (
          <div className="idn-pick" role="radiogroup" aria-labelledby="ar-role">
            {options.map((o) => (
              <button key={o.id} type="button" role="radio" aria-checked={role === o.id} className="idn-pick-row" data-selected={role === o.id || undefined}
                aria-disabled={!o.reach.ok || undefined} title={o.reach.reason || undefined}
                onClick={() => { if (o.reach.ok) setRole(o.id); }} data-testid={`ar-role-${o.key}`}>
                <RoleChip roleKey={o.key} name={o.name} />
              </button>
            ))}
          </div>
        ) : <span className="idn-dim">No role you can see carries it</span>}
      </div>
      <ExpiryField id="ar-exp" choice={expiry} date={date} onChoice={setExpiry} onDate={setDate} />
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!role || busy || (expiry === "date" && !expiresAt("date", date, now))} data-testid="request-approve-go"
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await guarded("Granting access changes what they can do.", () => idPost(`/v1/identity/access-requests/${encodeURIComponent(request.id)}/approve`,
                { role_id: role, expires_at: expiresAt(expiry, date, now), reason: null }));
              showToast("success", "Access granted");
              router.refresh();
              onClose();
            } catch (e) {
              setError(refusal(e));
            } finally {
              setBusy(false);
            }
          }}>
          {busy ? "Granting…" : "Approve"}
        </button>
      </div>
    </Sheet>
  );
}

export function AccessRequests({ requests, roles, failure = null, project = null }: {
  requests: AccessRequestRow[] | null; roles: AccessRole[]; failure?: ReadFailure | null;
  /** The project whose queue this is (a project Admin's), else null: the org's list. */
  project?: string | null;
}) {
  const guarded = useGuardedCall();
  const router = useRouter();
  const { projectId: mirroredProject } = useIdentity();
  // A request is judged at ITS project only when that is the project the console mirrors (its
  // snapshot is loaded); any other project's request is judged by the org snapshot, which is
  // what an org Admin decides it with. Otherwise it would read "still loading" for good.
  const judgedAt = (r: AccessRequestRow) => (r.project_id && r.project_id === mirroredProject ? r.project_id : null);
  const [approving, setApproving] = useState<AccessRequestRow | null>(null);
  if (failure) return <ErrorState compact what="access requests" failure={failure} testId="requests-error" />;
  const open = (requests ?? []).filter((r) => r.status === "open");
  if (!open.length) return null;
  return (
    <section className="idn-card idn-requests" aria-labelledby="idn-req-h" data-testid="access-requests">
      <div className="idn-card-h">
        <h2 id="idn-req-h"><HandWaving size={16} aria-hidden /> Access requests</h2>
        <span className="idn-sub" title={project ? `Requests for ${project}, where you assign roles` : "Requests across the org"}>
          {open.length} waiting{project ? ` · ${project}` : ""}
        </span>
      </div>
      {open.map((r) => (
        <div key={r.id} className="idn-req" data-testid={`request-${r.id}`}>
          <span className="idn-req-who">
            <b>{r.requester_name}</b>
            <span className="idn-row">
              <span className="idn-dim">asks for</span> <Perm p={r.permission_key} sm />
              {r.project_id ? <span className="idn-dim">in {r.project_id}</span> : null}
            </span>
            <small className="idn-dim" data-copy="data">{r.reason ? `“${r.reason}” · ` : ""}<Ago iso={r.created_at} /></small>
          </span>
          <span className="idn-row">
            <CanButton permission="role_binding:assign" projectId={judgedAt(r)} className="idn-btn sm" tip="Close it without granting" data-testid={`request-deny-${r.id}`}
              onClick={async () => {
                try {
                  await guarded("Declining is recorded.", () => idPost(`/v1/identity/access-requests/${encodeURIComponent(r.id)}/deny`, { reason: null }));
                  showToast("success", "Declined");
                  router.refresh();
                } catch (e) {
                  showToast("error", refusal(e));
                }
              }}>
              <X size={13} aria-hidden /> Deny
            </CanButton>
            <CanButton permission="role_binding:assign" projectId={judgedAt(r)} className="idn-btn sm primary" tip="Grant it through a role" data-testid={`request-approve-${r.id}`}
              onClick={() => setApproving(r)}>
              <Check size={13} aria-hidden /> Approve
            </CanButton>
          </span>
        </div>
      ))}
      {approving ? <Approve request={approving} roles={roles} onClose={() => setApproving(null)} /> : null}
    </section>
  );
}
