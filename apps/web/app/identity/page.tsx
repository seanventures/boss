import { pageAllowed } from "../../components/identity/route-shell";
import { loadDirectory, loadServiceAccounts } from "../../lib/identity/directory";
import { serverGet, type ServerRead } from "../../lib/identity/server";
import type { AuditPage, Passport, Posture } from "../../lib/identity/types";
import { Overview } from "./overview";

export const dynamic = "force-dynamic";

const data = <T,>(r: ServerRead<T>): T | null => (r.ok ? r.data : null);

/** Identity › Overview (mockup a): posture, the directory, and who is acting for whom right now. */
export default async function IdentityOverviewPage() {
  if (!(await pageAllowed("/identity"))) return null;
  const [posture, dir, agents, audit] = await Promise.all([
    serverGet<Posture>("/v1/identity/posture"),
    loadDirectory(),
    serverGet<{ agents: Passport[] }>("/v1/identity/agents"),
    serverGet<AuditPage>("/v1/identity/audit?limit=6"),
  ]);
  const sas = await loadServiceAccounts(dir);
  return (
    <Overview
      posture={data(posture)}
      people={dir.ok ? dir.people : null}
      roles={dir.readable.roles ? dir.roles : null}
      groups={dir.readable.groups ? dir.groups : null}
      serviceAccounts={sas}
      agents={data(agents)?.agents ?? null}
      events={data(audit)?.events.slice(0, 6) ?? null}
    />
  );
}
