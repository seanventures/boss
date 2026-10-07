"use client";

/**
 * A GROUP (IDENTITY.md §9.3, §6.8, §6.11): its members and the access roles they inherit, with
 * the impact of a change said before it is made. Members of a manual group are added and removed
 * here (group:manage_members; the API also requires you to hold every permission the group's
 * roles carry). A group synced from the IdP is read-only: change it there.
 */
import { useConsoleRefresh } from "../../../use-console-refresh";
import React, { useState } from "react";
import { ArrowsClockwise, Info, PencilSimple, Plus, Trash, UsersThree } from "@phosphor-icons/react";

import { showToast } from "../../../client-forms";
import { CanButton, useGuardedCall } from "../../../../components/identity/identity-provider";
import { Ago, Face, RoleChip, Sheet } from "../../../../components/identity/kit";
import { IdentityError, idDelete, idPost } from "../../../../lib/identity/api";
import type { AccessRole, Binding, Group, Person } from "../../../../lib/identity/types";
import type { ProjectChoice } from "../../../../lib/identity/types-people";
import { EmptyState } from "../../../../components/identity/states";
import { sourceWord } from "../groups-list";
import { grantedByWords } from "../../../../lib/identity/adapt-people";
import { BindRoleSheet, DeleteGroupSheet, GroupFormSheet, UnbindRoleSheet } from "../group-sheets";

function useAct() {
  const guarded = useGuardedCall();
  const refresh = useConsoleRefresh();
  return async (reason: string, call: () => Promise<unknown>, done: string) => {
    try {
      await guarded(reason, call);
      showToast("success", done);
      refresh("now");
      return true;
    } catch (e) {
      showToast("error", e instanceof IdentityError ? (e.code === "step_up_cancelled" ? "Not confirmed" : e.message) : "That didn't work");
      return false;
    }
  };
}

/** "4 people inherit Operator (org)". */
export function impactLine(g: Group): string {
  const n = g.members.length;
  const who = `${n} ${n === 1 ? "person inherits" : "people inherit"}`;
  if (!g.bindings.length) return `${who} nothing yet`;
  return `${who} ${g.bindings.map((b) => `${b.role_name} (${b.scope === "org" ? "org" : b.scope.replace(/^project:/, "")})`).join(", ")}`;
}

function AddMember({ group, people, onClose }: { group: Group; people: Person[]; onClose: () => void }) {
  const act = useAct();
  const [pick, setPick] = useState("");
  const candidates = people.filter((p) => p.status !== "disabled" && !group.members.some((m) => m.id === p.id));
  return (
    <Sheet title={`Add to ${group.name}`} icon={<Plus size={18} aria-hidden />} onClose={onClose} testId="add-member-sheet">
      {group.bindings.length ? (
        <p className="idn-lede2" data-copy="prose">They will inherit {group.bindings.map((b) => b.role_name).join(" and ")}. You must hold all of it.</p>
      ) : null}
      <div className="idn-field">
        <label htmlFor="am-person">Person</label>
        <select id="am-person" className="idn-input" value={pick} onChange={(e) => setPick(e.target.value)} data-autofocus>
          <option value="">Choose a person</option>
          {candidates.map((p) => <option key={p.id} value={p.id}>{p.display_name} · {p.email}</option>)}
        </select>
      </div>
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!pick} data-testid="add-member-save"
          onClick={async () => {
            if (await act("Adding a member grants the group's access.", () => idPost(`/v1/identity/groups/${encodeURIComponent(group.id)}/members`, { principal_id: pick }), "Member added")) onClose();
          }}>
          Add member
        </button>
      </div>
    </Sheet>
  );
}

type GroupSheet = { kind: "edit" } | { kind: "delete" } | { kind: "bind" } | { kind: "unbind"; binding: Binding };

export function GroupDetail({ group, people, roles = [], projects = null }: {
  group: Group; people: Person[] | null; roles?: AccessRole[]; projects?: ProjectChoice[] | null;
}) {
  const act = useAct();
  const [adding, setAdding] = useState(false);
  const [sheet, setSheet] = useState<GroupSheet | null>(null);
  const manual = group.source === "manual";
  return (
    <>
      <nav className="idn-crumb" aria-label="Breadcrumb"><a href="/identity/groups">Groups</a> <span aria-hidden>›</span> <span>{group.name}</span></nav>
      <div className="idn-dh">
        <span className="idn-ico idn-ico-lg"><UsersThree size={22} aria-hidden /></span>
        <div className="idn-grow">
          <h1 className="idn-h1">{group.name} <span className={`idn-tag${manual ? "" : " warn"}`}>{sourceWord(group)}</span></h1>
          <span className="idn-dim" data-copy="data">{group.description}</span>
        </div>
        <div className="idn-acts">
          {manual ? (
            <>
              <CanButton permission="group:manage" className="idn-btn sm" onClick={() => setSheet({ kind: "edit" })} tip="Rename or describe it" data-testid="group-edit">
                <PencilSimple size={14} aria-hidden /> Edit
              </CanButton>
              <CanButton permission="group:manage" className="idn-btn sm danger" onClick={() => setSheet({ kind: "delete" })} tip="Members lose what it grants" data-testid="group-delete">
                <Trash size={14} aria-hidden /> Delete
              </CanButton>
              <CanButton permission="group:manage_members" className="idn-btn primary sm" onClick={() => setAdding(true)} tip="Members inherit this group's access roles" data-testid="group-add-member">
                <Plus size={14} aria-hidden /> Add member
              </CanButton>
            </>
          ) : null}
        </div>
      </div>

      {!manual ? (
        <div className="idn-note info" data-testid="idp-read-only">
          <Info size={16} aria-hidden />
          <span data-copy="prose">Synced from {group.provider ?? "your identity provider"}{group.synced_at ? <>, <Ago iso={group.synced_at} /></> : ""}. Change its members there; give it access roles here.</span>
        </div>
      ) : null}

      <div className="idn-impact" data-testid="group-impact"><ArrowsClockwise size={14} aria-hidden /> <span data-copy="data">{impactLine(group)}</span></div>

      <div className="idn-cols">
        <section className="idn-card" aria-labelledby="idn-gm-h">
          <div className="idn-card-h"><h2 id="idn-gm-h">Members</h2><span className="idn-sub">{group.members.length}</span></div>
          <div className="idn-scroll">
            <table className="idn-t">
              <thead><tr><th>Person</th><th /></tr></thead>
              <tbody>
                {group.members.map((m) => (
                  <tr key={m.id} data-testid={`member-${m.id}`}>
                    <td>
                      <a className="idn-who" href={`/identity/people/${m.id}`}>
                        <Face name={m.name} tone={m.hue} size={30} />
                        <span><b>{m.name}</b><small data-copy="data">{m.email}</small></span>
                      </a>
                    </td>
                    <td className="idn-right">
                      {manual && (m.source ?? "manual") !== "manual" ? (
                        <span className="idn-tag warn" data-testid={`member-source-${m.id}`}
                          title="Synced at sign-in from your identity provider: change it there">
                          from {m.source === "scim" ? "SCIM" : "OIDC"} sign-in
                        </span>
                      ) : manual ? (
                        <CanButton permission="group:manage_members" className="idn-btn sm" tip="They lose what the group grants" data-testid={`remove-member-${m.id}`}
                          onClick={() => act("Removing a member changes their access.", () => idDelete(`/v1/identity/groups/${encodeURIComponent(group.id)}/members/${encodeURIComponent(m.id)}`), "Member removed")}>
                          Remove
                        </CanButton>
                      ) : <span className="idn-dim">from {sourceWord(group)}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!group.members.length ? (
            <EmptyState compact icon={<UsersThree size={18} />} title="No members yet" testId="group-no-members">
              {manual ? "Add people to give them this group's roles." : `Members arrive from ${group.provider ?? "your identity provider"}.`}
            </EmptyState>
          ) : null}
        </section>
        <section className="idn-card" aria-labelledby="idn-gb-h">
          <div className="idn-card-h">
            <h2 id="idn-gb-h">Access roles</h2><span className="idn-sub">every member inherits these</span>
            <span className="idn-spacer" />
            <CanButton permission="role_binding:assign" className="idn-btn sm" onClick={() => setSheet({ kind: "bind" })} tip="Every member gets this role" data-testid="group-bind">
              <Plus size={14} aria-hidden /> Add role
            </CanButton>
          </div>
          {group.bindings.map((b) => (
            <div key={b.id} className="idn-bind" data-testid={`group-binding-${b.id}`}>
              <RoleChip roleKey={b.role_key} name={b.role_name} scope={b.scope} />
              <span className="idn-src" data-copy="data">granted by {grantedByWords(b)}{b.expires_at ? ` · ends ${b.expires_at.slice(0, 10)}` : ""}</span>
              <span className="idn-row">
                <a className="idn-btn sm ghost" href={`/identity/roles/${b.role_id}`}>Open</a>
                <CanButton permission="role_binding:assign" className="idn-btn sm" tip="Members lose this role" data-testid={`group-unbind-${b.id}`}
                  onClick={() => setSheet({ kind: "unbind", binding: b })}>
                  Remove
                </CanButton>
              </span>
            </div>
          ))}
          {!group.bindings.length ? (
            <EmptyState compact title="No access roles yet" testId="group-no-roles">
              Add a role and every member inherits it.
            </EmptyState>
          ) : null}
        </section>
      </div>
      {adding && people ? <AddMember group={group} people={people} onClose={() => setAdding(false)} /> : null}
      {sheet?.kind === "edit" ? <GroupFormSheet group={group} onClose={() => setSheet(null)} /> : null}
      {sheet?.kind === "delete" ? <DeleteGroupSheet group={group} onClose={() => setSheet(null)} /> : null}
      {sheet?.kind === "bind" ? <BindRoleSheet group={group} roles={roles} projects={projects} onClose={() => setSheet(null)} /> : null}
      {sheet?.kind === "unbind" ? <UnbindRoleSheet group={group} binding={sheet.binding} onClose={() => setSheet(null)} /> : null}
    </>
  );
}
