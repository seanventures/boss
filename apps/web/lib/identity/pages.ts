/**
 * EVERY IDENTITY AND MY-ACCOUNT PAGE, AND WHAT OPENING IT NEEDS (IDENTITY.md §6.5,
 * IDENTITY_PERMISSIONS.md §8). The console areas are S2's table (areas.ts); these routes are
 * S4's own pages and declare here. A page's permission is the read permission of its primary
 * resource (D6). The layout of each section renders <AccessDenied> for a page the viewer
 * cannot open — before any of its data is fetched, so no page ever 403s after loading — and the
 * Identity tab strip shows only the pages the viewer can open.
 *
 * tests/identity-every-page-declares.test.ts walks app/identity/** and app/account/** and
 * fails on a page.tsx with no entry here.
 */
import type { Permission } from "./permissions.gen";

/** Set by middleware.ts on every page request: the pathname, for a layout's guard. */
export const PATH_HEADER = "x-boss-path";

export type PageAccess = { readonly permission: Permission } | { readonly authenticated: true };

export type IdentityPage = {
  /** Route pattern; `[id]` matches one segment. */
  readonly path: string;
  readonly label: string;
  /** Tooltip: what the page is for, ≤ 14 words. */
  readonly tip: string;
  readonly access: PageAccess;
  /** Shown in the Identity tab strip, in this order. */
  readonly tab?: true;
};

export const IDENTITY_PAGES: readonly IdentityPage[] = [
  { path: "/identity", label: "Overview", tip: "Posture, people, roles and agents at a glance", access: { permission: "directory:read" }, tab: true },
  { path: "/identity/people", label: "People", tip: "Everyone who signs in, and their access", access: { permission: "user:read" }, tab: true },
  { path: "/identity/people/[id]", label: "Person", tip: "One person's access, sessions and security", access: { permission: "user:read" } },
  { path: "/identity/groups", label: "Groups", tip: "Sets of people who share access roles", access: { permission: "group:read" }, tab: true },
  { path: "/identity/groups/[id]", label: "Group", tip: "Members and the roles they inherit", access: { permission: "group:read" } },
  { path: "/identity/roles", label: "Access roles", tip: "Named sets of permissions you bind to people", access: { permission: "access_role:read" }, tab: true },
  { path: "/identity/roles/[id]", label: "Access role", tip: "Edit permissions, preview who it affects", access: { permission: "access_role:read" } },
  { path: "/identity/service-accounts", label: "Service accounts", tip: "Non-human accounts for CI, scripts and integrations", access: { permission: "service_account:read" }, tab: true },
  { path: "/identity/agents", label: "Agents", tip: "Passports, ceilings and who agents act for", access: { permission: "agent_passport:read" }, tab: true },
  { path: "/identity/agents/[id]", label: "Passport", tip: "An agent's ceiling, sponsor and lineage", access: { permission: "agent_passport:read" } },
  { path: "/identity/agents/delegations/[id]", label: "Delegation", tip: "The chain, its tokens and egress, and revoke", access: { authenticated: true } },
  { path: "/identity/sign-in", label: "Sign-in & SSO", tip: "Providers, SCIM and the security policy", access: { permission: "identity_provider:read" }, tab: true },
  { path: "/identity/audit", label: "Audit", tip: "Every security event, hash-chained and verifiable", access: { permission: "audit:read" }, tab: true },
  { path: "/account", label: "Security", tip: "Your password, passkeys, MFA and recovery codes", access: { authenticated: true }, tab: true },
  { path: "/account/sessions", label: "Sessions", tip: "Where you are signed in", access: { authenticated: true }, tab: true },
  { path: "/account/agents", label: "Agents for me", tip: "Agents acting for you, and your connections", access: { authenticated: true }, tab: true },
  { path: "/account/keys", label: "API keys", tip: "Personal access tokens for the CLI and MCP", access: { permission: "api_key:manage_own" }, tab: true },
];

/** The page a pathname is, or null. */
export function pageFor(pathname: string): IdentityPage | null {
  const clean = pathname.replace(/\/+$/, "") || "/";
  const parts = clean.split("/");
  for (const page of IDENTITY_PAGES) {
    const want = page.path.split("/");
    if (want.length !== parts.length) continue;
    if (want.every((w, i) => w === "[id]" ? parts[i].length > 0 : w === parts[i])) return page;
  }
  return null;
}

export const IDENTITY_TABS = IDENTITY_PAGES.filter((p) => p.tab && p.path.startsWith("/identity"));
export const ACCOUNT_TABS = IDENTITY_PAGES.filter((p) => p.tab && p.path.startsWith("/account"));

/**
 * Pages and API paths a visitor with no session may open: the sign-in screens and the public
 * sign-in API (IDENTITY.md §10 "Sign-in"). Routing only; the API declares its own `@public`.
 */
const PUBLIC_PAGES = [/^\/login\/?$/, /^\/claim\/?$/, /^\/invite\/?$/, /^\/signin(\/link)?\/?$/, /^\/reset\/?$/, /^\/signup(\/verify)?\/?$/];
const PUBLIC_API = /^\/v1\/auth\/(options|login|mfa\/(totp|recovery)|webauthn\/(options|verify)|magic-link(\/verify)?|email-code(\/verify)?|signup(\/verify)?|password\/(forgot|reset)|setup\/(claim|status)|invitations\/(preview|accept)|sso\/[^/]+\/(start|callback))\/?$/;
const PUBLIC_ASSETS = new Set(["/boss-mark.png", "/BOSS-logo.png", "/boss-lockup.png", "/icon.png"]);

export function isPublicConsolePath(pathname: string): boolean {
  return PUBLIC_PAGES.some((re) => re.test(pathname)) || PUBLIC_API.test(pathname) || PUBLIC_ASSETS.has(pathname);
}
