import { avatarTone } from "../../../lib/identity/avatar";
import { redirect } from "next/navigation";

import { safeNext } from "../../../lib/console-session";
import { ME_PATH, type MeResponse } from "../../../lib/identity/contract";
import { serverGet } from "../../../lib/identity/server";
import { loadAuthOptions } from "../auth-options";
import { Enrol } from "./enrol";

export const dynamic = "force-dynamic";
export const metadata = { title: "Add a second factor · BOSS" };

type Props = { searchParams: Promise<{ next?: string }> };


/** Enrol a passkey or an authenticator, then recovery codes. Needs a session. */
export default async function EnrolPage({ searchParams }: Props) {
  const next = safeNext((await searchParams).next ?? "/identity");
  const me = await serverGet<MeResponse>(ME_PATH);
  if (!me.ok) redirect(`/login?next=${encodeURIComponent(`/enrol?next=${next}`)}`);
  const options = await loadAuthOptions();
  const who = { name: me.data.display_name, email: me.data.email ?? "", hue: avatarTone(me.data.principal_id, me.data.avatar_seed) };
  return <Enrol host={options.host} next={next} who={who} />;
}
