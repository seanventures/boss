import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet } from "../../../lib/identity/server";
import type { SessionRow } from "../../../lib/identity/types";
import { MySessions } from "./sessions";

export const dynamic = "force-dynamic";

/** My account › Sessions. */
export default async function MySessionsPage() {
  if (!(await pageAllowed("/account/sessions"))) return null;
  const r = await serverGet<{ sessions: SessionRow[] }>("/v1/identity/me/sessions");
  return <MySessions sessions={r.ok ? r.data.sessions : []} />;
}
