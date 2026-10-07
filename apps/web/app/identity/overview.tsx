"use client";

/**
 * IDENTITY › OVERVIEW (mockup a, IDENTITY.md §9.1): the posture score and its six checks, each
 * with the one place that fixes it; who holds which access role and who is acting for them now;
 * the directory (people, access roles, groups, service accounts, agents) and the live feed of
 * security events. A panel the viewer cannot read says which permission it needs.
 */
import { plainText } from "../../lib/identity/plain";
import React, { useState } from "react";
import {
  ArrowRight, ArrowUpRight, Fingerprint, GitBranch, Globe, Key, Plus, Robot, ShieldCheck, Users, UsersThree, Warning, LinkSimple,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { CanButton, useCan } from "../../components/identity/identity-provider";
import { InviteSheet } from "../../components/identity/invite-sheet";
import { Ago, Face, Perm, RoleChip, useNow, agoText } from "../../components/identity/kit";
import { BindingChips, Locked, SignIn } from "../../components/identity/parts";
import { EmptyState, ErrorState, type ReadFailure } from "../../components/identity/states";
import type { Permission } from "../../lib/identity/permissions.gen";
import { eventWords, keyCount, outcomeWords } from "../../lib/identity/words";
import type { AccessRole, AuditEvent, Group, Passport, Person, Posture, PostureCheck, ServiceAccount } from "../../lib/identity/types";

const CHECK_ICON: Record<PostureCheck["id"], React.ReactNode> = {
  mfa: <ShieldCheck size={13} aria-hidden />, keys: <Key size={13} aria-hidden />, delegations: <Robot size={13} aria-hidden />,
  legacy: <Warning size={13} aria-hidden />, owners: <UsersThree size={13} aria-hidden />, audit: <LinkSimple size={13} aria-hidden />,
  enforcement: <ShieldCheck size={13} aria-hidden />,
  origin: <Globe size={13} aria-hidden />,
  github: <GitBranch size={13} aria-hidden />,
};

/** The engine says "critical"; the posture cards have three tones. */
const tone = (status: PostureCheck["status"]) => (status === "critical" ? "bad" : status);

function Ring({ score }: { score: number }) {
  const c = 2 * Math.PI * 42;
  return (
    <div className="idn-ring" role="img" aria-label={`Posture score ${score} of 100`}>
      <svg width="96" height="96" viewBox="0 0 96 96" aria-hidden>
        <defs><linearGradient id="idn-ring-grad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" className="stop-a" /><stop offset="1" className="stop-b" /></linearGradient></defs>
        <circle className="track" cx="48" cy="48" r="42" fill="none" strokeWidth="8" />
        <circle className="val" cx="48" cy="48" r="42" fill="none" strokeWidth="8" strokeDasharray={`${(c * score) / 100} ${c}`} />
      </svg>
      <b aria-hidden>{score}</b>
    </div>
  );
}

function roleCounts(people: Person[], roles: AccessRole[]) {
  return roles.filter((r) => r.builtin).map((r) => {
    const holders = people.filter((p) => p.status === "active" && p.bindings.some((b) => b.role_key === r.key && !b.via_group));
    return { role: r, holders };
  });
}

/**
 * The lines between "people by access role" and "acting for them now": one per person acting,
 * from the role row they hold to their acting row. Real links only — no line, no claim.
 */
function Wires({ left, right, links }: { left: number; right: number; links: [number, number][] }) {
  const paths = links.map(([i, j]) => {
    const y1 = 8 + (i + 0.5) * (84 / Math.max(left, 1));
    const y2 = 8 + (j + 0.5) * (84 / Math.max(right, 1));
    return `M0 ${y1} C 20 ${y1}, 20 ${y2}, 40 ${y2}`;
  });
  return (
    <div className="idn-wire" aria-hidden>
      <svg viewBox="0 0 40 100" preserveAspectRatio="none">{paths.map((d, i) => <path key={i} d={d} vectorEffect="non-scaling-stroke" />)}</svg>
    </div>
  );
}

/** The outcome of an event, as a word (the API's ok/denied/failed/notice). */
/** A panel the viewer may not open names the permission (one line, inside its card). */
function Needs({ icon, what, p }: { icon: React.ReactNode; what: string; p: Permission }) {
  return <div className="idn-row idn-dim">{icon} {what} need <Perm p={p} sm /></div>;
}

/** Reads that FAILED (page.tsx): each panel then says so instead of looking locked or empty. */
export type OverviewFailures = Partial<Record<"posture" | "people" | "roles" | "groups" | "serviceAccounts" | "agents" | "events", ReadFailure | null>>;

export function Overview({ posture, people, roles, groups, serviceAccounts, agents, events, failures = {} }: {
  posture: Posture | null; people: Person[] | null; roles: AccessRole[] | null; groups: Group[] | null;
  serviceAccounts: ServiceAccount[] | null; agents: Passport[] | null; events: AuditEvent[] | null;
  failures?: OverviewFailures;
}) {
  const [inviting, setInviting] = useState(false);
  const now = useNow();
  const canGrant = useCan("delegation:grant");
  const counts = posture?.counts;
  const active = people?.filter((p) => p.status === "active") ?? [];
  const alone = people !== null && active.length <= 1;
  const rows = people && roles ? roleCounts(people, roles) : [];
  const acting = posture?.acting_now ?? [];
  const links = acting.flatMap((a, j): [number, number][] => {
    const i = rows.findIndex((r) => r.holders.some((p) => p.id === a.user.id));
    return i >= 0 ? [[i, j]] : [];
  });

  return (
    <>
      <section className="idn-hero" aria-labelledby="idn-hero-h">
        <div className="idn-hero-l">
          <div className="idn-eyebrow">
            <span className="idn-dot" aria-hidden />
            <span>Identity{counts ? ` · ${counts.people} people · ${counts.agents} agents` : ""}</span>
          </div>
          <h1 id="idn-hero-h">Who can do what, <em>and for whom.</em></h1>
          <p className="idn-lede" data-copy="prose">
            Everyone signs in as themselves. Every agent acts for someone, with less than they have, and carries the chain that allowed it.
          </p>
          <div className="idn-acts">
            <CanButton permission="user:invite" className="idn-btn primary" onClick={() => setInviting(true)} data-testid="overview-invite" tip="Invite by email, with an access role">
              <Plus size={15} weight="bold" aria-hidden /> Invite people
            </CanButton>
            <a className="idn-btn" href={canGrant.allowed ? "/account/agents?new=1" : undefined} aria-disabled={!canGrant.allowed || undefined}
              title={canGrant.allowed ? "Choose agents, abilities and an expiry" : canGrant.reason} data-permission="delegation:grant" data-can={canGrant.allowed ? "yes" : "no"}>
              <Robot size={15} aria-hidden /> Let an agent act for me
            </a>
          </div>
          {posture ? (
            <div className="idn-score">
              <Ring score={posture.score} />
              <div className="idn-score-t">
                <b>{posture.headline}</b>
                {posture.summary.map((line) => <span key={line} data-copy="data">{plainText(line)}</span>)}
              </div>
            </div>
          ) : null}
        </div>
        <div className="idn-hero-r">
          {people && roles ? (
            <div className="idn-map">
              <div className="idn-rcol">
                <h4>PEOPLE BY ACCESS ROLE</h4>
                {rows.map(({ role, holders }) => (
                  <a key={role.id} className="idn-rrow" href={`/identity/roles/${role.id}`} title={role.description}>
                    <RoleChip roleKey={role.key} name={role.name} />
                    <span className="idn-stack">{holders.slice(0, 3).map((p) => <Face key={p.id} name={p.display_name} tone={p.hue} size={22} />)}</span>
                    <span className="idn-cnt">{holders.length}</span>
                  </a>
                ))}
              </div>
              <Wires left={rows.length} right={Math.max(1, acting.length)} links={links} />
              <div className="idn-acol">
                <h4>ACTING FOR THEM NOW</h4>
                {(posture?.acting_now ?? []).map((a) => (
                  <div key={`${a.user.id}-${a.project}-${a.label}`} className="idn-arow">
                    <Face name={a.user.name} tone={a.user.hue} size={30} />
                    <ArrowRight size={13} className="idn-arr" aria-hidden />
                    <span className="idn-stack">{a.agents.slice(0, 3).map((g) => <AgentAvatar key={g.id} agent={g.id.replace(/^agt_/, "")} size={26} tip={g.name} />)}</span>
                    <span className="idn-g"><b>{a.project}</b><small>{a.label}</small></span>
                  </div>
                ))}
                {posture && !posture.acting_now.length ? <span className="idn-dim">No agent is acting now</span> : null}
              </div>
            </div>
          ) : failures.people || failures.roles ? <ErrorState compact what="who holds what" failure={(failures.people ?? failures.roles)!} />
            : <Locked permission="user:read" what="Who holds what" />}
        </div>
      </section>

      {failures.posture ? <ErrorState compact what="the security posture" failure={failures.posture} /> : null}
      {posture ? (
        <section aria-labelledby="idn-posture-h">
          <div className="idn-sec-h">
            <h2 id="idn-posture-h">Security posture</h2>
            <span className="idn-sub">Each check links to its fix</span>
            <span className="idn-spacer" />
            <a className="idn-link" href="/identity/sign-in#policy">Policy <ArrowUpRight size={13} aria-hidden /></a>
          </div>
          <div className="idn-posture">
            {posture.checks.map((c) => (
              <div key={c.id} className="idn-pc" data-status={tone(c.status)} data-testid={`posture-${c.id}`}>
                <span className="idn-label">{CHECK_ICON[c.id] ?? <Warning size={13} aria-hidden />} {c.label}</span>
                <span className="idn-num">{c.value}{c.unit ? <small>{c.unit}</small> : null}</span>
                <span className="idn-txt" data-copy="data">{plainText(c.detail)}</span>
                {c.fix
                  ? <a className="idn-fix" href={c.fix.href}>{c.fix.label} →</a>
                  : <span className="idn-fix done">All good</span>}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <div className="idn-grid3">
        {people && alone ? (
          <section className="idn-card" aria-labelledby="idn-people-h">
            <div className="idn-card-h">
              <h3 id="idn-people-h">People</h3>
              <span className="idn-spacer" />
              <a className="idn-link" href="/identity/people">All people <ArrowUpRight size={13} aria-hidden /></a>
            </div>
            <EmptyState icon={<Users size={20} />} title="You're the only person here" testId="alone"
              action={<CanButton permission="user:invite" className="idn-btn primary sm" onClick={() => setInviting(true)} tip="Invite by email, with an access role">Invite your team</CanButton>}>
              Invite the people you work with; each signs in as themselves.
            </EmptyState>
          </section>
        ) : people ? (
          <section className="idn-card" aria-labelledby="idn-people-h">
            <div className="idn-card-h">
              <h3 id="idn-people-h">People</h3>
              {counts ? <span className="idn-sub">{counts.active} active · {counts.invited} invited · {counts.disabled} disabled</span> : null}
              <span className="idn-spacer" />
              <a className="idn-link" href="/identity/people">All people <ArrowUpRight size={13} aria-hidden /></a>
            </div>
            <div className="idn-scroll">
              <table className="idn-t">
                <thead><tr><th>Person</th><th>Access</th><th>Sign-in</th></tr></thead>
                <tbody>
                  {people.filter((p) => p.status !== "pending").slice(0, 8).map((p) => (
                    <tr key={p.id} data-href={`/identity/people/${p.id}`} onClick={() => window.location.assign(`/identity/people/${p.id}`)}>
                      <td>
                        <a className="idn-who" href={`/identity/people/${p.id}`}>
                          <Face name={p.display_name} tone={p.hue} size={34} />
                          <span><b>{p.display_name}</b><small>{p.email} · {agoText(p.last_seen_at, now, "never").replace(" ago", "")}</small></span>
                        </a>
                      </td>
                      <td><BindingChips bindings={p.bindings} /></td>
                      <td><SignIn s={p.sign_in} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ) : failures.people ? (
          <section className="idn-card" aria-label="People"><ErrorState compact what="people" failure={failures.people} /></section>
        ) : <Locked permission="user:read" what="People" />}

        <section className="idn-card" aria-labelledby="idn-roles-h">
          {roles ? (
            <>
              <div className="idn-card-h">
                <h3 id="idn-roles-h">Access roles</h3>
                <span className="idn-sub">{roles.filter((r) => r.builtin).length} built-in · {roles.filter((r) => !r.builtin).length} custom</span>
                <span className="idn-spacer" />
                <a className="idn-link" href="/identity/roles">Edit <ArrowUpRight size={13} aria-hidden /></a>
              </div>
              {roles.map((r) => {
                const max = Math.max(...roles.map((x) => x.holders), 1);
                return (
                  <a key={r.id} className="idn-rolebar" href={`/identity/roles/${r.id}`}>
                    <RoleChip roleKey={r.key} name={r.name} />
                    <span className="idn-bar" data-role-tone={r.builtin ? r.key : "custom"}><i data-w={Math.round((r.holders / max) * 10)} /></span>
                    <span className="idn-cnt">{r.holders}</span>
                  </a>
                );
              })}
            </>
          ) : failures.roles ? <ErrorState compact what="access roles" failure={failures.roles} />
            : <Needs icon={<ShieldCheck size={14} aria-hidden />} what="Access roles" p="access_role:read" />}
          <div className="idn-hr" />
          {groups ? (
            <>
              <div className="idn-card-h">
                <h3>Groups</h3>
                <span className="idn-sub">{(() => {
                  const synced = groups.filter((g) => g.source !== "manual").length;
                  return synced ? `${groups.length} · ${synced} from ${groups.find((g) => g.provider)?.provider ?? "your IdP"}` : `${groups.length}`;
                })()}</span>
                <span className="idn-spacer" />
                <a className="idn-link" href="/identity/groups">All <ArrowUpRight size={13} aria-hidden /></a>
              </div>
              {!groups.length ? <span className="idn-dim idn-small">No groups yet · <a className="idn-link" href="/identity/groups">make one</a></span> : null}
              <div className="idn-groups">
                {groups.map((g) => (
                  <a key={g.id} className="idn-chip" href={`/identity/groups/${g.id}`}>
                    <UsersThree size={13} aria-hidden /> {g.name} <b>{g.member_count ?? g.members.length}</b>
                    {g.provider ? <span className="idn-tag">{g.provider}</span> : null}
                  </a>
                ))}
              </div>
            </>
          ) : failures.groups ? <ErrorState compact what="groups" failure={failures.groups} />
            : <Needs icon={<UsersThree size={14} aria-hidden />} what="Groups" p="group:read" />}
          <div className="idn-hr" />
          {serviceAccounts ? (
            <>
              <div className="idn-card-h">
                <h3>Service accounts</h3>
                <span className="idn-spacer" />
                <a className="idn-link" href="/identity/service-accounts">Keys <ArrowUpRight size={13} aria-hidden /></a>
              </div>
              {serviceAccounts.map((sa) => {
                const next = sa.keys.filter((k) => k.status === "active" && k.expires_at).map((k) => Date.parse(k.expires_at!)).sort()[0];
                const days = next ? Math.ceil((next - now) / 86_400_000) : null;
                return (
                  <a key={sa.id} className="idn-sa" href={`/identity/service-accounts?sa=${sa.id}`}>
                    <span className={`idn-ico${sa.kind === "legacy" ? " warn" : ""}`}>
                      {sa.kind === "legacy" ? <Warning size={15} aria-hidden /> : sa.kind === "scim" ? <UsersThree size={15} aria-hidden /> : sa.name.includes("github") ? <GitBranch size={15} aria-hidden /> : <Key size={15} aria-hidden />}
                    </span>
                    <span><b>{sa.name}</b><small data-copy="data">{sa.kind === "scim" ? "Directory sync only" : sa.kind === "legacy" ? sa.description : `${keyCount(sa.keys.filter((k) => k.status !== "revoked").length)} · owner ${sa.owners[0]?.name ?? "none"}`}</small></span>
                    <span className="idn-r">
                      {sa.kind === "legacy" ? <span className="idn-pill bad">legacy</span>
                        : sa.kind === "scim" ? <span className="idn-pill ok">synced <Ago iso={sa.last_used?.at} bare /></span>
                        : days !== null && days <= 14 ? <span className="idn-pill warn">expires {days}d</span>
                        : <span className="idn-pill ok">healthy</span>}
                    </span>
                  </a>
                );
              })}
            </>
          ) : failures.serviceAccounts ? <ErrorState compact what="service accounts" failure={failures.serviceAccounts} />
            : <Needs icon={<Key size={14} aria-hidden />} what="Service accounts" p="service_account:read" />}
        </section>

        <section className="idn-card" aria-labelledby="idn-agents-h">
          {agents ? (
            <>
              <div className="idn-card-h">
                <h3 id="idn-agents-h">Agents</h3>
                <span className="idn-sub">{agents.filter((a) => a.status === "suspended").length} suspended</span>
                <span className="idn-spacer" />
                <a className="idn-link" href="/identity/agents">Passports <ArrowUpRight size={13} aria-hidden /></a>
              </div>
              <div className="idn-agents">
                {agents.slice(0, 12).map((a) => (
                  <a key={a.id} href={`/identity/agents/${a.id}`} data-status={a.status} title={`${a.name} · ${a.title}${a.status !== "active" ? ` · ${a.status}` : ""}`}>
                    <AgentAvatar agent={a.role_key} size={30} state={a.status === "suspended" ? "paused" : "idle"} />
                    <span>{a.name}</span>
                  </a>
                ))}
              </div>
              {counts ? (
                <div className="idn-stats3">
                  <span><b>{counts.active_grants}</b>active grants</span>
                  <span><b>{counts.runs_acting}</b>runs acting now</span>
                  <span><b>{counts.tokens_out}</b>tokens handed out</span>
                </div>
              ) : null}
            </>
          ) : failures.agents ? <ErrorState compact what="agents" failure={failures.agents} />
            : <Needs icon={<Robot size={14} aria-hidden />} what="Agents" p="agent_passport:read" />}
          <div className="idn-hr" />
          <div className="idn-card-h">
            <h3>Recent security events</h3>
            <span className="idn-spacer" />
            {events ? <a className="idn-link" href="/identity/audit">Audit <ArrowUpRight size={13} aria-hidden /></a> : null}
          </div>
          {events && !events.length ? (
            <EmptyState compact icon={<Fingerprint size={16} />} title="No security events yet" testId="events-empty">
              Sign-ins, access changes and agent grants appear here as they happen.
            </EmptyState>
          ) : events ? (
            <div className="idn-feed">
              {events.map((e) => (
                <a key={e.id} className="idn-ev" href={`/identity/audit?event=${e.id}`}>
                  {e.actor.kind === "agent"
                    ? <AgentAvatar agent={e.actor.id.replace(/^agt_/, "")} size={30} />
                    : <Face name={e.actor.name} tone={e.actor.hue} size={30} />}
                  <span>
                    <span className="idn-t1" data-copy="data">{e.headline}</span>
                    <span className="idn-m" title={e.event_type}>{eventWords(e.event_type)} · {outcomeWords(e.outcome)}</span>
                  </span>
                  <span className="idn-when"><Ago iso={e.at} bare /></span>
                </a>
              ))}
            </div>
          ) : failures.events ? <ErrorState compact what="security events" failure={failures.events} />
            : <Needs icon={<Fingerprint size={14} aria-hidden />} what="Events" p="audit:read" />}
        </section>
      </div>
      {inviting && roles ? <InviteSheet roles={roles} groups={groups ?? []} onClose={() => setInviting(false)} onInvited={() => undefined} /> : null}
    </>
  );
}
