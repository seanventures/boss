import { pageAllowed } from "../../../components/identity/route-shell";
import { ErrorState } from "../../../components/identity/states";
import { serverGet } from "../../../lib/identity/server";
import type { Connection, Delegation, Passport, SecurityPolicy } from "../../../lib/identity/types";
import { AgentsForMe } from "./agents-for-me";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ new?: string; connected?: string; connect_error?: string }> };

/** My account › Agents acting for me (and the connections the vault keeps for you). */
export default async function AgentsForMePage({ searchParams }: Props) {
  if (!(await pageAllowed("/account/agents"))) return null;
  const query = await searchParams;
  const [mine, agents, connections, policy] = await Promise.all([
    serverGet<{ delegations: Delegation[] }>("/v1/identity/me/delegations"),
    serverGet<{ agents: Passport[] }>("/v1/identity/agents"),
    serverGet<{ connections: Connection[] }>("/v1/identity/me/connections"),
    serverGet<SecurityPolicy>("/v1/identity/policy"),
  ]);
  // Your grants failing to load must never read as "no agent acts for you".
  if (!mine.ok) return <ErrorState what="the agents acting for you" failure={mine} />;
  return (
    <AgentsForMe
      delegations={mine.data.delegations}
      agents={agents.ok ? agents.data.agents.filter((a) => a.status === "active").map((a) => ({ id: a.id, name: a.name })) : null}
      connections={connections.ok ? connections.data.connections : []}
      connectionsFailed={!connections.ok}
      untilRevokedAllowed={policy.ok ? policy.data.delegation_until_revoked : false}
      openNew={query.new === "1"}
      connectOutcome={{ connected: query.connected ?? null, error: query.connect_error ?? null }}
    />
  );
}
