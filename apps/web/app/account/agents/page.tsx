import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet } from "../../../lib/identity/server";
import type { Connection, Delegation, Passport, SecurityPolicy } from "../../../lib/identity/types";
import { AgentsForMe } from "./agents-for-me";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ new?: string }> };

/** My account › Agents acting for me (and the connections the vault keeps for you). */
export default async function AgentsForMePage({ searchParams }: Props) {
  if (!(await pageAllowed("/account/agents"))) return null;
  const [mine, agents, connections, policy] = await Promise.all([
    serverGet<{ delegations: Delegation[] }>("/v1/identity/me/delegations"),
    serverGet<{ agents: Passport[] }>("/v1/identity/agents"),
    serverGet<{ connections: Connection[] }>("/v1/identity/me/connections"),
    serverGet<SecurityPolicy>("/v1/identity/policy"),
  ]);
  return (
    <AgentsForMe
      delegations={mine.ok ? mine.data.delegations : []}
      agents={agents.ok ? agents.data.agents.filter((a) => a.status === "active").map((a) => ({ id: a.id, name: a.name })) : null}
      connections={connections.ok ? connections.data.connections : []}
      untilRevokedAllowed={policy.ok ? policy.data.delegation_until_revoked : false}
      openNew={(await searchParams).new === "1"}
    />
  );
}
