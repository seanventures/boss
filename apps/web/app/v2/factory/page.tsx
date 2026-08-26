import { Shell } from "../../../components/v2/Shell";
import { WorkforceScreen } from "../../../components/v2/WorkforceScreen";
import { fetchCompanyOverview } from "../../../lib/v2/overview";
import { fetchWorkforce } from "../../../lib/v2/workforce";
import { resolveProjectId } from "../../../lib/client";
import { fetchOrgs } from "../../../lib/orgs";

export const dynamic = "force-dynamic";

type Params = { searchParams?: Promise<Record<string, string | string[] | undefined>> };

const one = (v: string | string[] | undefined): string | undefined =>
  Array.isArray(v) ? v[0] : v;

export default async function V2Section({ searchParams }: Params) {
  const sp = searchParams ? await searchParams : undefined;
  const orgId = one(sp?.org) ?? one(sp?.org_id);

  const orgs = await fetchOrgs();
  const selected = orgId ? orgs.items.find((o) => o.org_id === orgId) : orgs.items[0];
  const projectId = resolveProjectId(selected?.project_id);

  const [overview, data] = await Promise.all([
    fetchCompanyOverview(projectId),
    fetchWorkforce(projectId),
  ]);
  if (selected?.org_name) overview.company.name = selected.org_name;

  return (
    <Shell active="factory" overview={overview} orgId={selected?.org_id}>
      <WorkforceScreen workforce={data} projectId={projectId} />
    </Shell>
  );
}
