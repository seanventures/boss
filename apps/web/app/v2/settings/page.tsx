import { redirect } from "next/navigation";

import { resolveLegacyRoute, searchParamsFrom } from "../../legacy-areas";
import { consolePageAllowed } from "../../../components/identity/route-shell";
import { PageDenied } from "../../../components/identity/page-denied";

/**
 * Retired: v2 Settings is Repo and models now (setup spec §6.1). The visitor's org travels with
 * them; app/legacy-areas.ts says where every retired surface lands.
 */
export const dynamic = "force-dynamic";

type Params = { searchParams?: Promise<Record<string, string | string[] | undefined>> };

export default async function RetiredV2Settings({ searchParams }: Params) {
  if (!(await consolePageAllowed("/v2/settings"))) return <PageDenied path="/v2/settings" />;
  const params = searchParamsFrom(searchParams ? await searchParams : undefined);
  redirect(resolveLegacyRoute("/v2/settings", params) ?? "/?area=setup");
}
