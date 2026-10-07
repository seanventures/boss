import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../../components/identity/route-shell";
import { ErrorState } from "../../../../../components/identity/states";
import { serverGet } from "../../../../../lib/identity/server";
import type { Delegation, DelegationDetail } from "../../../../../lib/identity/types";
import { detailOf } from "./detail";
import { DelegationView } from "./delegation";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

/**
 * Identity › Agents › a delegation (mockup d): the narrowing chain, its tokens and egress, revoke.
 *
 * GET /v1/identity/delegations/{id} answers the one grant — in the shape the lists answer — to the
 * person who granted it or a reader of every grant, and 404 to anyone else (another org's grant
 * and an unknown id read the same). The chain, tokens and egress are not served yet: they render
 * empty, honestly (detail.ts).
 */
export default async function DelegationPage({ params }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/agents/delegations/${id}`))) return null;
  const read = await serverGet<Delegation | DelegationDetail>(`/v1/identity/delegations/${encodeURIComponent(id)}`);
  if (!read.ok) {
    if (read.status === 404 || read.status === 403) notFound();
    return <ErrorState what="this grant" failure={read} />;
  }
  return <DelegationView initial={detailOf(read.data)} />;
}
