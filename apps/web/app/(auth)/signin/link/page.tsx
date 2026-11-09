import { safeNext } from "../../../../lib/console-session";
import { loadAuthOptions } from "../../auth-options";
import { MagicLinkLanding } from "./link";

export const dynamic = "force-dynamic";
export const metadata = { title: "Signing in, BOSS" };

type Props = { searchParams: Promise<{ next?: string }> };

export default async function MagicLinkPage({ searchParams }: Props) {
  const options = await loadAuthOptions();
  return <MagicLinkLanding host={options.host} next={safeNext((await searchParams).next ?? null)} />;
}
