import { serverGet } from "../../../../lib/identity/server";
import type { InvitationPreview } from "../../../../lib/identity/types-auth";
import { loadAuthOptions } from "../../auth-options";
import { InviteAccept } from "./invite";

export const dynamic = "force-dynamic";
export const metadata = { title: "Join BOSS" };

type Props = { params: Promise<{ token: string }> };

/** Accept an invitation (single use, 7 days). */
export default async function InvitePage({ params }: Props) {
  const { token } = await params;
  const [invitation, options] = await Promise.all([
    serverGet<InvitationPreview>(`/v1/auth/invitations/${encodeURIComponent(token)}`),
    loadAuthOptions(),
  ]);
  return <InviteAccept token={token} invitation={invitation.ok ? invitation.data : null} options={options} />;
}
