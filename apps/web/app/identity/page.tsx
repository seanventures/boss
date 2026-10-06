import { pageAllowed } from "../../components/identity/route-shell";
import { serverGet, type ServerRead } from "../../lib/identity/server";
import type { AccessRole, AuditPage, Group, Passport, Person, Posture, ServiceAccount } from "../../lib/identity/types";
import { Overview } from "./overview";

export const dynamic = "force-dynamic";

const data = <T,>(r: ServerRead<T>): T | null => (r.ok ? r.data : null);

/** Identity › Overview (mockup a): posture, the directory, and who is acting for whom right now. */
export default async function IdentityOverviewPage() {
  if (!(await pageAllowed("/identity"))) return null;
  const [posture, people, roles, groups, sas, agents, audit] = await Promise.all([
    serverGet<Posture>("/v1/identity/posture"),
    serverGet<{ users: Person[] }>("/v1/identity/users"),
    serverGet<{ roles: AccessRole[] }>("/v1/identity/access-roles"),
    serverGet<{ groups: Group[] }>("/v1/identity/groups"),
    serverGet<{ service_accounts: ServiceAccount[] }>("/v1/identity/service-accounts"),
    serverGet<{ agents: Passport[] }>("/v1/identity/agents"),
    serverGet<AuditPage>("/v1/identity/audit?limit=6"),
  ]);
  return (
    <Overview
      posture={data(posture)}
      people={data(people)?.users ?? null}
      roles={data(roles)?.roles ?? null}
      groups={data(groups)?.groups ?? null}
      serviceAccounts={data(sas)?.service_accounts ?? null}
      agents={data(agents)?.agents ?? null}
      events={data(audit)?.events.slice(0, 6) ?? null}
    />
  );
}
