/**
 * WHICH ORGANIZATION THIS CONSOLE REQUEST IS FOR — decided once, on the server.
 *
 * The page gated on `?org=` alone: any request without it rendered "Select your organization",
 * and OrgGate's stored-org effect sent the browser back a moment later ("Opening…" on the BOSS
 * card). Dozens of places build console URLs (rail, links, panels, page loads of an area, route
 * pages), and every one that lost the parameter flashed the picker mid-session — seen during
 * plain Command Center -> Infra -> Observability -> Workflows navigation. Patching each builder
 * is whack-a-mole; the org the session is in is known (a cookie the console writes whenever an
 * org is open), so a URL without one simply means "the org I am in".
 *
 * The picker is drawn only when there is genuinely a choice to make: the operator asked to
 * switch (`?pick=1`), or nothing is remembered and more than one org exists. A URL naming an org
 * that is not in the list still gets the picker — a bad link is a real mismatch.
 */
export const ORG_COOKIE = "boss_org";

const ORG_ID = /^[A-Za-z0-9_.:-]{1,128}$/;

/** The document.cookie assignment that remembers `orgId` for this browser's console session. */
export function orgCookie(orgId: string): string {
  return `${ORG_COOKIE}=${encodeURIComponent(orgId)}; Path=/; Max-Age=31536000; SameSite=Lax`;
}

/** Remember the open org (best effort: cookies blocked means the URL alone decides, as before). */
export function rememberOrg(orgId: string): void {
  if (!orgId || !ORG_ID.test(orgId)) return;
  try { document.cookie = orgCookie(orgId); } catch { /* no document / cookies blocked */ }
  try { window.localStorage.setItem("boss.org", orgId); } catch { /* storage blocked */ }
}

export function resolveConsoleOrg<T extends { org_id: string }>(input: {
  /** `?org=` or `?org_id=` from the URL. */
  requested?: string | null;
  /** `?pick=1`: the operator asked for the picker. */
  pick?: boolean;
  /** The org this browser last had open (ORG_COOKIE). */
  remembered?: string | null;
  orgs: readonly T[];
}): T | undefined {
  const { requested, pick, remembered, orgs } = input;
  if (requested) return orgs.find((o) => o.org_id === requested);
  if (pick) return undefined;
  const kept = remembered ? orgs.find((o) => o.org_id === remembered) : undefined;
  if (kept) return kept;
  // ONE ORGANIZATION IS NOT A CHOICE.
  return orgs.length === 1 ? orgs[0] : undefined;
}

/** The URL to show for `orgId` when the address bar lost it: same place, org restored. */
export function withOrgRestored(search: string, orgId: string): string | null {
  const params = new URLSearchParams(search);
  if (params.get("org") || params.get("org_id") || !orgId) return null;
  params.set("org", orgId);
  return `?${params.toString()}`;
}
