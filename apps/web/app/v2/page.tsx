import { Shell } from "../../components/v2/Shell";
import { fetchCompanyOverview } from "../../lib/v2/overview";
import { resolveProjectId } from "../../lib/client";
import { fetchOrgs } from "../../lib/orgs";
import { ProjectsScreen } from "../../components/v2/ProjectsScreen";
import { consolePageAllowed } from "../../components/identity/route-shell";
import { PageDenied } from "../../components/identity/page-denied";

export const dynamic = "force-dynamic";

type Params = { searchParams?: Promise<Record<string, string | string[] | undefined>> };

const one = (v: string | string[] | undefined): string | undefined =>
  Array.isArray(v) ? v[0] : v;

export default async function V2Projects({ searchParams }: Params) {
  if (!(await consolePageAllowed("/v2"))) return <PageDenied path="/v2" />;
  const sp = searchParams ? await searchParams : undefined;
  const orgId = one(sp?.org) ?? one(sp?.org_id);

  // Resolve the company the same way v1 does, so the toggle lands on the same data.
  const orgs = await fetchOrgs();
  const selected = orgId ? orgs.items.find((o) => o.org_id === orgId) : orgs.items[0];
  const projectId = resolveProjectId(selected?.project_id);

  const overview = await fetchCompanyOverview(projectId);
  if (selected?.org_name) overview.company.name = selected.org_name;

  return (
    <Shell active="missions" overview={overview} orgId={selected?.org_id}>
      <ProjectsScreen overview={overview} orgId={selected?.org_id} />
    </Shell>
  );
}
