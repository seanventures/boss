import type { Delegation, DelegationDetail } from "../../../../../lib/identity/types";

/**
 * A grant as the detail screen draws it. The API serves the grant itself (GET
 * /v1/identity/delegations/{id}) but not its live chain, tokens or egress yet: those start empty and
 * the screen says so, rather than inventing hops.
 */
export function detailFromGrant(grant: Delegation): DelegationDetail {
  return { ...grant, run: null, chain: [], tokens: [], egress: [], checked: null };
}

/** The API's answer as the screen draws it: a grant gains empty lineage; a full detail is kept. */
export function detailOf(answer: Delegation | DelegationDetail): DelegationDetail {
  return "chain" in answer && Array.isArray(answer.chain) ? answer : detailFromGrant(answer);
}
