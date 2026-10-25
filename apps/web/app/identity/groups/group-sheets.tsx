"use client";

/**
 * THE GROUP WRITES (IDENTITY.md §9.3, §6.8): create, rename, delete, and bind or unbind an access
 * role. Each is one sheet that says what it changes before it is made. The API re-checks every
 * rule (you must hold what you grant; an IdP group's members are not edited here).
 */
import { useConsoleRefresh } from "../../use-console-refresh";
import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { LockSimple, PencilSimple, Plus, Trash, UsersThree } from "@phosphor-icons/react";

import { showToast } from "../../client-forms";
import { useGuardedCall, useIdentity } from "../../../components/identity/identity-provider";
import { ErrorLine, RoleChip, Sheet } from "../../../components/identity/kit";
import { IdentityError, idDelete, idPatch, idPost, refusalSentence } from "../../../lib/identity/api";
import type { AccessRole, Binding, Group } from "../../../lib/identity/types";
import { grantReach } from "../../../lib/identity/grant-limit";
import type { ProjectChoice } from "../../../lib/identity/types-people";

/** The API's words for a refusal, never its code. */
export function refusalWords(e: unknown): string {
  if (e instanceof IdentityError) {
    if (e.code === "step_up_cancelled") return "Not confirmed";
    if (e.code === "group_exists") return "A group with that name exists";
    if (e.code === "invalid_group") return "Check the name";
    if (e.code === "binding_exists") return "They already have it";
    if (e.code === "scope_not_bindable") return "This role binds at org only";
  }
  return refusalSentence(e);
}

/** "Release Captains" → "release-captains": the group's key (lower case, 1–63, GROUP_KEY). */
export function groupKey(name: string): string {
  const k = name.trim().toLowerCase().normalize("NFKD").replace(/[^a-z0-9_.-]+/g, "-").replace(/^[^a-z0-9]+/, "").replace(/-+$/, "");
  return k.slice(0, 63);
}

function useWrite() {
  const guarded = useGuardedCall();
  const refresh = useConsoleRefresh();
  return async <T,>(reason: string, call: () => Promise<T>, done: string): Promise<{ ok: true; value: T } | { ok: false; error: string }> => {
    try {
      const value = await guarded(reason, call);
      showToast("success", done);
      refresh("now");
      return { ok: true, value };
    } catch (e) {
      return { ok: false, error: refusalWords(e) };
    }
  };
}

export function GroupFormSheet({ group, onClose, onCreated }: { group?: Group; onClose: () => void; onCreated?: (g: { id: string }) => void }) {
  const write = useWrite();
  const [name, setName] = useState(group?.name ?? "");
  const [description, setDescription] = useState(group?.description ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = groupKey(name);
  const valid = name.trim().length > 0 && (group || key.length > 0);
  return (
    <Sheet title={group ? `Edit ${group.name}` : "New group"} icon={group ? <PencilSimple size={18} aria-hidden /> : <UsersThree size={18} aria-hidden />}
      onClose={onClose} testId={group ? "group-edit-sheet" : "group-create-sheet"}>
      {!group ? <p className="idn-lede2" data-copy="prose">People in a group share its access roles. Add roles and members after you create it.</p> : null}
      <div className="idn-field">
        <label htmlFor="grp-name">Name</label>
        <input id="grp-name" className="idn-input" value={name} maxLength={120} placeholder="Release" onChange={(e) => setName(e.target.value)} data-autofocus data-testid="group-name" />
      </div>
      <div className="idn-field">
        <label htmlFor="grp-desc">Description</label>
        <input id="grp-desc" className="idn-input" value={description} maxLength={2000} placeholder="Who is in it and why" onChange={(e) => setDescription(e.target.value)} data-testid="group-description" />
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!valid || busy} data-testid="group-save"
          onClick={async () => {
            setBusy(true);
            setError(null);
            const out = group
              ? await write("Renaming a group is recorded.", () => idPatch(`/v1/identity/groups/${encodeURIComponent(group.id)}`, { name: name.trim(), description }), "Group saved")
              : await write("Creating a group is recorded.", () => idPost<{ id: string }>("/v1/identity/groups", { key, name: name.trim(), description }), "Group created");
            setBusy(false);
            if (!out.ok) { setError(out.error); return; }
            if (!group && out.value && typeof (out.value as { id?: unknown }).id === "string") onCreated?.(out.value as { id: string });
            onClose();
          }}>
          {busy ? "Saving…" : group ? "Save" : "Create group"}
        </button>
      </div>
    </Sheet>
  );
}

export function DeleteGroupSheet({ group, onClose }: { group: Group; onClose: () => void }) {
  const write = useWrite();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const n = group.member_count ?? group.members.length;
  return (
    <Sheet title={`Delete ${group.name}?`} icon={<Trash size={18} className="idn-bad" aria-hidden />} onClose={onClose} testId="group-delete-sheet">
      <ul className="idn-cascade-list" aria-label="What this does">
        <li data-copy="prose">{n === 0 ? "It has no members." : `${n} ${n === 1 ? "person leaves" : "people leave"} the group.`}</li>
        {group.bindings.length
          ? <li data-copy="prose">They lose {group.bindings.map((b) => b.role_name).join(", ")} unless they hold it another way.</li>
          : <li data-copy="prose">It grants no access role.</li>}
        <li data-copy="prose">The people themselves are not removed.</li>
      </ul>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn danger solid" disabled={busy} data-testid="group-delete-go"
          onClick={async () => {
            setBusy(true);
            setError(null);
            const out = await write("Deleting a group removes the access it grants.", () => idDelete(`/v1/identity/groups/${encodeURIComponent(group.id)}`), "Group deleted");
            setBusy(false);
            if (!out.ok) { setError(out.error); return; }
            onClose();
            router.push("/identity/groups");
          }}>
          {busy ? "Deleting…" : "Delete group"}
        </button>
      </div>
    </Sheet>
  );
}

/** The binding route for a scope: the org's, or that project's own (a project Admin manages it). */
export function bindingPath(scope: string, id?: string): string {
  const base = scope.startsWith("project:")
    ? `/v1/identity/projects/${encodeURIComponent(scope.slice(8))}/role-bindings`
    : "/v1/identity/role-bindings";
  return id ? `${base}/${encodeURIComponent(id)}` : base;
}

export function BindRoleSheet({ group, roles, projects, onClose }: {
  group: Group; roles: AccessRole[]; projects: ProjectChoice[] | null; onClose: () => void;
}) {
  const { org } = useIdentity();
  const write = useWrite();
  const held = org?.permissions;
  const [role, setRole] = useState("");
  const [where, setWhere] = useState<"org" | "project">("org");
  const [project, setProject] = useState(projects?.[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const chosen = roles.find((r) => r.id === role);
  const canProject = !chosen || chosen.bindable_scopes.includes("project");
  const n = group.member_count ?? group.members.length;
  return (
    <Sheet title={`Add a role to ${group.name}`} icon={<Plus size={18} aria-hidden />} onClose={onClose} testId="group-bind-sheet">
      <p className="idn-lede2" data-copy="prose">{n === 1 ? "Its 1 member inherits" : `All ${n} members inherit`} the role you choose. You must hold all of it.</p>
      <div className="idn-field">
        <span className="idn-label" id="gb-role">Access role</span>
        <div className="idn-pick" role="radiogroup" aria-labelledby="gb-role">
          {roles.filter((r) => !r.system && !group.bindings.some((b) => b.role_id === r.id && b.scope === "org")).map((r) => {
            const { ok, reason } = grantReach(held, r);
            const lack = !ok;
            return (
              <button key={r.id} type="button" role="radio" aria-checked={role === r.id} aria-disabled={lack || undefined}
                className="idn-pick-row" data-selected={role === r.id || undefined} data-testid={`gb-role-${r.key}`}
                title={reason || undefined}
                onClick={() => { if (!lack) { setRole(r.id); if (!r.bindable_scopes.includes("project")) setWhere("org"); } }}>
                <RoleChip roleKey={r.key} name={r.name} />
                <span className="idn-dim">{r.permissions.length} permissions</span>
                {lack ? <span className="idn-lockline"><LockSimple size={12} aria-hidden /> you don&apos;t hold this</span> : null}
              </button>
            );
          })}
        </div>
      </div>
      <div className="idn-field">
        <span className="idn-label" id="gb-where">Where</span>
        <div className="idn-segs" role="radiogroup" aria-labelledby="gb-where">
          <button type="button" role="radio" aria-checked={where === "org"} aria-pressed={where === "org"} onClick={() => setWhere("org")}>Whole org</button>
          <button type="button" role="radio" aria-checked={where === "project"} aria-pressed={where === "project"}
            aria-disabled={!canProject || undefined} title={canProject ? "One project only" : "This role binds at org only"}
            onClick={() => { if (canProject) setWhere("project"); }}>One project</button>
        </div>
        {where === "project" ? <ProjectPicker id="gb-project" projects={projects} value={project} onChange={setProject} /> : null}
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" data-testid="group-bind-save"
          disabled={!role || busy || (where === "project" && !project.trim())}
          onClick={async () => {
            setBusy(true);
            setError(null);
            const scope = where === "org" ? "org" : `project:${project.trim()}`;
            const out = await write("Granting a group a role changes access for every member.",
              () => idPost(bindingPath(scope), { subject_type: "group", subject_id: group.id, role_id: role }), "Role added");
            setBusy(false);
            if (!out.ok) { setError(out.error); return; }
            onClose();
          }}>
          {busy ? "Adding…" : "Add role"}
        </button>
      </div>
    </Sheet>
  );
}

export function UnbindRoleSheet({ group, binding, onClose }: { group: Group; binding: Binding; onClose: () => void }) {
  const write = useWrite();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const n = group.member_count ?? group.members.length;
  return (
    <Sheet title={`Remove ${binding.role_name}?`} icon={<Trash size={18} className="idn-bad" aria-hidden />} onClose={onClose} testId="group-unbind-sheet">
      <p className="idn-lede2" data-copy="prose">
        {n === 1 ? "Its 1 member loses" : `Its ${n} members lose`} {binding.role_name} unless they hold it another way.
      </p>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn danger solid" disabled={busy} data-testid="group-unbind-go"
          onClick={async () => {
            setBusy(true);
            setError(null);
            const out = await write("Removing a group's role changes access for every member.", () => idDelete(bindingPath(binding.scope, binding.id)), "Role removed");
            setBusy(false);
            if (!out.ok) { setError(out.error); return; }
            onClose();
          }}>
          {busy ? "Removing…" : "Remove role"}
        </button>
      </div>
    </Sheet>
  );
}

/** A project, chosen from the list the viewer can read; typed only when the list is unreadable. */
export function ProjectPicker({ id, projects, value, onChange }: {
  id: string; projects: ProjectChoice[] | null; value: string; onChange: (v: string) => void;
}) {
  if (projects && projects.length) {
    return (
      <>
        <label className="idn-sr" htmlFor={id}>Project</label>
        <select id={id} className="idn-input" value={value} onChange={(e) => onChange(e.target.value)} data-testid={`${id}-select`}>
          {!value ? <option value="">Choose a project</option> : null}
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </>
    );
  }
  return (
    <>
      <label className="idn-sr" htmlFor={id}>Project</label>
      <input id={id} className="idn-input" placeholder="Project name or id" value={value} onChange={(e) => onChange(e.target.value)} data-testid={`${id}-input`} />
    </>
  );
}
