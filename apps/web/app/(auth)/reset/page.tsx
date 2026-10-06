import { loadAuthOptions } from "../auth-options";
import { Reset } from "./reset";

export const dynamic = "force-dynamic";
export const metadata = { title: "Reset password · BOSS" };

/** Forgot password / set a new one. An address is never carried in the URL (privacy). */
export default async function ResetPage() {
  const options = await loadAuthOptions();
  return <Reset host={options.host} smtp={options.smtp} email="" />;
}
