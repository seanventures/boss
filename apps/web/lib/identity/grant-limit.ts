/**
 * THE GRANT LIMIT (IDENTITY.md §6, A-grant): a person may give a role only when they hold every
 * permission in it; the API refuses the rest. Every role picker that grants a role (Invite, New
 * service account, Edit service account, a group's role, a person's access) mirrors it through
 * this one helper, so a picker cannot offer a role the API will refuse, and every one says it in
 * the same words. tests/every-role-picker-applies-the-grant-limit.test.tsx holds the class.
 */
import type { AccessRole } from "./types";

export const GRANT_LIMIT_WORDS = "You don't hold all of it";

export type GrantReach = { ok: boolean; missing: string[]; reason: string };

/** `held` null: the viewer's permissions are unknown (identity unreadable) — offer it; the API decides. */
export function grantReach(held: ReadonlySet<string> | null | undefined, role: Pick<AccessRole, "permissions" | "description">): GrantReach {
  const missing = held ? role.permissions.filter((p) => !held.has(p)) : [];
  if (!missing.length) return { ok: true, missing, reason: role.description ?? "" };
  return { ok: false, missing, reason: GRANT_LIMIT_WORDS };
}
