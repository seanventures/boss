import { redirect } from "next/navigation";

import { resolveLegacyRoute, searchParamsFrom } from "../../legacy-areas";

/**
 * Retired: this v2 screen is a console area now (Workflow Studio spec 2.9). The visitor's org
 * travels with them; app/legacy-areas.ts says where every retired surface lands.
 */
export const dynamic = "force-dynamic";

type Params = { searchParams?: Promise<Record<string, string | string[] | undefined>> };

export default async function RetiredV2Screen({ searchParams }: Params) {
  const params = searchParamsFrom(searchParams ? await searchParams : undefined);
  redirect(resolveLegacyRoute("/v2/factory", params) ?? "/");
}
