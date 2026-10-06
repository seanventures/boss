import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet } from "../../../lib/identity/server";
import type { Posture, Provider, SecurityPolicy } from "../../../lib/identity/types";
import { SignInSettings } from "./sign-in-settings";

export const dynamic = "force-dynamic";

/** Identity › Sign-in & SSO (§9.8): providers, SCIM, the security policy and the legacy paths. */
export default async function SignInSettingsPage() {
  if (!(await pageAllowed("/identity/sign-in"))) return null;
  const [providers, policy, posture] = await Promise.all([
    serverGet<{ providers: Provider[] }>("/v1/identity/providers"),
    serverGet<SecurityPolicy>("/v1/identity/policy"),
    serverGet<Posture>("/v1/identity/posture"),
  ]);
  return (
    <SignInSettings
      providers={providers.ok ? providers.data.providers : []}
      policy={policy.ok ? policy.data : null}
      legacy={posture.ok ? posture.data.legacy : null}
    />
  );
}
