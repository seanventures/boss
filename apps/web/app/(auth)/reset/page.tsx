import { loadAuthOptions } from "../auth-options";
import { passwordMin } from "../../../lib/identity/types-account";
import { Reset } from "./reset";
import { emailSignInOffered } from "../login/sign-in";

export const dynamic = "force-dynamic";
export const metadata = { title: "Reset password · BOSS" };

/** Forgot password / set a new one. An address is never carried in the URL (privacy). */
export default async function ResetPage() {
  const options = await loadAuthOptions();
  // A reset link is emailed with BOSS's public URL in it: none bound, none sent (H2).
  return <Reset host={options.host} smtp={emailSignInOffered(options)} email="" minPassword={passwordMin(options)} />;
}
