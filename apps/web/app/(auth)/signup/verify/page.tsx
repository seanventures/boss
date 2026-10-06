import { loadAuthOptions } from "../../auth-options";
import { SignUpVerify } from "../signup";

export const dynamic = "force-dynamic";
export const metadata = { title: "Verify email · BOSS" };

export default async function SignUpVerifyPage() {
  const options = await loadAuthOptions();
  return <SignUpVerify host={options.host} />;
}
