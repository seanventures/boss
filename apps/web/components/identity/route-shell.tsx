/**
 * THE CHROME AND THE GUARD OF EVERY IDENTITY AND MY-ACCOUNT PAGE (server component).
 *
 * Reads who is signed in (once per request, shared with the page), resolves which page is being
 * opened (lib/identity/pages.ts), and renders <AccessDenied> instead of the page when the viewer
 * lacks its permission — before the page fetches anything, so no page 403s after loading. The
 * tab strip lists only pages the viewer can open. Inside the console shell, so the rail, the top
 * bar and My account are the same everywhere.
 */
import { headers } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import ConsoleShell from "../../app/ConsoleShell";
import { API_URL, PROJECT_ID } from "../../lib/client";
import { networkTrustedConsole } from "../../lib/console-session";
import { checkWith } from "../../lib/identity/check";
import { snapshotFrom } from "../../lib/identity/contract";
import { enrolHref, needsEnrolment, signInAgainHref } from "../../lib/identity/enrolment";
import { ACCOUNT_TABS, IDENTITY_TABS, PATH_HEADER, SEARCH_HEADER, pageFor, type IdentityPage } from "../../lib/identity/pages";
import { heldAnywhere, heldSomewhere } from "../../lib/identity/held-anywhere";
import { serverGet } from "../../lib/identity/server";
import { loadIdentityCached } from "../../lib/identity/server-cache";
import { identityStubEnabled } from "../../lib/identity/stub/enabled";
import type { Posture } from "../../lib/identity/types";
import { AccessDenied } from "./access-denied";
import { NowProvider } from "./kit";
import { TabStrip } from "./tab-strip";

export async function RouteShell({ section, children }: { section: "identity" | "account"; children: ReactNode }) {
  const path = (await headers()).get(PATH_HEADER) ?? (section === "identity" ? "/identity" : "/account");
  const boot = await loadIdentityCached(null);
  if (boot.problem === "unauthenticated") redirect(`/login?next=${encodeURIComponent(path)}`);
  // An enrolment-only session can do nothing here but enrol (lib/identity/enrolment.ts).
  if (needsEnrolment(boot.me, boot.problem)) redirect(enrolHref(path));
  const org = boot.org ? snapshotFrom(boot.org) : null;
  const unavailable = !boot.org;
  const page = pageFor(path);
  // A page held at the org OR any project (a project Admin's Access requests): asked once each.
  const anyScope = new Map<string, boolean>();
  for (const p of [...(section === "identity" ? IDENTITY_TABS : ACCOUNT_TABS), ...(page ? [page] : [])]) {
    if ("anyScope" in p.access && !anyScope.has(p.access.anyScope)) {
      anyScope.set(p.access.anyScope, heldSomewhere(await heldAnywhere(p.access.anyScope)));
    }
  }
  const allowed = (page: IdentityPage) => unavailable
    || ("anyScope" in page.access ? anyScope.get(page.access.anyScope) !== false
      : !("permission" in page.access) || checkWith(org, null, null, page.access.permission).allowed);
  const tabs = (section === "identity" ? IDENTITY_TABS : ACCOUNT_TABS).filter(allowed);

  // Tab counts (People 7, Groups 4 …) from posture, when the viewer may read it.
  let counts: Record<string, number> = {};
  if (section === "identity" && !unavailable && checkWith(org, null, null, "user:read").allowed) {
    const posture = await serverGet<Posture>("/v1/identity/posture");
    if (posture.ok) {
      const c = posture.data.counts;
      counts = { "/identity/people": c.people, "/identity/groups": c.groups, "/identity/roles": c.roles_builtin + c.roles_custom,
        "/identity/service-accounts": c.service_accounts, "/identity/agents": c.agents };
    }
  }

  const denied = page && !allowed(page)
    ? ("permission" in page.access ? page.access.permission : "anyScope" in page.access ? page.access.anyScope : null) : null;
  const meName = boot.me?.display_name ?? "";
  return (
    <ConsoleShell
      status={{ apiReachable: true, budgetState: "", deadletters: 0, pendingDecisions: 0, controlPlane: API_URL }}
      scope={{ project: boot.me?.org.name ?? "", projectId: PROJECT_ID, actor: meName }}
      org={boot.me ? { id: boot.me.org.id, name: boot.me.org.name } : undefined}
      initialArea={section === "identity" ? "enterprise" : undefined}
      title={section === "account" ? { label: "My account", sub: "Your sign-in, sessions, agents and keys" } : undefined}
      routeMode
      signOutAvailable={!networkTrustedConsole()}
      identity={{ me: boot.me, org: boot.org, stub: identityStubEnabled(), unavailable }}
    >
      <NowProvider now={Date.now()}>
        <div className="idn" data-section={section}>
          <TabStrip tabs={tabs.map((t) => ({ path: t.path, label: t.label, tip: t.tip, count: counts[t.path] ?? null }))} current={path}
            label={section === "identity" ? "Identity" : "My account"} />
          {denied ? <AccessDenied permission={denied} page={page!.label} home={section === "identity" ? "/identity" : "/"} /> : children}
        </div>
      </NowProvider>
    </ConsoleShell>
  );
}

/** For a page: may this request load the page's data? (The layout already chose to render it.) */
export async function pageAllowed(path: string): Promise<boolean> {
  const page = pageFor(path);
  if (page && "anyScope" in page.access) return heldSomewhere(await heldAnywhere(page.access.anyScope));
  if (!page || !("permission" in page.access)) return true;
  const boot = await loadIdentityCached(null);
  if (!boot.org) return true;
  return checkWith(snapshotFrom(boot.org), null, null, page.access.permission).allowed;
}

/** The identity API could not answer (not a signed-out visitor, who is sent to sign in instead). */
export function accessUncheckable(boot: { org: unknown; problem: string | null }): boolean {
  return !boot.org && boot.problem !== "unauthenticated";
}

/**
 * For a page outside identity/account (the /v2 screens): may this request load it? Checked at the
 * project the console is on, as the area shell checks its areas; a stale session goes to sign in.
 */
export async function consolePageAllowed(path: string): Promise<boolean> {
  const page = pageFor(path);
  const boot = await loadIdentityCached(PROJECT_ID);
  if (boot.problem === "unauthenticated") {
    const search = (await headers()).get(SEARCH_HEADER);
    // A query only: a client-supplied header must not put a fragment or a break into the return path.
    const place = search && /^\?[^#\\\u0000-\u001f]*$/.test(search) ? `${path}${search}` : path;
    redirect(signInAgainHref("unauthenticated", place) ?? "/login");
  }
  if (!page || !("permission" in page.access)) return true;
  // FAIL CLOSED: a page that needs a permission is not drawn when its access cannot be checked
  // (the identity API is unreachable); <PageDenied> then says so, with Retry.
  if (!boot.org) return accessUncheckable(boot) ? false : true;
  return checkWith(snapshotFrom(boot.org), boot.project ? snapshotFrom(boot.project) : null, PROJECT_ID,
                   page.access.permission).allowed;
}
