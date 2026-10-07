import { notFound } from "next/navigation";

import { pageAllowed } from "../../../../components/identity/route-shell";
import { ErrorState } from "../../../../components/identity/states";
import { loadDirectory } from "../../../../lib/identity/directory";
import { serverGet } from "../../../../lib/identity/server";
import type { Delegation, Passport } from "../../../../lib/identity/types";
import { PassportScreen } from "./passport";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

/** Identity › Agents › a Passport: its access limit, sponsor, spawn rules, lineage and lifecycle. */
export default async function PassportPage({ params }: Props) {
  const { id } = await params;
  if (!(await pageAllowed(`/identity/agents/${id}`))) return null;
  const [passport, delegations, dir] = await Promise.all([
    serverGet<Passport>(`/v1/identity/agents/${encodeURIComponent(id)}/passport`),
    serverGet<{ delegations: Delegation[] }>("/v1/identity/delegations"),
    loadDirectory({ groupMembers: false }),
  ]);
  if (!passport.ok) {
    if (passport.status === 404) notFound();
    return <ErrorState what="this Passport" failure={passport} />;
  }
  const grants = delegations.ok ? delegations.data.delegations.filter((d) => d.agents.some((a) => a.id === id)) : null;
  // Who may sponsor it: the active people the viewer can see (else only themselves).
  const people = dir.people.filter((p) => p.status === "active").map((p) => ({ id: p.id, name: p.display_name, hue: p.hue, email: p.email }));
  return <PassportScreen initial={passport.data} grants={grants} people={people} />;
}
