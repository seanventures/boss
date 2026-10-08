"use client";

/**
 * IDENTITY › GROUPS (IDENTITY.md §9.3, §6.8): each group, where it comes from (BOSS, or read-only
 * from the IdP by SCIM or sign-in claims), its members and the access roles they all inherit.
 */
import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowsClockwise, Plus, UsersThree } from "@phosphor-icons/react";

import { CanButton, useCan } from "../../../components/identity/identity-provider";
import { Ago, Face, RoleChip } from "../../../components/identity/kit";
import { EmptyState, ErrorState, type ReadFailure } from "../../../components/identity/states";
import { ListHeader } from "../../../components/identity/list-header";
import type { Group } from "../../../lib/identity/types";
import { GroupFormSheet } from "./group-sheets";

export function sourceWord(g: Group): string {
  if (g.source === "manual") return "BOSS";
  return g.provider ?? (g.source === "scim" ? "SCIM" : "IdP");
}

/** "4 groups · 2 synced from Okta" — the synced part only when there are any. */
export function groupsSub(groups: Group[]): string {
  const synced = groups.filter((g) => g.source !== "manual");
  const head = `${groups.length} ${groups.length === 1 ? "group" : "groups"}`;
  if (!synced.length) return head;
  const providers = [...new Set(synced.map((g) => g.provider).filter(Boolean))];
  return `${head} · ${synced.length} synced from ${providers.length === 1 ? providers[0] : "your identity provider"}`;
}

export function GroupsList({ groups, failure = null }: { groups: Group[]; failure?: ReadFailure | null }) {
  const [creating, setCreating] = useState(false);
  const router = useRouter();
  // The whole card opens the group, where its access roles are bound; the label says so.
  const canAssign = useCan("role_binding:assign").allowed;
  const create = (
    <CanButton permission="group:manage" className="idn-btn primary" onClick={() => setCreating(true)} tip="People in a group share its access roles" data-testid="groups-create">
      <Plus size={15} weight="bold" aria-hidden /> New group
    </CanButton>
  );
  return (
    <>
      <ListHeader title="Groups" sub={!failure ? groupsSub(groups) : null} empty={!groups.length && !failure} create={create} />
      {failure ? <ErrorState what="groups" failure={failure} testId="groups-error" /> : null}
      <div className="idn-glist" data-testid="groups-list">
        {groups.map((g) => (
          <a key={g.id} className="idn-gcard" href={`/identity/groups/${g.id}`} data-testid={`group-${g.id}`}>
            <span className="idn-gcard-h">
              <span className="idn-ico"><UsersThree size={16} aria-hidden /></span>
              <span className="idn-grow"><b>{g.name}</b><small data-copy="data">{g.description}</small></span>
              <span className={`idn-tag${g.source === "manual" ? "" : " warn"}`} title={g.source === "manual" ? "Managed in BOSS" : "Read-only: managed in your IdP"}>{sourceWord(g)}</span>
            </span>
            <span className="idn-row">
              <span className="idn-stack">{g.members.slice(0, 5).map((m) => <Face key={m.id} name={m.name} tone={m.hue} size={26} />)}</span>
              <span className="idn-dim">{g.member_count ?? g.members.length} {(g.member_count ?? g.members.length) === 1 ? "member" : "members"}</span>
            </span>
            <span className="idn-row">
              {g.bindings.map((b) => <RoleChip key={b.id} roleKey={b.role_key} name={b.role_name} scope={b.scope} />)}
              {!g.bindings.length ? <span className="idn-dim">No access roles</span> : null}
              {canAssign ? <><span className="idn-spacer" /><span className="idn-btn sm" aria-hidden data-testid={`group-manage-access-${g.id}`}>Manage access</span></> : null}
            </span>
            {g.synced_at ? <span className="idn-dim idn-small"><ArrowsClockwise size={12} aria-hidden /> synced <Ago iso={g.synced_at} /></span> : null}
          </a>
        ))}
      </div>
      {!groups.length && !failure ? (
        <EmptyState icon={<UsersThree size={20} />} title="No groups yet" testId="groups-empty"
          action={create}>
          Give a set of people the same access roles at once, such as Release or On-call. Groups from your identity provider appear here too.
        </EmptyState>
      ) : null}
      {creating ? <GroupFormSheet onClose={() => setCreating(false)} onCreated={(g) => router.push(`/identity/groups/${g.id}`)} /> : null}
    </>
  );
}
