import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet } from "../../../lib/identity/server";
import type { Delegation, Passport } from "../../../lib/identity/types";
import { AgentsScreen } from "./agents-screen";

export const dynamic = "force-dynamic";

/** Identity › Agents (§9.6): every Passport, and every grant that lets an agent act for someone. */
export default async function AgentsPage() {
  if (!(await pageAllowed("/identity/agents"))) return null;
  const [agents, delegations] = await Promise.all([
    serverGet<{ agents: Passport[] }>("/v1/identity/agents"),
    serverGet<{ delegations: Delegation[] }>("/v1/identity/delegations"),
  ]);
  return (
    <AgentsScreen
      agents={agents.ok ? agents.data.agents : []}
      delegations={delegations.ok ? delegations.data.delegations : null}
    />
  );
}
