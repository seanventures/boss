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
import { enrolHref, needsEnrolment } from "../../lib/identity/enrolment";
import { ACCOUNT_TABS, IDENTITY_TABS, PATH_HEADER, pageFor, type IdentityPage } from "../../lib/identity/pages";
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
  if (boot.problem === "unauthenticated" && !networkTrustedConsole()) redirect(`/login?next=${encodeURIComponent(path)}`);
  // An enrolment-only session can do nothing here but enrol (lib/identity/enrolment.ts).
  if (needsEnrolment(boot.me, boot.problem)) redirect(enrolHref(path));
  const org = boot.org ? snapshotFrom(boot.org) : null;
  const unavailable = !boot.org;
  const allowed = (page: IdentityPage) => unavailable || !("permission" in page.access) || checkWith(org, null, null, page.access.permission).allowed;
  const page = pageFor(path);
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

  const denied = page && !allowed(page) && "permission" in page.access ? page.access.permission : null;
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
  if (!page || !("permission" in page.access)) return true;
  const boot = await loadIdentityCached(null);
  if (!boot.org) return true;
  return checkWith(snapshotFrom(boot.org), null, null, page.access.permission).allowed;
}
