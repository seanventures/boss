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
    <Shell active="boss" overview={overview} orgId={selected?.org_id}>
      <NotMovedYet
        title="Workflows"
        summary="The delivery pipeline end to end: verdict, staging, health gate, production, rollout — with the evidence behind each step."
        v1Area="delivery"
        orgId={selected?.org_id}
      />
    </Shell>
  );
}
