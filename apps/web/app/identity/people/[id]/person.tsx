"use client";

/**
 * A PERSON'S PROFILE (mockup b, IDENTITY.md §9.2): who they are and how they sign in; every
 * access role they hold, directly or through a group; the effective access matrix by category
 * with the WHY of every permission ("via Operator ← Builders group"); sessions, factors, the
 * delegations they granted, their keys and their audit trail. Admin actions are mirrored: a
 * viewer without the permission sees them disabled with the reason, never hidden.
 */
import React, { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowsClockwise, ArrowUpRight, CaretDown, Check, Clock, Desktop, DeviceMobile, EnvelopeSimple, Fingerprint, GithubLogo,
  Key, LinkSimple, LockSimple, MagnifyingGlass, Plus, Prohibit, ShieldCheck, ShieldWarning, User, UsersThree, X,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../../../agent-avatar";
import { showToast } from "../../../client-forms";
import { CanButton, useGuardedCall, useIdentity } from "../../../../components/identity/identity-provider";
import { Ago, clock, ErrorLine, Face, Perm, Pill, RoleChip, Sheet, shortDate, useNow, daysUntil } from "../../../../components/identity/kit";
import { SignIn } from "../../../../components/identity/parts";
import { IdentityError, idDelete, idPost } from "../../../../lib/identity/api";
import { PERMISSION_META, PERMISSIONS, type Permission } from "../../../../lib/identity/permissions.gen";
import type {
  AccessRole, ApiKey, AuditEvent, Binding, CascadePreview, Delegation, EffectiveAccess, EffectiveEntry, Factor, Person, SessionRow, WhyStep,
} from "../../../../lib/identity/types";
import { statusPill } from "../people-list";

import { PERSON_TABS, type PersonTab } from "./tabs";

type Props = {
  person: Person;
  tab: PersonTab;
  factors: Factor[] | null;
  sessions: SessionRow[] | null;
  effective: EffectiveAccess | null;
  projectEffective: EffectiveAccess[];
  roles: AccessRole[];
  delegations: Delegation[] | null;
  keys: ApiKey[] | null;
  events: AuditEvent[] | null;
};

function refusal(e: unknown): string {
  if (e instanceof IdentityError) return e.code === "step_up_cancelled" ? "Not confirmed" : e.message;
  return "That didn't work";
}

/** A write from this page: step-up when asked, a ≤4-word toast either way, then a fresh render. */
function useAct() {
  const guarded = useGuardedCall();
  const router = useRouter();
  return async <T,>(reason: string, call: () => Promise<T>, done: string): Promise<T | null> => {
    try {
      const out = await guarded(reason, call);
      showToast("success", done);
      router.refresh();
      return out;
    } catch (e) {
      showToast("error", refusal(e));
      return null;
    }
  };
}

const scopeName = (s: string) => (s === "org" ? "org" : s.replace(/^project:/, "project "));

// ---- the header ---------------------------------------------------------------------------------

function Header({ person, roles, onSheet }: { person: Person; roles: AccessRole[]; onSheet: (s: SheetKind) => void }) {
  const p = person;
  const disabled = p.status === "disabled";
  void roles;
  return (
    <section className="idn-ph" aria-labelledby="idn-person-h">
      <Face name={p.display_name} tone={p.hue} size={78} />
      <div className="idn-ph-main">
        <h1 id="idn-person-h">{p.display_name} {statusPill(p)}</h1>
        <div className="idn-meta">
          <span><EnvelopeSimple size={14} aria-hidden /> <span data-copy="data">{p.email}</span></span>
          {p.sign_in.sso ? <span><LinkSimple size={14} aria-hidden /> {p.sign_in.sso} SSO linked</span> : null}
          {p.last_seen_at ? <span><Clock size={14} aria-hidden /> <span>Signed in <Ago iso={p.last_seen_at} />{p.last_seen_from ? `, ${p.last_seen_from}` : null}</span></span> : <span><Clock size={14} aria-hidden /> Never signed in</span>}
          <span><User size={14} aria-hidden /> Joined {shortDate(p.joined_at)}{p.joined_by ? ` by ${p.joined_by}` : ""}</span>
        </div>
        <div className="idn-roles">
          {p.bindings.filter((b) => !b.via_group).map((b) => <RoleChip key={b.id} roleKey={b.role_key} name={b.role_name} scope={b.scope} />)}
          {p.groups.map((g) => (
            <a key={g.id} className="idn-chip" href={`/identity/groups/${g.id}`}>
              <UsersThree size={12} aria-hidden /> {g.name}{g.provider ? <span className="idn-tag">{g.provider}</span> : null}
            </a>
          ))}
          {p.seat ? <span className="idn-chip" title="The approval seat this person holds"><ShieldCheck size={12} aria-hidden /> Seat: {p.seat}</span> : null}
        </div>
      </div>
      <div className="idn-acts">
        <CanButton permission="role_binding:assign" className="idn-btn sm" onClick={() => onSheet("add")} tip="Bind an access role at the org or a project" data-testid="person-add-access">
          <Plus size={14} aria-hidden /> Add access
        </CanButton>
        <CanButton permission="user:revoke_sessions" className="idn-btn sm" onClick={() => onSheet("signout")} tip="End every session on every device" data-testid="person-sign-out">
          <ArrowsClockwise size={14} aria-hidden /> Sign out everywhere
        </CanButton>
        <CanButton permission="user:reset_mfa" className="idn-btn sm" onClick={() => onSheet("mfa")} tip="Remove their factors; they enrol again" data-testid="person-reset-mfa">
          <Key size={14} aria-hidden /> Reset MFA
        </CanButton>
        <CanButton permission="user:disable" className={`idn-btn sm${disabled ? "" : " danger"}`} onClick={() => onSheet(disabled ? "enable" : "disable")}
          tip={disabled ? "Let them sign in again" : "Stop every session, key and grant"} data-testid="person-disable">
          <Prohibit size={14} aria-hidden /> {disabled ? "Enable" : "Disable"}
        </CanButton>
      </div>
    </section>
  );
}

// ---- sheets -------------------------------------------------------------------------------------

type SheetKind = "add" | "signout" | "mfa" | "disable" | "enable";

function AddAccessSheet({ person, roles, onClose }: { person: Person; roles: AccessRole[]; onClose: () => void }) {
  const { org } = useIdentity();
  const act = useAct();
  const held = org?.permissions;
  const projects = [...new Set(person.bindings.filter((b) => b.scope.startsWith("project:")).map((b) => b.scope.slice(8)))];
  const [role, setRole] = useState<string>("");
  const [where, setWhere] = useState<"org" | "project">("org");
  const [project, setProject] = useState(projects[0] ?? "");
  const [busy, setBusy] = useState(false);
  const chosen = roles.find((r) => r.key === role);
  const canProject = !chosen || chosen.bindable_scopes.includes("project");
  const missing = (r: AccessRole) => (held ? r.permissions.filter((p) => !held.has(p)) : []);
  return (
    <Sheet title={`Add access for ${person.display_name.split(" ")[0]}`} icon={<Plus size={18} aria-hidden />} onClose={onClose} testId="add-access-sheet">
      <div className="idn-field">
        <span className="idn-label" id="aa-role">Access role</span>
        <div className="idn-pick" role="radiogroup" aria-labelledby="aa-role">
          {roles.filter((r) => r.key !== "owner" || held?.has("owner:assign")).map((r) => {
            const lack = missing(r);
            const locked = lack.length > 0;
            return (
              <button key={r.id} type="button" role="radio" aria-checked={role === r.key} aria-disabled={locked || undefined}
                className="idn-pick-row" data-selected={role === r.key || undefined}
                title={locked ? `You don't hold this: ${lack.length} missing` : r.description}
                onClick={() => { if (!locked) setRole(r.key); }}>
                <RoleChip roleKey={r.key} name={r.name} />
                <span className="idn-dim">{r.permissions.length} permissions</span>
                {locked ? <span className="idn-lockline"><LockSimple size={12} aria-hidden /> you don&apos;t hold this</span> : null}
              </button>
            );
          })}
        </div>
      </div>
      <div className="idn-field">
        <span className="idn-label" id="aa-where">Where</span>
        <div className="idn-segs" role="radiogroup" aria-labelledby="aa-where">
          <button type="button" role="radio" aria-checked={where === "org"} aria-pressed={where === "org"} onClick={() => setWhere("org")}>Whole org</button>
          <button type="button" role="radio" aria-checked={where === "project"} aria-pressed={where === "project"}
            aria-disabled={!canProject || undefined} title={canProject ? "One project only" : "This role binds at org only"}
            onClick={() => { if (canProject) setWhere("project"); }}>One project</button>
        </div>
        {where === "project" ? (
          <>
            <label className="idn-sr" htmlFor="aa-project">Project id</label>
            <input id="aa-project" className="idn-input" placeholder="acme-web" value={project} onChange={(e) => setProject(e.target.value)} />
          </>
        ) : null}
      </div>
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" data-testid="add-access-save"
          disabled={!role || busy || (where === "project" && !project.trim())}
          onClick={async () => {
            setBusy(true);
            const out = await act("Granting access changes what they can do.",
              // The API binds by role id; a project binding goes to that project's own route.
              () => idPost(where === "org" ? "/v1/identity/role-bindings" : `/v1/identity/projects/${encodeURIComponent(project.trim())}/role-bindings`,
                { subject_type: "principal", subject_id: person.id, role_id: chosen?.id ?? "" }), "Access added");
            setBusy(false);
            if (out !== null) onClose();
          }}>
          {busy ? "Adding…" : "Add access"}
        </button>
      </div>
    </Sheet>
  );
}

const CONSEQUENCES: Record<Exclude<SheetKind, "add">, { title: string; lines: string[]; button: string; path: string; done: string; reason: string }> = {
  signout: { title: "Sign out everywhere?", lines: ["Every session on every device ends now.", "Their keys and grants keep working."], button: "Sign out everywhere", path: "revoke-sessions", done: "Signed out", reason: "Ending someone's sessions is sensitive." },
  mfa: { title: "Reset MFA?", lines: ["Their passkeys, authenticator and recovery codes are removed.", "They enrol a new factor at next sign-in.", "Every admin is told; it is audited as critical."], button: "Reset MFA", path: "reset-mfa", done: "MFA reset", reason: "Resetting a second factor is critical." },
  disable: { title: "Disable this person?", lines: ["Sessions end and their keys are suspended.", "Grants they gave agents are suspended; runs for them halt.", "Service accounts they alone own are flagged orphaned."], button: "Disable", path: "disable", done: "Disabled", reason: "Disabling someone stops all their access." },
  enable: { title: "Enable this person?", lines: ["They can sign in again.", "Suspended keys and grants stay suspended until renewed."], button: "Enable", path: "enable", done: "Enabled", reason: "Enabling someone restores their access." },
};

function ConfirmSheet({ person, kind, onClose }: { person: Person; kind: Exclude<SheetKind, "add">; onClose: () => void }) {
  const c = CONSEQUENCES[kind];
  const act = useAct();
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<CascadePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title={c.title} icon={kind === "disable" ? <Prohibit size={18} className="idn-bad" aria-hidden /> : <Key size={18} aria-hidden />} onClose={onClose} testId={`confirm-${kind}`}>
      <div className="idn-who-s"><Face name={person.display_name} tone={person.hue} size={34} /><span><b>{person.display_name}</b><small>{person.email}</small></span></div>
      {done ? (
        <ul className="idn-cascade-list" aria-label="What happened">
          {done.items.map((i) => <li key={i.kind}><Check size={14} className="idn-ok" aria-hidden /> <span data-copy="data">{i.label} · {i.detail}</span> <b>{i.count}</b></li>)}
        </ul>
      ) : (
        <ul className="idn-cascade-list" aria-label="What this does">
          {c.lines.map((l) => <li key={l} data-copy="prose">{l}</li>)}
        </ul>
      )}
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        {done ? <button type="button" className="idn-btn primary" onClick={onClose}>Done</button> : (
          <>
            <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
            <button type="button" className={`idn-btn ${kind === "disable" ? "danger solid" : "primary"}`} disabled={busy} data-testid={`confirm-${kind}-go`}
              onClick={async () => {
                setBusy(true);
                setError(null);
                const out = await act(c.reason, () => idPost<{ ok: boolean; cascade?: CascadePreview }>(`/v1/identity/users/${encodeURIComponent(person.id)}/${c.path}`), c.done);
                setBusy(false);
                if (out === null) { setError("Not done"); return; }
                if (out.cascade?.items.length) setDone(out.cascade);
                else onClose();
              }}>
              {busy ? "Working…" : c.button}
            </button>
          </>
        )}
      </div>
    </Sheet>
  );
}

// ---- access -------------------------------------------------------------------------------------

function Bindings({ person }: { person: Person }) {
  const act = useAct();
  return (
    <section className="idn-card" aria-labelledby="idn-bind-h">
      <div className="idn-card-h">
        <h2 id="idn-bind-h">Access roles</h2>
        <span className="idn-sub">{person.bindings.length} bindings · union, never deny</span>
      </div>
      {person.bindings.map((b) => (
        <div key={b.id} className="idn-bind" data-testid={`binding-${b.id}`}>
          <span className="idn-row">
            <RoleChip roleKey={b.role_key} name={b.role_name} scope={b.scope} />
            <span className="idn-src" data-copy="data">
              {b.via_group
                ? `Via group ${b.via_group.name} · ${b.via_group.source === "manual" ? "managed in BOSS" : `synced from ${b.via_group.source.toUpperCase()}`}`
                : `Direct · granted by ${b.granted_by ?? "BOSS"}, ${shortDate(b.granted_at)}`}
            </span>
          </span>
          <span className="idn-src">{b.expires_at ? `expires ${shortDate(b.expires_at)}` : ""}</span>
          {b.via_group ? (
            b.via_group.source === "manual"
              ? <a className="idn-btn sm" href={`/identity/groups/${b.via_group.id}`}>Group</a>
              : <span className="idn-btn sm ghost" title="Change it in your identity provider">{b.via_group.source === "scim" ? "SCIM" : "IdP"}</span>
          ) : b.synthetic ? (
            <span className="idn-btn sm ghost" title="Bindings need role_binding:read to manage">Role</span>
          ) : (
            <CanButton permission="role_binding:assign" className="idn-btn sm" tip="Remove this binding" data-testid={`remove-${b.id}`}
              onClick={() => act("Removing access changes what they can do.", () => idDelete(b.scope.startsWith("project:")
                ? `/v1/identity/projects/${encodeURIComponent(b.scope.slice(8))}/role-bindings/${encodeURIComponent(b.id)}`
                : `/v1/identity/role-bindings/${encodeURIComponent(b.id)}`), "Access removed")}>
              Remove
            </CanButton>
          )}
        </div>
      ))}
      {!person.bindings.length ? <div className="idn-empty">No access roles yet</div> : null}
    </section>
  );
}

type Row = EffectiveEntry & { onlyIn: string[] };

/** Org entries, plus what each project binding adds there ("only in acme-web"). */
export function mergeEffective(org: EffectiveAccess, projects: EffectiveAccess[]): Row[] {
  return org.entries.map((e) => {
    const extra = projects.filter((p) => !e.held && p.entries.find((x) => x.permission === e.permission)?.held);
    const why = e.held ? e.why : extra.flatMap((p) => p.entries.find((x) => x.permission === e.permission)?.why ?? []);
    return { ...e, held: e.held || extra.length > 0, why, onlyIn: extra.map((p) => p.scope.replace(/^project:/, "")) };
  });
}

const CATEGORIES = [...new Set(PERMISSIONS.map((p) => PERMISSION_META[p].category))];

function Why({ steps }: { steps: WhyStep[] }) {
  // The same role bound twice (direct and via a group) is one chip.
  const seen = new Set<string>();
  const uniq = steps.filter((s) => { const k = `${s.role_key}|${s.scope}|${s.via_group}`; if (seen.has(k)) return false; seen.add(k); return true; });
  return (
    <>
      {uniq.map((s, i) => (
        <React.Fragment key={`${s.role_key}-${s.scope}-${s.via_group}`}>
          {i > 0 ? <span className="idn-dim">+</span> : null}
          <RoleChip roleKey={s.role_key} name={s.role} scope={s.scope} />
          {s.via_group ? <span className="idn-dim">← group {s.via_group}</span> : null}
        </React.Fragment>
      ))}
    </>
  );
}

function PermRow({ r }: { r: Row }) {
  const meta = PERMISSION_META[r.permission];
  return (
    <div className="idn-prow" data-held={r.held} data-testid={`eff-${r.permission}`}>
      <Perm p={r.permission} />
      <span className="idn-why">
        {r.held ? (
          <>
            <Check size={13} className="idn-ok" aria-label="held" /> via <Why steps={r.why} />
            {r.onlyIn.length ? <span className="idn-dim">· only in {r.onlyIn.join(", ")}</span> : null}
            {meta.stepUp ? <span className="idn-dim" title="Needs a fresh second factor when used"><Key size={12} className="idn-warn" aria-hidden /> step-up</span> : null}
          </>
        ) : (
          <><X size={13} aria-label="not held" /> not held{r.held_by ? ` · ${r.held_by}` : ""}</>
        )}
      </span>
    </div>
  );
}

function Category({ name, rows, open, filtering }: { name: string; rows: Row[]; open: boolean; filtering: boolean }) {
  const [all, setAll] = useState(false);
  const held = rows.filter((r) => r.held).length;
  const tenths = Math.round((held / Math.max(rows.length, 1)) * 10);
  const ordered = [...rows].sort((a, b) => Number(b.held) - Number(a.held));
  // Collapsed, a category leads with what most needs explaining: critical permissions, ones
  // held through more than one role, ones held only in a project, then step-up — and one it lacks.
  const interest = (r: Row) => (PERMISSION_META[r.permission].critical ? 4 : 0)
    + (new Set(r.why.map((w) => w.role_key)).size > 1 ? 3 : 0) + (r.onlyIn.length ? 1 : 0) + (PERMISSION_META[r.permission].stepUp ? 1 : 0);
  const top = ordered.filter((r) => r.held).map((r, i) => ({ r, i })).sort((a, b) => interest(b.r) - interest(a.r) || a.i - b.i)
    .slice(0, 5).sort((a, b) => a.i - b.i).map((x) => x.r);
  const shown = filtering || all ? ordered : [...top, ...ordered.filter((r) => !r.held).slice(0, 1)];
  if (!open) return null;
  return (
    <div className="idn-acc" data-testid={`cat-${name}`}>
      <div className="idn-acc-h">
        <span>{name}</span>
        <span className="idn-mini" aria-hidden>{Array.from({ length: 10 }, (_, i) => <i key={i} data-on={i < tenths} />)}</span>
        <span className="idn-n" aria-label={`${held} of ${rows.length} held`}>{held} / {rows.length}</span>
      </div>
      {shown.map((r) => <PermRow key={r.permission} r={r} />)}
      {!filtering && shown.length < rows.length ? (
        <button type="button" className="idn-acc-more" onClick={() => setAll((v) => !v)} aria-expanded={all}>
          {all ? "Show fewer" : `Show all ${rows.length}`}
        </button>
      ) : null}
    </div>
  );
}

function Effective({ eff, projects }: { eff: EffectiveAccess; projects: EffectiveAccess[] }) {
  const rows = useMemo(() => mergeEffective(eff, projects), [eff, projects]);
  const [q, setQ] = useState("");
  const [expandAll, setExpandAll] = useState(false);
  const filtering = q.trim().length > 0;
  const match = (r: Row) => r.permission.includes(q.trim().toLowerCase()) || PERMISSION_META[r.permission].title.toLowerCase().includes(q.trim().toLowerCase());
  const byCat = CATEGORIES.map((c) => ({ name: c, rows: rows.filter((r) => PERMISSION_META[r.permission].category === c && (!filtering || match(r))) }))
    .filter((c) => c.rows.length);
  const defaults = byCat.filter((c) => c.rows.some((r) => r.held)).slice(0, 4).map((c) => c.name);
  const hidden = byCat.filter((c) => !defaults.includes(c.name));
  const heldTotal = rows.filter((r) => r.held).length;
  const projectLine = projects.map((p) => `${p.held} in ${p.scope.replace(/^project:/, "")}`).join(" · ");
  return (
    <section className="idn-card" aria-labelledby="idn-eff-h">
      <div className="idn-card-h">
        <h2 id="idn-eff-h">Effective access, and why</h2>
        <span className="idn-sub" data-copy="data">{eff.held} of {eff.total} at org{projectLine ? ` · ${projectLine}` : ""}</span>
        <span className="idn-spacer" />
        <label className="idn-find">
          <MagnifyingGlass size={13} aria-hidden />
          <span className="idn-sr">Find a permission</span>
          <input type="search" placeholder="Find a permission…" value={q} onChange={(e) => setQ(e.target.value)} data-testid="eff-find" />
        </label>
      </div>
      <div className="idn-legend">
        <span><Check size={12} className="idn-ok" aria-hidden /> held, with its binding</span>
        <span><code className="idn-perm crit">critical</code> typed confirm to grant</span>
        <span><Key size={12} className="idn-warn" aria-hidden /> step-up at use</span>
      </div>
      {byCat.map((c) => <Category key={c.name} name={c.name} rows={c.rows} filtering={filtering} open={filtering || expandAll || defaults.includes(c.name)} />)}
      {!filtering && !expandAll && hidden.length ? (
        <button type="button" className="idn-acc idn-acc-rest" onClick={() => setExpandAll(true)} aria-expanded={false} data-testid="eff-more">
          <span>{hidden.length} more categories</span>
          <span className="idn-n">{hidden.reduce((n, c) => n + c.rows.filter((r) => r.held).length, 0)} / {hidden.reduce((n, c) => n + c.rows.length, 0)} <CaretDown size={12} aria-hidden /></span>
        </button>
      ) : null}
      {filtering && !byCat.length ? <div className="idn-empty">No permission matches</div> : null}
      <span className="idn-sr">{heldTotal} permissions held in total</span>
    </section>
  );
}

// ---- right column -------------------------------------------------------------------------------

const FACTOR_ICON: Record<Factor["kind"], React.ReactNode> = {
  passkey: <Fingerprint size={16} aria-hidden />, totp: <DeviceMobile size={16} aria-hidden />,
  recovery: <LockSimple size={16} aria-hidden />, sso: <LinkSimple size={16} aria-hidden />,
};

function Factors({ person, factors }: { person: Person; factors: Factor[] }) {
  const mfa = person.sign_in.passkeys > 0 || person.sign_in.totp || Boolean(person.sign_in.idp_mfa);
  return (
    <section className="idn-card" aria-labelledby="idn-fac-h">
      <div className="idn-card-h">
        <h2 id="idn-fac-h">Sign-in and MFA</h2>
        <span className="idn-spacer" />
        {mfa ? <Pill tone="ok"><ShieldCheck size={12} aria-hidden /> MFA on</Pill> : <Pill tone="warn"><ShieldWarning size={12} aria-hidden /> No MFA</Pill>}
      </div>
      {factors.map((f) => (
        <div key={f.id} className="idn-fac" data-testid={`factor-${f.id}`}>
          <span className="idn-d" data-kind={f.kind}>{FACTOR_ICON[f.kind]}</span>
          <span><b>{f.name}</b><small data-copy="data">{f.detail}{f.last_used_at ? <> · used <Ago iso={f.last_used_at} /></> : ""}</small></span>
          <span className="idn-dim">{f.note ?? ""}</span>
        </div>
      ))}
      {!factors.length ? <div className="idn-empty">No factors</div> : null}
    </section>
  );
}

function Sessions({ person, sessions, full = false }: { person: Person; sessions: SessionRow[]; full?: boolean }) {
  const act = useAct();
  return (
    <section className="idn-card" aria-labelledby="idn-ses-h">
      <div className="idn-card-h">
        <h2 id="idn-ses-h">Sessions</h2>
        <span className="idn-sub">{sessions.length} active</span>
        <span className="idn-spacer" />
        {sessions.length ? (
          <CanButton permission="user:revoke_sessions" className="idn-link" tip="End every session on every device"
            onClick={() => act("Ending someone's sessions is sensitive.", () => idPost(`/v1/identity/users/${encodeURIComponent(person.id)}/revoke-sessions`), "Signed out")}>
            Sign out all
          </CanButton>
        ) : null}
      </div>
      {sessions.map((s) => (
        <div key={s.id} className="idn-sess" data-testid={`session-${s.id}`}>
          <span className="idn-d">{s.device === "phone" ? <DeviceMobile size={16} aria-hidden /> : <Desktop size={16} aria-hidden />}</span>
          <span>
            <b>{s.client}</b> {s.current ? <span className="idn-pill info">this one</span> : null}
            <small data-copy="data">{[s.location, s.methods.join(" + "), full && s.ip ? s.ip : null].filter(Boolean).join(" · ")} · <Ago iso={s.started_at} /></small>
          </span>
          {s.current ? <span /> : (
            <CanButton permission="session:revoke" className="idn-btn sm" tip="End this session now" data-testid={`end-${s.id}`}
              onClick={() => act("Ending a session is sensitive.", () => idDelete(`/v1/identity/sessions/${encodeURIComponent(s.id)}`), "Session ended")}>
              End
            </CanButton>
          )}
        </div>
      ))}
      {!sessions.length ? <div className="idn-empty">Not signed in anywhere</div> : null}
    </section>
  );
}

function grantLine(d: Delegation, now: number): string {
  const left = daysUntil(d.expires_at, now);
  const parts = [d.kind === "task" ? "this build only" : d.expires_at ? `${left !== null && left > 0 ? `${left} days left` : "expired"}` : "until revoked"];
  if (d.subdelegation.allowed) parts.push(`sub-delegation → ${d.subdelegation.to.join(", ")}`);
  if (d.last_used_run) parts.push(d.last_used_run);
  return parts.join(" · ");
}

function Grants({ person, delegations, full = false }: { person: Person; delegations: Delegation[]; full?: boolean }) {
  const now = useNow();
  const [revoking, setRevoking] = useState<Delegation | null>(null);
  return (
    <section className="idn-card" aria-labelledby="idn-dg-h">
      <div className="idn-card-h">
        <h2 id="idn-dg-h">Agents acting for {person.display_name.split(" ")[0]}</h2>
        <span className="idn-spacer" />
        <a className="idn-link" href={`/identity/audit?actor=${encodeURIComponent(person.id)}`}>Lineage <ArrowUpRight size={13} aria-hidden /></a>
      </div>
      {delegations.map((d) => (
        <div key={d.id} className="idn-dg" data-testid={`grant-${d.id}`}>
          <div className="idn-top">
            <span className="idn-stack">{d.agents.slice(0, 3).map((a) => <AgentAvatar key={a.id} agent={a.id.replace(/^agt_/, "")} size={30} tip={a.name} />)}</span>
            <a href={`/identity/agents/delegations/${d.id}`} className="idn-grow">
              <b>{d.label}</b>
              <small data-copy="data">{grantLine(d, now)}</small>
            </a>
            <Pill tone={d.status === "active" ? "ok" : d.status === "paused" ? "warn" : "mute"}>{d.status}</Pill>
          </div>
          <div className="idn-perms">
            {d.permissions.slice(0, full ? 99 : 4).map((p) => <Perm key={p} p={p} sm />)}
            {!full && d.permissions.length > 4 ? <span className="idn-chip">+{d.permissions.length - 4}</span> : null}
            {d.github ? <span className="idn-chip"><GithubLogo size={12} aria-hidden /> {d.github.repos[0]} · {[d.github.contents ? "contents" : null, d.github.pull_requests ? "PRs" : null].filter(Boolean).join(", ")}</span> : null}
          </div>
          {full && d.status === "active" ? (
            <div className="idn-row idn-dg-acts">
              <span className="idn-spacer" />
              <CanButton permission="delegation:revoke_any" className="idn-btn sm danger" tip="See what stops, then revoke" onClick={() => setRevoking(d)} data-testid={`revoke-${d.id}`}>
                Revoke
              </CanButton>
            </div>
          ) : null}
        </div>
      ))}
      {!delegations.length ? <div className="idn-empty">No agent acts for them</div> : null}
      {revoking ? <RevokeGrant grant={revoking} onClose={() => setRevoking(null)} /> : null}
    </section>
  );
}

function RevokeGrant({ grant, onClose }: { grant: Delegation; onClose: () => void }) {
  const act = useAct();
  const [preview, setPreview] = useState<CascadePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const path = `/v1/identity/delegations/${encodeURIComponent(grant.id)}/revoke`;
  React.useEffect(() => {
    let live = true;
    idPost<CascadePreview>(path, { dry_run: true }).then((p) => { if (live) setPreview(p); }).catch(() => { if (live) setPreview({ halts: 0, items: [] }); });
    return () => { live = false; };
  }, [path]);
  return (
    <Sheet title="Revoke this grant?" icon={<Prohibit size={18} className="idn-bad" aria-hidden />} onClose={onClose} testId="revoke-grant">
      <p className="idn-lede2" data-copy="prose">Authority leaves every agent in the chain within 5 seconds. Work stops where it is.</p>
      {preview === null ? <div className="idn-dim">Checking what stops…</div> : (
        <ul className="idn-cascade-list">
          {preview.items.map((i, n) => <li key={`${i.kind}-${n}`}><span data-copy="data">{i.label} · {i.detail}</span> <b>{i.count}</b></li>)}
        </ul>
      )}
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn danger solid" disabled={busy || preview === null}
          onClick={async () => { setBusy(true); await act("Revoking a grant halts its runs.", () => idPost(path, {}), "Grant revoked"); setBusy(false); onClose(); }}>
          {preview && preview.halts ? `Revoke and halt ${preview.halts} runs` : "Revoke"}
        </button>
      </div>
    </Sheet>
  );
}

function Activity({ events, full = false }: { events: AuditEvent[]; full?: boolean }) {
  return (
    <section className="idn-card" aria-labelledby="idn-act-h">
      <div className="idn-card-h">
        <h2 id="idn-act-h">{full ? "Audit" : "Recent activity"}</h2>
        <span className="idn-spacer" />
        <a className="idn-link" href="/identity/audit">All <ArrowUpRight size={13} aria-hidden /></a>
      </div>
      {events.slice(0, full ? 100 : 5).map((e) => (
        <a key={e.id} className="idn-mini-ev" href={`/identity/audit?event=${e.id}`}>
          <span className="idn-dim idn-mono">{clock(e.at).slice(0, 5)}</span>
          <span data-copy="data">{e.headline}{full ? <> · <span className="idn-mono">{e.event_type}</span></> : null}</span>
          <span className={e.outcome === "ok" ? "idn-ok" : e.outcome === "denied" || e.outcome === "failed" ? "idn-bad" : "idn-warn"}>{e.outcome}</span>
        </a>
      ))}
      {!events.length ? <div className="idn-empty">Nothing yet</div> : null}
    </section>
  );
}

function Keys({ keys }: { keys: ApiKey[] }) {
  const act = useAct();
  return (
    <section className="idn-card" aria-labelledby="idn-keys-h">
      <div className="idn-card-h">
        <h2 id="idn-keys-h">API keys</h2>
        <span className="idn-sub" title="BOSS never shows a key again, only its id">Shown once, then only its id</span>
      </div>
      <div className="idn-scroll">
        <table className="idn-t">
          <thead><tr><th>Key</th><th>Scopes</th><th>Last used</th><th>Expires</th><th /></tr></thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id} data-testid={`key-${k.id}`}>
                <td><span className="idn-kid">{k.id}</span><span data-copy="data">{k.name}</span></td>
                <td><span className="idn-scopes">{k.scopes.map((p) => <Perm key={p} p={p} sm />)}</span></td>
                <td className="idn-dim"><Ago iso={k.last_used_at} />{k.last_used_client ? <small className="idn-block" data-copy="data">{k.last_used_client}</small> : null}</td>
                <td>{k.status === "revoked" ? <Pill tone="mute">revoked</Pill> : k.expires_at ? <Ago iso={k.expires_at} /> : <Pill tone="warn">never</Pill>}</td>
                <td>
                  {k.status === "revoked" ? null : (
                    <CanButton permission="api_key:revoke" className="idn-btn sm" tip="Stops working at once" data-testid={`revoke-key-${k.id}`}
                      onClick={() => act("Revoking a key stops its clients.", () => idPost(`/v1/identity/api-keys/${encodeURIComponent(k.id)}/revoke`), "Key revoked")}>
                      Revoke
                    </CanButton>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!keys.length ? <div className="idn-empty">No personal keys</div> : null}
    </section>
  );
}

function OverviewTab({ person }: { person: Person }) {
  const p = person;
  return (
    <div className="idn-cols">
      <section className="idn-card" aria-labelledby="idn-ov-h">
        <div className="idn-card-h"><h2 id="idn-ov-h">Identity</h2></div>
        <dl className="idn-kv">
          <dt>Email</dt><dd data-copy="data">{p.email}</dd>
          <dt>Status</dt><dd>{statusPill(p)}</dd>
          <dt>Source</dt><dd>{p.source === "local" ? "Local account" : p.source === "sso" ? "SSO" : "SCIM"}{p.self_signed_up ? " · self sign-up" : ""}</dd>
          <dt>Sign-in</dt><dd><SignIn s={p.sign_in} />{p.sign_in.password ? <span className="idn-chip">password</span> : null}</dd>
          <dt>SSO links</dt><dd>{p.sign_in.sso ?? "none"}</dd>
          <dt>Last sign-in</dt><dd><Ago iso={p.last_seen_at} />{p.last_seen_from ? ` · ${p.last_seen_from}` : ""}</dd>
          <dt>Joined</dt><dd>{shortDate(p.joined_at)}{p.joined_by ? ` by ${p.joined_by}` : ""}</dd>
          <dt>Seat</dt><dd>{p.seat ?? "none"}</dd>
          <dt>Id</dt><dd><code className="idn-mono">{p.id}</code></dd>
        </dl>
      </section>
      <section className="idn-card" aria-labelledby="idn-ovg-h">
        <div className="idn-card-h"><h2 id="idn-ovg-h">Groups</h2><span className="idn-sub">{p.groups.length}</span></div>
        <div className="idn-groups">
          {p.groups.map((g) => (
            <a key={g.id} className="idn-chip" href={`/identity/groups/${g.id}`}><UsersThree size={13} aria-hidden /> {g.name}{g.provider ? <span className="idn-tag">{g.provider}</span> : null}</a>
          ))}
          {!p.groups.length ? <span className="idn-dim">In no group</span> : null}
        </div>
      </section>
    </div>
  );
}

function SecurityTab({ person, factors, onSheet }: { person: Person; factors: Factor[] | null; onSheet: (s: SheetKind) => void }) {
  return (
    <div className="idn-cols">
      {factors ? <Factors person={person} factors={factors} /> : <div className="idn-card idn-dim">Factors hidden</div>}
      <section className="idn-card" aria-labelledby="idn-adm-h">
        <div className="idn-card-h"><h2 id="idn-adm-h">Admin actions</h2></div>
        <div className="idn-adm">
          <div><b>Reset MFA</b><small data-copy="prose">Removes every factor; they enrol again at next sign-in.</small></div>
          <CanButton permission="user:reset_mfa" className="idn-btn sm" onClick={() => onSheet("mfa")}>Reset MFA</CanButton>
          <div><b>Sign out everywhere</b><small data-copy="prose">Ends every session on every device now.</small></div>
          <CanButton permission="user:revoke_sessions" className="idn-btn sm" onClick={() => onSheet("signout")}>Sign out</CanButton>
          <div><b>{person.status === "disabled" ? "Enable" : "Disable"}</b><small data-copy="prose">{person.status === "disabled" ? "Lets them sign in again." : "Stops their sessions, keys and the grants they gave."}</small></div>
          <CanButton permission="user:disable" className={`idn-btn sm${person.status === "disabled" ? "" : " danger"}`} onClick={() => onSheet(person.status === "disabled" ? "enable" : "disable")}>
            {person.status === "disabled" ? "Enable" : "Disable"}
          </CanButton>
        </div>
      </section>
    </div>
  );
}

function Hidden({ what, permission }: { what: string; permission: Permission }) {
  return (
    <section className="idn-card">
      <div className="idn-card-h"><h2>{what}</h2></div>
      <div className="idn-row idn-dim"><LockSimple size={15} aria-hidden /> Needs <code className="idn-perm">{permission}</code></div>
    </section>
  );
}

// ---- the page -----------------------------------------------------------------------------------

export function PersonProfile({ person, tab, factors, sessions, effective, projectEffective, roles, delegations, keys, events }: Props) {
  const [sheet, setSheet] = useState<SheetKind | null>(null);
  const counts: Partial<Record<PersonTab, number>> = {
    sessions: sessions?.length, delegations: delegations?.length, keys: keys?.filter((k) => k.status !== "revoked").length,
  };
  return (
    <>
      <nav className="idn-crumb" aria-label="Breadcrumb"><a href="/identity/people">People</a> <span aria-hidden>›</span> <span>{person.display_name}</span></nav>
      <Header person={person} roles={roles} onSheet={setSheet} />
      <nav className="idn-subtabs" aria-label="Profile sections">
        {PERSON_TABS.map((t) => (
          <a key={t.id} href={`?tab=${t.id}`} aria-current={tab === t.id ? "page" : undefined} data-tab={t.id}>
            {t.label}{counts[t.id] ? ` ${counts[t.id]}` : ""}
          </a>
        ))}
      </nav>

      {tab === "access" ? (
        <div className="idn-cols">
          <div className="idn-colr">
            <Bindings person={person} />
            {effective ? <Effective eff={effective} projects={projectEffective} /> : <Hidden what="Effective access" permission="user:read" />}
          </div>
          <div className="idn-colr">
            {factors ? <Factors person={person} factors={factors} /> : null}
            {sessions ? <Sessions person={person} sessions={sessions} /> : <Hidden what="Sessions" permission="session:read" />}
            {delegations ? <Grants person={person} delegations={delegations} /> : <Hidden what="Agents acting for them" permission="delegation:read_all" />}
            {events ? <Activity events={events} /> : null}
          </div>
        </div>
      ) : null}
      {tab === "overview" ? <OverviewTab person={person} /> : null}
      {tab === "sessions" ? (sessions ? <Sessions person={person} sessions={sessions} full /> : <Hidden what="Sessions" permission="session:read" />) : null}
      {tab === "security" ? <SecurityTab person={person} factors={factors} onSheet={setSheet} /> : null}
      {tab === "delegations" ? (delegations ? <Grants person={person} delegations={delegations} full /> : <Hidden what="Delegations" permission="delegation:read_all" />) : null}
      {tab === "keys" ? (keys ? <Keys keys={keys} /> : <Hidden what="API keys" permission="api_key:read" />) : null}
      {tab === "audit" ? (events ? <Activity events={events} full /> : <Hidden what="Audit" permission="audit:read" />) : null}

      {sheet === "add" ? <AddAccessSheet person={person} roles={roles} onClose={() => setSheet(null)} /> : null}
      {sheet && sheet !== "add" ? <ConfirmSheet person={person} kind={sheet} onClose={() => setSheet(null)} /> : null}
    </>
  );
}
