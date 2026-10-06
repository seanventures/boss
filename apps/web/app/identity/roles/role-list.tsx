"use client";

/**
 * ACCESS ROLES (IDENTITY.md §9.4): built-in roles are read-only — a lock, and Clone to make one
 * your own — and custom roles open in the editor. Holders, permission counts and where each can
 * be bound, at a glance.
 */
import React, { useState } from "react";
import { CopySimple, LockSimple, PencilSimple, ShieldCheck } from "@phosphor-icons/react";

import { CanButton, useGuardedCall } from "../../../components/identity/identity-provider";
import { ErrorLine, RoleChip } from "../../../components/identity/kit";
import { IdentityError, idPost } from "../../../lib/identity/api";
import type { AccessRole } from "../../../lib/identity/types";
import { ROLE_PERMS } from "./catalogue";

export function useCloneRole() {
  const guarded = useGuardedCall();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const clone = async (role: AccessRole) => {
    setBusy(role.id);
    setError(null);
    try {
      const made = await guarded(`Cloning ${role.name} creates a new access role.`,
        () => idPost<AccessRole>(`/v1/identity/access-roles/${encodeURIComponent(role.id)}/clone`, {}));
      window.location.assign(`/identity/roles/${made.id}`);
    } catch (e) {
      setError(e instanceof IdentityError ? e.message : "Not cloned");
      setBusy(null);
    }
  };
  return { clone, error, busy };
}

function Row({ role, onClone, busy }: { role: AccessRole; onClone: (r: AccessRole) => void; busy: boolean }) {
  return (
    <tr data-testid={`role-row-${role.key}`}>
      <td>
        <a className="idn-who" href={`/identity/roles/${role.id}`}>
          <span className={`idn-ico${role.builtin ? "" : " ok"}`} aria-hidden>
            {role.builtin ? <LockSimple size={15} /> : <ShieldCheck size={15} />}
          </span>
          <span>
            <RoleChip roleKey={role.key} name={role.name} />
            <small data-copy="data">{role.description}</small>
          </span>
        </a>
      </td>
      <td className="idn-mono">{role.permissions.length} / {ROLE_PERMS.length}</td>
      <td>{role.bindable_scopes.join(" · ")}</td>
      <td className="idn-mono">{role.holders}</td>
      <td>
        <span className="idn-row">
          {!role.builtin ? (
            <a className="idn-btn sm" href={`/identity/roles/${role.id}`}><PencilSimple size={13} aria-hidden /> Edit</a>
          ) : null}
          <CanButton permission="access_role:manage" className="idn-btn sm" onClick={() => onClone(role)} disabled={busy}
            tip="A custom copy you can edit" data-testid={`clone-${role.key}`}>
            <CopySimple size={13} aria-hidden /> {busy ? "Cloning…" : "Clone"}
          </CanButton>
        </span>
      </td>
    </tr>
  );
}

export function RoleList({ roles }: { roles: AccessRole[] }) {
  const { clone, error, busy } = useCloneRole();
  const builtin = roles.filter((r) => r.builtin);
  const custom = roles.filter((r) => !r.builtin);
  const table = (list: AccessRole[], label: string) => (
    <div className="idn-scroll">
      <table className="idn-t" aria-label={label}>
        <thead><tr><th>Access role</th><th>Permissions</th><th>Bound at</th><th>Holders</th><th><span className="idn-sr">Actions</span></th></tr></thead>
        <tbody>{list.map((r) => <Row key={r.id} role={r} onClone={clone} busy={busy === r.id} />)}</tbody>
      </table>
    </div>
  );
  return (
    <>
      <div className="idn-sec-h">
        <h1 className="idn-h1">Access roles</h1>
        <span className="idn-sub">{builtin.length} built-in · {custom.length} custom</span>
      </div>
      <ErrorLine error={error} />
      <section className="idn-card" aria-labelledby="idn-builtin-h">
        <div className="idn-card-h">
          <h2 id="idn-builtin-h">Built-in</h2>
          <span className="idn-sub" title="Clone one to change what it grants">Read-only · clone to customise</span>
        </div>
        {table(builtin, "Built-in access roles")}
      </section>
      <section className="idn-card" aria-labelledby="idn-custom-h">
        <div className="idn-card-h">
          <h2 id="idn-custom-h">Custom</h2>
          <span className="idn-sub">Versioned · previewed before save</span>
        </div>
        {custom.length ? table(custom, "Custom access roles") : <span className="idn-dim">No custom roles yet · clone a built-in</span>}
      </section>
    </>
  );
}
