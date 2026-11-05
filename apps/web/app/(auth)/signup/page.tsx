import { loadAuthOptions } from "../auth-options";
import { SignUp } from "./signup";

export const dynamic = "force-dynamic";
export const metadata = { title: "Create account · BOSS" };

export default async function SignUpPage() {
  const options = await loadAuthOptions();
  return <SignUp host={options.host} org={options.org_name} enabled={options.signup} unclaimed={options.claim != null} />;
}
