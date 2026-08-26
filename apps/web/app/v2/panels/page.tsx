import { Shell } from "../../../components/v2/Shell";
import { DecisionsScreen } from "../../../components/v2/DecisionsScreen";
import { fetchCompanyOverview } from "../../../lib/v2/overview";
import { fetchDecisionQueue, fetchPanels } from "../../../lib/v2/decisions";
import { resolveProjectId } from "../../../lib/client";
import { fetchOrgs } from "../../../lib/orgs";

export const dynamic = "force-dynamic";

type Params = { searchParams?: Promise<Record<string, string | string[] | undefined>> };

const one = (v: string | string[] | undefined): string | undefined =>
  Array.isArray(v) ? v[0] : v;

export default async function V2Decisions({ searchParams }: Params) {
  const sp = searchParams ? await searchParams : undefined;
  const orgId = one(sp?.org) ?? one(sp?.org_id);

  const orgs = await fetchOrgs();
  const selected = orgId ? orgs.items.find((o) => o.org_id === orgId) : orgs.items[0];
  const projectId = resolveProjectId(selected?.project_id);

  // Both at once: the shell needs the company strip, the screen needs the queue.
  const [overview, queue, panels] = await Promise.all([
    fetchCompanyOverview(projectId),
    fetchDecisionQueue(projectId),
    fetchPanels(projectId),
  ]);
  if (selected?.org_name) overview.company.name = selected.org_name;

  return (
    <Shell active="panels" overview={overview} orgId={selected?.org_id}>
      <DecisionsScreen queue={queue} panels={panels} />
    </Shell>
  );
}
