import { pageAllowed } from "../../components/identity/route-shell";
import { ErrorState } from "../../components/identity/states";
import { ME_PATH, type MeResponse } from "../../lib/identity/contract";
import { serverGet } from "../../lib/identity/server";
import type { Factor } from "../../lib/identity/types";
import type { LinkedIdentity } from "../../lib/identity/types-auth";
import { passwordMin, type MyProfile } from "../../lib/identity/types-account";
import { loadAuthOptions } from "../(auth)/auth-options";
import { AccountSecurity } from "./security";

export const dynamic = "force-dynamic";

/** My account › Security: your name, passkeys, authenticator, recovery codes, password, linked sign-in. */
type Props = { searchParams: Promise<{ linked?: string; error?: string }> };

export default async function AccountSecurityPage({ searchParams }: Props) {
  if (!(await pageAllowed("/account"))) return null;
  // Back from linking a single sign-on account (identity_sso.py: `/account?linked=1`).
  const { linked } = await searchParams;
  const [me, factors, identities, options, profile] = await Promise.all([
    serverGet<MeResponse>(ME_PATH),
    serverGet<{ factors: Factor[] }>("/v1/identity/me/factors"),
    serverGet<{ identities: LinkedIdentity[] }>("/v1/identity/me/identities"),
    loadAuthOptions(),
    serverGet<MyProfile>("/v1/identity/me/profile"),
  ]);
  // Your factors failing to load must never read as "no second factor".
  if (!me.ok) return <ErrorState what="your account" failure={me} />;
  if (!factors.ok) return <ErrorState what="your sign-in methods" failure={factors} />;
  return (
    <AccountSecurity
      me={me.data}
      factors={factors.data.factors}
      identities={identities.ok ? identities.data.identities : []}
      host={options.host}
      linked={linked === "1"}
      providers={options.providers}
      passwordMin={passwordMin(options)}
      profile={profile.ok ? profile.data : null}
    />
  );
}
