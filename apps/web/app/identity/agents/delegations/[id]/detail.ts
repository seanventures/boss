import type { Delegation, DelegationDetail } from "../../../../../lib/identity/types";

/**
 * A grant as the detail screen draws it. The API serves the grant itself (GET
 * /v1/identity/delegations) but not its live chain, tokens or egress yet: those start empty and
 * the screen says so, rather than inventing hops.
 */
export function detailFromGrant(grant: Delegation): DelegationDetail {
  return { ...grant, run: null, chain: [], tokens: [], egress: [], checked: null };
}
