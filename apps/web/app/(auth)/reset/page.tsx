import { loadAuthOptions } from "../auth-options";
import { Reset } from "./reset";

export const dynamic = "force-dynamic";
export const metadata = { title: "Reset password · BOSS" };

type Props = { searchParams: Promise<{ email?: string }> };

export default async function ResetPage({ searchParams }: Props) {
  const options = await loadAuthOptions();
  const email = String((await searchParams).email ?? "").slice(0, 254);
  return <Reset host={options.host} smtp={options.smtp} email={email} />;
}
