import { Shell } from "../../../components/v2/Shell";
import { ReceiptsScreen } from "../../../components/v2/ReceiptsScreen";
import { fetchCompanyOverview } from "../../../lib/v2/overview";
import { fetchReceipts } from "../../../lib/v2/receipts";
import { resolveProjectId } from "../../../lib/client";
import { fetchOrgs } from "../../../lib/orgs";
import { consolePageAllowed } from "../../../components/identity/route-shell";
import { PageDenied } from "../../../components/identity/page-denied";

export const dynamic = "force-dynamic";

type Params = { searchParams?: Promise<Record<string, string | string[] | undefined>> };

const one = (v: string | string[] | undefined): string | undefined =>
  Array.isArray(v) ? v[0] : v;

export default async function V2Receipts({ searchParams }: Params) {
  if (!(await consolePageAllowed("/v2/timeline"))) return <PageDenied path="/v2/timeline" />;
  const sp = searchParams ? await searchParams : undefined;
  const orgId = one(sp?.org) ?? one(sp?.org_id);
  const hours = Number(one(sp?.hours) ?? 24) || 24;

  const orgs = await fetchOrgs();
  const selected = orgId ? orgs.items.find((o) => o.org_id === orgId) : orgs.items[0];
  const projectId = resolveProjectId(selected?.project_id);

  const [overview, feed] = await Promise.all([
    fetchCompanyOverview(projectId),
    fetchReceipts(projectId, hours),
  ]);
  if (selected?.org_name) overview.company.name = selected.org_name;

  return (
    <Shell active="timeline" overview={overview} orgId={selected?.org_id}>
      <ReceiptsScreen feed={feed} />
    </Shell>
  );
}
