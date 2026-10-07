import { Shell } from "../../../../components/v2/Shell";
import { WorkspaceScreen } from "../../../../components/v2/WorkspaceScreen";
import { fetchCompanyOverview } from "../../../../lib/v2/overview";
import { fetchWorkspace } from "../../../../lib/v2/workspace";
import { resolveProjectId } from "../../../../lib/client";
import { fetchOrgs } from "../../../../lib/orgs";
import { consolePageAllowed } from "../../../../components/identity/route-shell";
import { PageDenied } from "../../../../components/identity/page-denied";

export const dynamic = "force-dynamic";

type Params = {
  params: Promise<{ id: string }>;
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
};

const one = (v: string | string[] | undefined): string | undefined =>
  Array.isArray(v) ? v[0] : v;

export default async function V2Workspace({ params, searchParams }: Params) {
  const { id } = await params;
  if (!(await consolePageAllowed(`/v2/missions/${encodeURIComponent(id)}`))) return <PageDenied path="/v2/missions/x" />;
  const sp = searchParams ? await searchParams : undefined;
  const orgId = one(sp?.org) ?? one(sp?.org_id);

  // Resolve the company the same way the Projects screen does, so a deep link lands on the
  // same data as a click-through.
  const orgs = await fetchOrgs();
  const selected = orgId ? orgs.items.find((o) => o.org_id === orgId) : orgs.items[0];
  const projectId = resolveProjectId(selected?.project_id);

  // Both fetches at once. The shell needs the company strip and the screen needs the app;
  // awaiting them in sequence would make this page twice as slow for no reason.
  const [overview, workspace] = await Promise.all([
    fetchCompanyOverview(projectId),
    fetchWorkspace(projectId, decodeURIComponent(id)),
  ]);
  if (selected?.org_name) overview.company.name = selected.org_name;

  return (
    <Shell active="missions" overview={overview} orgId={selected?.org_id}>
      <WorkspaceScreen workspace={workspace} orgId={selected?.org_id} />
    </Shell>
  );
}
