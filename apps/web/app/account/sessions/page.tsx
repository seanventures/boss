import { pageAllowed } from "../../../components/identity/route-shell";
import { ErrorState } from "../../../components/identity/states";
import { serverGet } from "../../../lib/identity/server";
import { toSessionRows } from "../../../lib/identity/adapt-session";
import { MySessions } from "./sessions";

export const dynamic = "force-dynamic";

/** My account › Sessions. */
export default async function MySessionsPage() {
  if (!(await pageAllowed("/account/sessions"))) return null;
  const r = await serverGet<{ sessions: unknown[] }>("/v1/identity/me/sessions");
  if (!r.ok) return <ErrorState what="your sessions" failure={r} />;
  return <MySessions sessions={toSessionRows(r.data.sessions)} />;
}
