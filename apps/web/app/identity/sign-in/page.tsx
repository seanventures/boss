import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet, type ServerRead } from "../../../lib/identity/server";
import type { Posture, Provider, SecurityPolicy } from "../../../lib/identity/types";
import { SignInSettings } from "./sign-in-settings";
import { loadAuthOptions } from "../../(auth)/auth-options";

export const dynamic = "force-dynamic";

/** Identity › Sign-in & SSO (§9.8): providers, SCIM, the security policy and the legacy paths. */
export default async function SignInSettingsPage() {
  if (!(await pageAllowed("/identity/sign-in"))) return null;
  const [providers, policy, posture, options] = await Promise.all([
    serverGet<{ providers: Provider[] }>("/v1/identity/providers"),
    serverGet<SecurityPolicy>("/v1/identity/policy"),
    serverGet<Posture>("/v1/identity/posture"),
    loadAuthOptions(),
  ]);
  // A refusal (403) is "you can't see this"; anything else failed and says so, never "none yet".
  const failed = <T,>(r: ServerRead<T>) => (!r.ok && r.status !== 403 ? r : null);
  return (
    <SignInSettings
      providers={providers.ok ? providers.data.providers : []}
      policy={policy.ok ? policy.data : null}
      legacy={posture.ok ? posture.data.legacy : null}
      failures={{ providers: providers.ok ? null : providers, policy: failed(policy), legacy: failed(posture) }}
      publicUrl={{ public_origin: options.public_origin, public_origin_source: options.public_origin_source }}
    />
  );
}
