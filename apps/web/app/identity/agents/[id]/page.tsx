import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { serverGet } from "../../../../lib/identity/server";
import type { Delegation, Passport } from "../../../../lib/identity/types";
import { PassportScreen } from "./passport";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

/** Identity › Agents › a Passport: its ceiling, sponsor, spawn rules, lineage and lifecycle. */
export default async function PassportPage({ params }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/agents/${id}`))) return null;
  const [passport, delegations] = await Promise.all([
    serverGet<Passport>(`/v1/identity/agents/${encodeURIComponent(id)}/passport`),
    serverGet<{ delegations: Delegation[] }>("/v1/identity/delegations"),
  ]);
  if (!passport.ok) {
    if (passport.status === 404) notFound();
    return <div className="idn-card idn-dim" role="alert">Passport unavailable · {passport.code}</div>;
  }
  const grants = delegations.ok ? delegations.data.delegations.filter((d) => d.agents.some((a) => a.id === id)) : null;
  return <PassportScreen initial={passport.data} grants={grants} />;
}
