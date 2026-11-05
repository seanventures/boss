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
import { AREA_ACCESS } from "./areas";
import type { Permission } from "./permissions.gen";
import { AUTH_LINKS, LEGACY_LINK_ALIASES } from "./auth-links.gen";

/**
 * EVERY PAGE A VISITOR WITHOUT A SESSION MAY OPEN: the sign-in screens, every page a minted link
 * lands on (AUTH_LINKS, the engine's one table) and every alias an older build printed. None of
 * them is ever sent to /login — a link's token would ride into a page that ignores it.
 */
export const PUBLIC_AUTH_PATHS: readonly string[] = [...new Set([
  "/login", "/signin", "/signup",
  ...Object.values(AUTH_LINKS).map((l) => l.path),
  ...Object.keys(LEGACY_LINK_ALIASES),
])];

/** Set by middleware.ts on every page request: the pathname, for a layout's guard. */
export const PATH_HEADER = "x-boss-path";
/** The query string of that same request (`?org=…`), so a sign-in can come back to it. */
export const SEARCH_HEADER = "x-boss-search";

export type PageAccess = { readonly permission: Permission } | { readonly authenticated: true }
  /** Held at the org or at ANY project (lib/identity/held-anywhere.ts): a project Admin's page. */
  | { readonly anyScope: Permission };

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
  { path: "/identity/requests", label: "Access requests", tip: "Requests for access waiting on you", access: { anyScope: "role_binding:assign" }, tab: true },
  { path: "/identity/groups", label: "Groups", tip: "Sets of people who share access roles", access: { permission: "group:read" }, tab: true },
  { path: "/identity/groups/[id]", label: "Group", tip: "Members and the roles they inherit", access: { permission: "group:read" } },
  { path: "/identity/seats", label: "Approval seats", tip: "Who approves decisions at each risk tier", access: { permission: "role_binding:read" }, tab: true },
  { path: "/identity/roles", label: "Access roles", tip: "Named sets of permissions you bind to people", access: { permission: "access_role:read" }, tab: true },
  { path: "/identity/roles/[id]", label: "Access role", tip: "Edit permissions, preview who it affects", access: { permission: "access_role:read" } },
  { path: "/identity/service-accounts", label: "Service accounts", tip: "Non-human accounts for CI, scripts and integrations", access: { permission: "service_account:read" }, tab: true },
  { path: "/identity/agents", label: "Agents", tip: "Passports, ceilings and who agents act for", access: { permission: "agent_passport:read" }, tab: true },
  { path: "/identity/agents/[id]", label: "Passport", tip: "An agent's ceiling, sponsor and lineage", access: { permission: "agent_passport:read" } },
  { path: "/identity/agents/delegations/[id]", label: "Delegation", tip: "The chain, its tokens and egress, and revoke", access: { authenticated: true } },
  { path: "/identity/app-sign-in", label: "App sign-in", tip: "BOSS as the sign-in for your apps: pools, clients, users, roles", access: { anyScope: "oidc_client:read" }, tab: true },
  { path: "/identity/sign-in", label: "Sign-in & SSO", tip: "Providers, SCIM and the security policy", access: { permission: "identity_provider:read" }, tab: true },
  { path: "/identity/audit", label: "Audit", tip: "Every security event, hash-chained and verifiable", access: { permission: "audit:read" }, tab: true },
  { path: "/account", label: "Security", tip: "Your password, passkeys, MFA and recovery codes", access: { authenticated: true }, tab: true },
  { path: "/account/sessions", label: "Sessions", tip: "Where you are signed in", access: { authenticated: true }, tab: true },
  { path: "/account/agents", label: "Agents for me", tip: "Agents acting for you, and your connections", access: { authenticated: true }, tab: true },
  { path: "/account/keys", label: "API keys", tip: "Personal access tokens for the CLI and MCP", access: { permission: "api_key:manage_own" }, tab: true },
];

/**
 * EVERY OTHER PAGE OF THE CONSOLE. `/` is the area shell: each area there is opened by its own
 * AREA_ACCESS entry (areas.ts), so the page itself needs only a session. The /v2 screens are the
 * v1 areas they show (components/v2/Shell.tsx `v1Area`) and need exactly that area's permission;
 * the retired ones only redirect to their area, and are guarded the same way. The sign-in pages
 * are public (isPublicConsolePath) and declared here so the walk in
 * tests/identity-every-page-declares.test.ts finds every page.tsx under app/ declared.
 */
const area = (id: keyof typeof AREA_ACCESS): PageAccess => AREA_ACCESS[id];
const PUBLIC: PageAccess = { authenticated: true };    // routing makes them public; no session check here
export const CONSOLE_PAGES: readonly IdentityPage[] = [
  { path: "/", label: "Console", tip: "Every area; each opens with its own permission", access: { authenticated: true } },
  { path: "/launch", label: "Launch", tip: "What BOSS is, and how to install it", access: { authenticated: true } },
  { path: "/enrol", label: "Enrol", tip: "Add the second factor your role needs", access: { authenticated: true } },
  { path: "/v2", label: "Factory", tip: "Projects and the builds in them", access: area("code_tasks") },
  { path: "/v2/missions", label: "Factory", tip: "Projects and the builds in them", access: area("code_tasks") },
  { path: "/v2/missions/[id]", label: "Build", tip: "One build's workspace", access: area("code_tasks") },
  { path: "/v2/hive", label: "Hive", tip: "Ask BOSS", access: area("agent") },
  { path: "/v2/factory", label: "Agent Studio", tip: "Your agents", access: area("workforce") },
  { path: "/v2/org", label: "Team structure", tip: "Who reports to whom", access: area("workforce") },
  { path: "/v2/panels", label: "Approvals", tip: "Decisions waiting for you", access: area("decisions") },
  { path: "/v2/boss", label: "Workflows", tip: "Delivery workflows", access: area("delivery_workflows") },
  { path: "/v2/timeline", label: "Receipts", tip: "What happened, with evidence", access: area("activity") },
  { path: "/v2/infra", label: "Infra & Models", tip: "Hosts, deploys and models", access: area("delivery") },
  { path: "/v2/settings", label: "Settings", tip: "Repo and models", access: area("setup") },
  { path: "/github-app", label: "GitHub App", tip: "Hands the App's manifest to GitHub", access: area("setup") },
  ...PUBLIC_AUTH_PATHS.map(
    (path) => ({ path, label: "Sign in", tip: "Public: signing in needs no session", access: PUBLIC })),
];

/** The page a pathname is, or null. */
export function pageFor(pathname: string): IdentityPage | null {
  const clean = pathname.replace(/\/+$/, "") || "/";
  const parts = clean.split("/");
  for (const page of [...IDENTITY_PAGES, ...CONSOLE_PAGES]) {
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
const PUBLIC_PAGES = PUBLIC_AUTH_PATHS.map((p) => new RegExp(`^${p.replace(/\//g, "\\/")}\\/?$`));
const PUBLIC_API = /^\/v1\/auth\/(options|login|mfa\/(totp|recovery)|webauthn\/(options|verify)|magic-link(\/verify)?|email-code(\/verify)?|signup(\/verify)?|password\/(forgot|reset)|setup\/(claim|status)|invitations\/(preview|accept)|sso\/[^/]+\/(start|callback))\/?$/;
const PUBLIC_ASSETS = new Set(["/boss-mark.png", "/BOSS-logo.png", "/boss-lockup.png", "/icon.png"]);

export function isPublicConsolePath(pathname: string): boolean {
  return PUBLIC_PAGES.some((re) => re.test(pathname)) || PUBLIC_API.test(pathname) || PUBLIC_ASSETS.has(pathname);
}
