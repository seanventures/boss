"use client";

/**
 * THE ROLE EDITOR (mockup c, IDENTITY.md §6.10, §6.11, §9.4).
 *
 *   left   — name, description, where it can be bound, what it was cloned from, its versions;
 *   centre — the permission matrix: category → resource → one toggle per action. Critical keys
 *            are red, step-up keys carry a key, keys you do not hold are dashed and locked;
 *   right  — the diff and the impact preview the API computes (who, which pages, how many API
 *            operations and MCP tools, which agents' grants change).
 *
 * Saving a critical addition needs the role's name typed back; the save itself asks for a fresh
 * second factor (step-up). A grant-ceiling refusal (403 forbidden + permission) lands on the
 * cell it names. Built-in roles are read-only: clone one to change it. The API re-checks it all.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { plainText } from "../../../../lib/identity/plain";
import { ArrowCounterClockwise, CaretDown, CaretRight, ClockCounterClockwise, CopySimple, Key, LockSimple, MagnifyingGlass, ShieldCheck, Trash, Warning } from "@phosphor-icons/react";

import { AgentAvatar } from "../../../agent-avatar";
import { CanButton, useCan, useGuardedCall, useIdentity } from "../../../../components/identity/identity-provider";
import { Delta, ErrorLine, Face, Perm, Sheet, shortDate } from "../../../../components/identity/kit";
import { showToast } from "../../../client-forms";
import { IdentityError, idDelete, idGet, idPatch, idPost } from "../../../../lib/identity/api";
import { permissionName } from "../../../../lib/identity/words";
import { PERMISSION_META, isPermission, type Permission } from "../../../../lib/identity/permissions.gen";
import { adaptRole, adaptRolePreview } from "../../../../lib/identity/adapt-people";
import type { AccessRole, RolePreview, RoleVersion } from "../../../../lib/identity/types";
import { MATRIX, ROLE_PERMS, actionOf } from "../catalogue";
import { useCloneRole } from "../role-list";

const OPEN_BY_DEFAULT = new Set(["Factory and code", "Workflows and work", "Deploy and infra", "Approvals"]);

type Cell = { on: boolean; change: "add" | "remove" | null; locked: boolean; error: boolean };

function Toggle({ p, cell, readOnly, why, onFlip }: { p: Permission; cell: Cell; readOnly: boolean; why: string; onFlip: (p: Permission) => void }) {
  const meta = PERMISSION_META[p];
  const off = readOnly || cell.locked;
  const tip = cell.error ? `You don't hold “${meta.title}”` : cell.locked ? "You don't hold this" : readOnly ? why
    : `${meta.title}${meta.critical ? " · critical" : ""}${meta.stepUp ? " · step-up at use" : ""}`;
  return (
    <button type="button" className="idn-tg" aria-pressed={cell.on} aria-disabled={off || undefined}
      data-crit={meta.critical || undefined} data-locked={cell.locked || undefined} data-change={cell.change ?? undefined}
      data-error={cell.error || undefined} data-perm={p} title={tip}
      aria-label={`${p}${cell.on ? ", on" : ", off"}${cell.change ? `, ${cell.change === "add" ? "added" : "removed"}` : ""}${off ? `, ${tip}` : ""}`}
      onClick={() => { if (!off) onFlip(p); }}>
      <span className="idn-sw" aria-hidden />
      {actionOf(p)}
      {meta.stepUp ? <Key size={11} className="idn-k" aria-hidden /> : null}
      {cell.locked ? <LockSimple size={11} aria-hidden /> : null}
    </button>
  );
}

/** "2 direct, 1 through Builders and 3 through Release" — every group by name, with its count. */
export function impactSplit(direct: number, viaGroups: { group: string; count: number }[]): string {
  const parts = [`${direct} direct`, ...viaGroups.map((g) => `${g.count} through ${g.group}`)];
  return parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

/** An agent's change line from the preview ("−deployment:read +x") with permission names. */
function changeWords(change: string): string {
  return change.split(/\s+/).filter(Boolean).map((t) => {
    const sign = /^[+−-]/.test(t) ? t[0] : "";
    const key = sign ? t.slice(1) : t;
    return key.includes(":") ? `${sign}${permissionName(key)}` : t;
  }).join(", ");
}

/** The API's added preview fields (counts, pages, routes, tools), laid over the adapted preview. */
type PreviewCounts = { people: number; groups: number; service_accounts: number; agents: number; grants: number };
function withExtras(base: RolePreview, raw: unknown): RolePreview & { counts: PreviewCounts | null } {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const pair = (v: unknown) => (v && typeof v === "object" ? v as { gained?: unknown; lost?: unknown } : null);
  const pages = pair(r.pages), routes = pair(r.routes), tools = pair(r.tools);
  const c = r.counts && typeof r.counts === "object" ? r.counts as Record<string, unknown> : null;
  const n = (v: unknown) => (typeof v === "number" ? v : 0);
  return {
    ...base,
    pages: pages && Array.isArray(pages.gained) ? { gained: pages.gained.map(String), lost: Array.isArray(pages.lost) ? pages.lost.map(String) : [] } : base.pages,
    routes: routes && typeof routes.gained === "number" ? { gained: n(routes.gained), lost: n(routes.lost) } : base.routes,
    tools: tools && typeof tools.gained === "number" ? { gained: n(tools.gained), lost: n(tools.lost) } : base.tools,
    counts: c ? { people: n(c.people), groups: n(c.groups), service_accounts: n(c.service_accounts), agents: n(c.agents), grants: n(c.grants) } : null,
  };
}

/** Delete a custom role. One that still has holders moves them to another role first. */
function DeleteRole({ role, roles, onClose }: { role: AccessRole; roles: AccessRole[]; onClose: () => void }) {
  const guarded = useGuardedCall();
  const [inUse, setInUse] = useState<number>(role.holders);
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const targets = roles.filter((r) => r.id !== role.id && !r.system && r.key !== "owner");
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      if (inUse > 0) {
        await guarded(`Moving ${role.name}'s holders changes their access.`, () =>
          idPost(`/v1/identity/access-roles/${encodeURIComponent(role.id)}/reassign`, { to_role_id: to, delete: true }));
      } else {
        await guarded(`Deleting ${role.name} removes it for good.`, () => idDelete(`/v1/identity/access-roles/${encodeURIComponent(role.id)}`));
      }
      showToast("success", "Role deleted");
      window.location.assign("/identity/roles");
    } catch (e) {
      if (e instanceof IdentityError && e.code === "role_in_use") {
        setInUse(typeof e.body.bindings === "number" ? e.body.bindings : Math.max(1, role.holders));
      } else if (e instanceof IdentityError && e.code === "scope_not_bindable") {
        setError("That role can't be bound everywhere this one is");
      } else if (e instanceof IdentityError && e.code === "same_role") {
        setError("Choose a different role");
      } else if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) {
        setError(e instanceof IdentityError ? e.message : "Not deleted");
      }
      setBusy(false);
    }
  };
  return (
    <Sheet title={`Delete ${role.name}`} icon={<Trash size={18} aria-hidden />} onClose={onClose} testId="delete-role">
      {inUse > 0 ? (
        <>
          <p className="idn-lede2" data-copy="prose">
            {inUse} {inUse === 1 ? "person or group holds" : "people and groups hold"} this role. Move them to another role first; then it is deleted.
          </p>
          <div className="idn-field">
            <label htmlFor="reassign-to">Move holders to</label>
            <select id="reassign-to" className="idn-input" value={to} onChange={(e) => setTo(e.target.value)} data-autofocus data-testid="reassign-to">
              <option value="">Choose a role</option>
              {targets.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          </div>
        </>
      ) : <p className="idn-lede2" data-copy="prose">Nobody holds it. Its history stays in the audit log.</p>}
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn danger" disabled={busy || (inUse > 0 && !to)} onClick={run} data-testid="delete-role-confirm">
          <Trash size={14} aria-hidden /> {busy ? "Deleting…" : inUse > 0 ? "Move and delete" : "Delete role"}
        </button>
      </div>
    </Sheet>
  );
}

export function RoleEditor({ role: initial, versions, names = {}, roles = [] }: {
  role: AccessRole; versions: RoleVersion[]; names?: Record<string, { name: string; hue: number }>;
  /** Every access role, for "move holders to" when deleting this one. */
  roles?: AccessRole[];
}) {
  const nameMap = useMemo(() => new Map(Object.entries(names)), [names]);
  const { org, mirrored } = useIdentity();
  const manage = useCan("access_role:manage");
  const guarded = useGuardedCall();
  const { clone, busy: cloning, error: cloneError } = useCloneRole();
  const [role, setRole] = useState(initial);
  const [draft, setDraft] = useState<Set<Permission>>(() => new Set(initial.permissions));
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description);
  const [scopes, setScopes] = useState<("org" | "project")[]>(initial.bindable_scopes);
  const [deleting, setDeleting] = useState(false);
  const [filter, setFilter] = useState("");
  const [onlyChanges, setOnlyChanges] = useState(false);
  const [open, setOpen] = useState<Set<string>>(() => new Set(MATRIX.filter((c) => OPEN_BY_DEFAULT.has(c.name)).map((c) => c.name)));
  const [preview, setPreview] = useState<(RolePreview & { counts: PreviewCounts | null }) | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [ceiling, setCeiling] = useState<Set<Permission>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const versionsRef = useRef<HTMLDivElement>(null);

  const readOnly = role.builtin || !manage.allowed;
  const readOnlyWhy = role.builtin ? "Built-in roles are read-only · clone it" : manage.reason;
  const held = mirrored ? org?.permissions ?? new Set<Permission>() : null;
  const base = useMemo(() => new Set(role.permissions), [role.permissions]);
  const added = useMemo(() => ROLE_PERMS.filter((p) => draft.has(p) && !base.has(p)), [draft, base]);
  const removed = useMemo(() => ROLE_PERMS.filter((p) => !draft.has(p) && base.has(p)), [draft, base]);
  const scopesDirty = [...scopes].sort().join() !== [...role.bindable_scopes].sort().join();
  const dirty = added.length + removed.length > 0 || name !== role.name || description !== role.description || scopesDirty;
  const permsDirty = added.length + removed.length > 0;
  const criticalAdds = added.filter((p) => PERMISSION_META[p].critical);

  // The impact preview: asked of the API whenever the permission set changes (debounced).
  useEffect(() => {
    if (!permsDirty) { setPreview(null); setPreviewError(null); return; }
    const t = window.setTimeout(() => {
      idPost<unknown>(`/v1/identity/access-roles/${encodeURIComponent(role.id)}/preview`, { permissions: [...draft] })
        .then((p) => { setPreview(withExtras(adaptRolePreview(p, nameMap), p)); setPreviewError(null); })
        .catch((e) => setPreviewError(e instanceof IdentityError ? e.message : "No preview"));
    }, 250);
    return () => window.clearTimeout(t);
  }, [draft, permsDirty, role.id, nameMap]);

  const cell = (p: Permission): Cell => ({
    on: draft.has(p),
    change: added.includes(p) ? "add" : removed.includes(p) ? "remove" : null,
    locked: !readOnly && held !== null && !held.has(p),
    error: ceiling.has(p),
  });
  const flip = (p: Permission) => {
    setDraft((d) => { const n = new Set(d); if (n.has(p)) n.delete(p); else n.add(p); return n; });
    setCeiling((c) => { if (!c.has(p)) return c; const n = new Set(c); n.delete(p); return n; });
  };

  const q = filter.trim().toLowerCase();
  const visible = (p: Permission) => (!q || p.includes(q) || PERMISSION_META[p].title.toLowerCase().includes(q))
    && (!onlyChanges || added.includes(p) || removed.includes(p));

  const people = preview?.counts?.people ?? preview?.affected.people.length ?? role.holders;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const next = await guarded(`Saving ${role.name} v${role.version + 1} changes access for ${people} ${people === 1 ? "person" : "people"}.`,
        // The typed name is the console's own gate; the API's is the list of critical keys.
        () => idPatch<unknown>(`/v1/identity/access-roles/${encodeURIComponent(role.id)}`, {
          permissions: [...draft], name, description, confirm_critical: criticalAdds,
          ...(scopesDirty ? { bindable_scopes: scopes } : {}),
        })).then((r) => ({ ...adaptRole(r), cloned_from: role.cloned_from }));
      setRole(next);
      setDraft(new Set(next.permissions));
      setName(next.name);
      setDescription(next.description);
      setScopes(next.bindable_scopes);
      setConfirming(false);
      setTyped("");
      showToast("success", `Saved v${next.version}`);
    } catch (e) {
      // A grant-ceiling refusal names what you lack: `missing` (roles.py), or one `permission`.
      const lacking = e instanceof IdentityError && e.code === "forbidden"
        ? [...(Array.isArray(e.body.missing) ? e.body.missing.map(String) : []), ...(e.permission ? [e.permission] : [])].filter(isPermission)
        : [];
      if (lacking.length) {
        setCeiling((c) => new Set([...c, ...lacking]));
        setError(`You don't hold ${lacking.map((p) => `“${permissionName(p)}”`).join(", ")}`);
        setConfirming(false);
      } else if (e instanceof IdentityError && (e.code === "confirmation_required" || e.code === "confirm_required")) {
        setConfirming(true);
      } else if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) {
        setError(e instanceof IdentityError ? e.message : "Not saved");
      }
    } finally {
      setSaving(false);
    }
  };
  const review = () => {
    if (criticalAdds.length) { setConfirming(true); return; }
    save();
  };
  const discard = () => {
    setDraft(new Set(role.permissions)); setName(role.name); setDescription(role.description); setScopes(role.bindable_scopes); setCeiling(new Set()); setError(null);
  };
  const loadVersion = async (v: RoleVersion) => {
    try {
      const got = await idGet<{ version: number; permissions: Permission[] }>(`/v1/identity/access-roles/${encodeURIComponent(role.id)}/versions/${v.version}`);
      setDraft(new Set(got.permissions));
      showToast("success", `Loaded v${v.version}`);
    } catch (e) {
      setError(e instanceof IdentityError ? e.message : "Version unavailable");
    }
  };

  const onCount = draft.size;
  const closed = MATRIX.filter((c) => !open.has(c.name) && !(q || onlyChanges));

  return (
    <>
      <header className="idn-rh">
        <ShieldCheck size={30} className="idn-shield" aria-hidden />
        <div className="idn-rh-t">
          <nav className="idn-crumb" aria-label="Breadcrumb"><a href="/identity/roles">Access roles</a><CaretRight size={11} aria-hidden /><span>{role.name}</span></nav>
          <h1>
            {role.name}
            <span className="idn-tag">{role.builtin ? "built-in" : "custom"}</span>
            {dirty ? <span className="idn-tag warn" data-testid="unsaved">unsaved · v{role.version} → v{role.version + 1}</span> : <span className="idn-tag">v{role.version}</span>}
          </h1>
        </div>
        <span className="idn-spacer" />
        <CanButton permission="access_role:manage" className="idn-btn" onClick={() => clone(role)} disabled={cloning === role.id} tip="A custom copy you can edit">
          <CopySimple size={15} aria-hidden /> Clone
        </CanButton>
        <button type="button" className="idn-btn" onClick={() => { versionsRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }); versionsRef.current?.focus(); }}>
          <ClockCounterClockwise size={15} aria-hidden /> History
        </button>
        {!role.builtin ? (
          <CanButton permission="access_role:manage" className="idn-btn" onClick={() => setDeleting(true)} tip="Delete it; its holders move to another role" data-testid="delete-role-open">
            <Trash size={15} aria-hidden /> Delete
          </CanButton>
        ) : null}
        {!role.builtin ? (
          <CanButton permission="access_role:manage" className="idn-btn primary" onClick={review} disabled={!dirty || saving}
            tip="See who it affects, then confirm it's you" data-testid="review-save">
            <Key size={15} aria-hidden /> Review and save
          </CanButton>
        ) : null}
      </header>
      <ErrorLine error={cloneError} />
      {role.builtin ? (
        <div className="idn-note info" data-copy="prose"><LockSimple size={15} aria-hidden /> Built-in roles never change. Clone this one to make a version you can edit.</div>
      ) : null}

      <div className="idn-ed">
        <aside className="idn-card idn-ed-l" aria-label="About this role">
          <div className="idn-field">
            <label htmlFor="role-name">Name</label>
            <input id="role-name" className="idn-input" value={name} readOnly={readOnly} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="idn-field">
            <label htmlFor="role-desc">Description</label>
            <textarea id="role-desc" className="idn-input" value={description} readOnly={readOnly} onChange={(e) => setDescription(e.target.value)} data-copy="data" />
          </div>
          <div className="idn-field">
            <span className="idn-label">Can be bound at</span>
            <div className="idn-segs idn-segs-2">
              {(["org", "project"] as const).map((sc) => {
                const on = scopes.includes(sc);
                const last = on && scopes.length === 1;
                const locked = readOnly || last;
                return (
                  <button key={sc} type="button" aria-pressed={on} aria-disabled={locked || undefined} data-testid={`scope-${sc}`}
                    title={readOnly ? readOnlyWhy : last ? "A role binds somewhere" : sc === "org" ? "Every project, now and later" : "One project at a time"}
                    onClick={() => { if (!locked) setScopes((x) => (on ? x.filter((v) => v !== sc) : [...x, sc])); }}>
                    {sc === "org" ? "Org" : "Project"}
                  </button>
                );
              })}
            </div>
          </div>
          {role.cloned_from ? (
            <div className="idn-field">
              <span className="idn-label">Started from</span>
              <span className="idn-chip"><CopySimple size={12} aria-hidden /> {role.cloned_from.name}{role.cloned_from.at ? ` · ${shortDate(role.cloned_from.at)}` : ""}</span>
            </div>
          ) : null}
          <div className="idn-hr" />
          <div className="idn-field" ref={versionsRef} tabIndex={-1} aria-label="Versions">
            <span className="idn-label">Versions</span>
            <div className="idn-ver">
              {versions.length ? versions.map((v) => (
                <button key={v.version} type="button" onClick={() => loadVersion(v)} disabled={readOnly}
                  title={readOnly ? readOnlyWhy : `Load v${v.version} as a new draft`}>
                  <b>v{v.version}</b>
                  <span>{nameMap.get(v.by)?.name ?? v.by} · {shortDate(v.at)} · <span data-copy="data" title={v.summary}>{plainText(v.summary)}</span></span>
                </button>
              )) : <span className="idn-dim">v1 · built in</span>}
            </div>
          </div>
          <div className="idn-hr" />
          <p className="idn-tipline" data-copy="prose">You can only add permissions you hold yourself. <LockSimple size={11} aria-hidden /> marks ones you don't.</p>
        </aside>

        <section className="idn-mx" aria-label="Permissions">
          <div className="idn-mx-top">
            <label className="idn-search">
              <MagnifyingGlass size={14} aria-hidden />
              <span className="idn-sr">Filter permissions</span>
              <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter permissions…" data-testid="matrix-filter" />
            </label>
            <div className="idn-segs" role="group" aria-label="Show">
              <button type="button" aria-pressed={!onlyChanges} onClick={() => setOnlyChanges(false)}>Show all</button>
              <button type="button" aria-pressed={onlyChanges} onClick={() => setOnlyChanges(true)}>Only changes</button>
            </div>
            <span className="idn-spacer" />
            <span className="idn-mono idn-dim" data-testid="on-count">{onCount} of {ROLE_PERMS.length} on</span>
          </div>
          <div className="idn-mx-legend" aria-hidden>
            <span><i className="idn-lg on" />on</span>
            <span><i className="idn-lg crit" />critical</span>
            <span><Key size={11} className="idn-warn" /> step-up at use</span>
            <span><LockSimple size={11} /> dashed: you don't hold it</span>
            <span><i className="idn-lg add" />added</span>
            <span><i className="idn-lg rm" />removed</span>
          </div>
          {readOnly && !role.builtin ? <div className="idn-mx-ro" role="note"><LockSimple size={13} aria-hidden /> {readOnlyWhy}</div> : null}
          {MATRIX.map((cat) => {
            const isOpen = open.has(cat.name) || !!q || onlyChanges;
            const rows = cat.resources.map((r) => ({ r, perms: r.perms.filter(visible) })).filter((x) => x.perms.length);
            if (!isOpen || !rows.length) return null;
            const on = cat.perms.filter((p) => draft.has(p)).length;
            return (
              <div key={cat.name} className="idn-mx-group" data-category={cat.name}>
                <button type="button" className="idn-mx-cat" aria-expanded onClick={() => setOpen((o) => { const n = new Set(o); n.delete(cat.name); return n; })}>
                  <CaretDown size={11} aria-hidden /> {cat.name} <span className="idn-n">{on} / {cat.perms.length}</span>
                </button>
                {rows.map(({ r, perms }) => (
                  <div key={r.key} className="idn-mx-row" data-resource={r.key}>
                    <div className="idn-res">{r.title}<small>{r.key}</small></div>
                    <div className="idn-acts2">
                      {perms.map((p) => <Toggle key={p} p={p} cell={cell(p)} readOnly={readOnly} why={readOnlyWhy} onFlip={flip} />)}
                    </div>
                    <span className="idn-cnt">{r.perms.filter((p) => draft.has(p)).length}/{r.perms.length}</span>
                    {perms.filter((p) => ceiling.has(p)).map((p) => (
                      <span key={p} className="idn-cell-err" role="alert">You don't hold “{PERMISSION_META[p].title}”</span>
                    ))}
                  </div>
                ))}
              </div>
            );
          })}
          {closed.length ? (
            <div className="idn-mx-closed">
              {closed.map((c) => (
                <button key={c.name} type="button" aria-expanded={false} onClick={() => setOpen((o) => new Set([...o, c.name]))}>
                  <CaretRight size={11} aria-hidden /> {c.name} <span className="idn-n">{c.perms.filter((p) => draft.has(p)).length} on</span>
                </button>
              ))}
            </div>
          ) : null}
        </section>

        <div className="idn-ed-r">
          <section className="idn-card" aria-labelledby="idn-changes-h">
            <div className="idn-card-h"><h2 id="idn-changes-h">Changes</h2><span className="idn-sub">v{role.version} → v{role.version + 1}</span></div>
            {permsDirty ? (
              <div className="idn-diff" data-testid="diff">
                {added.map((p) => (
                  <div key={p} className="idn-dl p" title={p}><b>+</b>{PERMISSION_META[p].title}{PERMISSION_META[p].critical ? <span className="idn-tag bad">critical</span> : null}</div>
                ))}
                {removed.map((p) => <div key={p} className="idn-dl m" title={p}><b>−</b>{PERMISSION_META[p].title}</div>)}
              </div>
            ) : scopesDirty ? <span className="idn-dim">Where it can be bound changes</span> : <span className="idn-dim">No changes yet</span>}
          </section>

          <section className="idn-imp" aria-labelledby="idn-impact-h" aria-live="polite" data-testid="impact">
            <span className="idn-eyebrow" id="idn-impact-h">Impact preview</span>
            {preview ? (
              <>
                <div className="idn-bigt">Affects <span>{preview.affected.people.length} {preview.affected.people.length === 1 ? "person" : "people"}</span></div>
                <div className="idn-dim idn-small" data-testid="impact-split">
                  {impactSplit(preview.affected.direct, preview.affected.via_groups)}
                  {preview.counts && (preview.counts.service_accounts || preview.counts.agents)
                    ? ` · ${preview.counts.service_accounts} service ${preview.counts.service_accounts === 1 ? "account" : "accounts"} · ${preview.counts.agents} ${preview.counts.agents === 1 ? "agent" : "agents"}`
                    : ""}
                </div>
                <div className="idn-row idn-imp-people">
                  <span className="idn-stack">{preview.affected.people.map((p) => <Face key={p.id} name={p.name} tone={p.hue} size={26} />)}</span>
                  <span className="idn-dim" data-copy="data">{preview.affected.people.map((p) => p.name).join(", ")}</span>
                </div>
                <div className="idn-gl">
                  <div><b><Delta gained={preview.pages.gained.length} lost={preview.pages.lost.length} /></b>
                    <span>page{preview.pages.gained.length === 1 ? "" : "s"}</span>
                    {preview.pages.gained[0] ? <span className="idn-gl-sub" data-copy="data">{preview.pages.gained[0]}</span> : null}</div>
                  <div><b><Delta gained={preview.routes.gained} lost={preview.routes.lost} /></b><span>API operations</span></div>
                  <div><b><Delta gained={preview.tools.gained} lost={preview.tools.lost} /></b><span>MCP tools</span></div>
                </div>
                <div className="idn-hr" />
                {preview.affected.agents.map((a) => (
                  <div key={a.id} className="idn-ip">
                    <AgentAvatar agent={a.id.replace(/^agt_/, "")} size={24} tip={a.name} />
                    <span>{a.name} · {a.grants} {a.grants === 1 ? "grant" : "grants"}</span>
                    <span className={`idn-r ${a.change ? "idn-bad" : "idn-ok"}`} data-copy="data">{a.change ? changeWords(a.change) : "unchanged"}</span>
                  </div>
                ))}
                {preview.warnings.map((w) => (
                  <div key={w.permission} className="idn-note" data-copy="prose">
                    <Warning size={15} aria-hidden />
                    <span><b title={w.permission}>{PERMISSION_META[w.permission].title}</b> is {w.text}. {PERMISSION_META[w.permission].delegable ? "" : "Agents gain nothing from it. "}You'll type the role name, then confirm it's you.</span>
                  </div>
                ))}
              </>
            ) : (
              <>
                <div className="idn-bigt">Held by <span>{role.holders}</span></div>
                <span className="idn-dim">{previewError ?? (permsDirty ? "Working out the impact…" : "Change a permission to preview")}</span>
              </>
            )}
            <ErrorLine error={error} />
            {!role.builtin ? (
              <div className="idn-save">
                <CanButton permission="access_role:manage" className="idn-btn primary" onClick={review} disabled={!dirty || saving} data-testid="save">
                  <Key size={14} aria-hidden /> {saving ? "Saving…" : `Save v${role.version + 1} for ${people} ${people === 1 ? "person" : "people"}`}
                </CanButton>
                <button type="button" className="idn-btn" onClick={discard} disabled={!dirty}><ArrowCounterClockwise size={14} aria-hidden /> Discard</button>
              </div>
            ) : (
              <div className="idn-save">
                <CanButton permission="access_role:manage" className="idn-btn primary" onClick={() => clone(role)} tip="A custom copy you can edit">
                  <CopySimple size={14} aria-hidden /> Clone to customise
                </CanButton>
              </div>
            )}
          </section>
        </div>
      </div>

      {deleting ? <DeleteRole role={role} roles={roles} onClose={() => setDeleting(false)} /> : null}
      {confirming ? (
        <Sheet title="Confirm a critical change" icon={<Warning size={18} className="idn-warn" aria-hidden />} onClose={() => { setConfirming(false); setTyped(""); }} testId="critical-confirm">
          <p className="idn-lede2" data-copy="prose">These are critical. Type the role name to add them for {people} {people === 1 ? "person" : "people"}.</p>
          <div className="idn-need">{criticalAdds.map((p) => <Perm key={p} p={p} sm />)}</div>
          <div className="idn-field">
            <label htmlFor="confirm-name">Type “{role.name}”</label>
            <input id="confirm-name" className="idn-input" value={typed} onChange={(e) => setTyped(e.target.value)} data-autofocus autoComplete="off" />
          </div>
          <div className="idn-mf">
            <span className="idn-spacer" />
            <button type="button" className="idn-btn ghost" onClick={() => { setConfirming(false); setTyped(""); }}>Cancel</button>
            <button type="button" className="idn-btn primary" disabled={typed !== role.name || saving} onClick={save} data-testid="confirm-save">
              <Key size={14} aria-hidden /> {saving ? "Saving…" : "Save with step-up"}
            </button>
          </div>
        </Sheet>
      ) : null}
      <span className="idn-sr" aria-live="polite">{dirty ? `${added.length} added, ${removed.length} removed` : ""}</span>
    </>
  );
}
