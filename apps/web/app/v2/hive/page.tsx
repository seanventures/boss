import { Shell } from "../../../components/v2/Shell";
import { NotMovedYet } from "../../../components/v2/NotMovedYet";
import { fetchCompanyOverview } from "../../../lib/v2/overview";
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
  const overview = await fetchCompanyOverview(resolveProjectId(selected?.project_id));
  if (selected?.org_name) overview.company.name = selected.org_name;

  return (
    <Shell active="hive" overview={overview} orgId={selected?.org_id}>
      <NotMovedYet
        title="Hive"
        summary="Every agent at work right now: what it is doing, which run it belongs to, and what it has spent."
        v1Area="agent"
        orgId={selected?.org_id}
      />
    </Shell>
  );
}
