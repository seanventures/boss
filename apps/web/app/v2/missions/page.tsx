import { redirect } from "next/navigation";

import { resolveLegacyRoute, searchParamsFrom } from "../../legacy-areas";
import { consolePageAllowed } from "../../../components/identity/route-shell";
import { PageDenied } from "../../../components/identity/page-denied";

/**
 * The project list is /v2. A bare /v2/missions bookmark used to be Next's own 404, outside
 * the console and outside sign-in. One build stays at /v2/missions/[id]. The org travels.
 */
export const dynamic = "force-dynamic";

type Params = { searchParams?: Promise<Record<string, string | string[] | undefined>> };

export default async function MissionsIndex({ searchParams }: Params) {
  if (!(await consolePageAllowed("/v2/missions"))) return <PageDenied path="/v2/missions" />;
  const params = searchParamsFrom(searchParams ? await searchParams : undefined);
  redirect(resolveLegacyRoute("/v2/missions", params) ?? "/v2");
}
