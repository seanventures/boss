import { avatarTone } from "../../../lib/identity/avatar";
import { redirect } from "next/navigation";

import { safeNext } from "../../../lib/console-session";
import { ME_PATH, type MeResponse } from "../../../lib/identity/contract";
import { serverGet } from "../../../lib/identity/server";
import { loadAuthOptions } from "../auth-options";
import { Enrol } from "./enrol";
import { firstOwnerNext } from "../../../lib/identity/claim-resume";
import { passwordMin } from "../../../lib/identity/types-account";

export const dynamic = "force-dynamic";
export const metadata = { title: "Protect your account, BOSS" };

type Props = { searchParams: Promise<{ next?: string }> };


/** Enrol a passkey or an authenticator, then recovery codes. Needs a session. */
export default async function EnrolPage({ searchParams }: Props) {
  const asked = (await searchParams).next;
  const me = await serverGet<MeResponse>(ME_PATH);
  if (!me.ok) redirect(`/login?next=${encodeURIComponent(`/enrol?next=${safeNext(asked ?? "/identity")}`)}`);
  // THE OWNER'S FIRST FACTOR comes right after the claim: then Setup is next, even when they got
  // here through Sign in (which asks for "/") after a reload mid-claim (stranger test r3).
  const next = safeNext(firstOwnerNext(me.data, asked) ?? asked ?? "/identity");
  const options = await loadAuthOptions();
  const who = { name: me.data.display_name, email: me.data.email ?? "", hue: avatarTone(me.data.principal_id, me.data.avatar_seed) };
  return <Enrol host={options.host} next={next} who={who} passkeyReason={options.passkey_reason ?? null} passwordMin={passwordMin(options)} />;
}
