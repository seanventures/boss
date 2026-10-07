/** What the sign-in screens may show, read on the server (GET /v1/auth/options — a seam, S1). */
import { headers } from "next/headers";

import { serverGet } from "../../lib/identity/server";
import type { AuthOptionsWithPolicy } from "../../lib/identity/types-account";

export async function loadAuthOptions(): Promise<AuthOptionsWithPolicy> {
  const read = await serverGet<AuthOptionsWithPolicy>("/v1/auth/options");
  if (read.ok) return read.data;
  // The API cannot say: show the least — passkey and password, no providers, no email tabs.
  const host = (await headers()).get("host") ?? "";
  return { org_name: "BOSS", host, providers: [], smtp: false, signup: false, legacy_password: false, claim: null };
}
