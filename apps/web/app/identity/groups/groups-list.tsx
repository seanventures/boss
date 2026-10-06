"use client";

/**
 * IDENTITY › GROUPS (IDENTITY.md §9.3, §6.8): each group, where it comes from (BOSS, or read-only
 * from the IdP by SCIM or sign-in claims), its members and the access roles they all inherit.
 */
import React from "react";
import { ArrowsClockwise, UsersThree } from "@phosphor-icons/react";

import { Ago, Face, RoleChip } from "../../../components/identity/kit";
import type { Group } from "../../../lib/identity/types";

export function sourceWord(g: Group): string {
  if (g.source === "manual") return "BOSS";
  return g.provider ?? (g.source === "scim" ? "SCIM" : "IdP");
}

export function GroupsList({ groups }: { groups: Group[] }) {
  return (
    <>
      <div className="idn-sec-h">
        <h1 className="idn-h1">Groups</h1>
        <span className="idn-sub">{groups.length} groups · {groups.filter((g) => g.source !== "manual").length} from your IdP</span>
      </div>
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
            </span>
            {g.synced_at ? <span className="idn-dim idn-small"><ArrowsClockwise size={12} aria-hidden /> synced <Ago iso={g.synced_at} /></span> : null}
          </a>
        ))}
      </div>
      {!groups.length ? <div className="idn-empty">No groups yet</div> : null}
    </>
  );
}
