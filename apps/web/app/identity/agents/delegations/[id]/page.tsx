import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../../components/identity/route-shell";
import { serverGet } from "../../../../../lib/identity/server";
import type { Delegation } from "../../../../../lib/identity/types";
import { detailFromGrant } from "./detail";
import { DelegationView } from "./delegation";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

/**
 * Identity › Agents › a delegation (mockup d): the narrowing chain, its tokens and egress, revoke.
 *
 * The API has no `GET /v1/identity/delegations/{id}` (only the list, /claims, /cascade-preview and
 * the actions), so the grant is found in the list — the org's for a reader of every grant, else
 * the person's own. The chain, tokens and egress are not served yet: they render empty, honestly.
 */
export default async function DelegationPage({ params }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/agents/delegations/${id}`))) return null;
  let read = await serverGet<{ delegations: Delegation[] }>("/v1/identity/delegations");
  let grant = read.ok ? read.data.delegations.find((g) => g.id === id) : undefined;
  if (!grant) {
    const mine = await serverGet<{ delegations: Delegation[] }>("/v1/identity/me/delegations");
    if (mine.ok) { read = mine; grant = mine.data.delegations.find((g) => g.id === id); }
  }
  if (!grant) {
    if (read.ok || read.status === 404 || read.status === 403) notFound();
    return <div className="idn-card idn-dim" role="alert">Grant unavailable · {read.code}</div>;
  }
  return <DelegationView initial={detailFromGrant(grant)} />;
}
