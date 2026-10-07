import { Shell } from "../../../components/v2/Shell";
import { HiveScreen } from "../../../components/v2/HiveScreen";
import { fetchCompanyOverview } from "../../../lib/v2/overview";
import { fetchWorkforce } from "../../../lib/v2/workforce";
import { resolveProjectId } from "../../../lib/client";
import { fetchOrgs } from "../../../lib/orgs";
import { consolePageAllowed } from "../../../components/identity/route-shell";
import { PageDenied } from "../../../components/identity/page-denied";

export const dynamic = "force-dynamic";

type Params = { searchParams?: Promise<Record<string, string | string[] | undefined>> };

const one = (v: string | string[] | undefined): string | undefined =>
  Array.isArray(v) ? v[0] : v;

export default async function V2Section({ searchParams }: Params) {
  if (!(await consolePageAllowed("/v2/hive"))) return <PageDenied path="/v2/hive" />;
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
    <Shell active="hive" overview={overview} orgId={selected?.org_id}>
      <HiveScreen workforce={data} />
    </Shell>
  );
}
