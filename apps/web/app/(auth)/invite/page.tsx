import { loadAuthOptions } from "../auth-options";
import { InviteLanding } from "./invite";

export const dynamic = "force-dynamic";
export const metadata = { title: "Join BOSS" };

/**
 * Accept an invitation (single use, 7 days). The link is `/invite#t=<token>`: the server never
 * sees the token — the page reads the fragment in the browser and POSTs it (invite.tsx).
 */
export default async function InvitePage() {
  return <InviteLanding options={await loadAuthOptions()} />;
}
