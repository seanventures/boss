import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { serverGet, type ServerRead } from "../../../../lib/identity/server";
import type {
  AccessRole, ApiKey, AuditPage, Delegation, EffectiveAccess, Factor, Person, SessionRow,
} from "../../../../lib/identity/types";
import { PersonProfile } from "./person";
import { PERSON_TABS, type PersonTab } from "./tabs";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }>; searchParams: Promise<{ tab?: string }> };

const data = <T,>(r: ServerRead<T>): T | null => (r.ok ? r.data : null);

/** Identity › People › a person (mockup b): access and why, sessions, security, delegations, keys, audit. */
export default async function PersonPage({ params, searchParams }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/people/${id}`))) return null;
  const asked = (await searchParams).tab;
  const tab = (PERSON_TABS.some((t) => t.id === asked) ? asked : "access") as PersonTab;
  const enc = encodeURIComponent(id);
  const person = await serverGet<Person>(`/v1/identity/users/${enc}`);
  if (!person.ok && person.status === 404) notFound();
  if (!person.ok) return null;
  const projects = [...new Set(person.data.bindings.filter((b) => b.scope.startsWith("project:")).map((b) => b.scope.slice(8)))];
  const [factors, sessions, effOrg, roles, delegations, keys, audit, ...effProjects] = await Promise.all([
    serverGet<{ factors: Factor[] }>(`/v1/identity/users/${enc}/factors`),
    serverGet<{ sessions: SessionRow[] }>(`/v1/identity/sessions?principal_id=${enc}`),
    serverGet<EffectiveAccess>(`/v1/identity/principals/${enc}/effective`),
    serverGet<{ roles: AccessRole[] }>("/v1/identity/access-roles"),
    serverGet<{ delegations: Delegation[] }>("/v1/identity/delegations"),
    serverGet<{ keys: ApiKey[] }>("/v1/identity/api-keys"),
    serverGet<AuditPage>(`/v1/identity/audit?actor=${enc}`),
    ...projects.map((p) => serverGet<EffectiveAccess>(`/v1/identity/principals/${enc}/effective?project_id=${encodeURIComponent(p)}`)),
  ]);
  return (
    <PersonProfile
      person={person.data}
      tab={tab}
      factors={data(factors)?.factors ?? null}
      sessions={data(sessions)?.sessions ?? null}
      effective={data(effOrg)}
      projectEffective={effProjects.map((r) => data(r)).filter((x): x is EffectiveAccess => x !== null)}
      roles={data(roles)?.roles ?? []}
      delegations={data(delegations)?.delegations.filter((d) => d.user.id === id) ?? null}
      keys={data(keys)?.keys.filter((k) => k.owner.id === id) ?? null}
      events={data(audit)?.events ?? null}
    />
  );
}
