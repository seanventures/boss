import type { Permission } from "../lib/identity/permissions.gen";

/** What POST /v1/orgs checks (tests/identity/matrix.yaml): the onboard card, its form and its
 *  action declare exactly this, so nobody is offered an organization the API will refuse. */
export const ORG_ONBOARD_PERMISSION: Permission = "org:update";
