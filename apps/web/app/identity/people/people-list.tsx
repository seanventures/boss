"use client";

/**
 * IDENTITY › PEOPLE (IDENTITY.md §9.2): search; filters for access role, group, status, MFA and
 * source (local, SSO, SCIM); invite. Each row opens the person's profile. Invited people who have
 * not accepted yet carry a pill, so the list never pretends they can sign in.
 */
import React, { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { MagnifyingGlass, Plus, UsersThree } from "@phosphor-icons/react";

import { CanButton } from "../../../components/identity/identity-provider";
import { InviteSheet } from "../../../components/identity/invite-sheet";
import { Ago, Face, Pill } from "../../../components/identity/kit";
import { BindingChips, SignIn } from "../../../components/identity/parts";
import type { AccessRole, Person } from "../../../lib/identity/types";

type Filter = { q: string; role: string; group: string; status: string; mfa: string; source: string };
const NONE: Filter = { q: "", role: "", group: "", status: "", mfa: "", source: "" };

export function hasMfa(p: Person): boolean {
  return p.sign_in.passkeys > 0 || p.sign_in.totp || Boolean(p.sign_in.idp_mfa);
}

export function statusPill(p: Person) {
  if (p.status === "pending") return <Pill tone="info" tip="Invited, not accepted yet">invited</Pill>;
  if (p.status === "disabled") return <Pill tone="bad">disabled</Pill>;
  if (p.status === "active") return <Pill tone="ok">active</Pill>;
  return <Pill tone="mute">{p.status}</Pill>;
}

export function filterPeople(people: Person[], f: Filter): Person[] {
  const q = f.q.trim().toLowerCase();
  return people.filter((p) =>
    (!q || `${p.display_name} ${p.email}`.toLowerCase().includes(q))
    && (!f.role || p.bindings.some((b) => b.role_key === f.role))
    && (!f.group || p.groups.some((g) => g.id === f.group))
    && (!f.status || p.status === f.status)
    && (!f.mfa || (f.mfa === "on") === hasMfa(p))
    && (!f.source || p.source === f.source));
}

function Select({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[];
}) {
  return (
    <label className="idn-fsel">
      <span className="idn-sr">{label}</span>
      <select className="idn-fchip" value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}
        aria-pressed={value ? "true" : undefined}>
        <option value="">{label}: all</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

export function PeopleList({ people, roles, invited: invitedCount = null }: { people: Person[]; roles: AccessRole[]; invited?: number | null }) {
  const [f, setF] = useState<Filter>(NONE);
  const [inviting, setInviting] = useState(false);
  const router = useRouter();
  const set = (k: keyof Filter) => (v: string) => setF((x) => ({ ...x, [k]: v }));
  const groups = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of people) for (const g of p.groups) m.set(g.id, g.name);
    return [...m].map(([value, label]) => ({ value, label }));
  }, [people]);
  const shown = filterPeople(people, f);
  const active = people.filter((p) => p.status === "active").length;
  // Open invitations are not people yet: the API lists them at /v1/identity/invitations.
  const invited = invitedCount ?? people.filter((p) => p.status === "pending").length;
  const filtered = JSON.stringify(f) !== JSON.stringify(NONE);

  return (
    <>
      <div className="idn-sec-h">
        <h1 className="idn-h1">People</h1>
        <span className="idn-sub">{active} active · {invited} invited</span>
        <span className="idn-spacer" />
        <CanButton permission="user:invite" className="idn-btn primary" onClick={() => setInviting(true)} tip="Invite by email, with an access role" data-testid="people-invite">
          <Plus size={15} weight="bold" aria-hidden /> Invite people
        </CanButton>
      </div>

      <div className="idn-fb" role="search">
        <label className="idn-q">
          <MagnifyingGlass size={15} aria-hidden />
          <span className="idn-sr">Search people</span>
          <input type="search" placeholder="Search name or email" value={f.q} onChange={(e) => set("q")(e.target.value)} data-testid="people-search" />
        </label>
        <Select label="Role" value={f.role} onChange={set("role")} options={roles.map((r) => ({ value: r.key, label: r.name }))} />
        <Select label="Group" value={f.group} onChange={set("group")} options={groups} />
        <Select label="Status" value={f.status} onChange={set("status")} options={[{ value: "active", label: "Active" }, { value: "pending", label: "Invited" }, { value: "disabled", label: "Disabled" }]} />
        <Select label="MFA" value={f.mfa} onChange={set("mfa")} options={[{ value: "on", label: "On" }, { value: "off", label: "Off" }]} />
        <Select label="Source" value={f.source} onChange={set("source")} options={[{ value: "local", label: "Local" }, { value: "sso", label: "SSO" }, { value: "scim", label: "SCIM" }]} />
        {filtered ? <button type="button" className="idn-link" onClick={() => setF(NONE)}>Clear</button> : null}
      </div>

      <section className="idn-card" aria-label="People">
        <div className="idn-scroll">
          <table className="idn-t" data-testid="people-table">
            <thead><tr><th>Person</th><th>Access</th><th>Groups</th><th>Sign-in</th><th>Last seen</th><th>Status</th></tr></thead>
            <tbody>
              {shown.map((p) => (
                <tr key={p.id} data-href={`/identity/people/${p.id}`} onClick={(e) => {
                  if ((e.target as HTMLElement).closest("a, button")) return;
                  window.location.assign(`/identity/people/${p.id}`);
                }}>
                  <td>
                    <a className="idn-who" href={`/identity/people/${p.id}`}>
                      <Face name={p.display_name} tone={p.hue} size={34} />
                      <span><b>{p.display_name}</b><small data-copy="data">{p.email}</small></span>
                    </a>
                  </td>
                  <td><BindingChips bindings={p.bindings} /></td>
                  <td>
                    <span className="idn-row">
                      {p.groups.slice(0, 2).map((g) => <span key={g.id} className="idn-chip"><UsersThree size={12} aria-hidden /> {g.name}</span>)}
                      {p.groups.length > 2 ? <span className="idn-dim">+{p.groups.length - 2}</span> : null}
                      {!p.groups.length ? <span className="idn-dim">none</span> : null}
                    </span>
                  </td>
                  <td><SignIn s={p.sign_in} /></td>
                  <td className="idn-dim"><Ago iso={p.last_seen_at} /></td>
                  <td>{statusPill(p)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!shown.length ? <div className="idn-empty">{filtered ? "No one matches" : "No people yet"}</div> : null}
      </section>
      {inviting ? <InviteSheet roles={roles} onClose={() => setInviting(false)} onInvited={() => router.refresh()} /> : null}
    </>
  );
}
