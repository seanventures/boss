import { pageAllowed } from "../../components/identity/route-shell";
import { ME_PATH, type MeResponse } from "../../lib/identity/contract";
import { serverGet } from "../../lib/identity/server";
import type { Factor } from "../../lib/identity/types";
import type { LinkedIdentity } from "../../lib/identity/types-auth";
import { loadAuthOptions } from "../(auth)/auth-options";
import { AccountSecurity } from "./security";

export const dynamic = "force-dynamic";

/** My account › Security: passkeys, authenticator, recovery codes, password, linked sign-in. */
export default async function AccountSecurityPage() {
  if (!(await pageAllowed("/account"))) return null;
  const [me, factors, identities, options] = await Promise.all([
    serverGet<MeResponse>(ME_PATH),
    serverGet<{ factors: Factor[] }>("/v1/identity/me/factors"),
    serverGet<{ identities: LinkedIdentity[] }>("/v1/identity/me/identities"),
    loadAuthOptions(),
  ]);
  return (
    <AccountSecurity
      me={me.ok ? me.data : null}
      factors={factors.ok ? factors.data.factors : []}
      identities={identities.ok ? identities.data.identities : []}
      host={options.host}
    />
  );
}
