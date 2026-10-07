import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { ErrorState, type ReadFailure } from "../../../../components/identity/states";
import { loadProjectChoices } from "../projects";
import { adaptEffective, adaptPerson, adaptSession, roleIndex } from "../../../../lib/identity/adapt-people";
import { loadDirectory } from "../../../../lib/identity/directory";
import { serverGet, type ServerRead } from "../../../../lib/identity/server";
import type { ApiKey, AuditPage, Delegation, EffectiveAccess, Factor } from "../../../../lib/identity/types";
import { PersonProfile, type PersonFailures } from "./person";
import { PERSON_TABS, type PersonTab } from "./tabs";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }>; searchParams: Promise<{ tab?: string }> };

const data = <T,>(r: ServerRead<T>): T | null => (r.ok ? r.data : null);
const why = <T,>(r: ServerRead<T>): ReadFailure | undefined => (r.ok ? undefined : { status: r.status, code: r.code, permission: r.permission });

/** A person's audit trail: what they did (actor) and what was done to them (target), newest first. */
function mergeAudit(a: AuditPage | null, b: AuditPage | null): AuditPage["events"] | null {
  if (!a && !b) return null;
  const seen = new Set<string>();
  return [...(a?.events ?? []), ...(b?.events ?? [])]
    .filter((e) => (seen.has(e.id) ? false : (seen.add(e.id), true)))
    .sort((x, y) => Date.parse(y.at) - Date.parse(x.at));
}

/** Identity › People › a person (mockup b): access and why, sessions, security, delegations, keys, audit. */
export default async function PersonPage({ params, searchParams }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/people/${id}`))) return null;
  const asked = (await searchParams).tab;
  const tab = (PERSON_TABS.some((t) => t.id === asked) ? asked : "access") as PersonTab;
  const enc = encodeURIComponent(id);
  const [detail, dir, projectChoices] = await Promise.all([serverGet<unknown>(`/v1/identity/users/${enc}`), loadDirectory(), loadProjectChoices()]);
  if (!detail.ok && detail.status === 404) notFound();
  if (!detail.ok) return <ErrorState what="this person" failure={{ status: detail.status, code: detail.code, permission: detail.permission }} testId="person-error" />;
  const raw = detail.data as { email_verified?: unknown };
  const emailVerified = typeof raw.email_verified === "boolean" ? raw.email_verified : null;
  // The profile (factors, SSO links) from /users/{id}; bindings and groups joined from the directory.
  const listed = dir.people.find((p) => p.id === id);
  const index = roleIndex(dir.roles);
  const person = adaptPerson(detail.data, { index, bindings: listed?.bindings, groups: listed?.groups });
  const projects = [...new Set(person.bindings.filter((b) => b.scope.startsWith("project:")).map((b) => b.scope.slice(8)))];
  const [factors, sessions, effOrg, delegations, keys, audit, auditAbout, ...effProjects] = await Promise.all([
    serverGet<{ factors: Factor[] }>(`/v1/identity/users/${enc}/factors`),
    serverGet<{ sessions: unknown[] }>(`/v1/identity/sessions?principal_id=${enc}`),
    serverGet<unknown>(`/v1/identity/principals/${enc}/effective`),
    serverGet<{ delegations: Delegation[] }>("/v1/identity/delegations"),
    serverGet<{ keys: ApiKey[] }>("/v1/identity/api-keys"),
    serverGet<AuditPage>(`/v1/identity/audit?actor=${enc}`),
    serverGet<AuditPage>(`/v1/identity/audit?q=${enc}`),
    ...projects.map((p) => serverGet<unknown>(`/v1/identity/principals/${enc}/effective?project_id=${encodeURIComponent(p)}`)),
  ]);
  const projectEffective = effProjects.flatMap((r) => (r.ok ? [adaptEffective(r.data, index)] : []));
  const org = effOrg.ok ? adaptEffective(effOrg.data, index) : null;
  const effective: EffectiveAccess | null = org && !Object.keys(org.by_project).length
    ? { ...org, by_project: Object.fromEntries(projectEffective.map((e) => [e.scope.replace(/^project:/, ""), e.held])) }
    : org;
  return (
    <PersonProfile
      person={person}
      tab={tab}
      factors={data(factors)?.factors ?? null}
      sessions={sessions.ok ? (sessions.data.sessions ?? []).map(adaptSession) : null}
      effective={effective}
      projectEffective={projectEffective}
      roles={dir.roles}
      delegations={data(delegations)?.delegations.filter((d) => d.user.id === id) ?? null}
      keys={data(keys)?.keys.filter((k) => k.owner.id === id) ?? null}
      events={mergeAudit(data(audit), data(auditAbout))}
      projects={projectChoices}
      emailVerified={emailVerified}
      failures={{
        factors: why(factors), sessions: why(sessions), effective: why(effOrg), delegations: why(delegations), keys: why(keys), events: why(audit),
      } satisfies PersonFailures}
    />
  );
}
