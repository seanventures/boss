import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../../components/identity/route-shell";
import { serverGet } from "../../../../../lib/identity/server";
import type { DelegationDetail } from "../../../../../lib/identity/types";
import { DelegationView } from "./delegation";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

/** Identity › Agents › a delegation (mockup d): the narrowing chain, its tokens and egress, revoke. */
export default async function DelegationPage({ params }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/agents/delegations/${id}`))) return null;
  const read = await serverGet<DelegationDetail>(`/v1/identity/delegations/${encodeURIComponent(id)}`);
  if (!read.ok) {
    if (read.status === 404) notFound();
    return <div className="idn-card idn-dim" role="alert">Grant unavailable · {read.code}</div>;
  }
  return <DelegationView initial={read.data} />;
}
