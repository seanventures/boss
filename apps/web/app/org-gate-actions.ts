"use server";

import { BossApiError } from "@boss/api-client";

import { client } from "../lib/client";
import { fetchOrgs } from "../lib/orgs";
import { declareAction } from "../lib/identity/guarded-action";

/**
 * Onboard an organization and SAY what happened.
 *
 * The old action returned void and threw on any API error. The app had no error boundary, so a
 * failed onboard landed on Next's generic "Application error" page with nothing about what
 * failed; a name that already existed was accepted and made a second org with the same name;
 * and on success the form stayed open and filled, so it read as though nothing had happened.
 * This returns a result the form renders, refuses a duplicate name with the way forward, and
 * hands back the new org so the gate can open it.
 */
export type OnboardOrgResult =
  | { ok: true; orgId: string; name: string }
  | { ok: false; error: string };

export async function onboardOrgFormAction(
  _previous: OnboardOrgResult | null,
  formData: FormData,
): Promise<OnboardOrgResult> {
  const name = String(formData.get("name") ?? "").trim();
  if (!name) {
    return { ok: false, error: "Give the organization a name, then press Onboard organization." };
  }
  const existing = await fetchOrgs();
  if (existing.apiReachable) {
    const clash = existing.items.find(
      (org) => org.org_name.trim().toLowerCase() === name.toLowerCase(),
    );
    if (clash) {
      return {
        ok: false,
        error: `An organization named "${clash.org_name}" already exists. Open it from the list above, or choose a different name.`,
      };
    }
  }
  try {
    const created = await client.onboardOrg(name);
    return { ok: true, orgId: created.org_id, name: created.name || name };
  } catch (error) {
    const detail =
      error instanceof BossApiError
        ? `the API answered ${error.status}${error.message ? ` (${error.message})` : ""}`
        : error instanceof Error
          ? error.message
          : "the API could not be reached";
    return {
      ok: false,
      error: `Could not onboard "${name}": ${detail}. Nothing was created — check that the BOSS API is running, then press Onboard organization again.`,
    };
  }
}

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(onboardOrgFormAction, "org:update");
