import { BossApiError } from "@boss/api-client";

/**
 * A 404 from a feature this edition does not have is not an error.
 *
 * The console printed "API returned 404" ten times on every load. None of it was in the area
 * being viewed, and none of it was broken: `/v1/marketplace/*`, `/v1/self-improvement/*` and the
 * enterprise identity routes are mounted only on PAID editions. This instance runs Community, so
 * 404 is the correct answer to the question asked. Rendering it as an infrastructure failure is
 * what made a healthy control plane read as down.
 *
 * The second, worse half: loaders gathered their calls with a bare `Promise.all`, which rejects
 * on the FIRST rejection. `fetchEnterpriseFeed` makes seven calls; one enterprise-only route
 * 404s and the catch discards all seven — so three SSO cards showed an API error, including one
 * whose own badge reads "included in Community", whose data had loaded perfectly.
 *
 * Two rules, one place:
 *   1. An edition-gated status yields the empty value, never an exception.
 *   2. Anything else is a REAL failure and still propagates — a 500 must not be quietly
 *      relabelled "not licensed", which is what the private helper in commercial.ts did by
 *      returning undefined from both branches.
 */

/** Statuses that mean "this route is not mounted for you", not "something broke". */
export const EDITION_GATED_STATUSES = [401, 403, 404, 405] as const;

export function isEditionGated(error: unknown): error is BossApiError {
  return (
    error instanceof BossApiError &&
    (EDITION_GATED_STATUSES as readonly number[]).includes(error.status)
  );
}

/**
 * Await `promise`, substituting `fallback` when the route is not mounted for this edition.
 *
 * Deliberately RETHROWS every other error. A caller that wants "never fail" should say so at the
 * call site; making that the default here is how an outage becomes invisible.
 */
export async function editionOptional<T>(
  promise: Promise<T>,
  fallback: T,
): Promise<T> {
  try {
    return await promise;
  } catch (error) {
    if (isEditionGated(error)) return fallback;
    throw error;
  }
}

/** Did any call in this feed get gated? Lets a panel say which of its sections is unavailable. */
export async function editionOptionalTracked<T>(
  promise: Promise<T>,
  fallback: T,
): Promise<{ value: T; gated: boolean; status?: number }> {
  try {
    return { value: await promise, gated: false };
  } catch (error) {
    if (isEditionGated(error)) {
      return { value: fallback, gated: true, status: (error as BossApiError).status };
    }
    throw error;
  }
}

/**
 * Operator-facing copy for a section this edition does not include.
 *
 * Prefer edition honesty over a bare status code — the point is that the operator should not go
 * looking for an outage that does not exist.
 */
export function editionUnavailableReason(
  feature: string,
  editionId?: string | null,
): string {
  const label = (editionId ?? "").trim() || "this";
  return (
    `${feature} is not included in ${label === "this" ? "this edition" : `the ${label} edition`}. ` +
    `The control plane does not mount those routes here, so the section is empty rather than broken.`
  );
}

/**
 * The common case: a list endpoint that answers `{ items: [...] }`.
 *
 * Typed generically so the empty fallback keeps the response's own type. A shared
 * `{ items: [] } as any` fallback compiles, and then every `.map()` downstream silently loses
 * its element type — trading a runtime bug for a type hole is not a fix.
 */
export async function editionOptionalList<T extends { items: unknown[] }>(
  promise: Promise<T>,
): Promise<T> {
  return editionOptional(promise, { items: [] } as unknown as T);
}
