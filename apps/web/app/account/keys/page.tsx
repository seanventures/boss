import { pageAllowed } from "../../../components/identity/route-shell";
import { ErrorState } from "../../../components/identity/states";
import { serverGet } from "../../../lib/identity/server";
import type { ApiKey } from "../../../lib/identity/types";
import { MyKeys } from "./my-keys";

export const dynamic = "force-dynamic";

/** My account › API keys (personal access tokens). */
export default async function MyKeysPage() {
  if (!(await pageAllowed("/account/keys"))) return null;
  const r = await serverGet<{ keys: ApiKey[] }>("/v1/identity/me/api-keys");
  if (!r.ok) return <ErrorState what="your API keys" failure={r} />;
  return <MyKeys keys={r.data.keys} />;
}
