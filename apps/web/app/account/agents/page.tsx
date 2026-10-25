import { pageAllowed } from "../../../components/identity/route-shell";
import { ErrorState } from "../../../components/identity/states";
import { serverGet } from "../../../lib/identity/server";
import type { Connection, Delegation, Passport, SecurityPolicy } from "../../../lib/identity/types";
import { consentFromBody } from "../../../lib/identity/consent";
import { AgentsForMe } from "./agents-for-me";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ new?: string; connected?: string; connect_error?: string; consent?: string }> };

/** My account › Agents acting for me (and the connections the vault keeps for you). */
export default async function AgentsForMePage({ searchParams }: Props) {
  if (!(await pageAllowed("/account/agents"))) return null;
  const query = await searchParams;
  // A consent link (identity/consent.offer_link): the request an agent turn, Metatron or a key met.
  const consentId = /^pcn_[a-z0-9]{1,64}$/.test(query.consent ?? "") ? query.consent! : null;
  const [mine, agents, connections, policy, asked] = await Promise.all([
    serverGet<{ delegations: Delegation[] }>("/v1/identity/me/delegations"),
    serverGet<{ agents: Passport[] }>("/v1/identity/agents"),
    serverGet<{ connections: Connection[] }>("/v1/identity/me/connections"),
    serverGet<SecurityPolicy>("/v1/identity/policy"),
    consentId ? serverGet<Record<string, unknown>>(`/v1/identity/me/consent-requests/${encodeURIComponent(consentId)}`) : Promise.resolve(null),
  ]);
  const consentAsk = asked && asked.ok ? consentFromBody(asked.data) : null;
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
      consentAsk={consentAsk}
      consentLink={query.consent ? (consentAsk ? "open" : "gone") : "none"}
    />
  );
}
