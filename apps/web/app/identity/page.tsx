import { pageAllowed } from "../../components/identity/route-shell";
import type { ReadFailure } from "../../components/identity/states";
import { checkWith } from "../../lib/identity/check";
import { snapshotFrom } from "../../lib/identity/contract";
import { loadDirectory, loadServiceAccounts } from "../../lib/identity/directory";
import type { Permission } from "../../lib/identity/permissions.gen";
import { serverGet, type ServerRead } from "../../lib/identity/server";
import { loadIdentityCached } from "../../lib/identity/server-cache";
import type { AuditPage, Passport, Posture } from "../../lib/identity/types";
import { Overview, type OverviewFailures } from "./overview";

export const dynamic = "force-dynamic";

const data = <T,>(r: ServerRead<T>): T | null => (r.ok ? r.data : null);
/** A read that FAILED — not one the viewer may not make (403), which each panel says itself. */
const failed = <T,>(r: ServerRead<T>): ReadFailure | null => (!r.ok && r.status !== 403 ? r : null);
const SERVER_ERROR: ReadFailure = { status: 500, code: "error" };

/** Identity › Overview (mockup a): posture, the directory, and who is acting for whom right now. */
export default async function IdentityOverviewPage() {
  if (!(await pageAllowed("/identity"))) return null;
  const [posture, dir, agents, audit, boot] = await Promise.all([
    serverGet<Posture>("/v1/identity/posture"),
    loadDirectory(),
    serverGet<{ agents: Passport[] }>("/v1/identity/agents"),
    serverGet<AuditPage>("/v1/identity/audit?limit=6"),
    loadIdentityCached(null),
  ]);
  const sas = await loadServiceAccounts(dir);
  // The directory joins several reads: a part missing although the viewer holds its permission failed.
  const org = boot.org ? snapshotFrom(boot.org) : null;
  const holds = (p: Permission) => !org || checkWith(org, null, null, p).allowed;
  const failures: OverviewFailures = {
    posture: failed(posture),
    people: !dir.ok && holds("user:read") ? SERVER_ERROR : null,
    roles: !dir.readable.roles && holds("access_role:read") ? SERVER_ERROR : null,
    groups: !dir.readable.groups && holds("group:read") ? SERVER_ERROR : null,
    serviceAccounts: sas === null && holds("service_account:read") ? SERVER_ERROR : null,
    agents: failed(agents),
    events: failed(audit),
  };
  return (
    <Overview
      posture={data(posture)}
      people={dir.ok ? dir.people : null}
      roles={dir.readable.roles ? dir.roles : null}
      groups={dir.readable.groups ? dir.groups : null}
      serviceAccounts={sas}
      agents={data(agents)?.agents ?? null}
      events={data(audit)?.events.slice(0, 6) ?? null}
      failures={failures}
    />
  );
}
