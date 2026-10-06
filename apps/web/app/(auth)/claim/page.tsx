import { loadAuthOptions } from "../auth-options";
import { ClaimFlow } from "./claim";

export const dynamic = "force-dynamic";
export const metadata = { title: "Claim this BOSS" };

/** Claim this BOSS: the first Owner, on the setup-link, legacy-password or network-bridge path. */
export default async function ClaimPage() {
  return <ClaimFlow options={await loadAuthOptions()} />;
}
