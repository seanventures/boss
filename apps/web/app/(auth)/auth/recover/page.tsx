import { loadAuthOptions } from "../../auth-options";
import { ForwardLegacyLink } from "./forward";

export const dynamic = "force-dynamic";
export const metadata = { title: "Recover, BOSS" };

/** `/auth/recover#token=…` from an older `boss identity recover-owner`: forwarded to /reset. */
export default async function LegacyRecoverPage() {
  const options = await loadAuthOptions();
  return <ForwardLegacyLink host={options.host} path="/auth/recover" />;
}
